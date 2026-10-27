"""可回溯的自传主线：候选、可信确认、修订和撤回保持分离。

自传只描述被确认的叙事投影，不能变成事实证据根。来源失效时隐藏/清理
生成正文，旧版本也不能借历史接口重新暴露。所有写入都沿用唯一 Runtime
连接及事务；本模块不调用模型，不自行判断一次模型推断等于用户确认。
"""

from datetime import datetime
import json

from .clock import to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .life_evidence import LifeEvidence, dumps
from .llm import EstimatedTokenCounter
from .models import Scope, new_id
from .storage.repository import _scope_key


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 必须为 1..{maximum} 字符")
    return value


def _version(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_version 必须为非负整数")
    return value


def _scope(scope):
    if not isinstance(scope, Scope):
        raise ValueError("scope 必须来自可信宿主")
    return (_scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id, scope.domain)


class NarrativeService:
    """无独立资源的 Runtime 能力，常用入口为 propose/approve/timeline。"""

    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db
        self.conn, self.evidence = runtime.db.connection, LifeEvidence(runtime)

    def _entry(self, scope, entry_id):
        row = self.conn.execute("SELECT * FROM narrative_entries WHERE entry_id=? AND scope_key=?",
                                (entry_id, _scope_key(scope))).fetchone()
        if row is None:
            raise ScopeNotFound("自传节点不存在或不属于绑定范围")
        return row

    def _proposal(self, scope, proposal_id):
        row = self.conn.execute("SELECT * FROM narrative_proposals WHERE proposal_id=? AND scope_key=?",
                                (proposal_id, _scope_key(scope))).fetchone()
        if row is None:
            raise ScopeNotFound("自传候选不存在或不属于绑定范围")
        return row

    def suggest_from_conversation(self, scope, conversation_id, *, limit=5):
        """从用户已保存的明确表达提出小量候选；只引用原话，不自动接受。

        这是不依赖额外模型的保守候选生成器。复杂意义由宿主/用户后续
        审核；关键词命中不是“这件事对角色很重要”的真实性判断。
        """
        _scope(scope); _text(conversation_id, "conversation_id", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须为 1..20")
        proposals = []
        with self.db.transaction():
            rows = self.conn.execute(
                "SELECT event_id,content,occurred_at FROM events WHERE user_id=? AND character_id=? "
                "AND relationship_id IS ? AND activity_id IS ? AND domain=? AND conversation_id=? "
                "AND speaker='user' ORDER BY recorded_at,event_id LIMIT 1000",
                (scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id, scope.domain, conversation_id),
            ).fetchall()
            for row in rows:
                text = row["content"]
                if len(text) > 1400 or not any(word in text for word in ("叫你", "称呼", "答应", "第一次", "以后", "约定", "喜欢", "改成")):
                    continue
                if not self.evidence.valid(scope, [row["event_id"]]):
                    continue
                from .clock import from_utc_iso
                proposal = self.propose(scope, "一次明确的相处表达", f"用户当时说：“{text}”", [row["event_id"]],
                    "event-narrative:"+row["event_id"], perspective="user",
                    modality="occurred" if scope.domain == "real" else "shared_fiction",
                    occurred_at=from_utc_iso(row["occurred_at"]))
                proposals.append(proposal)
                if len(proposals) >= limit:
                    break
            return {"kind": "autobiography_candidates", "proposals": proposals,
                    "applied": False, "automatic_facts_created": 0, "scan_limit": 1000}

    def propose(self, scope, title, text, sources, request_id, *, perspective="shared", modality="occurred",
                occurred_at=None, entry_id=None, expected_version=0):
        """保存候选或现有节点的修订候选；不覆盖仍有效的已确认主线。"""
        values = _scope(scope)
        _text(title, "title", 150); _text(text, "text", 2000); _text(request_id, "request_id", 200)
        _version(expected_version)
        if perspective not in ("user", "character", "shared") or modality not in ("occurred", "shared_fiction"):
            raise ValueError("perspective 或 modality 无效")
        if occurred_at is not None and not isinstance(occurred_at, datetime):
            raise ValueError("occurred_at 必须为 datetime")
        if entry_id is None and expected_version != 0:
            raise ValueError("新节点 expected_version 必须为 0")
        with self.db.transaction():
            previous = self.conn.execute("SELECT * FROM narrative_proposals WHERE scope_key=? AND request_id=?",
                                         (values[0], request_id)).fetchone()
            refs = self.evidence.capture(scope, sources)
            payload = {"title": title, "text": text, "perspective": perspective, "modality": modality,
                       "occurred_at": to_utc_iso(occurred_at or self.runtime.clock.now())}
            if occurred_at is None and previous and previous["payload_json"]:
                payload["occurred_at"] = json.loads(previous["payload_json"])["occurred_at"]
            material = [payload, refs, entry_id, expected_version]
            fingerprint = self.runtime.db.repositories.forgetting.fingerprint(dumps(material))
            if previous:
                if previous["request_key"] != fingerprint:
                    raise ValueError("同一自传幂等键不能对应不同内容或来源")
                return self.inspect(scope, previous["proposal_id"])
            if entry_id is not None:
                old = self._entry(scope, entry_id)
                if old["version"] != expected_version:
                    raise VersionConflict(expected_version, old["version"], "自传")
                if old["state"] != "accepted":
                    raise MemorySuppressed("已撤回或失效节点不能经修订静默恢复")
            proposal_id = new_id("nproposal")
            self.conn.execute(
                "INSERT INTO narrative_proposals(proposal_id,scope_key,user_id,character_id,relationship_id,activity_id,domain,"
                "request_id,request_key,target_id,expected_version,payload_json,sources_json,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?)",
                (proposal_id, *values, request_id, fingerprint, entry_id, expected_version, dumps(payload), dumps(refs),
                 to_utc_iso(self.runtime.clock.now())),
            )
            return self.inspect(scope, proposal_id)

    def _finish(self, row, state, reviewer, reason, receipt):
        self.conn.execute("UPDATE narrative_proposals SET state=?,reviewer=?,decision_reason=?,receipt_json=?,decided_at=? WHERE proposal_id=?",
                          (state, reviewer, reason, dumps(receipt), to_utc_iso(self.runtime.clock.now()), row["proposal_id"]))
        return receipt

    def inspect(self, scope, proposal_id):
        _scope(scope)
        with self.db.transaction():
            row = self._proposal(scope, proposal_id)
            if row["state"] == "proposed" and not self.evidence.valid(scope, json.loads(row["sources_json"])):
                receipt = {"proposal_id": proposal_id, "state": "invalidated", "applied": False,
                           "reason": "SOURCE_REVOKED"}
                self.conn.execute("UPDATE narrative_proposals SET payload_json=NULL WHERE proposal_id=?", (proposal_id,))
                return self._finish(row, "invalidated", None, None, receipt)
            if row["state"] != "proposed":
                return json.loads(row["receipt_json"])
            return {"proposal_id": proposal_id, "state": "proposed", "applied": False,
                    "target_id": row["target_id"], "expected_version": row["expected_version"],
                    "candidate": json.loads(row["payload_json"]), "sources": json.loads(row["sources_json"])}

    def approve(self, scope, proposal_id, reviewer, reason, *, user_event_id=None):
        """可信宿主已核实叙事与用户意图后确认，重新复核全部来源和版本。"""
        _scope(scope); _text(reviewer, "reviewer", 200); _text(reason, "reason", 500)
        with self.db.transaction():
            row = self._proposal(scope, proposal_id)
            if row["state"] != "proposed":
                if row["state"] == "accepted" and row["reviewer"] == reviewer and row["decision_reason"] == reason:
                    return {**json.loads(row["receipt_json"]), "duplicate": True}
                if row["state"] == "invalidated":
                    return json.loads(row["receipt_json"])
                raise ValueError("终态自传审批不能改写")
            refs = json.loads(row["sources_json"])
            if user_event_id is not None:
                self.evidence.capture(scope, [user_event_id], require_user=True)
            if not self.evidence.valid(scope, refs):
                self.conn.execute("UPDATE narrative_proposals SET payload_json=NULL WHERE proposal_id=?", (proposal_id,))
                return self._finish(row, "invalidated", reviewer, reason,
                    {"proposal_id": proposal_id, "state": "invalidated", "applied": False, "reason": "SOURCE_REVOKED"})
            old = self._entry(scope, row["target_id"]) if row["target_id"] else None
            if old and (old["version"] != row["expected_version"] or old["state"] != "accepted"):
                return self._finish(row, "invalidated", reviewer, reason,
                    {"proposal_id": proposal_id, "state": "invalidated", "applied": False, "reason": "VERSION_CONFLICT"})
            payload = json.loads(row["payload_json"])
            entry_id = old["entry_id"] if old else new_id("life")
            version = old["version"]+1 if old else 1
            now = to_utc_iso(self.runtime.clock.now())
            if old:
                self.conn.execute("UPDATE narrative_entries SET title=?,text=?,perspective=?,modality=?,occurred_at=?,sources_json=?,version=?,updated_at=? WHERE entry_id=?",
                    (payload["title"], payload["text"], payload["perspective"], payload["modality"], payload["occurred_at"], dumps(refs), version, now, entry_id))
            else:
                self.conn.execute("INSERT INTO narrative_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?)",
                    (entry_id, *_scope(scope), payload["perspective"], payload["modality"], payload["title"], payload["text"],
                     payload["occurred_at"], dumps(refs), version, now, now))
            self._record_version(scope, entry_id, version, "accepted", reason)
            receipt = {"proposal_id": proposal_id, "entry_id": entry_id, "version": version, "state": "accepted",
                       "applied": True, "duplicate": False, "record_kind": "autobiography_projection"}
            self._finish(row, "accepted", reviewer, reason, receipt)
            self.runtime.db.repositories.journal.append("narrative.accepted", scope=scope, entity_id=entry_id,
                                                       payload={"version": version}, now=self.runtime.clock.now())
            return receipt

    def reject(self, scope, proposal_id, reviewer, reason):
        _scope(scope); _text(reviewer, "reviewer", 200); _text(reason, "reason", 500)
        with self.db.transaction():
            row = self._proposal(scope, proposal_id)
            if row["state"] == "rejected" and row["reviewer"] == reviewer and row["decision_reason"] == reason:
                return {**json.loads(row["receipt_json"]), "duplicate": True}
            if row["state"] != "proposed":
                raise ValueError("终态自传审批不能改写")
            return self._finish(row, "rejected", reviewer, reason,
                {"proposal_id": proposal_id, "state": "rejected", "applied": False})

    def _record_version(self, scope, entry_id, version, operation, reason):
        row = self._entry(scope, entry_id)
        snapshot = {key: row[key] for key in ("title", "text", "perspective", "modality", "occurred_at", "state")}
        self.conn.execute("INSERT INTO narrative_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, version, *_scope(scope), dumps(snapshot), row["sources_json"], operation, reason,
             to_utc_iso(self.runtime.clock.now())))

    def withdraw(self, scope, entry_id, *, expected_version, reason):
        _scope(scope); _version(expected_version); _text(reason, "reason", 500)
        with self.db.transaction():
            row = self._entry(scope, entry_id)
            if row["version"] != expected_version:
                raise VersionConflict(expected_version, row["version"], "自传")
            if row["state"] != "accepted":
                return {"entry_id": entry_id, "state": row["state"], "version": row["version"], "changed": False}
            self.conn.execute("UPDATE narrative_entries SET state='withdrawn',version=version+1,updated_at=? WHERE entry_id=?",
                              (to_utc_iso(self.runtime.clock.now()), entry_id))
            self._record_version(scope, entry_id, row["version"]+1, "withdrawn", reason)
            return {"entry_id": entry_id, "state": "withdrawn", "version": row["version"]+1, "changed": True}

    def _public(self, row):
        return {"entry_id": row["entry_id"], "version": row["version"], "title": row["title"], "text": row["text"],
                "perspective": row["perspective"], "modality": row["modality"], "occurred_at": row["occurred_at"],
                "sources": json.loads(row["sources_json"]), "record_kind": "autobiography_projection"}

    def timeline(self, scope, *, limit=10, max_tokens=1200):
        _scope(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 128 <= max_tokens <= 4096:
            raise ValueError("max_tokens 必须为 128..4096")
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT * FROM narrative_entries WHERE scope_key=? AND state='accepted' ORDER BY occurred_at DESC,entry_id LIMIT 500",
                                     (_scope_key(scope),)).fetchall()
            entries = []
            for row in rows:
                if self.evidence.valid(scope, json.loads(row["sources_json"])):
                    entries.append(self._public(row))
                if len(entries) >= limit:
                    break
            entries.reverse()
            response = {"kind": "autobiography", "entries": entries, "truncated": len(rows) > limit}
            counter = EstimatedTokenCounter()
            while counter.count(dumps(response)) > max_tokens and response["entries"]:
                response["entries"].pop(0); response["truncated"] = True
            return response

    def history(self, scope, entry_id, *, limit=30):
        _scope(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        with self.db.transaction(write=False):
            self._entry(scope, entry_id)
            rows = self.conn.execute("SELECT * FROM narrative_versions WHERE entry_id=? ORDER BY version DESC LIMIT ?", (entry_id, limit)).fetchall()
            return [{"version": row["version"], "operation": row["operation"], "created_at": row["created_at"],
                     "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] and self.evidence.valid(scope, json.loads(row["sources_json"])) else None}
                    for row in rows]

    def invalidate_stale(self, scope=None):
        """供遗忘/纠错事务统一调用；历史正文与待审批载荷一并擦除。"""
        where, args = (" WHERE scope_key=?", (_scope_key(scope),)) if scope else ("", ())
        changed = []
        with self.db.transaction():
            for row in self.conn.execute("SELECT * FROM narrative_entries"+where, args).fetchall():
                actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                if row["state"] != "invalidated" and not self.evidence.valid(actual, json.loads(row["sources_json"])):
                    self.conn.execute("UPDATE narrative_entries SET state='invalidated',title='',text='',updated_at=? WHERE entry_id=?",
                                      (to_utc_iso(self.runtime.clock.now()), row["entry_id"]))
                    changed.append(row["entry_id"])
            for table, key in (("narrative_versions", "entry_id"), ("narrative_proposals", "proposal_id")):
                for row in self.conn.execute("SELECT * FROM "+table+where, args).fetchall():
                    actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                    if not self.evidence.valid(actual, json.loads(row["sources_json"])):
                        if table == "narrative_versions":
                            self.conn.execute("UPDATE narrative_versions SET snapshot_json=NULL,reason='' WHERE entry_id=? AND version=?", (row["entry_id"], row["version"]))
                        else:
                            self.conn.execute("UPDATE narrative_proposals SET payload_json=NULL WHERE proposal_id=?", (row[key],))
            return changed
