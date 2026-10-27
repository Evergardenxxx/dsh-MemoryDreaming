"""已有单项提议的整批确认；不增加模型工具或审批权限。"""

from dataclasses import replace
import json

from .clock import to_utc_iso
from .errors import InvalidCorrection, MemorySuppressed, ScopeNotFound, SourceRevisionChanged, VersionConflict
from .llm import AdapterError, dumps, text
from .models import CoreKind, Correction, new_id
from .proposals import ChangeProposals, CoreProposals
from .references import ViewReader, forbidden
from .retrieval.embedding import EmbeddingError
from .storage.repository import _scope_key


class ProposalBatches:
    def __init__(self, session):
        self.session, self.runtime = session, session.runtime
        self.conn, self.scope = self.runtime.db.connection, session.binding.scope
        self.scope_key = _scope_key(self.scope)
        self.core, self.change = CoreProposals(session), ChangeProposals(session)

    def fingerprint(self, value):
        return self.runtime.db.repositories.forgetting.fingerprint(dumps(value))

    def signatures(self, kind, row):
        reader = ViewReader(self.runtime, self.session.binding, self.session.clock)
        def content(view):
            return [view.kind, view.target, view.data, [content(source) for source in view.sources]]
        refs = json.loads(row["evidence_json"])
        refs += ([{"kind": "core_version", "target": row["target_kind"]}] if kind == "core"
                 else json.loads(row["target_refs_json"]))
        return [(ref["kind"], ref["target"], self.fingerprint(content(reader.load(ref["kind"], ref["target"])))) for ref in refs]

    def load(self, batch_id):
        text(batch_id, "batch_id", 150)
        row = self.conn.execute("SELECT * FROM proposal_batches WHERE batch_id=? AND scope_key=? AND conversation_id=?",
                (batch_id, self.scope_key, self.session.binding.conversation_id)).fetchone()
        if row is None:
            raise forbidden()
        return row

    def members(self, items, request_id):
        rows, targets = [], set()
        for item in items:
            row = self.core.load(item["proposal_id"]) if item["kind"] == "core" else self.change._load(item["proposal_id"])
            if row["request_event_id"] != request_id:
                raise forbidden()
            if row["state"] != "pending_validation":
                raise AdapterError("STALE_REFERENCE", "批次含已处理的单项提议")
            code = self.core.check(row) if item["kind"] == "core" else self.change._check(row)
            if code or (item.get("row_key") is not None and item["row_key"] != self.fingerprint(dict(row))):
                raise AdapterError(code or "STALE_REFERENCE", "批次单项已变化，请重新检查")
            keys = [("core", row["target_kind"])] if item["kind"] == "core" else [
                (ref["kind"], ref["target"]) for ref in json.loads(row["target_refs_json"])]
            if item["kind"] == "change" and row["operation"] == "forget_scope":
                raise AdapterError("UNSUPPORTED", "整库遗忘必须单独审批，不能混入批次")
            if targets.intersection(keys):
                raise AdapterError("INVALID_ARGUMENT", "同一目标不能在批次中重复修改")
            targets.update(keys)
            rows.append((item["kind"], row))
        return rows

    def create(self, items, *, reason, idempotency_key):
        text(reason, "reason", 500)
        text(idempotency_key, "idempotency_key", 200)
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise AdapterError("INVALID_ARGUMENT", "批次须含 1..20 个已存在的单项提议")
        if any(not isinstance(i, dict) or set(i) != {"kind", "proposal_id"} or i["kind"] not in ("core", "change")
               or not isinstance(i["proposal_id"], str) for i in items):
            raise AdapterError("INVALID_ARGUMENT", "成员须含 kind=core/change 和 proposal_id")
        request_id = self.session.current_event_id
        if not request_id:
            raise forbidden()
        request_key = self.fingerprint([request_id, self.session.binding.conversation_id, items, reason])
        with self.runtime.db.transaction():
            existing = self.conn.execute("SELECT * FROM proposal_batches WHERE scope_key=? AND idempotency_key=?",
                                         (self.scope_key, idempotency_key)).fetchone()
            if existing:
                if existing["request_key"] != request_key:
                    raise AdapterError("INVALID_ARGUMENT", "批次幂等键不能对应不同请求")
                return self.inspect(existing["batch_id"])
            rows = self.members(items, request_id)
            if len({(kind, row["proposal_id"]) for kind, row in rows}) != len(rows):
                raise AdapterError("INVALID_ARGUMENT", "批次不能重复包含同一提议")
            frozen = [{"kind": kind, "proposal_id": row["proposal_id"], "row_key": self.fingerprint(dict(row))} for kind, row in rows]
            batch_id = new_id("batch")
            self.conn.execute("INSERT INTO proposal_batches(batch_id,scope_key,idempotency_key,request_key,request_event_id,"
                              "conversation_id,items_json,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (batch_id, self.scope_key, idempotency_key, request_key, request_id, self.session.binding.conversation_id,
                 dumps(frozen), reason, to_utc_iso(self.session.clock.now())))
            return self.inspect(batch_id)

    def finish(self, row, state, code, reviewer=None, reason=None, receipts=()):
        receipt = {"batch_id": row["batch_id"], "state": state, "applied": state == "applied",
                   "decision_code": code, "receipts": list(receipts)}
        self.conn.execute("UPDATE proposal_batches SET state=?,reviewer=?,decision_reason=?,receipt_json=? WHERE batch_id=?",
                          (state, reviewer, reason, dumps(receipt), row["batch_id"]))
        return receipt

    def inspect(self, batch_id):
        with self.runtime.db.transaction():
            row = self.load(batch_id)
            if row["state"] != "pending":
                return json.loads(row["receipt_json"])
            try:
                self.members(json.loads(row["items_json"]), row["request_event_id"])
            except AdapterError as exc:
                return self.finish(row, "invalidated", exc.code)
            return {"batch_id": batch_id, "state": "pending", "applied": False,
                    "items": [{"kind": i["kind"], "proposal_id": i["proposal_id"]} for i in json.loads(row["items_json"])],
                    "reason": row["reason"], "user_event_id": row["request_event_id"]}

    def gate(self, batch_id, user_event_id, reviewer, reason, approve):
        row = self.load(batch_id)
        if row["request_event_id"] != user_event_id:
            raise forbidden()
        if row["state"] != "pending":
            if row["state"] != "invalidated" and (row["state"] != ("applied" if approve else "rejected")
                    or row["reviewer"] != reviewer or row["decision_reason"] != reason):
                raise AdapterError("INVALID_ARGUMENT", "终态批次不可改写，重试须保持同一决定")
            return row, None, json.loads(row["receipt_json"])
        try:
            rows = self.members(json.loads(row["items_json"]), user_event_id)
        except AdapterError as exc:
            return row, None, self.finish(row, "invalidated", exc.code)
        return row, rows, None

    def decide(self, batch_id, *, user_event_id, reviewer, reason, approve):
        for name, value in (("user_event_id", user_event_id), ("reviewer", reviewer), ("reason", reason)):
            text(value, name, 500)
        decision = (batch_id, user_event_id, reviewer, reason, approve)
        with self.runtime.db.transaction():
            row, rows, receipt = self.gate(*decision)
            if receipt is not None:
                return receipt
            generation = self.runtime.db.repositories.retrieval_cache.generation()
            signatures = {item["proposal_id"]: self.signatures(kind, item) for kind, item in rows}
        prepared = {}
        if approve:
            try:
                for kind, item in rows:
                    if kind == "change" and item["operation"] == "correct_fact":
                        payload = json.loads(item["payload_json"])
                        if payload.get("replacement_body") is not None:
                            prepared[item["proposal_id"]] = self.runtime._prepare_correction(
                                json.loads(item["target_refs_json"])[0]["target"], Correction(
                                    "change", corrected_body=payload["replacement_body"], evidence_event_ids=[user_event_id]))
            except EmbeddingError as exc:
                raise AdapterError("UNAVAILABLE", "批次向量计算失败，可重试原审批", retryable=True) from exc
            except (MemorySuppressed, SourceRevisionChanged, InvalidCorrection, ScopeNotFound) as exc:
                raise AdapterError("UNAVAILABLE", "批次依据在计算期间变化，请重新检查", retryable=True) from exc
        with self.runtime.db.transaction():
            row, rows, receipt = self.gate(*decision)
            if receipt is not None:
                return receipt
            if self.runtime.db.repositories.retrieval_cache.generation() != generation:
                raise AdapterError("UNAVAILABLE", "批次计算期间数据变化，请重试原审批", retryable=True)
            receipts = []
            try:
                with self.runtime.db.transaction():
                    for kind, item in rows:
                        # 忽略由本批其他独立操作递增的 scope epoch，仍逐项重读完整依据。
                        if signatures[item["proposal_id"]] != self.signatures(kind, item):
                            raise SourceRevisionChanged("批次先前操作使后续依据变化")
                        state, code = ("applied", "APPLIED") if approve else ("rejected", "REJECTED")
                        if kind == "core":
                            version = None
                            if approve:
                                fields = json.loads(item["fields_json"])
                                core = self.runtime.apply_core_update(self.scope, CoreKind(item["target_kind"]),
                                    {f["name"]: f["value"] for f in fields}, item["reason"], expected_version=item["expected_version"],
                                    field_evidence_ids={f["name"]: f["sources"] for f in fields})
                                version = core.core_version
                            receipts.append(self.core.receipt(self.core.finish(item, state, code, reviewer=reviewer, reason=reason, version=version)))
                        else:
                            plan = prepared.get(item["proposal_id"])
                            if plan is not None:
                                if self.runtime.db.repositories.memories.get(plan.old.memory_id) != plan.old:
                                    raise SourceRevisionChanged("批次先前操作影响了后续纠错目标")
                                plan = replace(plan, generation=self.runtime.db.repositories.retrieval_cache.generation(),
                                               epoch=self.runtime.db.repositories.forgetting.epoch(plan.old.scope))
                            applied = self.change._execute(item, plan) if approve else None
                            done = self.change._finish(item, state, code, reviewer, reason, applied)
                            receipts.append(self.change._receipt(done))
                    result = self.finish(row, "applied" if approve else "rejected", "APPLIED" if approve else "REJECTED", reviewer, reason, receipts)
                    self.runtime.db.repositories.journal.append("batch.decided", scope=self.scope,
                        entity_id=batch_id, payload={"state": result["state"], "count": len(receipts)}, now=self.session.clock.now())
                    return result
            except (MemorySuppressed, VersionConflict, SourceRevisionChanged, InvalidCorrection, ScopeNotFound, AdapterError):
                return self.finish(row, "invalidated", "BATCH_CONFLICT", reviewer, reason)
