"""有来源、版本与用户确认的角色表达成长，保护事实与人格核心边界。"""

from datetime import timedelta
import json

from .assertions import normalize
from .clock import ensure_utc, from_utc_iso, to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .life_evidence import LifeEvidence, dumps
from .models import Scope, new_id
from .storage.repository import _scope_key


GROWTH_FIELDS = frozenset(("response_style", "storytelling", "interaction_habit", "support_style"))


def short(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 必须为非空短文本（最多 {maximum} 字符）")
    return value


def scope_values(scope):
    if not isinstance(scope, Scope):
        raise ValueError("scope 必须由可信宿主绑定")
    return (_scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id, scope.domain)


class GrowthService:
    """所有写入由可信宿主发起；LLM 只能产生待审提议。"""

    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db
        self.conn = runtime.db.connection
        self.evidence = LifeEvidence(runtime)

    def _fingerprint(self, value):
        return self.db.repositories.forgetting.fingerprint(dumps(value))

    def _fresh_proposal(self, scope, row):
        if row["state"] == "pending" and (
                row["expires_at"] <= to_utc_iso(self.runtime.clock.now())
                or row["source_epoch"] != self.db.repositories.forgetting.epoch(scope)
                or not self.evidence.valid(scope, json.loads(row["sources_json"]))):
            self.conn.execute("UPDATE growth_proposals SET state='invalidated',value='',reason='' WHERE proposal_id=?", (row["proposal_id"],))
            return self.conn.execute("SELECT * FROM growth_proposals WHERE proposal_id=?", (row["proposal_id"],)).fetchone()
        return row

    @staticmethod
    def _proposed(row):
        return {"proposal_id": row["proposal_id"], "state": row["state"], "applied": row["state"] == "applied"}

    def propose(self, scope, field, value, evidence, *, reason, request_id):
        values = scope_values(scope)
        if field not in GROWTH_FIELDS:
            raise ValueError("成长仅支持表达方式、讲故事风格、互动习惯和支持方式；身份称呼走核心审批")
        short(value, "value", 1000); short(reason, "reason", 500); short(request_id, "request_id", 200)
        request_key = self._fingerprint([field, value, evidence, reason])
        with self.db.transaction():
            old = self.conn.execute("SELECT * FROM growth_proposals WHERE scope_key=? AND request_id=?", (values[0], request_id)).fetchone()
            if old:
                if old["request_key"] != request_key:
                    raise ValueError("同一成长请求不能改写参数")
                return self._proposed(self._fresh_proposal(scope, old))
            refs = sorted(self.evidence.capture(scope, evidence), key=lambda ref: (ref["kind"], ref["target"], ref["revision"]))
            # 内容签名不包含理由或客户端请求 ID：换个措辞、重排依据或换
            # 幂等键不能绕过一次明确拒绝，也不会堆积相同待审建议。
            content_key = self._fingerprint([field, normalize(value), refs])
            old = self.conn.execute("SELECT * FROM growth_proposals WHERE scope_key=? AND content_key=? ORDER BY rowid DESC LIMIT 1",
                                    (values[0], content_key)).fetchone()
            if old:
                return self._proposed(self._fresh_proposal(scope, old))
            now = self.runtime.clock.now()
            pending = self.conn.execute("SELECT COUNT(*) FROM growth_proposals WHERE scope_key=? AND state='pending' AND expires_at>?",
                                        (values[0], to_utc_iso(now))).fetchone()[0]
            if pending >= 64:
                raise ValueError("当前绑定最多保留64项未过期成长候选")
            proposal = new_id("growth")
            self.conn.execute("INSERT INTO growth_proposals(proposal_id,scope_key,user_id,character_id,relationship_id,activity_id,domain,"
                "request_id,request_key,content_key,field,value,sources_json,source_epoch,reason,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal, *values, request_id, request_key, content_key, field, value, dumps(refs),
                 self.db.repositories.forgetting.epoch(scope), reason, to_utc_iso(now), to_utc_iso(now+timedelta(days=1))))
            return {"proposal_id": proposal, "state": "pending", "applied": False}

    def _receipt(self, scope, request_id, request_key):
        row = self.conn.execute("SELECT * FROM growth_receipts WHERE scope_key=? AND request_id=?", (_scope_key(scope), request_id)).fetchone()
        if row:
            if row["request_key"] != request_key:
                raise ValueError("同一成长审批幂等键不能对应不同请求")
            return {**json.loads(row["receipt_json"]), "duplicate": True}

    def inspect(self, scope, proposal_id):
        scope_values(scope)
        with self.db.transaction():
            row = self.conn.execute("SELECT * FROM growth_proposals WHERE proposal_id=? AND scope_key=?", (proposal_id, _scope_key(scope))).fetchone()
            if row is None:
                raise ScopeNotFound("成长提议不属于绑定范围")
            row = self._fresh_proposal(scope, row)
            result = self._proposed(row)
            if row["state"] == "pending":
                result.update(field=row["field"], value=row["value"], sources=json.loads(row["sources_json"]), reason=row["reason"], expires_at=row["expires_at"])
            return result

    def _user_confirmation(self, scope, event_id):
        event = self.db.repositories.events.get(event_id)
        if event is None or event.scope != scope or event.speaker != "user":
            raise ValueError("成长批准必须关联同一精确范围的用户确认事件")
        return self.evidence.capture(scope, [event_id], require_user=True)

    def approve(self, scope, proposal_id, *, user_event_id, reviewer, reason, expected_version,
                request_id, allow_recent=False):
        short(proposal_id, "proposal_id", 200); short(reviewer, "reviewer", 200)
        short(reason, "reason", 500); short(request_id, "request_id", 200)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0 or not isinstance(allow_recent, bool):
            raise ValueError("expected_version 或 allow_recent 无效")
        request_key = self._fingerprint([proposal_id, user_event_id, reviewer, reason, expected_version, allow_recent])
        values = scope_values(scope)
        with self.db.transaction():
            prior = self._receipt(scope, request_id, request_key)
            if prior:
                return prior
            row = self.conn.execute("SELECT * FROM growth_proposals WHERE proposal_id=? AND scope_key=?", (proposal_id, values[0])).fetchone()
            if row is None:
                raise ScopeNotFound("成长提议不属于绑定范围")
            if row["state"] != "pending":
                raise ValueError("提议已处理，需要读取原审批回执")
            row = self._fresh_proposal(scope, row)
            if row["state"] == "invalidated":
                return {"proposal_id": proposal_id, "state": "invalidated", "applied": False}
            confirmation = self._user_confirmation(scope, user_event_id)
            current = self.conn.execute("SELECT * FROM growth_values WHERE scope_key=? AND field=?", (values[0], row["field"])).fetchone()
            version = current["version"] if current else 0
            if version != expected_version:
                raise VersionConflict(expected_version, version, "角色成长")
            now = self.runtime.clock.now()
            if current and current["state"] == "active" and not allow_recent and from_utc_iso(current["updated_at"]) > ensure_utc(now)-timedelta(hours=24):
                raise ValueError("同一表达字段24小时内已有成长；明确纠正时可信宿主可指定 allow_recent")
            next_version = version+1
            self._write(scope, row["field"], next_version, row["value"], row["sources_json"], dumps(confirmation), "active", reviewer, reason)
            self.conn.execute("UPDATE growth_proposals SET state='applied' WHERE proposal_id=?", (proposal_id,))
            receipt = {"proposal_id": proposal_id, "field": row["field"], "version": next_version,
                       "state": "applied", "applied": True, "core_changed": False, "duplicate": False}
            self._save_receipt(scope, request_id, request_key, receipt)
            return receipt

    def _write(self, scope, field, version, value, sources, confirmation, state, reviewer, reason):
        values = scope_values(scope)
        now = to_utc_iso(self.runtime.clock.now())
        epoch = self.db.repositories.forgetting.epoch(scope)
        self.conn.execute("INSERT INTO growth_values VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key,field) "
            "DO UPDATE SET version=excluded.version,value=excluded.value,sources_json=excluded.sources_json,"
            "confirmation_json=excluded.confirmation_json,source_epoch=excluded.source_epoch,state=excluded.state,updated_at=excluded.updated_at",
            (*values, field, version, value, sources, confirmation, epoch, state, now))
        self.conn.execute("INSERT INTO growth_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, field, version, value, sources, confirmation, epoch, state, reviewer, reason, now))
        self.db.repositories.journal.append("growth."+state, scope=scope, entity_id=field,
            payload={"version": version}, now=self.runtime.clock.now())

    def _save_receipt(self, scope, request_id, key, receipt):
        self.conn.execute("INSERT INTO growth_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*scope_values(scope), request_id, key, dumps(receipt), to_utc_iso(self.runtime.clock.now())))

    def revoke(self, scope, field, *, user_event_id, reviewer, reason, expected_version, request_id):
        if field not in GROWTH_FIELDS:
            raise ValueError("未知成长字段")
        short(reviewer, "reviewer", 200); short(reason, "reason", 500); short(request_id, "request_id", 200)
        key = self._fingerprint(["revoke", field, user_event_id, reviewer, reason, expected_version])
        with self.db.transaction():
            prior = self._receipt(scope, request_id, key)
            if prior:
                return prior
            confirmation = self._user_confirmation(scope, user_event_id)
            row = self.conn.execute("SELECT * FROM growth_values WHERE scope_key=? AND field=?", (_scope_key(scope), field)).fetchone()
            if row is None:
                raise ScopeNotFound("没有该成长字段")
            if isinstance(expected_version, bool) or row["version"] != expected_version:
                raise VersionConflict(expected_version, row["version"], "角色成长")
            self._write(scope, field, row["version"]+1, "", "[]", dumps(confirmation), "revoked", reviewer, reason)
            receipt = {"field": field, "version": row["version"]+1, "state": "revoked", "applied": True, "duplicate": False}
            self._save_receipt(scope, request_id, key, receipt)
            return receipt

    def reject(self, scope, proposal_id, *, user_event_id, reviewer, reason, request_id):
        short(reviewer, "reviewer", 200); short(reason, "reason", 500); short(request_id, "request_id", 200)
        key = self._fingerprint(["reject", proposal_id, user_event_id, reviewer, reason])
        with self.db.transaction():
            prior = self._receipt(scope, request_id, key)
            if prior:
                return prior
            self._user_confirmation(scope, user_event_id)
            row = self.conn.execute("SELECT * FROM growth_proposals WHERE proposal_id=? AND scope_key=?", (proposal_id, _scope_key(scope))).fetchone()
            if row is None or row["state"] != "pending":
                raise ValueError("只能拒绝当前待审提议")
            self.conn.execute("UPDATE growth_proposals SET state='rejected',value='',reason='' WHERE proposal_id=?", (proposal_id,))
            result = {"proposal_id": proposal_id, "state": "rejected", "applied": False, "duplicate": False}
            self._save_receipt(scope, request_id, key, result)
            return result

    def profile(self, scope, limit=8):
        scope_values(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须为 1..20")
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT * FROM growth_values WHERE scope_key=? AND state='active' ORDER BY field LIMIT ?", (_scope_key(scope), limit)).fetchall()
            return [{"field": row["field"], "value": row["value"], "version": row["version"], "epistemic_status": "user_confirmed_style"}
                    for row in rows if row["source_epoch"] == self.db.repositories.forgetting.epoch(scope)
                    and self.evidence.valid(scope, json.loads(row["sources_json"]))
                    and self.evidence.valid(scope, json.loads(row["confirmation_json"]), require_user=True)]

    def history(self, scope, field):
        scope_values(scope)
        if field not in GROWTH_FIELDS:
            raise ValueError("未知成长字段")
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT * FROM growth_history WHERE scope_key=? AND field=? ORDER BY version DESC LIMIT 100", (_scope_key(scope), field)).fetchall()
            return [{"version": row["version"], "state": row["state"], "created_at": row["created_at"],
                     "value": row["value"] if (row["state"] == "active" and row["source_epoch"] == self.db.repositories.forgetting.epoch(scope)
                        and self.evidence.valid(scope, json.loads(row["sources_json"]))
                        and self.evidence.valid(scope, json.loads(row["confirmation_json"]), require_user=True)) else None} for row in rows]

    def invalidate_stale(self, scope=None):
        with self.db.transaction():
            query, args = (" AND scope_key=?", (_scope_key(scope),)) if scope else ("", ())
            for proposal in self.conn.execute("SELECT * FROM growth_proposals WHERE state='pending'"+query, args).fetchall():
                actual = Scope(proposal["user_id"], proposal["character_id"], proposal["relationship_id"], proposal["activity_id"], proposal["domain"])
                self._fresh_proposal(actual, proposal)
            rows = self.conn.execute("SELECT * FROM growth_values WHERE state='active'"+query, args).fetchall()
            hidden = []
            for row in rows:
                actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                if (row["source_epoch"] != self.db.repositories.forgetting.epoch(actual)
                        or not self.evidence.valid(actual, json.loads(row["sources_json"]))
                        or not self.evidence.valid(actual, json.loads(row["confirmation_json"]), require_user=True)):
                    self.conn.execute("UPDATE growth_values SET state='invalidated',value='' WHERE scope_key=? AND field=?", (row["scope_key"], row["field"]))
                    self.conn.execute("UPDATE growth_history SET value='',reason='' WHERE scope_key=? AND field=?", (row["scope_key"], row["field"]))
                    hidden.append(row["field"])
            return hidden
