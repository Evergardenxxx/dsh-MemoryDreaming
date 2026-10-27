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

from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
from typing import Any

from .clock import Clock, SystemClock, load_timezone
from .config import Settings
from .consolidation import ConsolidationReport, MemoryExtractor, run_pending_jobs
from .errors import InvalidCorrection, MemorySuppressed, ScopeNotFound, SourceRevisionChanged
from .models import (
    Activity,
    ActivityStatus,
    BootstrapResult,
    CoreKind,
    CoreSnapshot,
    Correction,
    CorrectionResult,
    EventRecord,
    EvidenceRef,
    ForgetResult,
    ForgetSelector,
    LinkRelation,
    MemoryItem,
    MemoryStatus,
    ObserveResult,
    Query,
    RelearningResult,
    ReadFilter,
    RetrievalResult,
    RetrievedItem,
    Scope,
    SessionHandoff,
    Turn,
    new_id,
)
from .retrieval.embedding import Embedder, embedder_from_settings
from .retrieval.pipeline import RetrievalPipeline
from .retrieval.vector import PreparedVectors, VectorIndex
from .summarization import build_summaries
from .storage.database import Database
from .storage.repository import scope_sql, _scope_key
from .portability import import_snapshot, load_snapshot, preflight_snapshot_file, write_snapshot
from .worker import ConsolidationWorker


@dataclass(frozen=True)
class _PreparedCorrection:
    """可信运行时内部的单次计算计划，不是模型参数或审批凭证。"""

    owner: object
    generation: int
    epoch: int
    old: MemoryItem
    replacement: MemoryItem
    vectors: PreparedVectors
    digest: str


def _correction_digest(old: MemoryItem, replacement: MemoryItem) -> str:
    material = json.dumps([asdict(old), asdict(replacement)], ensure_ascii=False,
                          sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class MemoryRuntime:
    """长期记忆引擎的门面。一个进程通常只创建一个实例。"""

    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        db_path: str | None = None,
        embedder: Embedder | None = None,
        consolidation_extractor: MemoryExtractor | None = None,
    ) -> None:
        self._settings = settings or Settings.from_env()
        if db_path:
            self._settings = self._settings.with_db(db_path)

        # 时钟可替换是「可测试」的关键：测试里用 FixedClock 就能模拟时间流逝
        self._clock = clock or SystemClock()
        self._tz = load_timezone(self._settings.timezone)
        embedder = embedder if embedder is not None else embedder_from_settings(self._settings)
        self._db = Database.open(self._settings.db_path)
        self._repos = self._db.repositories
        self._consolidator: ConsolidationWorker | None = None

        # 嵌入模型同样可替换。默认的哈希嵌入是零依赖占位实现，
        # 换成真实模型时只需实现 Embedder 协议并从构造函数传进来。
        self._vectors = VectorIndex(self._db, embedder)
        self._pipeline = RetrievalPipeline(
            self._db, self._settings, self._clock, self._tz, vector_index=self._vectors
        )
        if (
            self._settings.auto_consolidation
            and consolidation_extractor is not None
            and self._db.path is not None
        ):
            self._consolidator = ConsolidationWorker(
                self._db.path,
                self._clock,
                consolidation_extractor,
                embedder=embedder,
                timezone=self._settings.timezone,
                interval_seconds=self._settings.consolidation_interval_seconds,
                batch_size=self._settings.consolidation_batch_size,
                lease_seconds=self._settings.job_lease_seconds,
                retry_base_seconds=self._settings.job_retry_base_seconds,
                retry_max_seconds=self._settings.job_retry_max_seconds,
                max_attempts=self._settings.job_max_attempts,
            )
            self._consolidator.start()

    # -- 生命周期 ---------------------------------------------------------

    def __enter__(self) -> "MemoryRuntime":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._consolidator is not None:
            self._consolidator.stop()
        self._db.close()

    @property
    def consolidator(self) -> ConsolidationWorker | None:
        """后台整理器状态；未启用时为 None。"""
        return self._consolidator

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def db(self) -> Database:
        return self._db

    @property
    def vectors(self) -> VectorIndex:
        """向量索引。换嵌入模型后调 rebuild_vectors() 重建即可。"""
        return self._vectors

    def retrieval_cache_signature(self) -> dict[str, Any]:
        """持久候选缓存的算法/配置边界，不包含数据库路径或凭据。"""
        settings = asdict(self._settings)
        settings.pop("db_path")
        return {"algorithm": "hybrid-retrieval-v2", "settings": settings,
                "embedding": self._vectors.cache_signature}

    # -- 1. 启动恢复 -------------------------------------------------------

    def bootstrap(
        self,
        user_id: str,
        character_id: str,
        activity_id: str | None = None,
        *,
        relationship_id: str | None = None,
        domain: str = "real",
    ) -> BootstrapResult:
        """恢复「我是谁、我们是什么关系、手上还有什么没做完」。

        核心缺失时**不抛异常**：新角色本来就没有核心。
        这时返回 recovery_warning，让宿主如实告诉用户恢复失败，
        而不是拿一个临时身份顶上去——后者会让角色在不知不觉中「换了个人」。
        """
        scope = Scope(
            user_id=user_id, character_id=character_id,
            relationship_id=relationship_id, domain=domain,
        )
        identity = self._repos.cores.latest(scope, CoreKind.IDENTITY)
        relationship = self._repos.cores.latest(scope, CoreKind.RELATIONSHIP)

        activities = self._repos.activities.list_for_scope(
            scope, statuses=(ActivityStatus.ACTIVE, ActivityStatus.PAUSED)
        )

        selected: Activity | None = None
        if activity_id:
            selected = self._repos.activities.get(activity_id)
            if selected is not None and not self._scope_matches(selected.scope, scope):
                # 显式 ID 仍须检查完整可见范围，不泄露外部活动。
                selected = None
        elif activities:
            selected = activities[0]

        highlights = self._repos.memories.list_important(
            scope, self._settings.bootstrap_highlight_limit
        )
        highlights = [item for item in highlights if not self._repos.forgetting.is_suppressed(item)]
        summaries = self._repos.summaries.list_active(scope, limit=5)

        warning = None
        if identity is None or not identity.payload:
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
        *, read_filter: ReadFilter | None = None,
        knowledge_at: datetime | None = None,
        fact_at: datetime | None = None,
        detail: str = "auto",
    ) -> RetrievalResult:
        """检索证据。返回结果自带检索轨迹与缺口说明。

        as_of 是「时间旅行」：给一个过去的时刻，只返回那一刻已经存在、
        并且当时有效的记忆。复盘「我们当时是怎么以为的」时用得上。
        """
        if as_of is not None and (knowledge_at is not None or fact_at is not None):
            raise ValueError("as_of 不能与 knowledge_at/fact_at 同时传入")
        query = Query(
            text=query_text,
            scope=scope,
            time_range=time_range,
            precision=precision,
            max_items=max_items,
            token_budget=token_budget,
            as_of=as_of,
            knowledge_at=knowledge_at,
            fact_at=fact_at,
            read_filter=read_filter,
            detail=detail,
        )
        return self._pipeline.run(query)

    def memory_history(self, memory_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """读取一条记忆的系统时间修订日志（不注入模型上下文）。"""
        return self._repos.memories.history(memory_id, limit=limit)

    def memory_at(
        self,
        memory_id: str,
        knowledge_at: datetime,
        *,
        fact_at: datetime | None = None,
    ) -> MemoryItem | None:
        """按双时间轴恢复单条记忆，供审计/回放工具使用。"""
        return self._repos.memories.at(memory_id, knowledge_at, fact_at=fact_at)

    def related_memories(
        self,
        memory_id: str,
        scope: Scope,
        *,
        depth: int = 2,
        limit: int = 50,
        relation: LinkRelation | str | None = None,
    ) -> list[dict[str, Any]]:
        """沿记忆关系图做有界双向遍历，返回可校验的邻居与跳数。

        图遍历只补充检索证据，仍受作用域、遗忘账本和记忆状态过滤；它不会
        自动提升证据等级，也不会把关系边本身当作事实正文注入模型。
        """
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 4:
            raise ValueError("depth 必须为 1..4")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit 必须为 1..200")
        root = self._repos.memories.get(memory_id)
        if root is None or not self._scope_matches(root.scope, scope):
            raise ScopeNotFound(f"找不到作用域内记忆 {memory_id}")
        if relation is not None and not isinstance(relation, LinkRelation):
            try:
                relation = LinkRelation(str(relation))
            except ValueError as exc:
                raise ValueError("relation 不是支持的记忆关系") from exc

        queue: deque[tuple[str, int]] = deque([(memory_id, 0)])
        visited = {memory_id}
        result: list[dict[str, Any]] = []
        while queue and len(result) < limit:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            edges = self._repos.links.neighbors(current, relation=relation)
            edges += self._repos.links.incoming(current, relation=relation)
            for target, edge_relation in edges:
                if target in visited:
                    continue
                visited.add(target)
                item = self._repos.memories.get(target)
                if item is None or not self._scope_matches(item.scope, scope):
                    continue
                if item.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED):
                    continue
                if self._repos.forgetting.is_suppressed(item):
                    continue
                next_depth = current_depth + 1
                result.append({"memory": item, "relation": edge_relation, "depth": next_depth})
                if len(result) >= limit:
                    break
                queue.append((target, next_depth))
        return result

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
        if turn.activity_id and scope.activity_id and turn.activity_id != scope.activity_id:
            raise ValueError("消息与作用域指定了不同的活动")
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
            if activity_id:
                activity = self._repos.activities.get(activity_id)
                if activity is None or not self._scope_matches(activity.scope, event_scope):
                    raise ScopeNotFound("找不到当前作用域中的活动")
            stored, duplicate = self._repos.events.insert(event)

            job_id: str | None = None
            if not duplicate:
                self._repos.journal.append(
                    "event.recorded",
                    scope=event_scope,
                    entity_id=stored.event_id,
                    payload={
                        "conversation_id": turn.conversation_id,
                        "speaker": turn.speaker,
                        "occurred_at": turn.occurred_at.isoformat(),
                        "content_digest": self._repos.journal.payload_digest({"content": turn.content}),
                    },
                    now=now,
                    idempotency_key=f"event:{stored.event_id}",
                )
                if activity_id:
                    self._repos.activities.touch_last_event(activity_id, event.occurred_at)
                job_id = self._repos.jobs.enqueue(
                    job_type="extract",
                    scope=event_scope,
                    payload={"event_id": stored.event_id, "conversation_id": turn.conversation_id},
                    now=now,
                    activity_id=activity_id,
                    max_attempts=self.settings.job_max_attempts,
                )
                self._repos.journal.append(
                    "consolidation.enqueued",
                    scope=event_scope,
                    entity_id=job_id,
                    payload={"job_type": "extract", "event_id": stored.event_id},
                    now=now,
                    idempotency_key=f"job:{job_id}:enqueued",
                )

        version = None
        if activity_id:
            activity = self._repos.activities.get(activity_id)
            version = activity.checkpoint_version if activity else None

        if self._consolidator is not None and job_id is not None:
            self._consolidator.wake()

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
            self._repos.journal.append(
                "activity.created",
                scope=scope,
                entity_id=activity.activity_id,
                payload={"title": title, "goal_digest": self._repos.journal.payload_digest({"goal": goal})},
                now=now,
                idempotency_key=f"activity:{activity.activity_id}:created",
            )
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
            new_version = self._repos.activities.update_checkpoint(
                activity_id=activity_id,
                expected_version=expected_version,
                now=self._clock.now(),
                goal=state.get("goal"),
                open_loops=state.get("open_loops"),
                active_entities=state.get("active_entities"),
                checkpoint=state.get("checkpoint"),
                status=status,
            )
            activity = self._repos.activities.get(activity_id)
            if activity is not None:
                self._repos.journal.append(
                    "activity.checkpoint",
                    scope=activity.scope,
                    entity_id=activity_id,
                    payload={"expected_version": expected_version,
                             "new_version": new_version,
                             "fields": sorted(state)},
                    now=self._clock.now(),
                    idempotency_key=f"checkpoint:{activity_id}:{new_version}",
                )
            return new_version

    def save_handoff(
        self,
        scope: Scope,
        conversation_id: str,
        payload: dict[str, Any],
        *,
        activity_id: str | None = None,
        source_event_id: str | None = None,
        activity_version: int | None = None,
        idempotency_key: str,
    ) -> tuple[SessionHandoff, bool]:
        """持久化上下文压缩前的短交接。

        交接本身不产生记忆事实；它只是下一窗口恢复工作目标和开放循环
        的索引卡。调用方应先用 ``checkpoint`` 做版本化更新，再调用本方法。
        """
        if not isinstance(payload, dict):
            raise ValueError("handoff payload 必须为对象")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id 必须为非空字符串")
        if activity_id is not None:
            activity = self._repos.activities.get(activity_id)
            expected_scope = Scope(
                scope.user_id, scope.character_id, scope.relationship_id,
                activity_id, scope.domain,
            )
            if activity is None or not self._scope_matches(activity.scope, expected_scope):
                raise ScopeNotFound("找不到当前作用域中的活动")
        handoff_scope = Scope(
            scope.user_id, scope.character_id, scope.relationship_id,
            activity_id, scope.domain,
        )
        now = self._clock.now()
        handoff = SessionHandoff(
            handoff_id=new_id("handoff"),
            scope=handoff_scope,
            conversation_id=conversation_id,
            activity_id=activity_id,
            payload=payload,
            idempotency_key=idempotency_key,
            source_event_id=source_event_id,
            activity_version=activity_version,
            created_at=now,
            updated_at=now,
        )
        stored, duplicate = self._repos.handoffs.upsert(handoff)
        if not duplicate:
            self._repos.journal.append(
                "handoff.created",
                scope=handoff_scope,
                entity_id=stored.handoff_id,
                payload={
                    "conversation_id": conversation_id,
                    "activity_version": activity_version,
                    "payload_digest": self._repos.journal.payload_digest(payload),
                },
                now=now,
                idempotency_key=f"handoff:{stored.handoff_id}:created",
            )
        return stored, duplicate

    def checkpoint_and_save_handoff(
        self,
        scope: Scope,
        activity_id: str,
        state: dict[str, Any],
        expected_version: int,
        conversation_id: str,
        *,
        handoff_extra: dict[str, Any] | None = None,
        source_event_id: str | None = None,
        idempotency_key: str,
    ) -> tuple[SessionHandoff, bool]:
        """原子完成活动检查点和 handoff，避免崩溃留下半个压缩状态。"""
        with self._db.transaction():
            existing = self.get_handoff(
                Scope(scope.user_id, scope.character_id, scope.relationship_id, activity_id, scope.domain),
                idempotency_key,
            )
            if existing is not None:
                return existing, True
            self.checkpoint(activity_id, state, expected_version)
            activity = self._repos.activities.get(activity_id)
            if activity is None:
                raise ScopeNotFound("找不到当前作用域中的活动")
            payload = {
                "title": activity.title,
                "goal": activity.goal,
                "open_loops": activity.open_loops,
                "active_entities": activity.active_entities,
                "checkpoint": activity.checkpoint,
                "status": activity.status.value,
                **(handoff_extra or {}),
            }
            return self.save_handoff(
                scope, conversation_id, payload, activity_id=activity_id,
                source_event_id=source_event_id, activity_version=activity.checkpoint_version,
                idempotency_key=idempotency_key,
            )

    def latest_handoff(self, scope: Scope, *, activity_id: str | None = None) -> SessionHandoff | None:
        """读取同一绑定范围的最新交接，不展开完整历史。"""
        return self._repos.handoffs.latest(scope, activity_id=activity_id)

    def get_handoff(self, scope: Scope, idempotency_key: str) -> SessionHandoff | None:
        """按宿主幂等键读取交接，供重试在修改活动前短路。"""
        return self._repos.handoffs.get_by_key(scope, idempotency_key)

    def memory_health(self, scope: Scope) -> dict[str, Any]:
        """返回供宿主监控使用的记忆健康摘要。

        健康度只描述覆盖和派生物状态，不把访问次数或模型自评置信度
        解释成事实概率。结果不包含正文，因此可以安全地写入运维日志。
        """
        conn = self._db.connection
        memory_where, memory_params = scope_sql(scope, "m")
        rows = conn.execute(
            "SELECT memory_type, status, COUNT(*) AS n FROM memories m "
            f"WHERE {memory_where} GROUP BY memory_type, status", memory_params,
        ).fetchall()
        memories: dict[str, dict[str, int]] = {}
        for row in rows:
            memories.setdefault(row["memory_type"], {})[row["status"]] = int(row["n"])
        inferred_where, inferred_params = scope_sql(scope, "m")
        inferred = conn.execute(
            "SELECT COUNT(*) AS n FROM memories m WHERE " + inferred_where
            + " AND m.status='active' AND m.epistemic_status='inferred' "
            "AND (m.source_event_ids_json='[]' OR m.source_event_ids_json='')",
            inferred_params,
        ).fetchone()["n"]

        summary_where, summary_params = scope_sql(scope, "s")
        summary_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM summaries s WHERE " + summary_where
            + " GROUP BY status", summary_params,
        ).fetchall()
        summaries = {row["status"]: int(row["n"]) for row in summary_rows}
        retrieval_cache = self._repos.retrieval_cache.stats(scope, self._clock.now())

        job_where, job_params = scope_sql(scope, "j")
        job_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM consolidation_jobs j WHERE " + job_where
            + " GROUP BY status", job_params,
        ).fetchall()
        jobs = {row["status"]: int(row["n"]) for row in job_rows}

        activities = self._repos.activities.list_for_scope(
            scope, statuses=(ActivityStatus.ACTIVE, ActivityStatus.PAUSED), limit=1000,
        )
        open_loops = sum(len(activity.open_loops) for activity in activities)
        gaps: list[dict[str, str]] = []
        if summaries.get("stale", 0):
            gaps.append({"code": "STALE_SUMMARIES", "message": "部分摘要等待来源重建"})
        if jobs.get("failed", 0):
            gaps.append({"code": "FAILED_JOBS", "message": "部分后台整理任务达到重试上限"})
        if inferred:
            gaps.append({"code": "INFERRED_WITHOUT_SOURCE", "message": "存在没有事件来源的推断候选"})
        journal = self._repos.journal.verify()
        if not journal["valid"]:
            gaps.append({"code": "JOURNAL_INTEGRITY", "message": "持久审计链完整性校验失败"})
        return {
            "status": "ok",
            "scope": {"user_id": scope.user_id, "character_id": scope.character_id,
                      "relationship_id": scope.relationship_id, "domain": scope.domain},
            "memories": memories,
            "inferred_without_source": int(inferred),
            "summaries": summaries,
            "retrieval_cache": retrieval_cache,
            "jobs": jobs,
            "working": {"active_activities": len(activities), "open_loops": open_loops},
            "journal": journal,
            "gaps": gaps,
        }

    def record_memory_health(self, scope: Scope) -> dict[str, Any]:
        """持久化一份不含记忆正文的健康快照，供趋势/告警系统读取。"""
        captured_at = self._clock.now()
        payload = self.memory_health(scope)
        snapshot_id = new_id("health")
        with self._db.transaction():
            self._db.connection.execute(
                """INSERT INTO health_snapshots (
                    snapshot_id, scope_key, user_id, character_id, relationship_id,
                    activity_id, domain, payload_json, captured_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, _scope_key(scope), scope.user_id, scope.character_id,
                 scope.relationship_id, scope.activity_id, scope.domain,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 captured_at.isoformat()),
            )
        return {"snapshot_id": snapshot_id, "captured_at": captured_at.isoformat(), "health": payload}

    def memory_health_trend(self, scope: Scope, *, limit: int = 30) -> list[dict[str, Any]]:
        """读取最近健康快照；只返回计数、缺口和审计状态。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 365:
            raise ValueError("limit 必须为 1..365")
        visible, params = scope_sql(scope, "h")
        rows = self._db.connection.execute(
            "SELECT snapshot_id, captured_at, payload_json FROM health_snapshots h WHERE "
            + visible + " ORDER BY captured_at DESC LIMIT ?", [*params, limit],
        ).fetchall()
        return [{"snapshot_id": row["snapshot_id"], "captured_at": row["captured_at"],
                 "health": json.loads(row["payload_json"])} for row in rows]

    def rebuild_entities(self, scope: Scope) -> int:
        """从当前有效记忆重建作用域实体索引，返回实体提及数。"""
        with self._db.transaction():
            return self._repos.entities.rebuild(scope, self._clock.now())

    def search_entities(self, query: str, scope: Scope, *, limit: int = 20) -> list[dict[str, Any]]:
        """按名称反查实体及其记忆 ID；结果仍需经正常引用读取校验。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须为非空字符串")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        return self._repos.entities.search(scope, query, limit=limit)

    def journal_entries(self, scope: Scope | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """读取审计链元数据；默认只返回最近记录，不包含对话正文。"""
        return self._repos.journal.list(scope=scope, limit=limit)

    def verify_journal(self) -> dict[str, Any]:
        """校验从 genesis 到当前 head 的全局哈希链。"""
        return self._repos.journal.verify()

    def export_snapshot(self, path: str, scope: Scope | None = None) -> dict[str, Any]:
        """导出可移植 JSON 快照；不会把快照内容注入模型上下文。"""
        return write_snapshot(self._db, path, scope=scope)

    def preflight_snapshot(self, path: str) -> dict[str, Any]:
        """只读检查快照能否导入当前库，失败时不修改任何数据。"""
        return preflight_snapshot_file(self._db, path)

    def import_snapshot(self, path: str, *, allow_partial: bool = True) -> dict[str, Any]:
        """校验并原子导入快照；失败时整个事务回滚。"""
        return import_snapshot(self._db, load_snapshot(path), allow_partial=allow_partial)

    # -- 5. 核心档案 -------------------------------------------------------

    def propose_core_update(
        self,
        scope: Scope,
        kind: CoreKind | str,
        patch: dict[str, Any],
        change_reason: str,
        expected_version: int | None = None,
        evidence_ids: list[str] | None = None,
        field_evidence_ids: dict[str, list[str]] | None = None,
    ) -> CoreSnapshot:
        """兼容旧版的可信本地直写入口；不会创建待审建议，勿注册为模型工具。"""
        return self.apply_core_update(scope, kind, patch, change_reason, expected_version,
                                      evidence_ids, field_evidence_ids)

    def apply_core_update(
        self, scope: Scope, kind: CoreKind | str, patch: dict[str, Any], change_reason: str,
        expected_version: int | None = None, evidence_ids: list[str] | None = None,
        field_evidence_ids: dict[str, list[str]] | None = None,
        *, effective_from: datetime | None = None,
    ) -> CoreSnapshot:
        """可信本地宿主直接应用一次核心变更；模型建议应走 CoreProposals 审批。

        这里是「补丁合并」语义：patch 里出现的键覆盖旧值，没出现的保持不变。
        版本号由后端计算，调用方只需声明「我是基于哪个版本改的」。

        架构文档的要求：核心变更要可解释。所以 change_reason 是必填项，
        evidence_ids 建议填上支撑这次变更的原始事件。
        """
        core_kind = kind if isinstance(kind, CoreKind) else CoreKind(kind)
        now = self._clock.now()
        from .clock import ensure_utc
        if effective_from is not None and (not isinstance(effective_from, datetime) or ensure_utc(effective_from) > ensure_utc(now)):
            raise ValueError("effective_from 必须为不晚于当前记录时间的 datetime")
        with self._db.transaction():
            current = self._repos.cores.latest(scope, core_kind)
            merged = dict(current.payload) if current else {}
            merged.update(patch)
            fields = dict(current.field_evidence_ids) if current else {}
            for name in patch:
                fields[name] = list((field_evidence_ids or {}).get(name, evidence_ids or []))
            refs = sorted({ref for values in fields.values() for ref in values})
            result = self._repos.cores.append(
                scope=scope,
                kind=core_kind,
                payload=merged,
                change_reason=change_reason,
                effective_from=effective_from or now,
                now=now,
                evidence_ids=refs,
                expected_version=expected_version,
                field_evidence_ids=fields,
                changed_fields=list(patch),
            )
            # 追记过去的字段变更不能把更晚已经生效的字段覆盖成旧状态。
            from .temporal import core_at
            projected = core_at(self, scope, core_kind, now, now)
            if projected is not None and (projected.payload != result.payload or projected.field_evidence_ids != result.field_evidence_ids):
                self._db.connection.execute(
                    "UPDATE core_versions SET payload_json=?,field_evidence_json=?,evidence_ids_json=? "
                    "WHERE scope_key=? AND kind=? AND core_version=?",
                    (json.dumps(projected.payload, ensure_ascii=False), json.dumps(projected.field_evidence_ids, ensure_ascii=False),
                     json.dumps(projected.evidence_ids), self._core_key(scope, core_kind), core_kind.value, result.core_version),
                )
                result = replace(result, payload=projected.payload, field_evidence_ids=projected.field_evidence_ids,
                                 evidence_ids=projected.evidence_ids)
            self.refresh_evidence()
            return result

    @staticmethod
    def _core_key(scope, kind):
        from .assertions import core_scope_key
        return core_scope_key(scope, kind)

    def core_at(self, scope: Scope, kind: CoreKind | str, *, knowledge_at: datetime,
                fact_at: datetime) -> CoreSnapshot | None:
        """按系统获知时间和字段生效时间回放，当前遗忘屏蔽继续生效。"""
        from .temporal import core_at
        with self._db.transaction(write=False):
            return core_at(self, scope, kind, knowledge_at, fact_at)

    def evidence_ref(self, kind: str, target: str, scope: Scope) -> EvidenceRef:
        """可信宿主获取内容版本锁定的依据；不属于模型写工具。"""
        from .evidence import evidence_reference
        return evidence_reference(self, kind, target, scope)

    def set_evidence_groups(self, memory_id: str, scope: Scope, groups: list[list[EvidenceRef]],
                            *, expected_version: int, reason: str) -> dict:
        """组内 AND，组间 OR；显式组替代目标的旧扁平来源语义。"""
        from .evidence import set_groups
        return set_groups(self, memory_id, scope, groups, expected_version=expected_version, reason=reason)

    def refresh_evidence(self) -> list[str]:
        """可信管理入口：撤回已失去全部支持的派生记忆，不自动重新激活。"""
        from .evidence import invalidate
        with self._db.transaction():
            return invalidate(self)

    def inspect_evidence(self, memory_id: str, scope: Scope) -> dict:
        from .evidence import inspect
        return inspect(self, memory_id, scope)

    # -- 6. 纠正与删除 -----------------------------------------------------

    def correct(self, memory_id: str, correction: Correction) -> CorrectionResult:
        """纠正一条记忆。

        kind="fix"    ：原先就错了 → 标记 retracted，并让依赖它的派生记忆失效
        kind="change" ：当时对、现在变了 → 结束有效期，写一条新记录并建立 supersedes 链接

        两者的区别决定了「历史能不能被解释」：
        fix 之后旧记录不再算数；change 之后旧记录仍然可查，只是标了截止时间。
        """
        if correction.kind not in ("fix", "change"):
            raise InvalidCorrection(f"未知的纠正类型：{correction.kind}")
        if correction.kind == "change":
            return self._apply_prepared_correction(self._prepare_correction(memory_id, correction))
        with self._db.transaction():
            old = self._correction_target(memory_id)
            now = self._clock.now()
            self._repos.memories.update_status(memory_id, MemoryStatus.RETRACTED, now)
            self._vectors.delete(memory_id)
            self._repos.entities.remove_memory(memory_id)
            invalidated = self._invalidate_derived(memory_id, now, being_removed={memory_id})
            return CorrectionResult(
                old_memory=old, invalidated_derived=invalidated,
                note="已撤回原记录；派生记忆已失效，需要时可重新归纳。",
            )

    def _correction_target(self, memory_id: str) -> MemoryItem:
        old = self._repos.memories.get(memory_id)
        if old is None:
            raise ScopeNotFound(f"找不到记忆 {memory_id}")
        if old.status is MemoryStatus.DELETED or self._repos.forgetting.is_suppressed(old):
            raise MemorySuppressed("已遗忘的记忆不能通过纠错重新启用")
        return old

    def _prepare_correction(self, memory_id: str, correction: Correction) -> _PreparedCorrection:
        """快照规划后退出事务编码；外层写事务调用必须先改为分阶段。"""
        if correction.kind != "change":
            raise InvalidCorrection("只有结构化替换需要预计算")
        request = deepcopy(correction)
        with self._db.read_snapshot():
            old = self._correction_target(memory_id)
            if old.status is not MemoryStatus.ACTIVE or old.valid_to is not None:
                raise InvalidCorrection("替换目标已终结，请重新读取当前事实")
            now = self._clock.now()
            replacement = self._build_replacement(old, request, request.effective_from or now, now)
            self._validate_correction_sources(replacement)
            generation = self._repos.retrieval_cache.generation()
            epoch = self._repos.forgetting.epoch(old.scope)
        vectors = self._vectors.prepare_memories([replacement])
        return _PreparedCorrection(self, generation, epoch, old, replacement, vectors,
                                   _correction_digest(old, replacement))

    def _validate_correction_sources(self, replacement: MemoryItem) -> None:
        if self._repos.forgetting.is_suppressed(replacement):
            raise MemorySuppressed("替换事实触及遗忘标记")
        for source_id in replacement.source_event_ids:
            event = self._repos.events.get(source_id)
            if (event is None or self._repos.forgetting.is_event_forgotten(source_id)
                    or not self._scope_matches(event.scope, replacement.scope)):
                raise InvalidCorrection("替换依据不存在、已遗忘或不属于目标作用域")

    def _apply_prepared_correction(self, plan: _PreparedCorrection) -> CorrectionResult:
        with self._db.transaction():
            if (plan.owner is not self or plan.digest != _correction_digest(plan.old, plan.replacement)
                    or self._repos.retrieval_cache.generation() != plan.generation
                    or self._repos.forgetting.epoch(plan.old.scope) != plan.epoch):
                raise SourceRevisionChanged("纠错计算期间数据已变化，请重新读取并计算")
            now = self._clock.now()
            old, new_item = plan.old, replace(plan.replacement, recorded_at=now)
            self._validate_correction_sources(new_item)
            self._repos.memories.close_validity(old.memory_id, new_item.valid_from, now)
            inserted = self._repos.memories.insert(new_item)
            if inserted is not None:
                self._repos.links.add(
                    new_item.memory_id, old.memory_id, LinkRelation.SUPERSEDES, now
                )
                self._vectors.index_memory(new_item, now, prepared=plan.vectors)
                self._repos.entities.index_memory(new_item, now)

            invalidated = self.refresh_evidence()
            return CorrectionResult(
                old_memory=old,
                new_memory=inserted or new_item,
                invalidated_derived=invalidated,
                note="旧记录保留为历史值，新记录已生效。",
            )

    def forget(self, selector: ForgetSelector) -> ForgetResult:
        """兼容入口：按选择器分派事实级或事件级遗忘。"""
        if selector.event_ids:
            if selector.memory_ids or len(selector.event_ids) != 1:
                raise ValueError("兼容 forget 入口一次只能指定一个 event_id，且不能同时指定 memory_id")
            return self.forget_event(selector.event_ids[0], scope=selector.scope, reason=selector.reason)
        return self.forget_fact(selector)

    def forget_scope(self, scope: Scope, *, reason: str = "用户要求清除作用域") -> ForgetResult:
        """物理清除一个作用域及其子活动，并留下不可逆的代次标记。

        这是 ``logical_suppressed`` 之上的主动擦除等级：主库正文、派生索引、
        handoff、核心投影和修订日志都会移除；HMAC 密钥与 hash-chain 骨架保留。
        已经投递给模型的上下文无法从宿主撤回，外部备份也只能通过导入预检拒绝
        旧代次，仍需用户单独删除。
        """
        now = self._clock.now()

        def matches(row: Any) -> bool:
            return (
                row.get("user_id") == scope.user_id
                and row.get("character_id") == scope.character_id
                and row.get("domain", "real") == scope.domain
                and (scope.relationship_id is None or row.get("relationship_id") == scope.relationship_id)
                and (scope.activity_id is None or row.get("activity_id") == scope.activity_id)
            )

        def key_scope(value: Any) -> Scope | None:
            try:
                user, character, relationship, activity, domain = json.loads(value)
                candidate = Scope(str(user), str(character), relationship, activity, str(domain))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return candidate if matches({
                "user_id": candidate.user_id, "character_id": candidate.character_id,
                "relationship_id": candidate.relationship_id, "activity_id": candidate.activity_id,
                "domain": candidate.domain,
            }) else None

        conn = self._db.connection
        affected: set[Scope] = {scope}
        explicit_tables = (
            "activities", "session_handoffs", "events", "memories", "memory_revisions",
            "summaries", "summary_revisions", "consolidation_jobs", "health_snapshots",
            "entities",
        )
        for table in explicit_tables:
            rows = conn.execute(f"SELECT user_id, character_id, relationship_id, activity_id, domain FROM {table}").fetchall()
            for row in rows:
                raw = dict(row)
                if matches(raw):
                    affected.add(Scope(raw["user_id"], raw["character_id"], raw.get("relationship_id"),
                                       raw.get("activity_id"), raw.get("domain", "real")))
        core_keys: set[str] = set()
        for row in conn.execute("SELECT scope_key,user_id,character_id,relationship_id,domain FROM core_versions").fetchall():
            raw = dict(row)
            if matches(raw):
                core_keys.add(raw["scope_key"])
                affected.add(Scope(raw["user_id"], raw["character_id"], raw.get("relationship_id"),
                                   None, raw.get("domain", "real")))
        key_tables = ("forgotten_events", "memory_tombstones", "session_handoffs",
                      "scope_mutations", "core_field_tombstones", "core_proposals",
                      "change_proposals", "scope_purges", "relearning_grants", "fact_forgetting_barriers",
                      "summary_restore_receipts", "proposal_batches")
        for table in key_tables:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "scope_key" not in columns:
                continue
            for row in conn.execute(f"SELECT scope_key FROM {table}").fetchall():
                candidate = key_scope(row["scope_key"])
                if candidate is not None:
                    affected.add(candidate)
        scope_keys = {_scope_key(item) for item in affected}

        with self._db.transaction():
            # 先擦除 journal 载荷，再删除正文。重算后追加 purge receipt，链仍可验证。
            self._repos.journal.redact_scope(scope, now=now, reason=reason)

            def delete_scoped(table: str, alias: str = "") -> int:
                visible, params = scope_sql(scope, alias)
                cursor = conn.execute(f"DELETE FROM {table} WHERE {visible}", params)
                return cursor.rowcount

            memory_ids = [row[0] for row in conn.execute(
                "SELECT memory_id FROM memories WHERE " + scope_sql(scope)[0], scope_sql(scope)[1]
            ).fetchall()]
            event_ids = [row[0] for row in conn.execute(
                "SELECT event_id FROM events WHERE " + scope_sql(scope)[0], scope_sql(scope)[1]
            ).fetchall()]
            handoff_ids = [row[0] for row in conn.execute(
                "SELECT handoff_id FROM session_handoffs WHERE " + scope_sql(scope)[0], scope_sql(scope)[1]
            ).fetchall()]
            tombstone_ids = [row[0] for row in conn.execute(
                "SELECT tombstone_id FROM memory_tombstones WHERE scope_key IN (" +
                ",".join("?" for _ in scope_keys) + ")", list(scope_keys)
            ).fetchall()] if scope_keys else []

            if event_ids:
                conn.execute("DELETE FROM proposal_batches WHERE request_event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
                conn.execute("DELETE FROM relearning_grants WHERE confirmation_event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
                conn.execute("DELETE FROM host_deliveries WHERE event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
                conn.execute("DELETE FROM core_proposals WHERE request_event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
                conn.execute("DELETE FROM change_proposals WHERE request_event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
                conn.execute("DELETE FROM tombstone_sources WHERE event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
                conn.execute("DELETE FROM forgotten_events WHERE event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                conn.execute(f"DELETE FROM evidence_sets WHERE memory_id IN ({placeholders})", memory_ids)
                conn.execute(f"DELETE FROM memory_links WHERE from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders})", [*memory_ids, *memory_ids])
                conn.execute(f"DELETE FROM memory_vectors WHERE memory_id IN ({placeholders})", memory_ids)
                conn.execute(f"DELETE FROM entity_mentions WHERE memory_id IN ({placeholders})", memory_ids)
            entity_ids = [row[0] for row in conn.execute(
                "SELECT entity_id FROM entities WHERE " + scope_sql(scope)[0], scope_sql(scope)[1]
            ).fetchall()]
            if entity_ids:
                placeholders = ",".join("?" for _ in entity_ids)
                conn.execute(f"DELETE FROM entity_mentions WHERE entity_id IN ({placeholders})", entity_ids)
            if tombstone_ids:
                for table in ("relearning_grants", "fact_forgetting_barriers"):
                    conn.execute(f"DELETE FROM {table} WHERE tombstone_id IN (" + ",".join("?" for _ in tombstone_ids) + ")", tombstone_ids)
                conn.execute("DELETE FROM tombstone_sources WHERE tombstone_id IN (" + ",".join("?" for _ in tombstone_ids) + ")", tombstone_ids)
            self._repos.retrieval_cache.delete_scopes(list(scope_keys))

            for table in ("memory_revisions", "memories", "summaries", "summary_revisions", "consolidation_jobs",
                          "health_snapshots", "entities", "session_handoffs", "events", "activities"):
                delete_scoped(table)
            if scope_keys:
                placeholders = ",".join("?" for _ in scope_keys)
                for table in ("core_field_tombstones", "core_proposals", "change_proposals",
                              "memory_tombstones", "scope_purges", "summary_restore_receipts", "proposal_batches"):
                    conn.execute(f"DELETE FROM {table} WHERE scope_key IN ({placeholders})", list(scope_keys))
            # 核心键采用 user/character/domain/relationship 四元组，不能用普通
            # scope 的五元组删除；两个命名空间必须分别处理。
            if core_keys:
                placeholders = ",".join("?" for _ in core_keys)
                conn.execute(f"DELETE FROM core_field_revisions WHERE scope_key IN ({placeholders})", list(core_keys))
                conn.execute(f"DELETE FROM core_field_tombstones WHERE scope_key IN ({placeholders})", list(core_keys))
                conn.execute(f"DELETE FROM core_versions WHERE scope_key IN ({placeholders})", list(core_keys))

            # scope_mutations 是缓存代次的一部分：保留空元数据行并递增，
            # 让其他进程/旧 session 的缓存立即失效。
            old_epochs = {
                row["scope_key"]: int(row["epoch"])
                for row in conn.execute("SELECT scope_key, epoch FROM scope_mutations").fetchall()
                if row["scope_key"] in scope_keys
            }
            if scope_keys:
                placeholders = ",".join("?" for _ in scope_keys)
                conn.execute(f"DELETE FROM scope_mutations WHERE scope_key IN ({placeholders})", list(scope_keys))
                for key in scope_keys:
                    next_epoch = old_epochs.get(key, 0) + 1
                    conn.execute("INSERT INTO scope_mutations(scope_key, epoch) VALUES (?,?)", (key, next_epoch))
                    raw = json.loads(key)
                    item_scope = Scope(raw[0], raw[1], raw[2], raw[3], raw[4])
                    conn.execute(
                        "INSERT INTO scope_purges(scope_key,user_id,character_id,relationship_id,activity_id,domain,purge_generation,reason,purged_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (key, item_scope.user_id, item_scope.character_id, item_scope.relationship_id,
                         item_scope.activity_id, item_scope.domain, next_epoch, reason[:500], now.isoformat()),
                    )
            self._repos.journal.append(
                "scope.purged", scope=scope, payload={"redacted": True, "scope_count": len(scope_keys)},
                now=now, idempotency_key=f"scope-purge:{_scope_key(scope)}:{now.isoformat()}",
            )
            self.refresh_evidence()

        return ForgetResult(
            deleted_ids=sorted(memory_ids), forgotten_event_ids=sorted(event_ids),
            erasure_level="active_store_purged",
            note="已物理清除本地作用域主数据、派生索引和修订日志。",
            residuals=["当前模型上下文无法撤回", "外部备份需单独清理或重新导出"],
        )

    def forget_event(
        self,
        event_id: str,
        *,
        scope: Scope | None = None,
        reason: str = "用户要求删除事件",
    ) -> ForgetResult:
        """撤销一条原始事件对系统的支持并逻辑擦除正文。

        事件行和 ID 保留以维护外键与审计结构；对外读取、重新抽取和核心
        字段来源均检查 forgotten_events。记忆若还有其他独立事件来源则只
        移除本事件，否则软删除并传播派生失效。
        """
        now = self._clock.now()
        event = self._repos.events.get(event_id)
        if event is None:
            raise ScopeNotFound(f"找不到事件 {event_id}")
        if scope is not None and not self._scope_matches(event.scope, scope):
            raise ScopeNotFound(f"事件 {event_id} 不属于当前作用域")
        target_scope = event.scope
        deleted: list[str] = []
        invalidated: list[str] = []
        core_fields: list[str] = []
        updated: list[str] = []

        with self._db.transaction():
            if self._repos.forgetting.is_event_forgotten(event_id):
                return ForgetResult(
                    forgotten_event_ids=[event_id],
                    note="该事件已经完成逻辑遗忘。",
                    erasure_level="event_redacted",
                )
            self._repos.forgetting.mark_event(event_id, target_scope, now, reason)
            self._repos.forgetting.bump(target_scope)
            self._repos.jobs.cancel_event(event_id, now)

            candidates = self._repos.memories.list_by_scope(
                target_scope, limit=-1,
                statuses=(MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED, MemoryStatus.RETRACTED),
            )
            for item in candidates:
                if event_id not in item.source_event_ids:
                    continue
                remaining_ids = [source for source in item.source_event_ids if source != event_id]
                remaining_spans = {
                    source: intervals for source, intervals in item.source_spans.items()
                    if source != event_id
                }
                if remaining_ids:
                    # 仍有独立来源的事实保留，避免一次事件遗忘误删其他证据。
                    if not self._repos.forgetting.is_suppressed(item):
                        self._repos.memories.update_content(
                            item.memory_id,
                            item.body,
                            now,
                            source_event_ids=remaining_ids,
                            source_spans=remaining_spans,
                        )
                        updated.append(item.memory_id)
                elif self._repos.memories.delete(item.memory_id, now):
                    deleted.append(item.memory_id)
                    self._vectors.delete(item.memory_id)
                    self._repos.entities.remove_memory(item.memory_id)

            self._repos.events.redact(event_id)
            invalidated = self.refresh_evidence()
            core_fields = self._repos.forgetting.redact_cores(now)

        return ForgetResult(
            deleted_ids=deleted,
            invalidated_derived=sorted(set(invalidated)),
            core_fields_removed=core_fields,
            forgotten_event_ids=[event_id],
            updated_memory_ids=updated,
            note="已撤销该事件的来源贡献并逻辑擦除正文；备份与供应商历史仍需单独清理。",
            erasure_level="event_redacted",
        )

    def forget_fact(self, selector: ForgetSelector) -> ForgetResult:
        """持久抑制选定断言，保留同消息中来源区间不重叠的其他事实。

        同一作用域内的相同断言一起删除；未知来源片段保守隔离。
        这是 logical_suppressed，不等于正文、备份或供应商历史已擦除。
        """
        now = self._clock.now()
        deleted: list[str] = []
        invalidated: list[str] = []
        tombstones: set[str] = set()
        conservative: set[str] = set()

        with self._db.transaction():
            seeds = self._repos.memories.fetch_many(self._resolve_forget_targets(selector))
            scopes = {item.scope for item in seeds.values()}
            for item in seeds.values():
                tombstones.add(self._repos.forgetting.suppress(item, now))
                conservative.update(event_id for event_id in item.source_event_ids
                                    if not item.source_spans.get(event_id))
            for tombstone_id in tombstones:
                self._repos.forgetting.renew_barrier(tombstone_id, now)
            targets: set[str] = set(seeds)
            candidates: list[MemoryItem] = []
            for scope in scopes:
                self._repos.forgetting.bump(scope)
                candidates.extend(item for item in self._repos.memories.list_by_scope(
                    scope, limit=-1, statuses=tuple(MemoryStatus),
                ) if item.scope == scope)
            # 先收齐同一断言的全部来源，再判断区间重叠，避免依赖记录遍历顺序。
            for item in candidates:
                if self._repos.forgetting.assertion_tombstone(item) is not None:
                    tombstones.add(self._repos.forgetting.suppress(item, now))
                    conservative.update(event_id for event_id in item.source_event_ids
                                        if not item.source_spans.get(event_id))
            targets.update(item.memory_id for item in candidates if self._repos.forgetting.is_fact_suppressed(item))
            for memory_id in sorted(targets):
                if self._repos.memories.delete(memory_id, now):
                    deleted.append(memory_id)
                    # 向量是派生数据，主记录删除后必须一起清掉，
                    # 否则它还会出现在语义通道的候选里。
                    self._vectors.delete(memory_id)
                    self._repos.entities.remove_memory(memory_id)
            # 整批变更后统一判断 memory/event/core 三类来源。
            invalidated = self.refresh_evidence()
            core_fields = self._repos.forgetting.redact_cores(now)

        return ForgetResult(
            deleted_ids=deleted,
            invalidated_derived=sorted(set(invalidated)),
            note=("已持久抑制选定事实；原始消息正文与备份未擦除。" if seeds
                  else "没有找到匹配的记忆；未新增抑制标记。"),
            tombstone_ids=sorted(tombstones),
            core_fields_removed=core_fields,
            conservative_event_ids=sorted(conservative),
        )

    def relearn_fact(
        self, memory_id: str, scope: Scope, *, confirmation_event_id: str,
        approved_by: str, reason: str, idempotency_key: str,
        source_span: tuple[int, int] | None = None,
    ) -> RelearningResult:
        """可信宿主审批一条新的用户重申；不恢复旧来源或旧核心。

        这是本地管理接口，不是 LLM 工具。宿主必须独立确认用户重申了所选
        事实并允许重新记住，不能把模型建议当成审批。
        """
        from .relearning import relearn_fact
        return relearn_fact(self, memory_id, scope, confirmation_event_id=confirmation_event_id,
                            approved_by=approved_by, reason=reason, idempotency_key=idempotency_key,
                            source_span=source_span)

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
        return self._vectors.rebuild(scope, self._clock.now())

    def run_consolidation(
        self,
        extractor: MemoryExtractor | None = None,
        limit: int = 10,
    ) -> ConsolidationReport:
        """执行待处理的抽取任务。

        每次调用先回收过期租约。各 worker 须使用独立 runtime/数据库连接；
        文件库会在抽取期间续租，副作用与完成确认在同一事务内提交。
        """
        return run_pending_jobs(
            db=self._db,
            clock=self._clock,
            extractor=extractor,
            vector_index=self._vectors,
            limit=limit,
            tz=self._tz,
            lease_seconds=self.settings.job_lease_seconds,
            retry_base_seconds=self.settings.job_retry_base_seconds,
            retry_max_seconds=self.settings.job_retry_max_seconds,
            max_attempts=self.settings.job_max_attempts,
        )

    def pending_jobs(self) -> int:
        return self._repos.jobs.pending_count()

    def job_stats(self) -> dict[str, int]:
        """队列只读状态，包含等待退避、过期租约和最终失败任务。"""
        return self._repos.jobs.stats(self._clock.now())

    # -- 8. 主题摘要 -------------------------------------------------------

    def build_summaries(self, scope: Scope, *, reactivate: bool = False) -> int:
        """立即重建主题摘要，返回卡片数。

        平时不用手动调：extract → dedupe → summarize 会自动串起来。
        这个方法用于「刚导入一批历史数据，想马上有索引」的场景。
        默认保留人工撤回状态；可信宿主可用 reactivate=True 显式重新激活。
        """
        return build_summaries(self._db, scope, self._clock, self._tz, reactivate=reactivate)

    def withdraw_summaries(self, summary_ids: list[str], scope: Scope) -> dict[str, Any]:
        """可信宿主撤回摘要派生物；不会删除其来源记忆。"""
        return self._transition_summaries(summary_ids, scope, "withdrawn")

    def rollback_summaries(self, summary_ids: list[str], scope: Scope) -> dict[str, Any]:
        """标记派生物已撤销，等待显式重新激活；不恢复历史摘要正文。"""
        return self._transition_summaries(summary_ids, scope, "rolled_back")

    def summary_history(self, summary_id: str, scope: Scope, *, limit: int = 100) -> list[dict]:
        from .temporal import summary_history
        with self._db.transaction(write=False):
            return summary_history(self, summary_id, scope, limit)

    def restore_summary(self, summary_id: str, revision: int, scope: Scope, *, expected_version: int,
                        reason: str, idempotency_key: str) -> dict:
        """显式恢复经过来源复核的历史正文；旧 rollback_summaries 仅撤销卡片。"""
        from .temporal import restore_summary
        return restore_summary(self, summary_id, revision, scope, expected_version=expected_version,
                               reason=reason, idempotency_key=idempotency_key)

    def _transition_summaries(self, summary_ids: list[str], scope: Scope, status: str) -> dict[str, Any]:
        if not isinstance(summary_ids, list) or not 1 <= len(summary_ids) <= 100:
            raise ValueError("summary_ids 必须包含 1..100 项")
        if any(not isinstance(item, str) or not item for item in summary_ids):
            raise ValueError("summary_id 必须为非空字符串")
        ids = list(dict.fromkeys(summary_ids))
        now = self._clock.now()
        with self._db.transaction():
            summaries = []
            for summary_id in ids:
                summary = self._repos.summaries.get(summary_id)
                if summary is None or not self._scope_matches(summary.scope, scope):
                    raise ScopeNotFound(f"找不到作用域内摘要 {summary_id}")
                summaries.append(summary)
            changed = (self._repos.summaries.mark_withdrawn(ids, now)
                       if status == "withdrawn"
                       else self._repos.summaries.mark_rolled_back(ids, now))
            for summary in summaries:
                if summary.status == status:
                    continue
                self._repos.journal.append(
                    f"summary.{status}", scope=summary.scope, entity_id=summary.summary_id,
                    payload={"status": status}, now=now,
                )
        return {"status": status, "summary_ids": ids, "changed": changed}

    def expand_summary(self, summary_id: str, limit: int = 5) -> list[RetrievedItem]:
        """把一张索引卡回源展开成原始记忆。

        这是「先摘要后展开」的后半段：模型看到摘要后如果觉得相关，
        再用这个方法取具体证据，而不是一上来就塞十条记忆。
        """
        summary = self._repos.summaries.get(summary_id)
        if summary is None:
            raise ScopeNotFound(f"找不到摘要 {summary_id}")
        if summary.status != "active":
            return []

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
            and not self._repos.forgetting.is_suppressed(memory)
            and self._scope_matches(memory.scope, summary.scope)
            and not (
                memory.status is MemoryStatus.SUPERSEDED
                and self._repos.links.has_relation(memory.memory_id, LinkRelation.PART_OF)
            )
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
        """兼容内部入口；统一求解类型化支持组和旧 OR 派生链接。"""
        return self.refresh_evidence()

    @staticmethod
    def _scope_matches(actual: Scope, requested: Scope) -> bool:
        """与仓储相同的可见性规则；可选 ID 仅在指定时收窄范围。"""
        return (
            actual.user_id == requested.user_id
            and actual.character_id == requested.character_id
            and actual.domain == requested.domain
            and (requested.relationship_id is None or actual.relationship_id == requested.relationship_id)
            and (requested.activity_id is None or actual.activity_id == requested.activity_id)
        )

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
            source_spans={key: value for key, value in old.source_spans.items() if key in sources},
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
            ids = list(dict.fromkeys(selector.memory_ids))
            if selector.scope is None and selector.domain is None and not selector.memory_types:
                return ids
            items = self._repos.memories.fetch_many(ids)
            return [
                memory_id for memory_id in ids
                if (item := items.get(memory_id)) is not None
                and (selector.scope is None or self._scope_matches(item.scope, selector.scope))
                and (selector.domain is None or item.scope.domain == selector.domain)
                and (not selector.memory_types or item.memory_type in selector.memory_types)
            ]

        if selector.scope is None:
            raise ValueError("forget 需要 memory_ids 或 scope 之一，不能都为空")

        scope = replace(selector.scope, domain=selector.domain) if selector.domain else selector.scope
        return self._repos.memories.list_ids_for_scope(scope, selector.memory_types)
