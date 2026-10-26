"""持久核心候选；只有可信宿主的独立审批入口可以令其生效。"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

from .clock import to_utc_iso
from .errors import MemorySuppressed, VersionConflict
from .llm import AdapterError, dumps, integer, text
from .models import CoreKind, Correction, ForgetSelector
from .references import ViewReader, forbidden
from .storage.repository import _scope_key


@dataclass(frozen=True)
class CoreProposalPolicy:
    """宿主允许模型建议的字段；默认全部关闭，不代表自动批准这些字段。"""

    identity_fields: tuple[str, ...] = ()
    relationship_fields: tuple[str, ...] = ()
    ttl_seconds: int = 86400
    change_proposals: bool = False
    checkpoint_proposals: bool = False

    def __post_init__(self):
        for name in ("identity_fields", "relationship_fields"):
            fields = getattr(self, name)
            if not isinstance(fields, (tuple, list)) or len(fields) > 32:
                raise ValueError("字段白名单必须是最多 32 项的显式列表")
            for value in fields:
                text(value, "字段名", 100)
            object.__setattr__(self, name, tuple(dict.fromkeys(fields)))
        integer(self.ttl_seconds, "ttl_seconds", 1, 604800)
        if not isinstance(self.change_proposals, bool) or not isinstance(self.checkpoint_proposals, bool):
            raise ValueError("change_proposals/checkpoint_proposals 必须为布尔值")

    @property
    def enabled(self):
        return bool(self.identity_fields or self.relationship_fields)

    def allows(self, kind, fields):
        allowed = self.identity_fields if kind == "identity" else self.relationship_fields
        return bool(fields) and all(field["name"] in allowed for field in fields)


class CoreProposals:
    def __init__(self, session):
        self.session = session
        self.runtime, self.binding = session.runtime, session.binding
        self.conn = self.runtime.db.connection
        self.policy = session.adapter.core_policy
        self.scope_key = _scope_key(self.binding.scope)

    def reader(self):
        # 当前输入可作为修改请求的依据，不能沿用历史搜索的本轮排除规则。
        return ViewReader(self.runtime, self.binding, self.session.clock)

    @staticmethod
    def snapshot(view):
        return {"kind": view.kind, "target": view.target, "revision": view.revision}

    def create(self, args):
        if not self.policy.enabled:
            raise AdapterError("UNSUPPORTED", "宿主未开放核心建议")
        if self.session.current_event_id is None:
            raise AdapterError("UNAVAILABLE", "核心建议需要宿主先保存本轮真实用户消息")
        reader = self.reader()
        request = reader.load("event", self.session.current_event_id)
        actual = self.runtime.db.repositories.events.get(request.target)
        if (request.data["speaker"] != "user" or actual.conversation_id != self.binding.conversation_id
                or request.data["text"] != actual.content):
            raise forbidden()
        target = self.session.registry.resolve([args["target_ref"]], reader)[0]
        if target.kind != "core_version":
            raise AdapterError("INVALID_ARGUMENT", "target_ref 必须为当前核心引用")
        fields = sorted(args["fields"], key=lambda field: field["name"])
        if not self.policy.allows(target.target, fields):
            raise AdapterError("INVALID_ARGUMENT", "建议字段不在宿主允许目录内")
        evidence = {(request.kind, request.target): self.snapshot(request)}
        stored_fields = []
        for field in fields:
            views = self.session.registry.resolve(field["evidence_refs"], reader)
            # 核心与摘要不能为自己背书；首版只接受真实用户事件或用户事实。
            if any(view.kind not in ("event", "memory") or view.data["speaker"] != "user"
                   or (view.kind == "memory" and (view.data["modality"] != "occurred"
                       or view.data["epistemic_status"] == "inferred")) for view in views):
                raise AdapterError("INVALID_ARGUMENT", "字段依据必须是可验证的用户事件或事实")
            sources = {request.target, *(view.target for view in views)}
            evidence.update({(view.kind, view.target): self.snapshot(view) for view in views})
            stored_fields.append({"name": field["name"], "value": field["value"], "sources": sorted(sources)})
        evidence = sorted(evidence.values(), key=lambda value: (value["kind"], value["target"]))
        material = [self.scope_key, request.target, target.target, target.revision, stored_fields, evidence, args["reason"]]
        key = hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()
        previous = self.conn.execute("SELECT * FROM core_proposals WHERE dedupe_key=?", (key,)).fetchone()
        if previous is not None:
            # 重试不会创建另一份候选，也不会延长有效期或再次应用。
            if previous["state"] != "pending_validation" or self.check(previous):
                raise AdapterError("STALE_REFERENCE", "该建议已终结，请由宿主查询回执")
            return self.proposed(previous["proposal_id"])
        now = self.session.clock.now()
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM core_proposals WHERE scope_key=? AND state='pending_validation' AND expires_at>?",
            (self.scope_key, to_utc_iso(now)),
        ).fetchone()[0]
        if pending >= 64:
            raise AdapterError("UNAVAILABLE", "当前绑定的待审核心建议已达上限")
        proposal_id = "proposal_" + secrets.token_urlsafe(18)
        self.conn.execute(
            "INSERT INTO core_proposals(proposal_id,operation_id,scope_key,dedupe_key,request_event_id,"
            "target_kind,expected_version,target_revision,fields_json,evidence_json,reason,state,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending_validation',?,?)",
            (proposal_id, "operation_" + secrets.token_urlsafe(18), self.scope_key, key, request.target,
             target.target, target.data["record_version"], target.revision, dumps(stored_fields), dumps(evidence),
             args["reason"], to_utc_iso(now), to_utc_iso(now + timedelta(seconds=self.policy.ttl_seconds))),
        )
        return self.proposed(proposal_id)

    @staticmethod
    def proposed(proposal_id):
        return {"kind": "proposal", "proposal_id": proposal_id, "state": "pending_validation",
                "operation": "update_core", "applied": False}

    def load(self, proposal_id):
        text(proposal_id, "proposal_id", 100)
        row = self.conn.execute("SELECT * FROM core_proposals WHERE proposal_id=? AND scope_key=?",
                                (proposal_id, self.scope_key)).fetchone()
        if row is None:
            raise forbidden()
        return row

    def check(self, row):
        if row["expires_at"] <= to_utc_iso(self.session.clock.now()):
            return "EXPIRED"
        if not self.policy.allows(row["target_kind"], json.loads(row["fields_json"])):
            return "POLICY_CHANGED"
        reader = self.reader()
        try:
            core = reader.load("core_version", row["target_kind"])
            if core.data["record_version"] != row["expected_version"]:
                return "VERSION_CONFLICT"
            if core.revision != row["target_revision"]:
                return "STALE_REFERENCE"
            for source in json.loads(row["evidence_json"]):
                if reader.load(source["kind"], source["target"]).revision != source["revision"]:
                    return "STALE_REFERENCE"
        except AdapterError:
            return "STALE_REFERENCE"
        return None

    def finish(self, row, state, code, *, reviewer=None, reason=None, version=None):
        self.conn.execute(
            "UPDATE core_proposals SET state=?,decision_code=?,reviewer=?,decision_reason=?,decided_at=?,applied_version=? "
            "WHERE proposal_id=? AND state='pending_validation'",
            (state, code, reviewer, reason, to_utc_iso(self.session.clock.now()), version, row["proposal_id"]),
        )
        return self.load(row["proposal_id"])

    def inspect(self, proposal_id):
        row = self.load(proposal_id)
        if row["state"] == "pending_validation":
            code = self.check(row)
            if code:
                row = self.finish(row, "invalidated", code)
        result = {"proposal_id": row["proposal_id"], "state": row["state"], "receipt": self.receipt(row),
                  "applied_version": row["applied_version"], "reviewer": row["reviewer"],
                  "decision_code": row["decision_code"], "decided_at": row["decided_at"]}
        # 失效/遗忘后不再通过检查接口返回旧 patch 或旧理由。
        if row["state"] == "pending_validation":
            result.update(core_kind=row["target_kind"], expected_version=row["expected_version"],
                          fields=json.loads(row["fields_json"]), reason=row["reason"],
                          user_event_id=row["request_event_id"], expires_at=row["expires_at"])
        return result

    def decide(self, proposal_id, *, approve, user_event_id, reviewer, reason):
        text(user_event_id, "user_event_id", 200)
        text(reviewer, "reviewer", 200)
        text(reason, "审批原因", 1000)
        row = self.load(proposal_id)
        if row["request_event_id"] != user_event_id:
            raise forbidden()
        terminal = "applied" if approve else "rejected"
        if row["state"] != "pending_validation":
            if row["state"] == "invalidated":
                return self.receipt(row)
            if row["state"] != terminal or row["reviewer"] != reviewer or row["decision_reason"] != reason:
                raise AdapterError("INVALID_ARGUMENT", "终态审批不可改写；重试需保持原决定与审批信息")
            return self.receipt(row)
        if not approve:
            return self.receipt(self.finish(row, "rejected", "REJECTED", reviewer=reviewer, reason=reason))
        code = self.check(row)
        if code:
            return self.receipt(self.finish(row, "invalidated", code, reviewer=reviewer, reason=reason))
        fields = json.loads(row["fields_json"])
        try:
            core = self.runtime.apply_core_update(
                self.binding.scope, CoreKind(row["target_kind"]),
                {field["name"]: field["value"] for field in fields}, row["reason"],
                expected_version=row["expected_version"],
                field_evidence_ids={field["name"]: field["sources"] for field in fields},
            )
        except (MemorySuppressed, VersionConflict) as exc:
            code = "VERSION_CONFLICT" if isinstance(exc, VersionConflict) else "STALE_REFERENCE"
            return self.receipt(self.finish(row, "invalidated", code, reviewer=reviewer, reason=reason))
        return self.receipt(self.finish(row, "applied", "APPLIED", reviewer=reviewer, reason=reason,
                                        version=core.core_version))

    @staticmethod
    def receipt(row):
        applied = row["state"] == "applied"
        return {"kind": "change_receipt", "operation_id": row["operation_id"], "proposal_id": row["proposal_id"],
                "operation": "update_core", "state": "applied" if applied else "not_applied", "applied": applied,
                "effective_at": row["decided_at"] if applied else None, "erasure_level": "not_applicable",
                "affected": {"events": 0, "memories": 0, "core_fields": len(json.loads(row["fields_json"])) if applied else 0,
                             "activities": 0},
                "remaining_work": [] if applied else [row["decision_code"] or "PENDING_APPROVAL"]}


class ChangeProposals:
    """事实/遗忘/检查点的统一提议层。

    该层借鉴核心候选的版本快照和审批幂等，但把执行动作限制在 runtime
    已有的安全 API；模型永远只会得到 ``proposed``，审批者必须重新确认
    本轮用户事件和引用仍然有效。
    """

    def __init__(self, session):
        self.session = session
        self.runtime, self.binding = session.runtime, session.binding
        self.conn = self.runtime.db.connection
        self.scope_key = _scope_key(self.binding.scope)

    def _reader(self):
        return ViewReader(self.runtime, self.binding, self.session.clock)

    def _request(self, reader):
        if self.session.current_event_id is None:
            raise AdapterError("UNAVAILABLE", "提议需要宿主先保存本轮真实用户消息")
        view = reader.load("event", self.session.current_event_id)
        event = self.runtime.db.repositories.events.get(self.session.current_event_id)
        if (event is None or event.speaker != "user" or event.conversation_id != self.binding.conversation_id
                or view.data.get("text") != event.content):
            raise forbidden()
        return view, event

    @staticmethod
    def _snapshot(view):
        return {"kind": view.kind, "target": view.target, "revision": view.revision}

    def create(self, tool_name, args):
        if tool_name == "memory_checkpoint_propose" and not self.session.adapter.core_policy.checkpoint_proposals:
            raise AdapterError("UNSUPPORTED", "宿主未开放检查点建议")
        if tool_name == "memory_request_change" and not self.session.adapter.core_policy.change_proposals:
            raise AdapterError("UNSUPPORTED", "宿主未开放记忆变更建议")
        reader = self._reader()
        request_view, request_event = self._request(reader)
        if tool_name == "memory_checkpoint_propose":
            activity = self.session.registry.resolve([args["activity_ref"]], reader, activity=True)[0]
            evidence_views = self._resolve_evidence(args["evidence_refs"], reader)
            payload = {"expected_version": args["expected_version"], "goal": args["goal"],
                       "open_loops": args["open_loops"], "checkpoint_note": args["checkpoint_note"]}
            operation = "checkpoint"
            targets = [self._snapshot(activity)]
        else:
            operation = args["operation"]
            target_refs = list(args["target_refs"])
            if operation == "forget_scope":
                if target_refs or args["replacement_text"] is not None:
                    raise AdapterError("INVALID_ARGUMENT", "forget_scope 不接受 target_refs 或 replacement_text")
                targets, evidence_views = [], []
            else:
                if operation == "forget_event" and len(target_refs) != 1:
                    raise AdapterError("INVALID_ARGUMENT", "forget_event 必须指定一个事件引用")
                if operation in ("correct_fact", "forget_fact") and not target_refs:
                    raise AdapterError("INVALID_ARGUMENT", "该操作至少需要一个记忆引用")
                views = self.session.registry.resolve(target_refs, reader)
                expected_kind = "event" if operation == "forget_event" else "memory"
                if any(view.kind != expected_kind for view in views):
                    raise AdapterError("INVALID_ARGUMENT", f"{operation} 的目标引用类型不正确")
                targets = [self._snapshot(view) for view in views]
                evidence_views = self._resolve_evidence(args["evidence_refs"], reader)
            payload = {"replacement_text": args["replacement_text"]}
        for view in evidence_views:
            if view.kind not in ("event", "memory") or view.data.get("speaker") not in ("user", None):
                raise AdapterError("INVALID_ARGUMENT", "依据必须来自当前作用域的用户事件或记忆")
        evidence = [self._snapshot(request_view), *[self._snapshot(view) for view in evidence_views]]
        material = [self.scope_key, request_view.target, operation, targets, evidence, payload,
                    args.get("reason", "")]
        key = hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()
        previous = self.conn.execute("SELECT * FROM change_proposals WHERE dedupe_key=?", (key,)).fetchone()
        if previous is not None:
            return self._public(previous)
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM change_proposals WHERE scope_key=? AND state='pending_validation' AND expires_at>?",
            (self.scope_key, to_utc_iso(self.session.clock.now())),
        ).fetchone()[0]
        if pending >= 64:
            raise AdapterError("UNAVAILABLE", "当前绑定的待审核变更建议已达上限")
        now = self.session.clock.now()
        proposal_id = "change_" + secrets.token_urlsafe(18)
        expires = now + timedelta(seconds=self.session.adapter.core_policy.ttl_seconds)
        self.conn.execute(
            """INSERT INTO change_proposals (
                proposal_id, operation_id, scope_key, dedupe_key, request_event_id,
                operation, target_refs_json, evidence_json, payload_json, reason,
                state, created_at, expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,'pending_validation',?,?)""",
            (proposal_id, "operation_" + secrets.token_urlsafe(18), self.scope_key, key,
             request_view.target, operation, dumps(targets), dumps(evidence), dumps(payload),
             args.get("reason", args.get("checkpoint_note", "")), to_utc_iso(now), to_utc_iso(expires)),
        )
        return {"kind": "proposal", "proposal_id": proposal_id, "operation": operation,
                "state": "pending_validation", "applied": False}

    def _resolve_evidence(self, refs, reader):
        if not refs:
            return []
        return self.session.registry.resolve(list(dict.fromkeys(refs)), reader)

    def _load(self, proposal_id):
        text(proposal_id, "proposal_id", 150)
        row = self.conn.execute("SELECT * FROM change_proposals WHERE proposal_id=? AND scope_key=?",
                                (proposal_id, self.scope_key)).fetchone()
        if row is None:
            raise forbidden()
        return row

    def _check(self, row):
        if row["expires_at"] <= to_utc_iso(self.session.clock.now()):
            return "EXPIRED"
        reader = self._reader()
        try:
            for source in json.loads(row["target_refs_json"]) + json.loads(row["evidence_json"]):
                view = reader.load(source["kind"], source["target"])
                if view.revision != source["revision"]:
                    return "STALE_REFERENCE"
        except AdapterError:
            return "STALE_REFERENCE"
        return None

    @staticmethod
    def _public(row):
        result = {"kind": "proposal", "proposal_id": row["proposal_id"],
                  "operation": row["operation"], "state": row["state"],
                  "applied": row["state"] == "applied"}
        if row["state"] == "pending_validation":
            result.update(target_refs=json.loads(row["target_refs_json"]),
                          reason=row["reason"], expires_at=row["expires_at"],
                          user_event_id=row["request_event_id"])
        return result

    def inspect(self, proposal_id):
        row = self._load(proposal_id)
        if row["state"] == "pending_validation":
            code = self._check(row)
            if code:
                self._finish(row, "invalidated", code, None, None)
                row = self._load(proposal_id)
        return self._public(row) | {
            "operation_id": row["operation_id"], "decision_code": row["decision_code"],
            "reviewer": row["reviewer"], "decided_at": row["decided_at"],
        }

    def _finish(self, row, state, code, reviewer, reason, receipt=None):
        self.conn.execute(
            "UPDATE change_proposals SET state=?,decision_code=?,reviewer=?,decision_reason=?,decided_at=?,receipt_json=? WHERE proposal_id=? AND state='pending_validation'",
            (state, code, reviewer, reason, to_utc_iso(self.session.clock.now()),
             dumps(receipt) if receipt is not None else None, row["proposal_id"]),
        )
        return self._load(row["proposal_id"])

    def decide(self, proposal_id, *, approve, user_event_id, reviewer, reason):
        text(user_event_id, "user_event_id", 200)
        text(reviewer, "reviewer", 200)
        text(reason, "审批原因", 1000)
        row = self._load(proposal_id)
        if row["request_event_id"] != user_event_id:
            raise forbidden()
        if row["state"] != "pending_validation":
            if row["state"] == "invalidated":
                return self._receipt(row)
            if row["state"] != ("applied" if approve else "rejected") or row["reviewer"] != reviewer or row["decision_reason"] != reason:
                raise AdapterError("INVALID_ARGUMENT", "终态审批不可改写；重试需保持原决定与审批信息")
            return self._receipt(row)
        if not approve:
            return self._receipt(self._finish(row, "rejected", "REJECTED", reviewer, reason))
        code = self._check(row)
        if code:
            return self._receipt(self._finish(row, "invalidated", code, reviewer, reason))
        operation = row["operation"]
        targets = json.loads(row["target_refs_json"])
        payload = json.loads(row["payload_json"])
        try:
            if operation == "checkpoint":
                state = {key: payload[key] for key in ("goal", "open_loops") if key in payload}
                # checkpoint_note 保留在提议记录中，不把自由文本伪装成活动正文。
                version = self.runtime.checkpoint(targets[0]["target"], state,
                                                  payload["expected_version"])
                receipt = {"kind": "change_receipt", "operation": operation, "applied": True,
                           "activity_version": version, "remaining_work": []}
            elif operation == "forget_scope":
                result = self.runtime.forget_scope(self.binding.scope, reason=row["reason"])
                receipt = {"kind": "change_receipt", "operation": operation, "applied": True,
                           "erasure_level": result.erasure_level, "affected": len(result.deleted_ids),
                           "remaining_work": result.residuals}
            elif operation == "forget_event":
                result = self.runtime.forget_event(targets[0]["target"], scope=self.binding.scope, reason=row["reason"])
                receipt = {"kind": "change_receipt", "operation": operation, "applied": True,
                           "erasure_level": result.erasure_level, "affected": len(result.deleted_ids),
                           "remaining_work": result.residuals}
            elif operation == "forget_fact":
                result = self.runtime.forget_fact(ForgetSelector(
                    memory_ids=[target["target"] for target in targets], reason=row["reason"], scope=self.binding.scope))
                receipt = {"kind": "change_receipt", "operation": operation, "applied": True,
                           "erasure_level": result.erasure_level, "affected": len(result.deleted_ids),
                           "remaining_work": result.residuals}
            else:  # correct_fact: safe first step is explicit retraction; replacement is retained in receipt only.
                result = self.runtime.correct(targets[0]["target"], Correction(
                    kind="fix", statement=payload.get("replacement_text") or row["reason"]))
                receipt = {"kind": "change_receipt", "operation": operation, "applied": True,
                           "erasure_level": "logical_suppressed", "affected": len(result.invalidated_derived),
                           "remaining_work": ["replacement must be separately confirmed and extracted"]}
        except Exception as exc:
            code = "VERSION_CONFLICT" if exc.__class__.__name__ == "VersionConflict" else "EXECUTION_FAILED"
            return self._receipt(self._finish(row, "invalidated", code, reviewer, reason))
        return self._receipt(self._finish(row, "applied", "APPLIED", reviewer, reason, receipt))

    def _receipt(self, row):
        if row["receipt_json"]:
            return json.loads(row["receipt_json"])
        return {"kind": "change_receipt", "operation_id": row["operation_id"],
                "proposal_id": row["proposal_id"], "operation": row["operation"],
                "state": row["state"], "applied": row["state"] == "applied",
                "remaining_work": [row["decision_code"] or "PENDING_APPROVAL"]}
