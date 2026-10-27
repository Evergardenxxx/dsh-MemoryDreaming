"""可纠正的场景化陪伴偏好：候选、用户确认、版本与撤销。

本模块不判断人的心理状态，也不把模型猜测提升为事实。场景由可信宿主
显式选择；确认需要绑定真实用户事件。所有写入复用 Runtime 的事务和连接，
读取重新检查来源，因此换窗口、遗忘或纠错不能通过旧偏好复活敏感内容。
"""

from __future__ import annotations

from datetime import timedelta
import json
import unicodedata

from .assertions import normalize
from .clock import to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .life_evidence import LifeEvidence, dumps
from .models import Scope, new_id
from .storage.repository import _scope_key


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 必须为非空短字符串，最多 {maximum} 字符")
    return value.strip()


def _label(value, name):
    return unicodedata.normalize("NFKC", _text(value, name, 80)).casefold()


def _natural(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须为非负整数")
    return value


class CompanionshipService:
    """可信 SDK 的相处偏好服务；实例本身不启动模型或注册工具。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.db, self.conn = runtime.db, runtime.db.connection
        self.evidence = LifeEvidence(runtime)

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, Scope):
            raise ValueError("scope 必须由可信宿主绑定")
        return (_scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id,
                scope.activity_id, scope.domain)

    def _fingerprint(self, value):
        return self.db.repositories.forgetting.fingerprint(dumps(value))

    def _head(self, scope, key, context):
        return self.conn.execute(
            "SELECT * FROM companionship_preferences WHERE scope_key=? AND preference_key=? AND context=?",
            (_scope_key(scope), key, context),
        ).fetchone()

    def _proposal(self, scope, proposal_id):
        self._scope(scope)
        _text(proposal_id, "proposal_id", 200)
        row = self.conn.execute(
            "SELECT * FROM companionship_proposals WHERE scope_key=? AND proposal_id=?",
            (_scope_key(scope), proposal_id),
        ).fetchone()
        if row is None:
            raise ScopeNotFound("找不到绑定范围内的相处偏好候选")
        return row

    def _user_confirmation(self, scope, user_event_id):
        _text(user_event_id, "user_event_id", 200)
        event = self.db.repositories.events.get(user_event_id)
        if event is None or event.scope != scope or event.speaker != "user":
            raise ScopeNotFound("确认必须来自相同完整作用域的真实用户事件")
        return self.evidence.capture(scope, [user_event_id], require_user=True)

    def _valid(self, scope, row):
        return (row["state"] == "confirmed" and row["value_json"] is not None
                and self.evidence.valid(scope, json.loads(row["evidence_json"]))
                and self.evidence.valid(scope, json.loads(row["approval_event_json"]), require_user=True))

    def _fresh_proposal(self, scope, row):
        if row["state"] != "pending":
            return row
        if (row["expires_at"] <= to_utc_iso(self.runtime.clock.now())
                or not self.evidence.valid(scope, json.loads(row["evidence_json"]))):
            # 候选过期或来源撤销时不通过 inspection 返回旧 value/reason。
            self.conn.execute(
                "UPDATE companionship_proposals SET state='invalidated',value_json=NULL,reason='' WHERE proposal_id=?",
                (row["proposal_id"],),
            )
            return self._proposal(scope, row["proposal_id"])
        return row

    @staticmethod
    def _public(row):
        result = {"proposal_id": row["proposal_id"], "state": row["state"],
                  "preference_key": row["preference_key"], "context": row["context"],
                  "expected_version": row["base_version"], "applied_version": row["applied_version"]}
        if row["state"] == "pending":
            result.update(value=json.loads(row["value_json"]), reason=row["reason"],
                          evidence=json.loads(row["evidence_json"]), expires_at=row["expires_at"],
                          epistemic_status="pending_confirmation")
        return result

    def propose(self, scope, key, value, *, context="default", evidence, reason, idempotency_key,
                ttl_seconds=86400):
        """创建待确认建议。模型只可提出建议，不能通过该调用改变相处方式。"""
        self._scope(scope)
        key, context = _label(key, "key"), _label(context, "context")
        value, reason = _text(value, "value", 1000), _text(reason, "reason", 500)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 604800:
            raise ValueError("ttl_seconds 必须为 1..604800")
        with self.db.transaction():
            refs = self.evidence.capture(scope, evidence)
            content_key = self._fingerprint([key, context, normalize(value), refs])
            request_key = self._fingerprint([content_key, reason, ttl_seconds])
            previous = self.conn.execute(
                "SELECT * FROM companionship_proposals WHERE scope_key=? AND idempotency_key=?",
                (_scope_key(scope), idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["request_key"] != request_key:
                    raise ValueError("相同候选幂等键不能对应不同请求")
                return self._public(self._fresh_proposal(scope, previous))
            previous = self.conn.execute(
                "SELECT * FROM companionship_proposals WHERE scope_key=? AND content_key=? ORDER BY rowid DESC LIMIT 1",
                (_scope_key(scope), content_key),
            ).fetchone()
            if previous is not None:
                # 拒绝也占据内容签名：换理由/幂等键不能重复施压。用户提供新的
                # 确认依据后才会形成不同内容签名，允许其以后改变选择。
                return self._public(self._fresh_proposal(scope, previous))
            head = self._head(scope, key, context)
            now = self.runtime.clock.now()
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM companionship_proposals WHERE scope_key=? AND state='pending' AND expires_at>?",
                (_scope_key(scope), to_utc_iso(now)),
            ).fetchone()[0]
            if pending >= 64:
                raise ValueError("当前绑定最多保留 64 项未过期待确认偏好")
            if head is None and self.conn.execute(
                "SELECT COUNT(*) FROM companionship_preferences WHERE scope_key=?", (_scope_key(scope),),
            ).fetchone()[0] >= 256:
                raise ValueError("当前绑定的场景偏好已达 256 项，需要可信宿主先整理")
            proposal_id = new_id("companion")
            self.conn.execute(
                "INSERT INTO companionship_proposals(proposal_id,scope_key,user_id,character_id,relationship_id,activity_id,domain,"
                "preference_key,context,value_json,evidence_json,reason,base_version,content_key,request_key,idempotency_key,"
                "state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (proposal_id, *self._scope(scope), key, context, dumps(value), dumps(refs), reason,
                 head["version"] if head else 0, content_key, request_key, idempotency_key,
                 to_utc_iso(now), to_utc_iso(now+timedelta(seconds=ttl_seconds))),
            )
            self.db.repositories.journal.append("companionship.proposed", scope=scope, entity_id=proposal_id,
                                                payload={"key": key, "context": context}, now=now)
            return self._public(self._proposal(scope, proposal_id))

    def inspect(self, scope, proposal_id):
        with self.db.transaction():
            return self._public(self._fresh_proposal(scope, self._proposal(scope, proposal_id)))

    def _receipt(self, scope, receipt_id, request_key):
        row = self.conn.execute(
            "SELECT * FROM companionship_receipts WHERE scope_key=? AND receipt_id=?", (_scope_key(scope), receipt_id),
        ).fetchone()
        if row is None:
            return None
        if row["request_key"] != request_key:
            raise ValueError("终态决定不可改写；重试须保持原审批参数")
        return json.loads(row["receipt_json"])

    def _save_receipt(self, scope, receipt_id, request_key, receipt):
        self.conn.execute("INSERT INTO companionship_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*self._scope(scope), receipt_id, request_key, dumps(receipt), to_utc_iso(self.runtime.clock.now())))
        return receipt

    def _write_head(self, scope, key, context, *, version, state, value, evidence, confirmation, proposal_id, reason):
        now = to_utc_iso(self.runtime.clock.now())
        values = (*self._scope(scope), key, context, version, state, dumps(value) if value is not None else None,
                  dumps(evidence), dumps(confirmation), proposal_id, now)
        self.conn.execute(
            "INSERT INTO companionship_preferences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_key,preference_key,context) DO UPDATE SET version=excluded.version,state=excluded.state,"
            "value_json=excluded.value_json,evidence_json=excluded.evidence_json,approval_event_json=excluded.approval_event_json,"
            "source_proposal_id=excluded.source_proposal_id,updated_at=excluded.updated_at", values,
        )
        self.conn.execute("INSERT INTO companionship_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (*values[:-1], reason, now))

    def confirm(self, scope, proposal_id, *, user_event_id, reviewer, reason, expected_version):
        """可信宿主已核验用户意思后调用；真实用户事件只是授权证据，不代替意图核验。"""
        reviewer, reason = _text(reviewer, "reviewer", 200), _text(reason, "reason", 500)
        _natural(expected_version, "expected_version")
        with self.db.transaction():
            row = self._proposal(scope, proposal_id)
            decision_key = self._fingerprint(["confirm", proposal_id, user_event_id, reviewer, reason, expected_version])
            previous = self._receipt(scope, proposal_id, decision_key)
            if previous is not None:
                return previous
            row = self._fresh_proposal(scope, row)
            if row["state"] != "pending":
                raise MemorySuppressed("候选已拒绝、失效或终结，不能重新批准")
            confirmation = self._user_confirmation(scope, user_event_id)
            head = self._head(scope, row["preference_key"], row["context"])
            actual_version = head["version"] if head else 0
            if expected_version != actual_version or row["base_version"] != actual_version:
                raise VersionConflict(expected_version, actual_version, "相处偏好")
            version = actual_version+1
            self._write_head(scope, row["preference_key"], row["context"], version=version, state="confirmed",
                value=json.loads(row["value_json"]), evidence=json.loads(row["evidence_json"]),
                confirmation=confirmation, proposal_id=proposal_id, reason=reason)
            self.conn.execute("UPDATE companionship_proposals SET state='confirmed',approval_event_json=?,decision_key=?,"
                              "applied_version=?,decided_at=? WHERE proposal_id=?",
                (dumps(confirmation), decision_key, version, to_utc_iso(self.runtime.clock.now()), proposal_id))
            receipt = {"kind": "companionship_receipt", "proposal_id": proposal_id, "state": "confirmed", "applied": True,
                       "key": row["preference_key"], "context": row["context"], "version": version}
            self._save_receipt(scope, proposal_id, decision_key, receipt)
            self.db.repositories.journal.append("companionship.confirmed", scope=scope, entity_id=proposal_id,
                payload={"key": row["preference_key"], "context": row["context"], "version": version}, now=self.runtime.clock.now())
            return receipt

    def reject(self, scope, proposal_id, *, user_event_id, reviewer, reason):
        reviewer, reason = _text(reviewer, "reviewer", 200), _text(reason, "reason", 500)
        with self.db.transaction():
            row = self._proposal(scope, proposal_id)
            decision_key = self._fingerprint(["reject", proposal_id, user_event_id, reviewer, reason])
            previous = self._receipt(scope, proposal_id, decision_key)
            if previous is not None:
                return previous
            self._user_confirmation(scope, user_event_id)
            if row["state"] != "pending":
                raise ValueError("只能拒绝仍待确认的候选")
            self.conn.execute("UPDATE companionship_proposals SET state='rejected',value_json=NULL,reason='',decision_key=?,decided_at=? "
                              "WHERE proposal_id=?", (decision_key, to_utc_iso(self.runtime.clock.now()), proposal_id))
            receipt = {"kind": "companionship_receipt", "proposal_id": proposal_id, "state": "rejected", "applied": False}
            self._save_receipt(scope, proposal_id, decision_key, receipt)
            self.db.repositories.journal.append("companionship.rejected", scope=scope, entity_id=proposal_id,
                                                payload={}, now=self.runtime.clock.now())
            return receipt

    def feedback(self, scope, proposal_id, *, approved, user_event_id, reviewer, reason, expected_version=0):
        if not isinstance(approved, bool):
            raise ValueError("approved 必须为布尔值")
        if approved:
            return self.confirm(scope, proposal_id, user_event_id=user_event_id, reviewer=reviewer,
                                reason=reason, expected_version=expected_version)
        return self.reject(scope, proposal_id, user_event_id=user_event_id, reviewer=reviewer, reason=reason)

    def correct(self, scope, key, value, **kwargs):
        """纠正沿用新候选→用户确认流程，不凭一个方法名绕过审批。"""
        return self.propose(scope, key, value, **kwargs)

    def revoke(self, scope, key, *, context="default", user_event_id, reviewer, reason, expected_version, idempotency_key):
        key, context = _label(key, "key"), _label(context, "context")
        reviewer, reason = _text(reviewer, "reviewer", 200), _text(reason, "reason", 500)
        _natural(expected_version, "expected_version")
        request_id = "revoke:"+_text(idempotency_key, "idempotency_key", 180)
        request_key = self._fingerprint([key, context, user_event_id, reviewer, reason, expected_version])
        with self.db.transaction():
            self._scope(scope)
            previous = self._receipt(scope, request_id, request_key)
            if previous is not None:
                return previous
            confirmation = self._user_confirmation(scope, user_event_id)
            head = self._head(scope, key, context)
            actual = head["version"] if head else 0
            if expected_version != actual:
                raise VersionConflict(expected_version, actual, "相处偏好")
            if head is None:
                raise ScopeNotFound("没有可撤销的相处偏好")
            version = actual+1
            # 撤销成为新的 head，不回退任何历史值。场景 tombstone 同样遮住
            # default 回退，避免用户刚撤销某情境表达便从默认值再次补回来。
            self._write_head(scope, key, context, version=version, state="revoked", value=None,
                             evidence=[], confirmation=confirmation, proposal_id=None, reason=reason)
            receipt = {"kind": "companionship_receipt", "state": "revoked", "applied": True,
                       "key": key, "context": context, "version": version}
            self._save_receipt(scope, request_id, request_key, receipt)
            self.db.repositories.journal.append("companionship.revoked", scope=scope,
                payload={"key": key, "context": context, "version": version}, now=self.runtime.clock.now())
            return receipt

    def read_for_context(self, scope, context="default", *, limit=12):
        """只返回当前已确认且来源有效的值，具体场景优先于 default。"""
        self._scope(scope)
        context = _label(context, "context")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32:
            raise ValueError("limit 必须为 1..32")
        with self.db.transaction(write=False):
            rows = self.conn.execute(
                "SELECT * FROM companionship_preferences WHERE scope_key=? AND context IN ('default',?) "
                "ORDER BY preference_key,CASE WHEN context='default' THEN 0 ELSE 1 END",
                (_scope_key(scope), context),
            ).fetchall()
            selected = {row["preference_key"]: row for row in rows}
            return [self._value(row) for row in selected.values() if self._valid(scope, row)][:limit]

    @staticmethod
    def _value(row):
        return {"key": row["preference_key"], "context": row["context"], "value": json.loads(row["value_json"]),
                "version": row["version"], "state": "confirmed", "epistemic_status": "user_confirmed_preference",
                "evidence": json.loads(row["evidence_json"]), "confirmation": json.loads(row["approval_event_json"])}

    def list_confirmed(self, scope, *, context=None, limit=32):
        self._scope(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        where, args = "scope_key=?", [_scope_key(scope)]
        if context is not None:
            where += " AND context=?"; args.append(_label(context, "context"))
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT * FROM companionship_preferences WHERE "+where+
                                     " ORDER BY preference_key,context", args).fetchall()
            return [self._value(row) for row in rows if self._valid(scope, row)][:limit]

    def history(self, scope, key, *, context="default", limit=20):
        self._scope(scope)
        key, context = _label(key, "key"), _label(context, "context")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT * FROM companionship_history WHERE scope_key=? AND preference_key=? AND context=? "
                "ORDER BY version DESC LIMIT ?", (_scope_key(scope), key, context, limit)).fetchall()
            return [{"version": row["version"], "state": row["state"], "created_at": row["created_at"],
                     "available": self._valid(scope, row), "value": json.loads(row["value_json"]) if self._valid(scope, row) else None}
                    for row in rows]

    def invalidate_stale(self, scope=None):
        """供纠错/遗忘提交链调用；读取本身也保护来源，不依赖调用此清理。"""
        if scope is not None:
            self._scope(scope)
        clause, args = ("scope_key=?", (_scope_key(scope),)) if scope is not None else ("1", ())
        changed = {"proposals": 0, "preferences": 0, "history": 0}
        with self.db.transaction():
            for row in self.conn.execute("SELECT * FROM companionship_proposals WHERE "+clause+" AND state='pending'", args).fetchall():
                actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                if self._fresh_proposal(actual, row)["state"] == "invalidated":
                    changed["proposals"] += 1
            for table, key in (("companionship_preferences", "preferences"), ("companionship_history", "history")):
                rows = self.conn.execute(f"SELECT rowid,* FROM {table} WHERE "+clause+" AND state='confirmed'", args).fetchall()
                for row in rows:
                    actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                    if not self._valid(actual, row):
                        self.conn.execute(f"UPDATE {table} SET state='invalidated',value_json=NULL WHERE rowid=?", (row["rowid"],))
                        changed[key] += 1
            return changed
