"""运行时门面：宿主只需要认识这一个类。

对外接口严格对齐架构文档第 7 节：

    bootstrap           恢复身份、关系、活动目录
    retrieve            取证据、来源、缺口与检索轨迹
    observe             持久保存实际对话，并提交整理任务
    checkpoint          保存未完活动，支持跨窗口继续
    propose_core_update 受控地更新身份/关系核心
    correct / forget    纠正与删除，由后端负责传播

另外加了几个便利方法（register_activity、mark_used、run_consolidation），
它们不是架构文档要求的最小接口，但宿主写起来会舒服很多。

一条重要约束：**记忆正文只是资料**。
检索到的内容不会因为被取出来就获得修改核心、注册工具或执行代码的权限；
核心变更必须走 propose_core_update，且需要证据和版本号。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .clock import Clock, SystemClock, load_timezone
from .config import Settings
from .consolidation import ConsolidationReport, MemoryExtractor, run_pending_jobs
from .errors import InvalidCorrection, ScopeNotFound
from .models import (
    Activity,
    ActivityStatus,
    BootstrapResult,
    CoreKind,
    CoreSnapshot,
    Correction,
    CorrectionResult,
    EventRecord,
    ForgetResult,
    ForgetSelector,
    LinkRelation,
    MemoryItem,
    MemoryStatus,
    ObserveResult,
    Query,
    RetrievalResult,
    RetrievedItem,
    Scope,
    Turn,
    new_id,
)
from .retrieval.embedding import Embedder
from .retrieval.pipeline import RetrievalPipeline
from .retrieval.vector import VectorIndex
from .summarization import build_summaries
from .storage.database import Database


class MemoryRuntime:
    """长期记忆引擎的门面。一个进程通常只创建一个实例。"""

    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        db_path: str | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._settings = settings or Settings.from_env()
        if db_path:
            self._settings = self._settings.with_db(db_path)

        # 时钟可替换是「可测试」的关键：测试里用 FixedClock 就能模拟时间流逝
        self._clock = clock or SystemClock()
        self._tz = load_timezone(self._settings.timezone)
        self._db = Database.open(self._settings.db_path)
        self._repos = self._db.repositories

        # 嵌入模型同样可替换。默认的哈希嵌入是零依赖占位实现，
        # 换成真实模型时只需实现 Embedder 协议并从构造函数传进来。
        self._vectors = VectorIndex(self._db, embedder)
        self._pipeline = RetrievalPipeline(
            self._db, self._settings, self._clock, self._tz, vector_index=self._vectors
        )

    # -- 生命周期 ---------------------------------------------------------

    def __enter__(self) -> "MemoryRuntime":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def db(self) -> Database:
        return self._db

    @property
    def vectors(self) -> VectorIndex:
        """向量索引。换嵌入模型后调 rebuild_vectors() 重建即可。"""
        return self._vectors

    # -- 1. 启动恢复 -------------------------------------------------------

    def bootstrap(
        self,
        user_id: str,
        character_id: str,
        activity_id: str | None = None,
    ) -> BootstrapResult:
        """恢复「我是谁、我们是什么关系、手上还有什么没做完」。

        核心缺失时**不抛异常**：新角色本来就没有核心。
        这时返回 recovery_warning，让宿主如实告诉用户恢复失败，
        而不是拿一个临时身份顶上去——后者会让角色在不知不觉中「换了个人」。
        """
        scope = Scope(user_id=user_id, character_id=character_id)
        identity = self._repos.cores.latest(scope, CoreKind.IDENTITY)
        relationship = self._repos.cores.latest(scope, CoreKind.RELATIONSHIP)

        activities = self._repos.activities.list_for_scope(
            scope, statuses=(ActivityStatus.ACTIVE, ActivityStatus.PAUSED)
        )

        selected: Activity | None = None
        if activity_id:
            selected = self._repos.activities.get(activity_id)
            if selected is not None and selected.scope.character_id != character_id:
                # 有人拿别的角色的活动 ID 来查：当作没找到，不泄露存在性
                selected = None
        elif activities:
            selected = activities[0]

        highlights = self._repos.memories.list_important(
            scope, self._settings.bootstrap_highlight_limit
        )
        summaries = self._repos.summaries.list_active(scope, limit=5)

        warning = None
        if identity is None:
            warning = (
                f"未找到角色 {character_id} 的身份核心。"
                "请先通过 propose_core_update 建立身份，再让角色开始对话。"
            )

        return BootstrapResult(
            scope=scope,
            identity=identity,
            relationship=relationship,
            activities=activities,
            selected_activity=selected,
            highlights=highlights,
            summaries=summaries,
            core_version=identity.core_version if identity else None,
            recovery_warning=warning,
        )

    # -- 2. 检索 -----------------------------------------------------------

    def retrieve(
        self,
        query_text: str,
        scope: Scope,
        time_range: tuple[datetime, datetime] | None = None,
        precision: str | None = None,
        max_items: int | None = None,
        token_budget: int | None = None,
        as_of: datetime | None = None,
    ) -> RetrievalResult:
        """检索证据。返回结果自带检索轨迹与缺口说明。

        as_of 是「时间旅行」：给一个过去的时刻，只返回那一刻已经存在、
        并且当时有效的记忆。复盘「我们当时是怎么以为的」时用得上。
        """
        query = Query(
            text=query_text,
            scope=scope,
            time_range=time_range,
            precision=precision,
            max_items=max_items,
            token_budget=token_budget,
            as_of=as_of,
        )
        return self._pipeline.run(query)

    # -- 3. 观察（保存原始对话） -------------------------------------------

    def observe(
        self,
        turn: Turn,
        scope: Scope,
        idempotency_key: str,
    ) -> ObserveResult:
        """保存一轮实际对话，并把抽取任务写进同一个事务。

        幂等键命中时不会重复写入，也不会重复入队——宿主可以放心重试。
        """
        activity_id = turn.activity_id or scope.activity_id
        event_scope = Scope(
            user_id=scope.user_id,
            character_id=scope.character_id,
            relationship_id=scope.relationship_id,
            activity_id=activity_id,
            domain=scope.domain,
        )
        now = self._clock.now()

        event = EventRecord(
            event_id=new_id("evt"),
            scope=event_scope,
            conversation_id=turn.conversation_id,
            speaker=turn.speaker,
            content=turn.content,
            occurred_at=turn.occurred_at,
            source_timezone=turn.source_timezone,
            occurred_precision=turn.occurred_precision,
            recorded_at=now,
            idempotency_key=idempotency_key,
        )

        with self._db.transaction():
            stored, duplicate = self._repos.events.insert(event)

            job_id: str | None = None
            if not duplicate:
                if activity_id:
                    self._repos.activities.touch_last_event(activity_id, event.occurred_at)
                job_id = self._repos.jobs.enqueue(
                    job_type="extract",
                    scope=event_scope,
                    payload={"event_id": stored.event_id, "conversation_id": turn.conversation_id},
                    now=now,
                    activity_id=activity_id,
                )

        version = None
        if activity_id:
            activity = self._repos.activities.get(activity_id)
            version = activity.checkpoint_version if activity else None

        return ObserveResult(
            event=stored,
            duplicate=duplicate,
            job_id=job_id,
            activity_version=version,
        )

    # -- 4. 活动与检查点 ---------------------------------------------------

    def register_activity(
        self,
        scope: Scope,
        title: str,
        goal: str = "",
        open_loops: list[str] | None = None,
        active_entities: list[str] | None = None,
    ) -> Activity:
        """登记一段新活动（例如「《海底邮局》的故事」）。

        同一个角色可以有多段活动并行；工作记忆靠 activity_id 隔离，
        这样昨天讲故事讲到一半的状态，不会污染今天学英语的状态。
        """
        now = self._clock.now()
        activity = Activity(
            activity_id=new_id("act"),
            scope=scope,
            title=title,
            goal=goal,
            open_loops=list(open_loops or []),
            active_entities=list(active_entities or []),
            checkpoint={},
            checkpoint_version=1,
            created_at=now,
            updated_at=now,
        )
        with self._db.transaction():
            self._repos.activities.create(activity)
        return activity

    def checkpoint(
        self,
        activity_id: str,
        state: dict[str, Any],
        expected_version: int,
    ) -> int:
        """保存未完成活动的状态，返回新的版本号。

        state 支持的键：
            goal / open_loops / active_entities / checkpoint / status
        其他键会直接报错——把拼写错误当静默忽略，是新手最难查的一类 bug。

        版本号对不上会抛 VersionConflict：说明另一个窗口已经改过这段活动，
        调用方应当重新 bootstrap 再决定怎么合并。
        """
        allowed = {"goal", "open_loops", "active_entities", "checkpoint", "status"}
        unknown = set(state) - allowed
        if unknown:
            raise ValueError(f"未知的检查点字段：{sorted(unknown)}；可用字段：{sorted(allowed)}")

        status = state.get("status")
        if isinstance(status, str):
            status = ActivityStatus(status)

        with self._db.transaction():
            return self._repos.activities.update_checkpoint(
                activity_id=activity_id,
                expected_version=expected_version,
                now=self._clock.now(),
                goal=state.get("goal"),
                open_loops=state.get("open_loops"),
                active_entities=state.get("active_entities"),
                checkpoint=state.get("checkpoint"),
                status=status,
            )

    # -- 5. 核心档案 -------------------------------------------------------

    def propose_core_update(
        self,
        scope: Scope,
        kind: CoreKind | str,
        patch: dict[str, Any],
        change_reason: str,
        expected_version: int | None = None,
        evidence_ids: list[str] | None = None,
    ) -> CoreSnapshot:
        """提出并应用一次核心变更。

        这里是「补丁合并」语义：patch 里出现的键覆盖旧值，没出现的保持不变。
        版本号由后端计算，调用方只需声明「我是基于哪个版本改的」。

        架构文档的要求：核心变更要可解释。所以 change_reason 是必填项，
        evidence_ids 建议填上支撑这次变更的原始事件。
        """
        core_kind = kind if isinstance(kind, CoreKind) else CoreKind(kind)
        now = self._clock.now()
        current = self._repos.cores.latest(scope, core_kind)
        merged = dict(current.payload) if current else {}
        merged.update(patch)

        with self._db.transaction():
            return self._repos.cores.append(
                scope=scope,
                kind=core_kind,
                payload=merged,
                change_reason=change_reason,
                effective_from=now,
                now=now,
                evidence_ids=evidence_ids,
                expected_version=expected_version,
            )

    # -- 6. 纠正与删除 -----------------------------------------------------

    def correct(self, memory_id: str, correction: Correction) -> CorrectionResult:
        """纠正一条记忆。

        kind="fix"    ：原先就错了 → 标记 retracted，并让依赖它的派生记忆失效
        kind="change" ：当时对、现在变了 → 结束有效期，写一条新记录并建立 supersedes 链接

        两者的区别决定了「历史能不能被解释」：
        fix 之后旧记录不再算数；change 之后旧记录仍然可查，只是标了截止时间。
        """
        old = self._repos.memories.get(memory_id)
        if old is None:
            raise ScopeNotFound(f"找不到记忆 {memory_id}")
        if correction.kind not in ("fix", "change"):
            raise InvalidCorrection(f"未知的纠正类型：{correction.kind}")

        now = self._clock.now()
        effective = correction.effective_from or now
        invalidated: list[str] = []

        with self._db.transaction():
            if correction.kind == "fix":
                self._repos.memories.update_status(memory_id, MemoryStatus.RETRACTED, now)
                invalidated = self._invalidate_derived(
                    memory_id, now, being_removed={memory_id}
                )
                return CorrectionResult(
                    old_memory=old,
                    invalidated_derived=invalidated,
                    note="已撤回原记录；派生记忆已失效，需要时可重新归纳。",
                )

            # kind == "change"
            self._repos.memories.close_validity(memory_id, effective, now)
            new_item = self._build_replacement(old, correction, effective, now)
            inserted = self._repos.memories.insert(new_item)
            if inserted is not None:
                self._repos.links.add(
                    new_item.memory_id, memory_id, LinkRelation.SUPERSEDES, now
                )
                # 新事实要能被语义通道立刻检索到，所以同步建索引
                self._vectors.index_memory(new_item, now)

            return CorrectionResult(
                old_memory=old,
                new_memory=inserted or new_item,
                note="旧记录保留为历史值，新记录已生效。",
            )

    def forget(self, selector: ForgetSelector) -> ForgetResult:
        """删除记忆，并把失效传播到索引与派生记录。

        删除是「用户要求忘掉」，比纠正更强硬：
        状态置为 deleted（保留审计痕迹），检索与索引都不再返回它，
        依赖它的派生记忆一并失效，避免它换个形式又冒出来。
        """
        targets = self._resolve_forget_targets(selector)
        now = self._clock.now()
        deleted: list[str] = []
        invalidated: list[str] = []

        with self._db.transaction():
            for memory_id in targets:
                if self._repos.memories.delete(memory_id, now):
                    deleted.append(memory_id)
                    # 向量是派生数据，主记录删除后必须一起清掉，
                    # 否则它还会出现在语义通道的候选里。
                    self._vectors.delete(memory_id)
                    # 把「本次要删的全部 id」传进去：同一批删除里的互相支持不算数，
                    # 只有批次之外还活着的来源才能保住一条派生记忆。
                    invalidated.extend(
                        self._invalidate_derived(memory_id, now, being_removed=set(targets))
                    )

        return ForgetResult(
            deleted_ids=deleted,
            invalidated_derived=sorted(set(invalidated)),
            note=selector.reason or "已按请求删除",
        )

    def mark_used(self, memory_ids: list[str]) -> None:
        """标记「这些记忆确实被用于回答」。

        单独提供这个接口，而不是在 retrieve 里自动加访问次数：
        架构文档明确要求「反复读到摘要不增加独立证据」，否则一条错误记忆
        会因为被反复检索而越排越前。
        """
        if not memory_ids:
            return
        with self._db.transaction():
            self._repos.memories.mark_used(memory_ids, self._clock.now())

    # -- 7. 整理任务 -------------------------------------------------------

    def rebuild_vectors(self, scope: Scope) -> int:
        """重建某个角色的向量索引，返回重建条数。

        什么时候需要：
          - 换成真实嵌入模型之后（旧模型的向量不能再用）；
          - 从备份恢复，或者早期版本写的记忆还没有向量；
          - 手动改过记忆正文，怀疑索引漂移。
        """
        with self._db.transaction():
            return self._vectors.rebuild(scope, self._clock.now())

    def run_consolidation(
        self,
        extractor: MemoryExtractor | None = None,
        limit: int = 10,
    ) -> ConsolidationReport:
        """执行待处理的抽取任务。

        第一版同步执行；真实部署可以把这个方法丢进后台线程或队列 worker，
        因为任务本身已经落库，进程被杀也不会丢。
        """
        return run_pending_jobs(
            db=self._db,
            clock=self._clock,
            extractor=extractor,
            vector_index=self._vectors,
            limit=limit,
            tz=self._tz,
        )

    def pending_jobs(self) -> int:
        return self._repos.jobs.pending_count()

    # -- 8. 主题摘要 -------------------------------------------------------

    def build_summaries(self, scope: Scope) -> int:
        """立即重建主题摘要，返回卡片数。

        平时不用手动调：extract → dedupe → summarize 会自动串起来。
        这个方法用于「刚导入一批历史数据，想马上有索引」的场景。
        """
        return build_summaries(self._db, scope, self._clock, self._tz)

    def expand_summary(self, summary_id: str, limit: int = 5) -> list[RetrievedItem]:
        """把一张索引卡回源展开成原始记忆。

        这是「先摘要后展开」的后半段：模型看到摘要后如果觉得相关，
        再用这个方法取具体证据，而不是一上来就塞十条记忆。
        """
        summary = self._repos.summaries.get(summary_id)
        if summary is None:
            raise ScopeNotFound(f"找不到摘要 {summary_id}")

        memories = self._repos.memories.fetch_many(summary.source_memory_ids)
        now = self._clock.now()

        items = [
            RetrievedItem(
                memory=memory,
                # 展开结果不带检索分：它的相关性由摘要决定，不是由这一层重新算
                score=summary.importance,
                channels=["summary_expand"],
                valid_now=memory.is_current(now),
                note="来自主题摘要展开",
            )
            for memory in memories.values()
            if memory.status not in (MemoryStatus.RETRACTED, MemoryStatus.DELETED)
        ]
        items.sort(key=lambda item: (-item.memory.importance, item.memory.recorded_at))
        return items[:limit]

    # -- 内部实现 ---------------------------------------------------------

    def _invalidate_derived(
        self,
        memory_id: str,
        now: datetime,
        being_removed: set[str] | None = None,
    ) -> list[str]:
        """把「失去全部来源支持」的派生记录标成 retracted。

        规则取自 Zep 的 remove_episode：**只有当一个事实的所有父来源都没了，
        它才该消失**。如果一条派生记忆同时由 A 和 B 支撑，删掉 A 不该动它——
        否则删一次来源就会误伤别的证据。

        用广度优先遍历，visited 防止链接成环时死循环。
        """
        removed = set(being_removed or ()) | {memory_id}
        visited: set[str] = set()
        queue = [memory_id]
        invalidated: list[str] = []

        while queue:
            current = queue.pop(0)
            for dependent in self._repos.links.dependents(current):
                if dependent in visited:
                    continue
                visited.add(dependent)

                # 还有别的父来源活着 → 保留，只当没看见这次删除
                if self._has_surviving_parent(dependent, removed):
                    continue

                self._repos.memories.update_status(dependent, MemoryStatus.RETRACTED, now)
                invalidated.append(dependent)
                queue.append(dependent)

        return invalidated

    def _has_surviving_parent(self, memory_id: str, removed: set[str]) -> bool:
        """这条派生记忆是否还有「没被删除且仍然有效」的父来源。"""
        parents = self._repos.links.parents_of(memory_id)
        if not parents:
            # 没登记来源的派生记录（例如早期数据）无法判断，按保守策略处理：
            # 不因为「查不到来源」就把它连带删掉。
            return True

        for parent_id in parents:
            if parent_id in removed:
                continue
            parent = self._repos.memories.get(parent_id)
            if parent is not None and parent.status not in (
                MemoryStatus.RETRACTED,
                MemoryStatus.DELETED,
            ):
                return True
        return False

    def _build_replacement(
        self,
        old: MemoryItem,
        correction: Correction,
        effective: datetime,
        now: datetime,
    ) -> MemoryItem:
        """为「现在变了」构造一条新记忆：沿用旧记录的作用域与来源。"""
        body = correction.corrected_body if correction.corrected_body is not None else old.body
        sources = correction.evidence_event_ids or old.source_event_ids

        return MemoryItem(
            memory_id=new_id("mem"),
            memory_type=old.memory_type,
            scope=old.scope,
            body=dict(body),
            source_event_ids=list(sources),
            source_speaker=old.source_speaker,
            epistemic_status=old.epistemic_status,
            modality=old.modality,
            occurred_start=effective,
            occurred_end=None,
            occurred_precision="exact",
            valid_from=effective,
            valid_to=None,
            supersedes_id=old.memory_id,
            status=MemoryStatus.ACTIVE,
            schema_version=old.schema_version,
            importance=old.importance,
            retrieval_strength=old.retrieval_strength,
            recorded_at=now,
        )

    def _resolve_forget_targets(self, selector: ForgetSelector) -> list[str]:
        """把删除选择器解析成具体的记忆 ID 列表。"""
        if selector.memory_ids:
            return list(dict.fromkeys(selector.memory_ids))

        if selector.scope is None:
            raise ValueError("forget 需要 memory_ids 或 scope 之一，不能都为空")

        items = self._repos.memories.list_by_scope(
            selector.scope, selector.memory_types, limit=1000
        )
        if selector.domain:
            items = [item for item in items if item.scope.domain == selector.domain]
        return [item.memory_id for item in items]
