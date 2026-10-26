"""异步整理：把「原始事件」变成「类型记忆」，再把重复的记忆合并。

架构文档第 6 节的流程：
    原始事件 → 类型候选 → 来源/说话者/模态校验 → 写入类型记忆 → 建立证据链接

这一版支持两类任务：

    extract  抽取：谁来抽、怎么抽由调用方决定（示例里是 RuleBasedExtractor）
    dedupe   去重：同一件事被两次对话分别记下来时，合成一条

刻意没做的：summarize（主题摘要）和 propose_core（核心成长候选）。
它们都需要「归纳质量」的判断，没有模型时做出来只是假动作。
任务类型已经留在表里，实现时不用改结构。

幂等的关键在 dedupe_key：
    source_event_id + extractor_version + candidate_key
同一事件、同一抽取器版本、同一个候选，重跑多少次都只会写一条。
升级抽取器后版本号变了，才会产生新候选——但独立证据数仍按原始事件算，
不会因为多跑了一次抽取就「证据变多」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .clock import Clock
from .models import (
    EventRecord,
    LinkRelation,
    MemoryDraft,
    MemoryItem,
    MemoryStatus,
    Scope,
    new_id,
)
from .retrieval.keyword import text_shingles
from .retrieval.vector import VectorIndex
from .storage.database import Database
from .storage.repository import JobRecord

# 去重的默认相似度阈值：两条记忆的字符二元组重合度达到 85% 就视为同一件事。
# 调高的后果是漏合并（同一件事留两条），调低则可能把「相关但不是同一件」的两条
# 合并掉。0.85 是个偏保守的起点，真实数据上应当靠评估来调。
DEFAULT_DEDUPE_THRESHOLD = 0.85


class MemoryExtractor(Protocol):
    """抽取器协议。实现类只需要提供 version 和 extract 两样东西。

    version 参与幂等键，所以每次调整抽取规则都应该改它，
    否则旧候选不会更新，你会以为「规则没生效」。
    """

    version: str

    def extract(self, event: EventRecord) -> list[MemoryDraft]:
        """从一条原始事件里抽出若干条记忆候选。返回空列表表示这条不值得记。"""
        ...


@dataclass
class ConsolidationReport:
    """一次整理的统计结果。"""

    claimed: int = 0          # 本次取出多少任务
    created: int = 0          # 新建多少条记忆
    duplicates: int = 0       # 因幂等键命中而跳过
    merged: int = 0           # 去重合并掉多少条
    skipped: int = 0          # 因缺少处理条件而退回队列
    failed: int = 0           # 失败任务数
    errors: list[str] = field(default_factory=list)


def run_pending_jobs(
    db: Database,
    clock: Clock,
    extractor: MemoryExtractor | None = None,
    vector_index: VectorIndex | None = None,
    limit: int = 10,
) -> ConsolidationReport:
    """取出待处理任务并逐条执行。

    每个任务单独一个事务：一条失败不影响其他任务，
    失败信息写回任务表，未超过重试上限的会自动退回 pending。

    参数:
        extractor:    抽取器。为 None 时 extract 任务会被退回队列（不消耗重试次数），
                      这样可以只跑去重任务——例如换嵌入模型后的批量整理。
        vector_index: 传入后，新写入的记忆会顺带建立向量索引；
                      不传则跳过（检索时就没有语义通道）。
    """
    repos = db.repositories
    report = ConsolidationReport()

    # 先原子地把任务从 pending 改成 running，避免被别的 worker 重复取走
    with db.transaction():
        jobs = repos.jobs.claim(limit, clock.now())
    report.claimed = len(jobs)

    for job in jobs:
        try:
            if job.job_type == "extract":
                if extractor is None:
                    # 没带抽取器：把任务放回队列，下次再处理。
                    # 用 release 而不是 fail，否则三次空跑就把任务判死了。
                    with db.transaction():
                        repos.jobs.release(job.job_id, clock.now())
                    report.skipped += 1
                    continue

                created, duplicates = _run_extract(db, job, extractor, clock, vector_index)
                report.created += created
                report.duplicates += duplicates
                _enqueue_follow_up(db, job, clock)

            elif job.job_type == "dedupe":
                report.merged += _run_dedupe(db, job.scope, clock, vector_index)

            else:
                raise NotImplementedError(f"暂未实现的任务类型：{job.job_type}")

            with db.transaction():
                repos.jobs.complete(job.job_id, clock.now())

        except Exception as exc:  # noqa: BLE001 - 单个任务失败不应中断整批
            report.failed += 1
            report.errors.append(f"{job.job_id}: {exc}")
            with db.transaction():
                repos.jobs.fail(job.job_id, str(exc), clock.now())

    return report


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


def _run_extract(
    db: Database,
    job: JobRecord,
    extractor: MemoryExtractor,
    clock: Clock,
    vector_index: VectorIndex | None,
) -> tuple[int, int]:
    """执行一个抽取任务，返回 (新建条数, 幂等跳过条数)。"""
    repos = db.repositories
    event_id = str(job.payload.get("event_id") or "")
    event = repos.events.get(event_id)
    if event is None:
        raise ValueError(f"任务引用的原始事件不存在：{event_id}")

    drafts = extractor.extract(event)
    created = 0
    duplicates = 0
    now = clock.now()

    with db.transaction():
        for draft in drafts:
            memory = _build_memory(draft, event, clock)
            dedupe_key = f"{event.event_id}:{extractor.version}:{draft.candidate_key}"
            inserted = repos.memories.insert(
                memory,
                dedupe_key=dedupe_key,
                extraction_version=extractor.version,
                candidate_key=draft.candidate_key,
            )
            if inserted is None:
                duplicates += 1
                continue

            created += 1
            # 向量索引是派生数据，和主记录分开写：即使索引失败，
            # 记忆本身也已经落库，下次 rebuild 就能补上。
            if vector_index is not None:
                vector_index.index_memory(memory, now)

    return created, duplicates


def _enqueue_follow_up(db: Database, job: JobRecord, clock: Clock) -> None:
    """抽取完成后排一个去重任务：新记忆可能和旧记忆在说同一件事。

    先检查有没有同类待处理任务，避免每抽一次就堆一条。
    """
    repos = db.repositories
    with db.transaction():
        if not repos.jobs.has_pending("dedupe", job.scope):
            repos.jobs.enqueue(
                job_type="dedupe",
                scope=job.scope,
                payload={"reason": "extract-follow-up"},
                now=clock.now(),
                activity_id=job.activity_id,
            )


def _build_memory(draft: MemoryDraft, event: EventRecord, clock: Clock) -> MemoryItem:
    """把候选 + 来源事件组装成一条待入库的记忆。

    来源字段在这里补齐：抽取器只需要关心「内容是什么」，
    「谁说的、哪条事件、什么时候记的」由引擎统一填，避免每个抽取器各写一套。
    """
    return MemoryItem(
        memory_id=new_id("mem"),
        memory_type=draft.memory_type,
        scope=event.scope,
        body=dict(draft.body),
        source_event_ids=[event.event_id],
        source_speaker=draft.source_speaker or event.speaker,
        epistemic_status=draft.epistemic_status,
        modality=draft.modality,
        occurred_start=draft.occurred_start or event.occurred_at,
        occurred_end=draft.occurred_end,
        occurred_precision=draft.occurred_precision,
        valid_from=draft.valid_from,
        valid_to=draft.valid_to,
        importance=draft.importance,
        recorded_at=clock.now(),
    )


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


def _run_dedupe(
    db: Database,
    scope: Scope,
    clock: Clock,
    vector_index: VectorIndex | None,
    threshold: float = DEFAULT_DEDUPE_THRESHOLD,
) -> int:
    """合并同一角色下内容高度重复的记忆，返回被合并掉的条数。

    合并策略（保守优先）：
      - 只在同类型、同角色内比较，跨类型不合并（工作记忆和情景记忆本来就不是一回事）；
      - 保留「更重要、更早」的那条作为主记录，其余标记 superseded；
      - 正文里的列表字段取并集，来源事件也合并，避免丢证据；
      - 被合并的记录用 part_of 链接指向主记录，历史可追溯。
    """
    repos = db.repositories
    memories = repos.memories.list_by_scope(
        scope, limit=500, statuses=(MemoryStatus.ACTIVE,)
    )
    clusters = _cluster_duplicates(memories, threshold)
    if not clusters:
        return 0

    now = clock.now()
    merged = 0

    for group in clusters:
        canonical, duplicates = _choose_canonical(group)
        body, importance, sources = _merge_group(canonical, duplicates)

        with db.transaction():
            repos.memories.update_content(
                canonical.memory_id,
                body,
                now,
                importance=importance,
                source_event_ids=sources,
            )
            for duplicate in duplicates:
                repos.memories.update_status(duplicate.memory_id, MemoryStatus.SUPERSEDED, now)
                repos.links.add(
                    duplicate.memory_id, canonical.memory_id, LinkRelation.PART_OF, now
                )
                # 副本的内容已经被主记录吸收，它的向量没有任何独立信息，
                # 留着只会在语义通道里制造重复候选。
                if vector_index is not None:
                    vector_index.delete(duplicate.memory_id)

        merged += len(duplicates)

        # 正文变了，向量必须跟着重算，否则语义通道还拿着旧向量
        if vector_index is not None:
            refreshed = repos.memories.get(canonical.memory_id)
            if refreshed is not None:
                vector_index.index_memory(refreshed, now)

    return merged


def _cluster_duplicates(
    memories: list[MemoryItem],
    threshold: float,
) -> list[list[MemoryItem]]:
    """把互为重复的记忆聚成簇。

    做法是朴素的 O(n²) 两两比较，但加了两个廉价剪枝：
      1. 类型不同直接跳过；
      2. 长度比已经小于阈值时，Jaccard 不可能达标，直接跳过。
    本地几百条记忆完全够用；真到几万条，应该先按时间窗口或向量近邻粗筛。
    """
    prepared: list[tuple[MemoryItem, set[str]]] = []
    for memory in memories:
        shingles = text_shingles(memory.body_text)
        if len(shingles) >= 3:  # 太短的正文（比如只有两个字）不参与，误判风险太高
            prepared.append((memory, shingles))

    used: set[str] = set()
    clusters: list[list[MemoryItem]] = []

    for index, (memory, shingles) in enumerate(prepared):
        if memory.memory_id in used:
            continue

        group = [memory]
        used.add(memory.memory_id)

        for other, other_shingles in prepared[index + 1 :]:
            if other.memory_id in used or other.memory_type is not memory.memory_type:
                continue

            shorter, longer = sorted((len(shingles), len(other_shingles)))
            if longer == 0 or shorter / longer < threshold:
                continue  # Jaccard 的上界就是 min/max，先排掉明显不可能的组合

            if _jaccard(shingles, other_shingles) >= threshold:
                group.append(other)
                used.add(other.memory_id)

        if len(group) > 1:
            clusters.append(group)

    return clusters


def _jaccard(left: set[str], right: set[str]) -> float:
    """两个集合的交并比，取值 0~1。"""
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _choose_canonical(group: list[MemoryItem]) -> tuple[MemoryItem, list[MemoryItem]]:
    """挑出主记录：优先重要度高，其次更早记录的那条。"""
    ordered = sorted(group, key=lambda item: (-item.importance, item.recorded_at))
    return ordered[0], ordered[1:]


def _merge_group(
    canonical: MemoryItem,
    duplicates: list[MemoryItem],
) -> tuple[dict[str, Any], float, list[str]]:
    """合并正文、重要度和来源事件。

    标量字段保留主记录的非空值；列表字段取并集（未完成事项、参与者这些，
    合并时丢掉任何一条都可能丢信息）。
    """
    body: dict[str, Any] = dict(canonical.body)
    importance = canonical.importance
    sources = list(canonical.source_event_ids)

    for other in duplicates:
        importance = max(importance, other.importance)
        for event_id in other.source_event_ids:
            if event_id not in sources:
                sources.append(event_id)

        for key, value in other.body.items():
            if isinstance(value, list):
                existing = body.get(key) or []
                merged_values = list(existing)
                for element in value:
                    if element not in merged_values:
                        merged_values.append(element)
                body[key] = merged_values
            elif isinstance(value, str):
                if value.strip() and not str(body.get(key, "")).strip():
                    body[key] = value
            elif value is not None and body.get(key) in (None, ""):
                body[key] = value

    return body, importance, sources
