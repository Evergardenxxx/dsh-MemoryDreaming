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
from .models import CoreKind
from .references import ViewReader, forbidden
from .storage.repository import _scope_key


@dataclass(frozen=True)
class CoreProposalPolicy:
    """宿主允许模型建议的字段；默认全部关闭，不代表自动批准这些字段。"""

    identity_fields: tuple[str, ...] = ()
    relationship_fields: tuple[str, ...] = ()
    ttl_seconds: int = 86400

    def __post_init__(self):
        for name in ("identity_fields", "relationship_fields"):
            fields = getattr(self, name)
            if not isinstance(fields, (tuple, list)) or len(fields) > 32:
                raise ValueError("字段白名单必须是最多 32 项的显式列表")
            for value in fields:
                text(value, "字段名", 100)
            object.__setattr__(self, name, tuple(dict.fromkeys(fields)))
        integer(self.ttl_seconds, "ttl_seconds", 1, 604800)

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
