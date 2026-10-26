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
from dataclasses import replace
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
    ReadFilter,
    RetrievalResult,
    RetrievedItem,
    Scope,
    SessionHandoff,
    Turn,
    new_id,
)
from .retrieval.embedding import Embedder
from .retrieval.pipeline import RetrievalPipeline
from .retrieval.vector import VectorIndex
from .summarization import build_summaries
from .storage.database import Database
from .storage.repository import scope_sql
from .worker import ConsolidationWorker


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
        detail: str = "auto",
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
            read_filter=read_filter,
            detail=detail,
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
            "jobs": jobs,
            "working": {"active_activities": len(activities), "open_loops": open_loops},
            "journal": journal,
            "gaps": gaps,
        }

    def journal_entries(self, scope: Scope | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """读取审计链元数据；默认只返回最近记录，不包含对话正文。"""
        return self._repos.journal.list(scope=scope, limit=limit)

    def verify_journal(self) -> dict[str, Any]:
        """校验从 genesis 到当前 head 的全局哈希链。"""
        return self._repos.journal.verify()

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
    ) -> CoreSnapshot:
        """可信本地宿主直接应用一次核心变更；模型建议应走 CoreProposals 审批。

        这里是「补丁合并」语义：patch 里出现的键覆盖旧值，没出现的保持不变。
        版本号由后端计算，调用方只需声明「我是基于哪个版本改的」。

        架构文档的要求：核心变更要可解释。所以 change_reason 是必填项，
        evidence_ids 建议填上支撑这次变更的原始事件。
        """
        core_kind = kind if isinstance(kind, CoreKind) else CoreKind(kind)
        now = self._clock.now()
        with self._db.transaction():
            current = self._repos.cores.latest(scope, core_kind)
            merged = dict(current.payload) if current else {}
            merged.update(patch)
            fields = dict(current.field_evidence_ids) if current else {}
            for name in patch:
                fields[name] = list((field_evidence_ids or {}).get(name, evidence_ids or []))
            refs = sorted({ref for values in fields.values() for ref in values})
            return self._repos.cores.append(
                scope=scope,
                kind=core_kind,
                payload=merged,
                change_reason=change_reason,
                effective_from=now,
                now=now,
                evidence_ids=refs,
                expected_version=expected_version,
                field_evidence_ids=fields,
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
                self._vectors.delete(memory_id)
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
        """兼容入口：按选择器分派事实级或事件级遗忘。"""
        if selector.event_ids:
            if selector.memory_ids or len(selector.event_ids) != 1:
                raise ValueError("兼容 forget 入口一次只能指定一个 event_id，且不能同时指定 memory_id")
            return self.forget_event(selector.event_ids[0], scope=selector.scope, reason=selector.reason)
        return self.forget_fact(selector)

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
                elif self._repos.memories.delete(item.memory_id, now):
                    deleted.append(item.memory_id)
                    self._vectors.delete(item.memory_id)

            if deleted:
                invalidated = self._invalidate_derived(
                    deleted[0], now, being_removed=set(deleted)
                )
            self._repos.events.redact(event_id)
            core_fields = self._repos.forgetting.redact_cores(now)

        return ForgetResult(
            deleted_ids=deleted,
            invalidated_derived=sorted(set(invalidated)),
            core_fields_removed=core_fields,
            forgotten_event_ids=[event_id],
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
            targets.update(item.memory_id for item in candidates if self._repos.forgetting.is_suppressed(item))
            for memory_id in sorted(targets):
                if self._repos.memories.delete(memory_id, now):
                    deleted.append(memory_id)
                    # 向量是派生数据，主记录删除后必须一起清掉，
                    # 否则它还会出现在语义通道的候选里。
                    self._vectors.delete(memory_id)
            if deleted:
                # 整批显式删除完成后统一判断来源，避免遍历顺序改变结果。
                invalidated = self._invalidate_derived(
                    deleted[0], now, being_removed=set(deleted)
                )
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
        """把「失去全部来源支持」的派生记录标成 retracted。

        规则取自 Zep 的 remove_episode：**只有当一个事实的所有父来源都没了，
        它才该消失**。如果一条派生记忆同时由 A 和 B 支撑，删掉 A 不该动它——
        否则删一次来源就会误伤别的证据。

        先取受影响子图，再保留仍能连接到外部有效来源的节点。
        不等长路径和环形依赖都不能凭互相引用充当独立证据。
        """
        removed = set(being_removed or ()) | {memory_id}
        affected: set[str] = set()
        queue = deque(sorted(removed))
        while queue:
            for dependent in self._repos.links.dependents(queue.popleft()):
                if dependent not in affected and dependent not in removed:
                    affected.add(dependent)
                    queue.append(dependent)

        supported: set[str] = set()
        for dependent in affected:
            item = self._repos.memories.get(dependent)
            if item is None or item.status in (MemoryStatus.RETRACTED, MemoryStatus.DELETED):
                continue
            for parent_id in self._repos.links.parents_of(dependent):
                if parent_id in affected or parent_id in removed:
                    continue
                parent = self._repos.memories.get(parent_id)
                if parent is not None and parent.status not in (
                    MemoryStatus.RETRACTED, MemoryStatus.DELETED,
                ):
                    supported.add(dependent)
                    break

        queue = deque(sorted(supported))
        while queue:
            for dependent in self._repos.links.dependents(queue.popleft()):
                if dependent not in affected or dependent in supported:
                    continue
                item = self._repos.memories.get(dependent)
                if item is not None and item.status not in (
                    MemoryStatus.RETRACTED, MemoryStatus.DELETED,
                ):
                    supported.add(dependent)
                    queue.append(dependent)

        invalidated: list[str] = []
        for dependent in sorted(affected - supported):
            item = self._repos.memories.get(dependent)
            if item is not None and item.status not in (
                MemoryStatus.RETRACTED, MemoryStatus.DELETED,
            ):
                self._repos.memories.update_status(dependent, MemoryStatus.RETRACTED, now)
                self._vectors.delete(dependent)
                invalidated.append(dependent)
        return invalidated

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
