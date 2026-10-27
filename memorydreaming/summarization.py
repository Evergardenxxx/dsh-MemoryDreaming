"""主题摘要：把零散的记忆压成「索引卡」，供「先摘要后展开」使用。

为什么摘要不能是第五类记忆，也不能存全文？

架构文档第 6 节要求「摘要保留来源，不让反复压缩的摘要成为唯一事实底座」。
一旦摘要承担了事实职责，第二代摘要在第一代上再压缩，错误就会被固化，
而且没有任何一条原始证据能推翻它。所以这里的设计是：

    summaries 只是索引：主题 + 时间范围 + 一句概述 + 来源 ID
    真正的事实永远在 source_memory_ids 指向的记忆里
    需要细节时用 runtime.expand_summary() 回源展开

生成方式（第一版）是**抽取式**的：不写新句子，只从已有记忆里挑重要的、
按时间排好拼起来。它读起来不如模型写的地道，但有两个好处：
不会编造、结果可复现。等接入模型后，把 _compose 换成生成式即可，
表结构和检索逻辑都不用动。

聚类方式不是「整句相似度」，而是**主题词 + 文档频率**：
先统计哪些二字词在多少条记忆里出现过，再按词把记忆分组。
整句 Jaccard 在这里行不通——「第一次去海边」和「周末去海边要带相机」
只共享两个词，算出来只有 0.07，永远跨不过阈值；而它们显然属于同一个主题。
代价是同一主题下的记忆会按词重叠，所以还要对「几乎同一批记忆」
的主题词去重（见 _topic_groups）。
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from .clock import Clock
from .models import (MemoryItem, MemoryStatus, ReadFilter, Scope, Summary, SummaryHit,
                     memory_source_digest, new_id)
from .retrieval.keyword import text_shingles
from .storage.database import Database

# 少于三条记忆不生成摘要：两条记忆的「摘要」只是换个说法重复一遍，
# 没有索引价值，还会挤占检索预算。
MIN_CLUSTER_SIZE = 3

# 一次最多生成几张卡。主题太多等于没有主题，8 张足够覆盖常见话题。
MAX_TOPICS = 8

# 摘要正文上限。摘要一旦变长就会开始承担事实职责，那条线不能越。
MAX_DIGEST_CHARS = 180
SUMMARY_ALGORITHM_VERSION = "extractive-v1"

# 选主题词时要排除的高频虚词：它们出现在每条记忆里，区分度为零。
_TOPIC_STOPWORDS = {
    "用户", "我们", "你们", "他们", "自己", "这个", "那个", "这些", "那些",
    "时候", "什么", "怎么", "可以", "就是", "已经", "因为", "所以", "然后",
    "一下", "一个", "没有", "不是", "还是", "但是", "而且", "以及", "现在",
}


def build_summaries(
    db: Database,
    scope: Scope,
    clock: Clock,
    tz: tzinfo,
    *,
    min_size: int = MIN_CLUSTER_SIZE,
    max_topics: int = MAX_TOPICS,
    reactivate: bool = False,
) -> int:
    """重建某个角色的全部主题摘要，返回写入/更新的卡片数。

    重建而不是增量追加，是因为主题会合并、会消失。
    卡片 ID 保持不变；正文变化后旧引用仍按 revision 规则失效。
    后台调用默认保留 withdrawn/rolled_back；可信宿主可显式重新激活。
    """
    if not isinstance(reactivate, bool):
        raise ValueError("reactivate 必须为布尔值")
    with db.transaction():
        return _build_summaries_locked(db, scope, clock, tz, min_size, max_topics, reactivate)


def _build_summaries_locked(db, scope, clock, tz, min_size, max_topics, reactivate) -> int:
    # 读取来源、状态检查和投影更新使用同一写事务，避免并发撤回被覆盖。
    repos = db.repositories
    memories = repos.memories.list_by_scope(
        scope, limit=500, statuses=(MemoryStatus.ACTIVE,)
    )
    memories = [item for item in memories if not repos.forgetting.is_suppressed(item)]
    groups = _topic_groups(memories, min_size, max_topics)
    now = clock.now()
    kept_topics: list[str] = []
    built = 0

    for topic, group in groups:
        digest, span_start, span_end, importance = _compose(group, tz)
        summary = Summary(
            summary_id=new_id("sum"),
            scope=scope,
            topic=topic,
            digest=digest,
            source_memory_ids=[item.memory_id for item in group],
            source_event_ids=sorted({eid for item in group for eid in item.source_event_ids}),
            source_digest=memory_source_digest(group, SUMMARY_ALGORITHM_VERSION),
            algorithm_version=SUMMARY_ALGORITHM_VERSION,
            span_start=span_start,
            span_end=span_end,
            importance=importance,
            created_at=now,
            updated_at=now,
        )

        stored = repos.summaries.upsert(summary, now, reactivate=reactivate)

        if stored.status == "active":
            kept_topics.append(topic)
            built += 1

    # 主题消失后，旧卡标 stale 而不是删除：外部可能还在引用它
    existing = repos.summaries.list_active(scope, limit=200)
    stale = [item.summary_id for item in existing if item.topic not in kept_topics]
    if stale:
        repos.summaries.mark_stale(stale, now)

    return built


def find_summaries(
    db: Database,
    scope: Scope,
    query_text: str,
    limit: int = 2,
    min_score: float = 0.12,
    *, read_filter: ReadFilter | None = None,
) -> list[SummaryHit]:
    """按问题找最相关的索引卡。

    打分用**覆盖率**（问题里的词有多少被这张卡覆盖），而不是 Jaccard。
    因为摘要和问题往往长度悬殊，Jaccard 会被长文本稀释到接近 0；
    覆盖率回答的是「这张卡能不能解释用户提到的东西」，更符合索引的用途。

    卡片数量少（几十张），所以直接全量算，不做倒排。
    """
    repos = db.repositories
    summaries = repos.summaries.list_active(scope, limit=50, read_filter=read_filter)
    if not summaries:
        return []

    query_shingles = text_shingles(query_text)
    if not query_shingles:
        return []

    hits: list[SummaryHit] = []
    for summary in summaries:
        card_text = f"{summary.topic} {summary.digest}"
        card_score = _coverage(query_shingles, text_shingles(card_text))

        # 只比索引卡本身可能不够：摘要写的是概述，用户问的往往是细节。
        # 所以再拿前几条来源记忆的正文比一次，取较高分。
        sources = repos.memories.fetch_many(summary.source_memory_ids[:5])
        source_score = max(
            (_coverage(query_shingles, text_shingles(item.body_text)) for item in sources.values()),
            default=0.0,
        )

        score = max(card_score, source_score)

        # 用户原话里直接出现了这张卡的主题词，就是最强的相关性信号。
        # 覆盖率会被「记得吗」「那次」这类虚词稀释——一句 19 个片段的问句里
        # 只命中「海边」两字，算出来只有 0.05，但意图其实非常明确。
        if summary.topic in query_text:
            score = max(score, 0.5)

        if score >= min_score:
            hits.append(SummaryHit(summary=summary, score=round(score, 4)))

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _coverage(query: set[str], document: set[str]) -> float:
    """问题里的词有多少被文档覆盖，取值 0~1。"""
    if not query:
        return 0.0
    return len(query & document) / len(query)


def _topic_groups(
    memories: list[MemoryItem],
    min_size: int,
    max_topics: int,
) -> list[tuple[str, list[MemoryItem]]]:
    """按主题词把记忆分组，返回 [(主题词, 该主题下的记忆)]。

    步骤：
      1. 统计每个二字词出现在哪些记忆里（文档频率）；
      2. 只保留「至少有 min_size 条记忆提到」的词——一次提及不算主题；
      3. 按覆盖条数从多到少排序，跳过与已选主题几乎同一批记忆的词。

    第 3 步是必需的：「去海」和「海边」往往覆盖同一批记忆，
    不去重就会生成两张内容雷同的卡，等于没有分类。
    """
    occurrences: dict[str, list[MemoryItem]] = {}

    for memory in memories:
        # sizes=(2,) 只取二字词：三字词太具体，做主题会碎
        for gram in text_shingles(memory.body_text, sizes=(2,)):
            if gram in _TOPIC_STOPWORDS:
                continue
            occurrences.setdefault(gram, []).append(memory)

    candidates = [
        (gram, group) for gram, group in occurrences.items() if len(group) >= min_size
    ]
    candidates.sort(key=lambda pair: (-len(pair[1]), pair[0]))

    chosen: list[tuple[str, list[MemoryItem]]] = []
    chosen_sets: list[set[str]] = []

    for gram, group in candidates:
        if len(chosen) >= max_topics:
            break

        ids = {memory.memory_id for memory in group}
        # 与已选主题的记忆集合重合度太高 → 是同一个主题的另一种说法
        if any(_overlap_ratio(ids, seen) > 0.8 for seen in chosen_sets):
            continue

        chosen.append((gram, group))
        chosen_sets.append(ids)

    return chosen


def _overlap_ratio(left: set[str], right: set[str]) -> float:
    """两个记忆集合的重合比例（用较小的集合做分母）。"""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _compose(
    group: list[MemoryItem],
    tz: tzinfo,
) -> tuple[str, datetime | None, datetime | None, float]:
    """拼出一句概述 + 时间跨度 + 重要度。

    只挑三条最重要的记忆、每条截断到 40 字：
    摘要的职责是「让人知道这张卡值不值得展开」，不是「把内容再讲一遍」。
    """
    ordered = sorted(
        group,
        key=lambda item: (-item.importance, item.occurred_start or item.recorded_at),
    )

    fragments: list[str] = []
    for memory in ordered[:3]:
        text = memory.render(60).replace("\n", " ").strip()
        if text:
            fragments.append(text[:40])

    moments = [item.occurred_start or item.recorded_at for item in group]
    span_start = min(moments)
    span_end = max(moments)

    start_text = span_start.astimezone(tz).strftime("%m-%d")
    end_text = span_end.astimezone(tz).strftime("%m-%d")
    span_text = start_text if start_text == end_text else f"{start_text}~{end_text}"

    digest = f"{span_text}｜" + "；".join(fragments)
    if len(digest) > MAX_DIGEST_CHARS:
        digest = digest[:MAX_DIGEST_CHARS] + "…"

    return digest, span_start, span_end, max(item.importance for item in group)
