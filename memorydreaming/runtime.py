"""公开运行时门面与资源组合入口。

宿主使用 MemoryRuntime，不需要分别创建仓储、检索器或业务服务。本文件
保留资源生命周期与日常读写路径；活动、核心、变更、整理和运维方法按业务
域放在 services/，在同一个运行时实例上执行，不产生隐藏连接或第二套状态。

主要路径：bootstrap 恢复核心/活动；retrieve 组装有来源的候选；observe
持久保存真实对话并入队。checkpoint、核心、纠错和遗忘入口由能力类提供，
继续保持原方法签名与 self 派发。完整模块图见 docs/system-architecture.md。

记忆正文始终只是资料。apply_core_update 及兼容名 propose_core_update 都是
可信宿主直写入口；模型只能通过 host/proposals 提出待审批建议，不能从检索
结果中取得核心、工具或文件操作权限。
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime
from typing import Any

from .clock import Clock, SystemClock, load_timezone
from .config import Settings
from .consolidation import MemoryExtractor
from .errors import ScopeNotFound
from .models import (
    Activity,
    ActivityStatus,
    BootstrapResult,
    CoreKind,
    EventRecord,
    LinkRelation,
    MemoryItem,
    MemoryStatus,
    ObserveResult,
    Query,
    ReadFilter,
    RetrievalResult,
    Scope,
    Turn,
    new_id,
)
from .retrieval.embedding import Embedder, embedder_from_settings
from .retrieval.pipeline import RetrievalPipeline
from .retrieval.vector import VectorIndex
from .storage.database import Database
from .worker import ConsolidationWorker
from .services.activities import ActivityService
from .services.core import CoreService
from .services.mutations import MutationService, _PreparedCorrection, _correction_digest
from .services.consolidation import ConsolidationService
from .services.maintenance import MaintenanceService


class MemoryRuntime(
    ActivityService,
    CoreService,
    MutationService,
    ConsolidationService,
    MaintenanceService,
):
    """唯一公开运行时门面与资源所有者。

    连接/模型/后台 worker 的生命周期集中于本类；业务域方法由 services
    中的能力类提供，仍通过 self 派发。这样宿主保留原 API，也可以替换时钟、
    编码器或覆写具体能力，而不会获得另一套隐式数据库/事务。
    """

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

        # 先确定可替换依赖；业务代码不直接读取系统时间，便于回放和衰减测试。
        self._clock = clock or SystemClock()
        self._tz = load_timezone(self._settings.timezone)
        self._owns_embedder = embedder is None
        if embedder is None and self._settings.embedding_provider == "sentence-transformers":
            from .retrieval.isolated import IsolatedSentenceTransformerEmbedder
            embedder = IsolatedSentenceTransformerEmbedder(self._settings.embedding_model_path,
                revision=self._settings.embedding_revision, dim=self._settings.embedding_dim,
                query_prefix=self._settings.embedding_query_prefix, document_prefix=self._settings.embedding_document_prefix,
                max_seq_length=self._settings.embedding_max_seq_length, batch_size=self._settings.embedding_batch_size,
                timeout_seconds=self._settings.embedding_timeout_seconds)
        embedder = embedder if embedder is not None else embedder_from_settings(self._settings)
        # 运行时是唯一资源所有者。业务能力复用这些对象，后台线程另开连接。
        self._db = Database.open(self._settings.db_path)
        self._repos = self._db.repositories
        from .sleep import SleepService
        self.sleep = SleepService(self)
        self._host_adapters = []
        self._consolidator: ConsolidationWorker | None = None

        # 索引和候选流程使用同一个编码器配置指纹，避免混用不同向量空间。
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
        for adapter in self._host_adapters:
            adapter.close()
        self._host_adapters.clear()
        if self._consolidator is not None:
            self._consolidator.stop()
        if self._owns_embedder and hasattr(self._vectors._embedder, "close"):
            self._vectors._embedder.close()
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

    # -- 内部实现 ---------------------------------------------------------

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
