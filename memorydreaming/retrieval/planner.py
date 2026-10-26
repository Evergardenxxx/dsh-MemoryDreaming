"""查询计划：检索之前，先把「我要什么」想清楚。

这是架构文档第 4 节的核心：不要一上来就查，而是先产出一份计划——
四类记忆各自的相关度、时间范围、精度要求、每类给多少候选预算。

三个刻意保留的设计：

1. **多标签分数，不做归一化**。四类分数各表示「这类可能有用」，加起来不等于 1。
   一句「上次答应的睡前故事，今天接着讲吧」确实同时涉及工作进度、情景约定、
   故事习惯和用户偏好，硬要归一化会人为压低其中几类。

2. **规则打分，不用模型**。第一版要的是可解释、可测试：你能说清为什么
   这一句被判成「偏情景」。等有了标注数据，再换成模型输出同样的结构。

3. **不把 48 小时当硬边界**。时间解析只回答「用户指的是哪段时间」，
   「48 小时内优先展开细节」是 expansion_mode 的事，两者不能混在一起。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, tzinfo

from ..clock import Clock
from ..config import Settings
from ..models import MemoryType, Query, QueryPlan, TimeHint
from .keyword import extract_entities, tokenize_query
from .timeexpr import parse_time_hint

# 每类记忆的「语言线索」。命中越多，这类记忆越可能有用。
# 这些词只是朴素信号，不代表语言学上的严谨分类；后续可以按真实对话继续补。
_TYPE_HINTS: dict[MemoryType, tuple[str, ...]] = {
    MemoryType.WORKING: (
        "约定", "答应", "说好", "还没", "进度", "继续", "接着",
        "未完成", "计划", "下次", "安排", "待办", "进行到",
    ),
    MemoryType.EPISODIC: (
        "那次", "当时", "第一次", "说过", "聊过", "发生", "故事",
        "上次", "回忆", "记得", "那天", "原话", "提起",
    ),
    MemoryType.SEMANTIC: (
        "喜欢", "讨厌", "习惯", "名字", "住在", "生日", "偏好",
        "最怕", "不吃", "爱吃", "叫什么", "是谁", "是不是",
    ),
    MemoryType.PROCEDURAL: (
        "怎么", "步骤", "规则", "仪式", "流程", "做法", "每次", "先", "然后",
    ),
}

# 没有任何线索时的默认分：四类平均看待，避免因为「猜不出类别」就漏掉证据
_NEUTRAL_SCORE = 0.5

# 「向前看」的说法。它们出现在句子里时，句中的时间词（今天、现在）指的是
# **说话时刻**，而不是要过滤的事件时间。
#
# 例子：「上次答应的睡前故事，今天接着讲吧」
#   「今天」是「现在继续讲」，不是在问「今天发生了什么」。
# 如果把它当成硬性时间过滤，就会把三天前的约定全部排除掉——
# 这正是架构文档提醒的「事件时间与记录时间不能混用」的另一种变体。
_PROSPECTIVE_MARKERS = (
    "接着", "继续", "接下来", "下次", "待会", "一会儿", "还要", "讲吧", "然后呢",
)

# 「在延续当前交流」的标记。
# 架构文档把「最近 48 小时」的用途限定为「继续当前交流」，
# 所以只有出现这些说法时才启用最近窗口通道。
# 不加这层判断的话，任何问题都会把今天的聊天记录带进上下文——
# 那不是「记得住」，是「什么都能扯上」。
_RECENT_MARKERS = (
    "刚才", "刚刚", "继续", "接着", "接下来", "前面", "上面", "这个话题",
    "我们聊", "你说过", "我说过", "说到哪", "下一句",
)


def _is_prospective(text: str) -> bool:
    """这句话是在「继续做某事」吗？"""
    return any(marker in text for marker in _PROSPECTIVE_MARKERS)


class QueryPlanner:
    """把用户问题翻译成 QueryPlan。"""

    def __init__(self, settings: Settings, clock: Clock, tz: tzinfo) -> None:
        self._settings = settings
        self._clock = clock
        self._tz = tz

    def plan(self, query: Query) -> QueryPlan:
        now = self._clock.now()
        time_hint = self._resolve_time(query, now)

        # 向前看的句子里，时间词指向「现在」而不是「事件发生的时候」，
        # 所以把它降级成软提示：仍然显示给调用方看，但不作为过滤条件。
        if time_hint is not None and time_hint.hard and _is_prospective(query.text):
            time_hint = replace(time_hint, hard=False)

        type_scores = self._score_types(query.text, time_hint, now)
        expansion_mode = self._expansion_mode(time_hint, now)

        # 高分类型给足候选预算，低分类型只留小额探查预算。
        # 「探查」的意义在于：路由可能猜错，留一点预算能兜住意外。
        per_type_budget = {
            memory_type: (
                self._settings.per_type_candidate_budget
                if score >= _NEUTRAL_SCORE
                else self._settings.probe_candidate_budget
            )
            for memory_type, score in type_scores.items()
        }

        return QueryPlan(
            query_text=query.text,
            type_scores=type_scores,
            per_type_budget=per_type_budget,
            keywords=tokenize_query(query.text),
            entities=extract_entities(query.text),
            time_hint=time_hint,
            precision=time_hint.precision if time_hint else "coarse",
            hard_time=bool(time_hint and time_hint.hard),
            prefers_recent=self._prefers_recent(query.text, expansion_mode),
            expansion_mode=expansion_mode,
            max_items=query.max_items or self._settings.max_context_items,
            token_budget=query.token_budget or self._settings.context_token_budget,
            as_of=query.as_of,
            generated_at=now,
        )

    # -- 内部实现 ---------------------------------------------------------

    def _resolve_time(self, query: Query, now: datetime) -> TimeHint | None:
        """调用方显式给的时间范围优先，其次解析问题文本。"""
        if query.time_range is not None:
            start, end = query.time_range
            return TimeHint(
                label="调用方指定",
                start=start,
                end=end,
                precision=query.precision or "day",
                hard=True,
            )
        return parse_time_hint(query.text, now, self._tz)

    def _score_types(
        self,
        text: str,
        time_hint: TimeHint | None,
        now: datetime,
    ) -> dict[MemoryType, float]:
        """按语言线索给四类打分，返回 0~1 的分数。"""
        scores: dict[MemoryType, float] = {memory_type: 0.2 for memory_type in MemoryType}

        for memory_type, words in _TYPE_HINTS.items():
            hits = sum(1 for word in words if word in text)
            if hits:
                # 每命中一个线索 +0.25，最多加 0.6，防止长句靠堆词刷分
                scores[memory_type] += min(0.6, 0.25 * hits)

        if time_hint is not None:
            if time_hint.hard and time_hint.precision in ("exact", "day"):
                # 「昨天我们说了什么」多半在问那次事件，同时可能涉及未完成的事
                scores[MemoryType.EPISODIC] += 0.15
                scores[MemoryType.WORKING] += 0.05
            elif time_hint.precision == "coarse":
                # 「上次」「那次」更需要靠情景记忆去找
                scores[MemoryType.EPISODIC] += 0.2
                scores[MemoryType.WORKING] += 0.1

        if any(word in text for word in ("原话", "说过", "叫什么", "名字")):
            scores[MemoryType.EPISODIC] += 0.15
            scores[MemoryType.SEMANTIC] += 0.1

        # 完全没有线索：不猜了，四类平均分，靠关键词通道自己说话
        if max(scores.values()) <= 0.2:
            return {memory_type: _NEUTRAL_SCORE for memory_type in MemoryType}

        # 截断到 [0, 1] 并保留两位小数，便于日志肉眼比对
        return {memory_type: round(min(1.0, score), 2) for memory_type, score in scores.items()}

    def _prefers_recent(self, text: str, expansion_mode: str) -> bool:
        """判断该不该启用「最近 48 小时」通道。

        两个条件同时满足才启用：
          1. 默认粒度是细节（说明问题指向当下，而不是很久以前）；
          2. 句子里有延续交流的说法（刚才、继续、我们聊……）。
        """
        if expansion_mode != "detail":
            return False
        return any(marker in text for marker in _RECENT_MARKERS)

    def _expansion_mode(self, time_hint: TimeHint | None, now: datetime) -> str:
        """决定默认展开粒度：细节 / 先摘要再回源 / 精确回查。

        对应架构文档第 5 节那张表：
        - 最近 48 小时 → 优先细节；
        - 更早且主题宽泛 → 先摘要再按相关性展开；
        - 明确日期/原话 → 精确回查。
        """
        if time_hint is not None and time_hint.hard and time_hint.precision in ("exact", "day"):
            return "exact_lookup"

        window_start = now - timedelta(hours=self._settings.recent_detail_window_hours)

        if time_hint is None:
            return "detail"
        if time_hint.end is not None and time_hint.end > window_start:
            return "detail"
        return "summary_then_expand"
