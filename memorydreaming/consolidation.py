"""异步整理：把「原始事件」变成「类型记忆」，再把重复的记忆合并。

架构文档第 6 节的流程：
    原始事件 → 类型候选 → 来源/说话者/模态校验 → 写入类型记忆 → 建立证据链接

这一版支持三类任务：

    extract  抽取：谁来抽、怎么抽由调用方决定（示例里是 RuleBasedExtractor）
    dedupe   去重：同一件事被两次对话分别记下来时，合成一条
    summarize  生成可回源的主题索引卡

propose_core（核心成长候选）尚无执行器，保留在队列中等待支持它的 worker。

幂等的关键在 dedupe_key：
    source_event_id + extractor_version + candidate_key
同一事件、同一抽取器版本、同一个候选，重跑多少次都只会写一条。
升级抽取器后版本号变了，才会产生新候选——但独立证据数仍按原始事件算，
不会因为多跑了一次抽取就「证据变多」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import tzinfo, timezone
from typing import Any, Protocol

from .clock import Clock, ensure_utc
from .errors import JobLeaseLost, SourceRevisionChanged
from .formation import MemoryFormationPolicy
from .models import (
    EpistemicStatus,
    EventRecord,
    LinkRelation,
    MemoryDraft,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    Modality,
    Scope,
    new_id,
)
from .similarity import cluster as cluster_by_similarity
from .summarization import build_summaries
from .retrieval.vector import VectorIndex
from .storage.database import Database
from .storage.repository import JobRecord
from .worker import JobHeartbeat

# 有效记忆少于这个数就不生成主题摘要：两三条记忆凑不出主题，
# 生成出来的「摘要」只是换句话重复一遍，还会挤占检索预算。
SUMMARY_TRIGGER_MEMORIES = 4

# 单值谓词：同一个主体身上，这个属性同时只能有一个当前值。
#
# 这是从 Zep/Graphiti 学来的「写时失效」思路的规则版：
# 新事实与旧事实冲突时，不删除旧的，而是**关闭它的有效期**并写新值。
# 判断「是否冲突」本来应该由模型做（Graphiti 就是这么做的），
# 没有模型时用一份单值谓词表 + 否定词检测，覆盖最常见的几类。
SINGLE_VALUED_PREDICATES = {
    "名字", "称呼", "住在", "住在哪", "生日", "年龄", "职业", "家乡", "手机号", "血型",
    "希望被称呼为",
}

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
    summarized: int = 0       # 生成/更新了多少张主题摘要卡
    skipped: int = 0          # 因没有对应执行器而保留在队列
    failed: int = 0           # 失败任务数
    recovered: int = 0        # 回收的失联任务（含达到上限的任务）
    lease_lost: int = 0       # 已失去提交权，交给新持有者或下一批恢复
    suppressed: int = 0       # 因持久遗忘标记隔离的候选
    stale_results: int = 0    # 计算期间内容代次变化，丢弃并重新排队
    errors: list[str] = field(default_factory=list)


def run_pending_jobs(
    db: Database,
    clock: Clock,
    extractor: MemoryExtractor | None = None,
    vector_index: VectorIndex | None = None,
    limit: int = 10,
    tz: tzinfo = timezone.utc,
    *,
    lease_seconds: float = 120,
    retry_base_seconds: float = 5,
    retry_max_seconds: float = 300,
    max_attempts: int = 3,
) -> ConsolidationReport:
    """取出待处理任务并逐条执行。

    每个任务的结果、后续任务和完成确认在同一事务里提交。
    抽取在写事务外执行，文件库有独立连接续租。失败按指数退避重试，
    租约失效则放弃全部写入；下一次调用会回收过期任务。

    参数:
        extractor:    抽取器。为 None 时不领取 extract 任务（不消耗重试次数），
                      这样可以只跑去重任务——例如换嵌入模型后的批量整理。
        vector_index: 传入后，新写入的记忆会顺带建立向量索引；
                      不传则跳过（检索时就没有语义通道）。
        tz:           生成摘要时用来把 UTC 时间转成用户本地日期。
    """
    repos = db.repositories
    report = ConsolidationReport()
    if (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        or not 0 < lease_seconds < float("inf")
        or isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1
    ):
        raise ValueError("任务数量、租约时长和尝试上限必须大于 0")
    if not 0 < retry_base_seconds <= retry_max_seconds < float("inf"):
        raise ValueError("重试间隔必须为有限正数，且上限不小于基础间隔")

    # 只领取当前 worker 能执行的类型。先领再释放会反复卡在最老的 extract，
    # 导致后面的 dedupe/summarize 永远没有机会运行。
    supported_types = ["dedupe", "summarize"]
    if extractor is not None:
        supported_types.append("extract")
    placeholders = ",".join("?" for _ in supported_types)
    with db.transaction():
        report.recovered = repos.jobs.recover_expired(clock.now())
        report.skipped = int(db.connection.execute(
            f"SELECT COUNT(*) FROM consolidation_jobs WHERE status = 'pending' "
            f"AND job_type NOT IN ({placeholders})", supported_types,
        ).fetchone()[0])
        job_ids = repos.jobs.ready_ids(limit, clock.now(), supported_types)

    for job_id in job_ids:
        with db.transaction():
            claimed = repos.jobs.claim(1, clock.now(), job_ids=[job_id], lease_seconds=lease_seconds)
        if not claimed:
            continue  # 其他 worker 已领取本批候选
        job = claimed[0]
        report.claimed += 1
        try:
            created = duplicates = merged = summarized = suppressed = 0
            # 抽取可调用外部模型，不能在等待网络时持有 SQLite 写锁。
            with JobHeartbeat(db, job, clock, lease_seconds):
                epoch = repos.forgetting.epoch(job.scope)
                event = None
                drafts: list[MemoryDraft] = []
                if job.job_type == "extract":
                    assert extractor is not None
                    event_id = str(job.payload.get("event_id") or "")
                    event = repos.events.get(event_id)
                    if event is None or event.scope != job.scope:
                        raise ValueError(f"任务引用的原始事件不存在或作用域不匹配：{event_id}")
                    drafts = extractor.extract(event)

                with db.transaction():
                    repos.jobs.assert_owned(job.job_id, job.claim_token, clock.now())
                    if repos.forgetting.epoch(job.scope) != epoch:
                        raise SourceRevisionChanged("抽取期间发生遗忘，旧候选已丢弃")
                    if job.job_type == "extract":
                        created, duplicates, suppressed = _run_extract(
                            db, event, drafts, extractor.version, clock, vector_index,
                        )
                        _enqueue_follow_up(db, job, clock, max_attempts)
                    elif job.job_type == "dedupe":
                        merged = _run_dedupe(db, job.scope, clock, vector_index)
                        _enqueue_summarize(db, job, clock, max_attempts)
                    elif job.job_type == "summarize":
                        summarized = build_summaries(db, job.scope, clock, tz)
                    if repos.forgetting.epoch(job.scope) != epoch:
                        raise SourceRevisionChanged("提交前内容代次变化")
                    repos.jobs.complete(job.job_id, job.claim_token, clock.now())
            # 只有事务真正提交后才把结果计入报告。
            report.created += created
            report.duplicates += duplicates
            report.merged += merged
            report.summarized += summarized
            report.suppressed += suppressed
        except SourceRevisionChanged as exc:
            report.stale_results += 1
            report.errors.append(f"{job.job_id}: {exc}")
            try:
                with db.transaction():
                    repos.jobs.release(job.job_id, job.claim_token, clock.now())
            except JobLeaseLost:
                report.lease_lost += 1
        except JobLeaseLost as exc:
            report.lease_lost += 1
            report.errors.append(f"{job.job_id}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不应中断整批
            report.errors.append(f"{job.job_id}: {exc}")
            try:
                with db.transaction():
                    repos.jobs.fail(
                        job.job_id, job.claim_token, str(exc), clock.now(),
                        retry_base_seconds=retry_base_seconds, retry_max_seconds=retry_max_seconds,
                    )
                report.failed += 1
            except JobLeaseLost:
                report.lease_lost += 1

    return report


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


def _run_extract(
    db: Database,
    event: EventRecord,
    drafts: list[MemoryDraft],
    extractor_version: str,
    clock: Clock,
    vector_index: VectorIndex | None,
) -> tuple[int, int, int]:
    """写入合法候选，返回 (新建条数, 幂等跳过条数, 抑制条数)。"""
    repos = db.repositories
    created = 0
    duplicates = 0
    suppressed = 0
    now = clock.now()

    policy = MemoryFormationPolicy()
    # 先统一校验整批候选，再进入事务。任何一条格式错误都会让任务按
    # 原有退避策略重试，避免一批候选部分写入后留下不可解释状态。
    validated = [policy.validate(event, draft) for draft in drafts]

    with db.transaction():
        memories = [_build_memory(draft, event, clock) for draft in validated]
        # 相同断言的新来源也须记录，防止以后从这条消息换格式抽取而复活。
        # 先处理整批来源，使区间不明时的保守隔离不受候选顺序影响。
        for memory in memories:
            if repos.forgetting.assertion_tombstone(memory) is not None:
                repos.forgetting.suppress(memory, now)
        for draft, memory in zip(validated, memories):
            if repos.forgetting.is_suppressed(memory):
                suppressed += 1
                continue

            # 写时冲突消解：先把「这条新事实会让哪条旧事实过期」找出来。
            # 注意顺序——必须先算冲突、再插入，这样新记录能带上 supersedes_id。
            conflicts = _find_conflicts(repos, memory)
            if conflicts:
                memory.supersedes_id = conflicts[0].memory_id

            dedupe_key = f"{event.event_id}:{extractor_version}:{draft.candidate_key}"
            inserted = repos.memories.insert(
                memory,
                dedupe_key=dedupe_key,
                extraction_version=extractor_version,
                candidate_key=draft.candidate_key,
            )
            if inserted is None:
                duplicates += 1
                continue

            created += 1

            # 旧事实不被删除，只关闭有效期：历史仍然可查（Zep 的双时间模型）
            for old in conflicts:
                repos.memories.close_validity(
                    old.memory_id,
                    memory.valid_from or memory.occurred_start or memory.recorded_at,
                    now,
                )
                repos.links.add(memory.memory_id, old.memory_id, LinkRelation.SUPERSEDES, now)
            # 当前实现将索引和主记录一起提交；索引失败时本任务回滚并重试。
            if vector_index is not None:
                vector_index.index_memory(memory, now)

    return created, duplicates, suppressed


def _find_conflicts(repos, candidate: MemoryItem) -> list[MemoryItem]:
    """找出会被这条新事实「顶掉」的旧记忆。

    只处理语义记忆，因为只有它有明确的三元组结构。两条冲突规则：

    1. 单值谓词 + 同一主体 + 不同客体 → 冲突。
       「我住在北京」之后说「我住在上海」，北京那条必须失效，
       否则检索会两句话都拿出来。
    2. 同一客体的否定翻转 → 冲突。
       「我喜欢猫」之后说「我不喜欢猫」，这是直接推翻，不是并列偏好。

    其余情况一律不冲突：「喜欢猫」和「喜欢狗」可以同时成立，
    这正是 Mem0 现行实现强调的「历史是时间线，不是一份可覆盖的档案」。
    """
    if (
        candidate.memory_type is not MemoryType.SEMANTIC
        or candidate.modality is not Modality.OCCURRED
        or candidate.epistemic_status is EpistemicStatus.INFERRED
    ):
        return []

    body = candidate.body
    subject = str(body.get("subject", "")).strip()
    predicate = str(body.get("predicate", "")).strip()
    obj = str(body.get("obj", "")).strip()
    if not subject or not predicate:
        return []

    existing = repos.memories.list_by_scope(
        candidate.scope,
        memory_types=[MemoryType.SEMANTIC],
        limit=-1,
        statuses=(MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED),
    )

    comparable: list[MemoryItem] = []
    for old in existing:
        if (
            old.scope != candidate.scope
            or old.source_speaker != candidate.source_speaker
            or old.modality is not Modality.OCCURRED
            or old.epistemic_status is EpistemicStatus.INFERRED
            or (
                old.status is MemoryStatus.SUPERSEDED
                and repos.links.has_relation(old.memory_id, LinkRelation.PART_OF)
            )
        ):
            continue
        old_body = old.body
        if str(old_body.get("subject", "")).strip() != subject:
            continue

        old_predicate = str(old_body.get("predicate", "")).strip()
        old_obj = str(old_body.get("obj", "")).strip()

        if old_predicate == predicate:
            if old_obj == obj:
                continue  # 一模一样，交给去重处理
            if predicate in SINGLE_VALUED_PREDICATES:
                comparable.append(old)
        elif _is_negation(predicate, old_predicate) and old_obj == obj:
            comparable.append(old)

    # 按事实生效时间而非处理顺序写时间线。补录旧对话时，旧候选可以形成
    # 有界历史，但不能关闭更晚才生效的当前事实，更不能生成倒置有效期。
    start = ensure_utc(candidate.valid_from or candidate.occurred_start or candidate.recorded_at)
    conflicts: list[MemoryItem] = []
    for old in comparable:
        old_start = ensure_utc(old.valid_from or old.occurred_start or old.recorded_at)
        if old_start > start:
            if candidate.valid_to is None or old_start < ensure_utc(candidate.valid_to):
                candidate.valid_to = old_start
                candidate.status = MemoryStatus.SUPERSEDED
            continue
        if old.valid_to is not None and ensure_utc(old.valid_to) <= start:
            continue
        conflicts.append(old)

    return conflicts


def _is_negation(left: str, right: str) -> bool:
    """判断两个谓词是不是「肯定 / 否定」的关系。"""
    return left == f"不{right}" or right == f"不{left}"


def _enqueue_follow_up(db: Database, job: JobRecord, clock: Clock, max_attempts: int) -> None:
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
                max_attempts=max_attempts,
            )


def _enqueue_summarize(db: Database, job: JobRecord, clock: Clock, max_attempts: int) -> None:
    """去重之后排一个摘要任务。

    摘要依赖去重后的结果：如果先摘要、后去重，摘要里会留下已经被合并掉的来源。
    顺序是 extract → dedupe → summarize，一条链走完再结束。
    """
    repos = db.repositories
    if repos.memories.count_active(job.scope) < SUMMARY_TRIGGER_MEMORIES:
        return

    with db.transaction():
        if not repos.jobs.has_pending("summarize", job.scope):
            repos.jobs.enqueue(
                job_type="summarize",
                scope=job.scope,
                payload={"reason": "dedupe-follow-up"},
                now=clock.now(),
                activity_id=job.activity_id,
                max_attempts=max_attempts,
            )


def _build_memory(draft: MemoryDraft, event: EventRecord, clock: Clock) -> MemoryItem:
    """把候选 + 来源事件组装成一条待入库的记忆。

    来源字段在这里补齐：抽取器只需要关心「内容是什么」，
    「谁说的、哪条事件、什么时候记的」由引擎统一填，避免每个抽取器各写一套。
    """
    spans: dict[str, list[list[int]]] = {}
    if draft.source_span is not None:
        start, end = draft.source_span
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
                or not isinstance(end, int) or not 0 <= start < end <= len(event.content)):
            raise ValueError("候选来源片段不是有效的 Unicode 码点区间")
        spans[event.event_id] = [[start, end]]
    return MemoryItem(
        memory_id=new_id("mem"),
        memory_type=draft.memory_type,
        scope=event.scope,
        body=dict(draft.body),
        source_event_ids=[event.event_id],
        source_spans=spans,
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
        scope, limit=-1, statuses=(MemoryStatus.ACTIVE,)
    )
    # 相似措辞不代表相同事实。先以完整作用域、模态、来源及有效期隔离，
    # 防止把不同活动的工作状态、意愿和真实经历合为同一条。
    partitions: dict[tuple[Any, ...], list[MemoryItem]] = {}
    for memory in memories:
        if repos.forgetting.is_suppressed(memory):
            continue
        key: tuple[Any, ...] = (
            memory.scope, memory.memory_type, memory.modality,
            memory.source_speaker, memory.epistemic_status,
            memory.valid_from, memory.valid_to,
        )
        if memory.memory_type is MemoryType.SEMANTIC:
            # 一个否定字或名字不同也可能有很高的字面相似度，语义三元组
            # 必须相同才允许继续做近重复合并。
            key += tuple(str(memory.body.get(field, "")).strip()
                         for field in ("subject", "predicate", "obj"))
        partitions.setdefault(key, []).append(memory)
    clusters = [
        group for partition in partitions.values()
        for group in cluster_by_similarity(partition, threshold, respect_type=True)
    ]
    if not clusters:
        return 0

    now = clock.now()
    merged = 0

    for group in clusters:
        canonical, duplicates = _choose_canonical(group)
        body, importance, sources = _merge_group(canonical, duplicates)
        spans: dict[str, list[list[int]]] = {}
        for item in group:
            for event_id, intervals in item.source_spans.items():
                target = spans.setdefault(event_id, [])
                for interval in intervals:
                    if interval not in target:
                        target.append(interval)

        with db.transaction():
            repos.memories.update_content(
                canonical.memory_id,
                body,
                now,
                importance=importance,
                source_event_ids=sources,
                source_spans=spans,
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
