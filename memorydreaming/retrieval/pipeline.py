"""检索主流程：计划 → 多通道取候选 → 融合 → 校验 → 限预算 → 交付。

这条流水线对应架构文档第 4 节推荐读取顺序的第 3~8 步。
每一步都刻意拆成独立方法，因为它们是**可以单独验证**的：

    _keyword_channel   只看关键词能不能找对
    _recent_channel    只看 48 小时窗口有没有生效
    _time_channel      只看明确日期会不会越界
    _validate          只看过期/撤回/越权数据有没有被拦住
    _apply_budget      只看 token 预算够不够

第一版只有关键词、时间、最近三个通道。以后加向量检索，
在这里多写一个 `_vector_channel` 并加入 hits 列表即可，
融合和校验逻辑完全复用。
"""

from __future__ import annotations

import time as time_module
from datetime import datetime, timedelta, tzinfo

from ..clock import Clock
from ..config import Settings
from ..models import (
    LinkRelation,
    MemoryItem,
    MemoryStatus,
    Query,
    QueryPlan,
    RetrievalResult,
    RetrievalTrace,
    RetrievedItem,
    SummaryHit,
)
from ..storage.database import Database
from ..summarization import find_summaries
from .context import estimate_retrieval_tokens, estimate_tokens, render_item_line
from .decay import effective_strength
from .fusion import ChannelHit, FusedCandidate, fuse
from .keyword import KeywordIndex
from .planner import QueryPlanner
from .vector import VectorIndex

# 通道权重：得分越高，说明这个通道对本次问题越可信。
# 时间通道是硬约束，权重固定为 1.0；关键词通道用类型相关度加权。
_TIME_CHANNEL_WEIGHT = 1.0
_RECENT_CHANNEL_BASE_WEIGHT = 0.6


class RetrievalPipeline:
    """把一次 Query 跑成 RetrievalResult。"""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        clock: Clock,
        tz: tzinfo,
        vector_index: VectorIndex | None = None,
    ) -> None:
        self._db = db
        self._repos = db.repositories
        self._settings = settings
        self._clock = clock
        self._tz = tz
        self._keyword = KeywordIndex(db.connection)
        self._planner = QueryPlanner(settings, clock, tz)
        # 向量索引可以为 None：没有嵌入模型时，检索退化成「关键词 + 时间」，
        # 其余逻辑（融合、校验、预算）完全不变。
        self._vector = vector_index

    def run(self, query: Query) -> RetrievalResult:
        started = time_module.perf_counter()
        now = self._clock.now()
        plan = self._planner.plan(query)

        # 0) 摘要优先：主题宽泛或久远的问题，先给索引卡，再回源展开。
        summary_hits, summary_channels = self._summary_stage(plan, query)

        # 1) 多通道取候选。各通道相互独立，只共享同一份查询计划。
        hits: list[ChannelHit] = []
        hits.extend(summary_channels)
        hits.extend(self._recent_channel(plan, query, now))
        hits.extend(self._keyword_channel(plan, query))
        hits.extend(self._vector_channel(plan, query))
        hits.extend(self._time_channel(plan, query))

        # 2) 融合排序
        fused = fuse(hits, k=self._settings.rrf_k)

        # 3) 回表校验：过滤撤回/删除/越权，标注是否仍然有效
        items, dropped = self._validate(fused, query, plan, now)

        # 4) 按条数与 token 预算截断
        selected, token_used = self._apply_budget(items, plan)

        # 5) 汇总轨迹与缺口
        trace = RetrievalTrace(
            channel_hits=self._count_channels(hits),
            fused_candidates=len(fused),
            selected=len(selected),
            dropped=dropped,
            token_estimate=token_used,
            elapsed_ms=round((time_module.perf_counter() - started) * 1000, 2),
            notes=[
                "路由：" + "、".join(t.label_zh for t in plan.top_types())
                + f" 展开：{plan.expansion_mode}"
            ],
        )
        gaps = self._collect_gaps(selected, plan, hits)

        result = RetrievalResult(
            query_text=query.text,
            plan=plan,
            items=selected,
            summaries=summary_hits,
            trace=trace,
            gaps=gaps,
        )
        self._fit_context_budget(result)
        return result

    # -- 通道 -------------------------------------------------------------

    def _summary_stage(
        self,
        plan: QueryPlan,
        query: Query,
    ) -> tuple[list[SummaryHit], list[ChannelHit]]:
        """摘要阶段：宽泛/久远的问题先给索引卡，并把卡上的来源作为候选。

        为什么只在 summary_then_expand 模式下启用？
        用户问「昨天我们聊了什么」时，答案就该是那几条原始记录，
        先给一张摘要卡反而绕远了。
        """
        # 当前摘要是可变的投影，没有历史版本。即使摘要创建得早，后续重建
        # 也可能混入 as_of 之后的内容；历史回放只使用逐条校验后的来源记忆。
        if plan.expansion_mode != "summary_then_expand" or plan.as_of is not None:
            return [], []

        hits = find_summaries(self._db, query.scope, plan.query_text, limit=2)
        channel_hits: list[ChannelHit] = []

        for rank, hit in enumerate(hits, start=1):
            # 每张卡只展开三条来源：更多就变成「绕过摘要直接倒记忆」了
            for memory_id in hit.summary.source_memory_ids[:3]:
                channel_hits.append(ChannelHit(memory_id, "summary_expand", rank, 0.85))

        return hits, channel_hits

    def _recent_channel(self, plan: QueryPlan, query: Query, now: datetime) -> list[ChannelHit]:
        """最近窗口通道：优先把 48 小时内的细节端上来。

        明确日期查询（exact_lookup）会跳过它——那时用户要的是「那一天」，
        而不是「最近的」。
        """
        # 两道开关：
        #   1. 明确日期查询不要「最近的」，要「那一天的」；
        #   2. 没有延续交流的说法时，最近窗口只会塞进无关内容。
        if plan.expansion_mode == "exact_lookup" or not plan.prefers_recent:
            return []

        since = now - timedelta(hours=self._settings.recent_detail_window_hours)
        hits: list[ChannelHit] = []

        for memory_type, budget in plan.per_type_budget.items():
            if budget <= 0:
                continue
            memories = self._repos.memories.list_recent(
                query.scope, since, [memory_type], budget
            )
            weight = _RECENT_CHANNEL_BASE_WEIGHT + 0.4 * plan.type_scores[memory_type]
            hits.extend(
                ChannelHit(memory.memory_id, "recent", rank, weight)
                for rank, memory in enumerate(memories, start=1)
            )
        return hits

    def _keyword_channel(self, plan: QueryPlan, query: Query) -> list[ChannelHit]:
        """关键词通道：按类型分别取候选，这样高分类型能拿到更多位置。"""
        if not plan.keywords:
            return []

        hits: list[ChannelHit] = []
        for memory_type, budget in plan.per_type_budget.items():
            if budget <= 0:
                continue
            results = self._keyword.search(
                plan.keywords,
                query.scope.user_id,
                query.scope.character_id,
                memory_types=[memory_type],
                limit=budget,
                scope=query.scope,
            )
            weight = 0.5 + 0.5 * plan.type_scores[memory_type]
            hits.extend(
                ChannelHit(result.memory_id, "keyword", rank, weight)
                for rank, result in enumerate(results, start=1)
            )
        return hits

    def _vector_channel(self, plan: QueryPlan, query: Query) -> list[ChannelHit]:
        """向量通道：找「意思接近但用词不同」的记忆。

        第一版的嵌入是字面哈希（见 retrieval/embedding.py），所以这个通道
        的实际收益接近「模糊关键词」；但通道本身是通的——换成真实模型后，
        它才会真正承担语义召回。

        只保留路由给出的高分类型：向量空间里没有类型概念，
        如果不做这层过滤，程序记忆会挤掉本来该给情景记忆的位置。
        """
        if self._vector is None or not plan.keywords:
            return []

        results = self._vector.search(
            query.scope,
            plan.query_text,
            limit=self._settings.vector_candidate_limit,
            min_score=self._settings.vector_min_score,
        )

        hits: list[ChannelHit] = []
        rank = 0
        for result in results:
            if plan.per_type_budget.get(result.memory_type, 0) <= 0:
                continue
            rank += 1
            weight = 0.55 + 0.45 * plan.type_scores.get(result.memory_type, 0.5)
            hits.append(ChannelHit(result.memory_id, "vector", rank, weight))
        return hits

    def _time_channel(self, plan: QueryPlan, query: Query) -> list[ChannelHit]:
        """时间通道：用户给了明确范围时，按事件时间扫一遍。

        注意用 occurred_start（事件何时发生），不是 recorded_at（何时入库）。
        补录一段一年前的对话时，这两个时间会差很远。
        """
        hint = plan.time_hint
        # 只有硬约束才按时间过滤。软提示（「上次」「今天接着讲」）交给关键词通道，
        # 否则会把「今天是说话时间」误当成「只找今天发生的事」。
        if not plan.hard_time or hint is None or hint.start is None or hint.end is None:
            return []

        memories = self._repos.memories.list_between(
            query.scope,
            hint.start,
            hint.end,
            memory_types=None,  # 时间范围内不预设类型，避免漏掉
            limit=self._settings.keyword_candidate_limit,
        )
        return [
            ChannelHit(memory.memory_id, "time", rank, _TIME_CHANNEL_WEIGHT)
            for rank, memory in enumerate(memories, start=1)
        ]

    # -- 校验与预算 -------------------------------------------------------

    def _validate(
        self,
        fused: list[FusedCandidate],
        query: Query,
        plan: QueryPlan,
        now: datetime,
    ) -> tuple[list[RetrievedItem], int]:
        """回表取正文，并做四道过滤。

        为什么要回表？因为候选只带了 ID，而「这条记忆是否还有效」
        需要读 status、valid_to 等字段才能判断，不能靠索引里的文本猜。
        """
        if not fused:
            return [], 0

        fetched = self._repos.memories.fetch_many([candidate.memory_id for candidate in fused])
        items: list[RetrievedItem] = []
        dropped = 0

        for candidate in fused:
            memory = fetched.get(candidate.memory_id)
            if memory is None:
                dropped += 1
                continue
            if self._repos.forgetting.is_suppressed(memory):
                dropped += 1
                continue

            # 已被撤回或删除的记忆不再作为证据；它们只应出现在审计视图里
            if memory.status in (MemoryStatus.RETRACTED, MemoryStatus.DELETED):
                dropped += 1
                continue

            # 去重合并留下的副本：状态是 superseded，且有一条 part_of 指向主记录。
            # 它的内容已经并进主记录，再返回一次只是重复占预算。
            # 注意要用「superseded + part_of」两个条件一起判断：
            # part_of 本身还表示「某条记忆是另一条的一部分」，那是合法关系。
            if memory.status is MemoryStatus.SUPERSEDED and self._repos.links.has_relation(
                memory.memory_id, LinkRelation.PART_OF
            ):
                dropped += 1
                continue

            # 防御性检查：检索 SQL 已经过滤过作用域，这里再拦一次，
            # 避免将来新增通道时漏掉过滤条件导致串号。
            if (
                memory.scope.user_id != query.scope.user_id
                or memory.scope.character_id != query.scope.character_id
                or memory.scope.domain != query.scope.domain
                or (
                    query.scope.relationship_id is not None
                    and memory.scope.relationship_id != query.scope.relationship_id
                )
                or (
                    query.scope.activity_id is not None
                    and memory.scope.activity_id != query.scope.activity_id
                )
            ):
                dropped += 1
                continue

            # 时间旅行：只要 as_of 那一刻还不存在、或当时尚未生效的记忆，
            # 都不该出现在「当时的视角」里。
            if not self._exists_at(memory, plan):
                dropped += 1
                continue

            # 硬时间约束在这里统一执行。
            # 为什么不放在各个通道里？因为「昨天」是用户给出的强约束，
            # 任何通道（包括以后新增的向量通道）都不应该绕过它。
            # 放在校验这一步，只有一处实现，不会漏。
            if self._violates_hard_time(memory, plan):
                dropped += 1
                continue

            valid_now = memory.is_current(now)
            note = ""
            if memory.status is MemoryStatus.SUPERSEDED:
                # 被新版本取代：仍然可查（方便解释「以前是怎么样的」），但不算当前值。
                # 不再重复写「历史值」——渲染时标签里已经有了。
                note = "已被更新"
            elif not valid_now:
                note = "历史值，不再代表当前状态"

            items.append(
                RetrievedItem(
                    memory=memory,
                    score=candidate.score,
                    channels=list(dict.fromkeys(candidate.channels)),  # 去重但保留顺序
                    valid_now=valid_now,
                    note=note,
                )
            )

        return items, dropped

    def _apply_budget(
        self,
        items: list[RetrievedItem],
        plan: QueryPlan,
    ) -> tuple[list[RetrievedItem], int]:
        """按条数与 token 预算截断。

        排序依据是融合分，不做二次调整——多通道一致的记忆排前面，
        这是 RRF 的应有结果。重要度（importance）作为次要键，
        用于同分时优先给「值得记住」的条目。
        """
        # 排序：先看融合分；同分时看「可用性」（越近越常用越靠前）；
        # 再同分才看重要度。衰减只影响排序，不改变一条记忆是否成立。
        now = self._clock.now()
        ordered = sorted(
            items,
            key=lambda item: (
                -item.score,
                -effective_strength(
                    item.memory,
                    now,
                    base_half_life_hours=self._settings.decay_half_life_hours,
                    floor=self._settings.decay_floor,
                ),
                -item.memory.importance,
            ),
        )

        selected: list[RetrievedItem] = []
        used_tokens = 0

        for item in ordered:
            cost = estimate_tokens(render_item_line(item, self._tz))
            if len(selected) >= plan.max_items:
                break
            if used_tokens + cost > plan.token_budget:
                # 大条目不能突破预算，也不能阻止后面的短条目被考虑。
                continue
            selected.append(item)
            used_tokens += cost

        return selected, used_tokens

    def _fit_context_budget(self, result: RetrievalResult) -> None:
        """对真正交给模型的完整文本限预算，而非只计算证据正文。

        先去掉可重新查询的摘要卡，再去掉最低排名的证据；结构化缺口
        始终保留。预算小于固定围栏时，渲染器输出空串并由宿主处理缺口。
        """
        budget_gap = "上下文预算不足，部分索引或证据未展示；可增加预算或缩小范围后重查。"
        while True:
            cost = estimate_retrieval_tokens(result, self._tz)
            if cost <= result.plan.token_budget:
                result.trace.selected = len(result.items)
                result.trace.token_estimate = cost
                return

            if budget_gap not in result.gaps:
                result.gaps.append(budget_gap)
            if result.summaries:
                result.summaries.pop()
            elif result.items:
                result.items.pop()
            else:
                result.trace.selected = 0
                result.trace.token_estimate = 0
                result.trace.notes.append("预算小于固定上下文开销，渲染结果为空。")
                return

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def _exists_at(memory: MemoryItem, plan: QueryPlan) -> bool:
        """这条记忆在 plan.as_of 那一刻是否已经存在且有效。"""
        moment = plan.as_of
        if moment is None:
            return True
        if memory.recorded_at > moment:
            return False
        if memory.valid_from is not None and memory.valid_from > moment:
            return False
        if memory.valid_to is not None and memory.valid_to <= moment:
            return False
        return True

    @staticmethod
    def _violates_hard_time(memory: MemoryItem, plan: QueryPlan) -> bool:
        """这条记忆是否落在用户明确说出的时间范围之外？

        只有 hard=True（用户给了明确日期/「昨天」这类可确定的范围）才检查。
        「上次」「第一次」属于软提示，范围未知，不能拿它去排除任何东西。
        """
        hint = plan.time_hint
        if not plan.hard_time or hint is None or hint.start is None or hint.end is None:
            return False

        anchor = memory.occurred_start or memory.recorded_at
        return not (hint.start <= anchor < hint.end)

    @staticmethod
    def _count_channels(hits: list[ChannelHit]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for hit in hits:
            counts[hit.channel] = counts.get(hit.channel, 0) + 1
        return counts

    def _collect_gaps(
        self,
        selected: list[RetrievedItem],
        plan: QueryPlan,
        hits: list[ChannelHit],
    ) -> list[str]:
        """把「没找到什么」明确写出来，避免模型用想象填补空白。"""
        gaps: list[str] = []

        if not selected:
            gaps.append("没有检索到可用的记忆证据。请如实说明不确定，不要虚构共同经历。")

        if plan.hard_time and not any(hit.channel == "time" for hit in hits):
            gaps.append("指定的时间范围内没有匹配记录，可以扩大范围或向用户确认具体日期。")

        if plan.expansion_mode == "exact_lookup" and selected:
            if not any(item.memory.source_event_ids for item in selected):
                gaps.append("命中记忆缺少原始事件来源，无法回原文核对措辞。")

        if selected and plan.token_budget and len(selected) >= plan.max_items:
            gaps.append(f"候选超过上限（{plan.max_items} 条），其余证据未展示；如需要请缩小范围后重查。")

        return gaps
