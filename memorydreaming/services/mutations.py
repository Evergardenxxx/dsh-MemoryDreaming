"""事实纠正、遗忘与重新学习服务。

纠错使用“读取快照 → 事务外计算向量 → 代次复核后原子发布”；遗忘必须同时
更新来源屏障、索引、派生摘要和核心投影。此模块不负责识别用户授权，授权由
可信宿主/审批层确认。方法保留在 MemoryRuntime 的 MRO 上，调用 self 以兼容
宿主覆写与测试故障注入，不捕获另一个服务实例或数据库连接。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
from typing import Any
from ..errors import InvalidCorrection, MemorySuppressed, ScopeNotFound, SourceRevisionChanged
from ..models import (
    Correction,
    CorrectionResult,
    ForgetResult,
    ForgetSelector,
    LinkRelation,
    MemoryItem,
    MemoryStatus,
    RelearningResult,
    Scope,
    new_id,
)
from ..retrieval.vector import PreparedVectors
from ..storage.repository import scope_sql, _scope_key


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


class MutationService:
    """MemoryRuntime 的业务能力；不直接实例化。"""

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
            "summaries", "summary_revisions", "consolidation_jobs", "health_snapshots", "operational_metrics", "sleep_runs",
            "entities",
            "narrative_entries", "narrative_proposals", "narrative_versions", "intentions", "intention_revisions",
            "intention_operations", "proactive_policies", "proactive_deliveries", "proactive_ack_receipts", "intention_trigger_receipts",
            "companionship_proposals", "companionship_preferences", "companionship_history", "companionship_receipts",
            "growth_proposals", "growth_values", "growth_history", "growth_receipts",
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
                      "summary_restore_receipts", "proposal_batches", "operational_alerts")
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
            # 梦与审阅正文是可撤销派生物；作用域清除必须一起清除，不能
            # 留在“另外一种记忆”的历史中。子表由 FK ON DELETE CASCADE 处理。
            self._db.connection.execute(
                "DELETE FROM sleep_runs WHERE " + scope_sql(scope)[0], scope_sql(scope)[1],
            )
            # 连续性投影可能重复用户原话或承诺。范围擦除要先删子表/回执，
            # 不能只隐藏事实表却把同一内容留在“自传”或待发提醒中。
            for table in ("narrative_versions", "narrative_proposals", "narrative_entries", "proactive_ack_receipts",
                          "proactive_deliveries", "intention_revisions", "intention_operations", "intention_trigger_receipts",
                          "proactive_policies", "intentions", "companionship_receipts", "companionship_history",
                          "companionship_preferences", "companionship_proposals", "growth_receipts", "growth_history",
                          "growth_values", "growth_proposals"):
                self._db.connection.execute("DELETE FROM "+table+" WHERE "+scope_sql(scope)[0], scope_sql(scope)[1])

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
                          "health_snapshots", "operational_metrics", "entities", "session_handoffs", "events", "activities"):
                delete_scoped(table)
            if scope_keys:
                placeholders = ",".join("?" for _ in scope_keys)
                for table in ("core_field_tombstones", "core_proposals", "change_proposals",
                              "memory_tombstones", "scope_purges", "summary_restore_receipts", "proposal_batches", "operational_alerts"):
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
        from ..relearning import relearn_fact
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

    def _invalidate_derived(
        self,
        memory_id: str,
        now: datetime,
        being_removed: set[str] | None = None,
    ) -> list[str]:
        """兼容内部入口；统一求解类型化支持组和旧 OR 派生链接。"""
        return self.refresh_evidence()

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
