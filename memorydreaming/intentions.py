"""有来源的承诺与用户允许的主动性。

服务只产生“待宿主投递”的领取回执，不发送消息、不启动定时线程，也不猜测
用户同意。创建、确认、触发、领取、确认投递分别有持久幂等边界；跨窗口和
跨进程通过同一 SQLite 写事务竞争。投递成功不等于承诺已完成。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import ensure_utc, from_utc_iso, to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .life_evidence import LifeEvidence, dumps
from .models import Scope, new_id
from .storage.repository import _scope_key


class IntentionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IntentionError("INVALID_ARGUMENT", f"{name} 须为 1..{limit} 字符")
    return value


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise IntentionError("INVALID_ARGUMENT", f"{name} 须为 {low}..{high} 整数")
    return value


def _moment(value, name="due_at"):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IntentionError("INVALID_ARGUMENT", f"{name} 须为带时区的 datetime")
    return ensure_utc(value)


def _scope_values(scope):
    if not isinstance(scope, Scope):
        raise IntentionError("INVALID_ARGUMENT", "scope 必须由可信宿主绑定")
    return (_scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id, scope.domain)


class IntentionsService:
    """可信本地意向服务；实例不拥有单独连接。"""

    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db
        self.conn = runtime.db.connection
        self.evidence = LifeEvidence(runtime)

    def _fingerprint(self, value):
        return self.db.repositories.forgetting.fingerprint(dumps(value))

    def _load(self, scope, intention_id):
        _text(intention_id, "intention_id")
        row = self.conn.execute("SELECT * FROM intentions WHERE intention_id=? AND scope_key=?",
                                (intention_id, _scope_values(scope)[0])).fetchone()
        if row is None:
            raise ScopeNotFound("承诺不存在或不属于精确绑定范围")
        return row

    def _sources_valid(self, scope, row):
        if row["state"] == "invalidated":
            return False
        if not self.evidence.valid(scope, json.loads(row["sources_json"])):
            return False
        confirmation = json.loads(row["confirmation_json"])
        if confirmation and not self.evidence.valid(scope, confirmation, require_user=True):
            return False
        triggers = json.loads(row["trigger_evidence_json"] or "[]")
        return not triggers or self.evidence.valid(scope, triggers)

    def _invalidate(self, row):
        if row["state"] == "invalidated":
            return
        # 当前正文与历史叙事一起擦除；历史仍保留操作/版本元信息。
        self.conn.execute("UPDATE intentions SET state='invalidated',text='',sources_json='[]',confirmation_json='[]',"
                          "event_trigger=NULL,triggered_at=NULL,trigger_evidence_json=NULL,due_at=NULL,version=version+1,updated_at=? "
                          "WHERE intention_id=? AND state!='invalidated'",
                          (to_utc_iso(self.runtime.clock.now()), row["intention_id"]))
        self.conn.execute("UPDATE intention_revisions SET snapshot_json=? WHERE intention_id=?",
                          (dumps({"redacted": True, "reason": "SOURCE_REVOKED"}), row["intention_id"]))
        self.conn.execute("UPDATE proactive_deliveries SET state='dropped',claim_token=NULL,lease_until=NULL WHERE intention_id=? AND state!='delivered'",
                          (row["intention_id"],))
        # 历史操作回执只保留动作事实，不能成为另一份可展开的旧承诺正文。
        for receipt in self.conn.execute("SELECT request_id,receipt_json FROM intention_operations WHERE scope_key=?",
                                         (row["scope_key"],)).fetchall():
            value = json.loads(receipt["receipt_json"])
            if value.get("intention_id") == row["intention_id"]:
                self.conn.execute("UPDATE intention_operations SET receipt_json=? WHERE scope_key=? AND request_id=?",
                    (dumps({"intention_id": row["intention_id"], "state": "invalidated", "applied": False,
                            "redacted": True, "duplicate": False}), row["scope_key"], receipt["request_id"]))

    def _valid_row(self, scope, row):
        if not self._sources_valid(scope, row):
            self._invalidate(row)
            return False
        return True

    def _public(self, row):
        return {"intention_id": row["intention_id"], "state": row["state"], "version": row["version"],
                "text": row["text"] if row["state"] != "invalidated" else None,
                "owner": row["owner"], "category": row["category"], "due_at": row["due_at"],
                "event_trigger": row["event_trigger"], "triggered_at": row["triggered_at"],
                "source_refs": json.loads(row["sources_json"]) if row["state"] != "invalidated" else [],
                "confirmation_refs": json.loads(row["confirmation_json"]) if row["state"] != "invalidated" else [],
                "confirmed": row["state"] in ("open", "completed", "cancelled")}

    def _revision(self, scope, intention_id, operation):
        row = self._load(scope, intention_id)
        self.conn.execute("INSERT INTO intention_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (intention_id, row["version"], *_scope_values(scope), operation, dumps(self._public(row)), to_utc_iso(self.runtime.clock.now())))

    def _operation(self, scope, request_id, request_key):
        _text(request_id, "request_id")
        row = self.conn.execute("SELECT * FROM intention_operations WHERE scope_key=? AND request_id=?",
                                (_scope_values(scope)[0], request_id)).fetchone()
        if row:
            if row["request_key"] != request_key:
                raise IntentionError("IDEMPOTENCY_CONFLICT", "同一意向操作幂等键不可换参数")
            receipt = json.loads(row["receipt_json"])
            if receipt.get("intention_id"):
                item = self._load(scope, receipt["intention_id"])
                if not self._valid_row(scope, item):
                    return {"intention_id": item["intention_id"], "state": "invalidated", "applied": False, "duplicate": True}
                refs = receipt.get("source_refs", [])
                confirmations = receipt.get("confirmation_refs", [])
                if ((refs and not self.evidence.valid(scope, refs))
                        or (confirmations and not self.evidence.valid(scope, confirmations, require_user=True))):
                    return {"intention_id": item["intention_id"], "state": "invalidated", "applied": False, "duplicate": True}
            return {**receipt, "duplicate": True}
        return None

    def _store_operation(self, scope, request_id, request_key, receipt):
        self.conn.execute("INSERT INTO intention_operations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*_scope_values(scope), request_id, request_key, dumps(receipt), to_utc_iso(self.runtime.clock.now())))
        return receipt

    def propose(self, scope, text, evidence, *, owner="character", category="promise", due_at=None,
                event_trigger=None, request_id):
        """只创建候选承诺；自然语言含义和双方同意须由宿主独立确认。"""
        _text(text, "text", 1000)
        _text(request_id, "request_id")
        if owner not in ("user", "character", "shared") or category not in ("promise", "continuation", "creative"):
            raise IntentionError("INVALID_ARGUMENT", "owner/category 无效")
        due_at = _moment(due_at)
        if event_trigger is not None:
            _text(event_trigger, "event_trigger", 100)
        with self.db.transaction():
            sources = self.evidence.capture(scope, evidence)
            key = self._fingerprint([text, owner, category, to_utc_iso(due_at), event_trigger, sources])
            previous = self.conn.execute("SELECT * FROM intentions WHERE scope_key=? AND request_id=?",
                                         (_scope_values(scope)[0], request_id)).fetchone()
            if previous:
                if previous["request_key"] != key:
                    raise IntentionError("IDEMPOTENCY_CONFLICT", "同一承诺请求不能换内容")
                self._valid_row(scope, previous)
                return {**self._public(self._load(scope, previous["intention_id"])), "duplicate": True}
            now, intention_id = to_utc_iso(self.runtime.clock.now()), new_id("intent")
            self.conn.execute("INSERT INTO intentions(intention_id,scope_key,user_id,character_id,relationship_id,activity_id,domain,"
                "request_id,request_key,text,owner,category,due_at,event_trigger,sources_json,state,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',1,?,?)",
                (intention_id, *_scope_values(scope), request_id, key, text, owner, category, to_utc_iso(due_at), event_trigger,
                 dumps(sources), now, now))
            self._revision(scope, intention_id, "proposed")
            self.db.repositories.journal.append("intention.proposed", scope=scope, entity_id=intention_id,
                                               payload={"version": 1}, now=self.runtime.clock.now())
            return {**self._public(self._load(scope, intention_id)), "duplicate": False}

    create = propose

    def confirm(self, scope, intention_id, *, expected_version, confirmation_refs, reviewer, reason, request_id):
        _integer(expected_version, "expected_version", 1, 2**31-1)
        _text(reviewer, "reviewer"); _text(reason, "reason", 500)
        with self.db.transaction():
            refs = self.evidence.capture(scope, confirmation_refs, require_user=True)
            key = self._fingerprint(["confirm", intention_id, expected_version, refs, reviewer, reason])
            receipt = self._operation(scope, request_id, key)
            if receipt:
                return receipt
            row = self._load(scope, intention_id)
            if not self._valid_row(scope, row):
                raise MemorySuppressed("承诺依据已撤销，需要新的提议")
            if row["version"] != expected_version:
                raise VersionConflict(expected_version, row["version"], "承诺")
            if row["state"] != "proposed":
                raise IntentionError("INTENTION_CLOSED", "只能确认尚未接受的候选承诺")
            self.conn.execute("UPDATE intentions SET state='open',confirmation_json=?,version=version+1,updated_at=? WHERE intention_id=?",
                              (dumps(refs), to_utc_iso(self.runtime.clock.now()), intention_id))
            self._revision(scope, intention_id, "confirmed")
            self.db.repositories.journal.append("intention.confirmed", scope=scope, entity_id=intention_id,
                payload={"reviewer": reviewer, "version": expected_version+1}, now=self.runtime.clock.now())
            return self._store_operation(scope, request_id, key,
                {**self._public(self._load(scope, intention_id)), "applied": True, "duplicate": False})

    def resolve(self, scope, intention_id, *, state, expected_version, evidence, reviewer, reason, request_id):
        if state not in ("completed", "cancelled"):
            raise IntentionError("INVALID_ARGUMENT", "state 须为 completed 或 cancelled")
        _integer(expected_version, "expected_version", 1, 2**31-1)
        _text(reviewer, "reviewer"); _text(reason, "reason", 500)
        with self.db.transaction():
            refs = self.evidence.capture(scope, evidence, require_user=True)
            key = self._fingerprint(["resolve", intention_id, state, expected_version, refs, reviewer, reason])
            receipt = self._operation(scope, request_id, key)
            if receipt:
                return receipt
            row = self._load(scope, intention_id)
            if not self._valid_row(scope, row):
                raise MemorySuppressed("承诺依据已撤销")
            if row["version"] != expected_version:
                raise VersionConflict(expected_version, row["version"], "承诺")
            if row["state"] not in ("proposed", "open"):
                raise IntentionError("INTENTION_CLOSED", "终态承诺不能改写")
            sources = {(ref["kind"], ref["target"]): ref for ref in json.loads(row["sources_json"])+refs}
            if len(sources) > 20:
                raise IntentionError("INVALID_ARGUMENT", "完成依据与原始依据合计不能超过20项")
            self.conn.execute("UPDATE intentions SET state=?,sources_json=?,version=version+1,updated_at=? WHERE intention_id=?",
                              (state, dumps(list(sources.values())), to_utc_iso(self.runtime.clock.now()), intention_id))
            self.conn.execute("UPDATE proactive_deliveries SET state='dropped',claim_token=NULL,lease_until=NULL WHERE intention_id=? AND state!='delivered'", (intention_id,))
            self._revision(scope, intention_id, state)
            self.db.repositories.journal.append("intention."+state, scope=scope, entity_id=intention_id,
                payload={"version": expected_version+1}, now=self.runtime.clock.now())
            return self._store_operation(scope, request_id, key,
                {**self._public(self._load(scope, intention_id)), "applied": True, "duplicate": False})

    def revise(self, scope, intention_id, *, text, evidence, expected_version, due_at=None,
               event_trigger=None, request_id):
        """更改后的承诺重新进入 proposed，原审批不能自动授权新的内容。"""
        _text(text, "text", 1000)
        _integer(expected_version, "expected_version", 1, 2**31-1)
        due_at = _moment(due_at)
        if event_trigger is not None:
            _text(event_trigger, "event_trigger", 100)
        with self.db.transaction():
            refs = self.evidence.capture(scope, evidence, require_user=True)
            key = self._fingerprint(["revise", intention_id, text, refs, expected_version, to_utc_iso(due_at), event_trigger])
            receipt = self._operation(scope, request_id, key)
            if receipt:
                return receipt
            row = self._load(scope, intention_id)
            if not self._valid_row(scope, row):
                raise MemorySuppressed("原承诺依据已撤销，不能通过修订复活")
            if row["version"] != expected_version:
                raise VersionConflict(expected_version, row["version"], "承诺")
            if row["state"] not in ("proposed", "open"):
                raise IntentionError("INTENTION_CLOSED", "终态承诺不能修订")
            self.conn.execute("UPDATE intentions SET text=?,sources_json=?,confirmation_json='[]',state='proposed',"
                "due_at=?,event_trigger=?,triggered_at=NULL,trigger_evidence_json=NULL,version=version+1,updated_at=? WHERE intention_id=?",
                (text, dumps(refs), to_utc_iso(due_at), event_trigger, to_utc_iso(self.runtime.clock.now()), intention_id))
            self.conn.execute("DELETE FROM proactive_deliveries WHERE intention_id=?", (intention_id,))
            self._revision(scope, intention_id, "revised")
            return self._store_operation(scope, request_id, key,
                {**self._public(self._load(scope, intention_id)), "applied": True, "duplicate": False})

    def get(self, scope, intention_id):
        with self.db.transaction():
            row = self._load(scope, intention_id)
            self._valid_row(scope, row)
            return self._public(self._load(scope, intention_id))

    def list_open(self, scope, *, limit=20):
        _integer(limit, "limit", 1, 100)
        with self.db.transaction():
            rows = self.conn.execute("SELECT * FROM intentions WHERE scope_key=? AND state='open' ORDER BY due_at IS NULL,due_at,created_at",
                                     (_scope_values(scope)[0],)).fetchall()
            results = []
            for row in rows:
                if self._valid_row(scope, row):
                    results.append(self._public(row))
                    if len(results) >= limit:
                        break
            return results

    def history(self, scope, intention_id, *, limit=50):
        _integer(limit, "limit", 1, 200)
        with self.db.transaction():
            row = self._load(scope, intention_id)
            self._valid_row(scope, row)
            history = []
            for revision in self.conn.execute("SELECT * FROM intention_revisions WHERE intention_id=? ORDER BY version DESC LIMIT ?",
                                               (intention_id, limit)).fetchall():
                snapshot = json.loads(revision["snapshot_json"])
                refs, confirmations = snapshot.get("source_refs", []), snapshot.get("confirmation_refs", [])
                if ((refs and not self.evidence.valid(scope, refs))
                        or (confirmations and not self.evidence.valid(scope, confirmations, require_user=True))):
                    snapshot = {"redacted": True, "reason": "SOURCE_REVOKED"}
                    self.conn.execute("UPDATE intention_revisions SET snapshot_json=? WHERE intention_id=? AND version=?",
                                      (dumps(snapshot), intention_id, revision["version"]))
                history.append({"version": revision["version"], "operation": revision["operation"],
                                "recorded_at": revision["recorded_at"], "snapshot": snapshot})
            return history

    def record_trigger(self, scope, event_name, event_id, *, request_id):
        """触发名来自宿主明确调用；不会从模型或普通文本猜测事件已发生。"""
        _text(event_name, "event_name", 100); _text(request_id, "request_id")
        with self.db.transaction():
            refs = self.evidence.capture(scope, [event_id], require_user=True)
            key = self._fingerprint([event_name, refs])
            old = self.conn.execute("SELECT * FROM intention_trigger_receipts WHERE scope_key=? AND request_id=?",
                                     (_scope_values(scope)[0], request_id)).fetchone()
            if old:
                if old["request_key"] != key:
                    raise IntentionError("IDEMPOTENCY_CONFLICT", "同一触发回执不能换事件")
                return {**json.loads(old["receipt_json"]), "duplicate": True}
            affected = []
            for row in self.conn.execute("SELECT * FROM intentions WHERE scope_key=? AND state='open' AND event_trigger=? AND triggered_at IS NULL",
                                          (_scope_values(scope)[0], event_name)).fetchall():
                if self._valid_row(scope, row):
                    self.conn.execute("UPDATE intentions SET triggered_at=?,trigger_evidence_json=?,updated_at=? WHERE intention_id=?",
                        (to_utc_iso(self.runtime.clock.now()), dumps(refs), to_utc_iso(self.runtime.clock.now()), row["intention_id"]))
                    affected.append(row["intention_id"])
            result = {"event_name": event_name, "triggered_intention_ids": affected, "duplicate": False}
            self.conn.execute("INSERT INTO intention_trigger_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*_scope_values(scope), request_id, key, dumps(result), to_utc_iso(self.runtime.clock.now())))
            return result

    @staticmethod
    def _zone(name):
        _text(name, "timezone", 100)
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            raise IntentionError("TIMEZONE_UNAVAILABLE", "时区不可用，请安装 tzdata 或明确选择 UTC") from None

    @staticmethod
    def _minutes(value):
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise IntentionError("INVALID_ARGUMENT", "安静时段须为 HH:MM")
        return int(value[:2])*60+int(value[3:])

    def configure_policy(self, scope, *, enabled, confirmation_refs=(), request_id, expected_version=0,
                         timezone="UTC", quiet_start="22:00", quiet_end="08:00", min_interval_seconds=86400,
                         max_per_day=1):
        if not isinstance(enabled, bool):
            raise IntentionError("INVALID_ARGUMENT", "enabled 须为布尔值")
        _integer(expected_version, "expected_version", 0, 2**31-1)
        _integer(min_interval_seconds, "min_interval_seconds", 60, 30*86400)
        _integer(max_per_day, "max_per_day", 1, 24)
        self._zone(timezone); self._minutes(quiet_start); self._minutes(quiet_end)
        with self.db.transaction():
            refs = self.evidence.capture(scope, confirmation_refs, require_user=True) if enabled else []
            key = self._fingerprint(["policy", enabled, refs, expected_version, timezone, quiet_start, quiet_end, min_interval_seconds, max_per_day])
            receipt = self._operation(scope, request_id, key)
            if receipt:
                return receipt
            row = self.conn.execute("SELECT * FROM proactive_policies WHERE scope_key=?", (_scope_values(scope)[0],)).fetchone()
            actual = row["version"] if row else 0
            if actual != expected_version:
                raise VersionConflict(expected_version, actual, "主动性策略")
            self.conn.execute("INSERT INTO proactive_policies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_key) DO UPDATE SET enabled=excluded.enabled,version=excluded.version,confirmation_json=excluded.confirmation_json,"
                "timezone=excluded.timezone,quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,"
                "min_interval_seconds=excluded.min_interval_seconds,max_per_day=excluded.max_per_day,updated_at=excluded.updated_at",
                (*_scope_values(scope), int(enabled), actual+1, dumps(refs), timezone, quiet_start, quiet_end,
                 min_interval_seconds, max_per_day, None, to_utc_iso(self.runtime.clock.now())))
            # 每次策略变更使旧领取失去权限；稳定 delivery_id 仍可在新授权下领取。
            self.conn.execute("UPDATE proactive_deliveries SET state='pending',claim_token=NULL,lease_until=NULL WHERE scope_key=? AND state='claimed'",
                              (_scope_values(scope)[0],))
            result = {"enabled": enabled, "version": actual+1, "applied": True, "duplicate": False,
                      "timezone": timezone, "quiet_start": quiet_start, "quiet_end": quiet_end,
                      "min_interval_seconds": min_interval_seconds, "max_per_day": max_per_day,
                      "delivery_mode": "trusted_host_claim_only"}
            return self._store_operation(scope, request_id, key, result)

    def policy(self, scope):
        with self.db.transaction():
            row = self._policy_row(scope)
            if row is None:
                return {"enabled": False, "version": 0, "delivery_mode": "trusted_host_claim_only"}
            return {key: bool(row[key]) if key == "enabled" else row[key] for key in
                    ("enabled", "version", "timezone", "quiet_start", "quiet_end", "min_interval_seconds", "max_per_day")}

    def _policy_row(self, scope):
        row = self.conn.execute("SELECT * FROM proactive_policies WHERE scope_key=?", (_scope_values(scope)[0],)).fetchone()
        if row and row["enabled"] and not self.evidence.valid(scope, json.loads(row["confirmation_json"]), require_user=True):
            self.conn.execute("UPDATE proactive_policies SET enabled=0,confirmation_json='[]',version=version+1,updated_at=? WHERE scope_key=?",
                              (to_utc_iso(self.runtime.clock.now()), row["scope_key"]))
            self.conn.execute("UPDATE proactive_deliveries SET state='pending',claim_token=NULL,lease_until=NULL WHERE scope_key=? AND state='claimed'", (row["scope_key"],))
            row = self.conn.execute("SELECT * FROM proactive_policies WHERE scope_key=?", (row["scope_key"],)).fetchone()
        return row

    def pause(self, scope, *, expected_version, request_id):
        current = self.policy(scope)
        if current["version"] != expected_version:
            raise VersionConflict(expected_version, current["version"], "主动性策略")
        return self.configure_policy(scope, enabled=False, request_id=request_id, expected_version=expected_version,
            **{key: current[key] for key in ("timezone", "quiet_start", "quiet_end", "min_interval_seconds", "max_per_day") if key in current})

    def resume(self, scope, *, expected_version, confirmation_refs, request_id):
        """恢复需要新的用户确认；复用暂停前的时段与频率，重启本身无需调用。"""
        current = self.policy(scope)
        if current["version"] != expected_version:
            raise VersionConflict(expected_version, current["version"], "主动性策略")
        return self.configure_policy(scope, enabled=True, confirmation_refs=confirmation_refs,
            request_id=request_id, expected_version=expected_version,
            **{key: current[key] for key in ("timezone", "quiet_start", "quiet_end", "min_interval_seconds", "max_per_day") if key in current})

    def clear_policy(self, scope, *, expected_version, request_id):
        """清除主动授权及自定义频率，回到关闭的默认策略，保留最小操作回执。"""
        return self.configure_policy(scope, enabled=False, expected_version=expected_version, request_id=request_id)

    def _is_due(self, row, now):
        if row["event_trigger"] and row["triggered_at"] is None:
            return False
        if row["due_at"] and from_utc_iso(row["due_at"]) > now:
            return False
        return bool(row["due_at"] or row["event_trigger"])

    def preview_due(self, scope, *, limit=10):
        """供苏醒状态卡只读挑选到期事项；不领取，不发送，不将承诺标完成。"""
        _integer(limit, "limit", 1, 100)
        with self.db.transaction():
            now, result = ensure_utc(self.runtime.clock.now()), []
            rows = self.conn.execute("SELECT i.* FROM intentions i LEFT JOIN proactive_deliveries d ON d.intention_id=i.intention_id "
                "WHERE i.scope_key=? AND i.state='open' AND (d.state IS NULL OR d.state!='delivered') ORDER BY i.due_at IS NULL,i.due_at,i.created_at",
                (_scope_values(scope)[0],)).fetchall()
            for row in rows:
                if self._valid_row(scope, row) and self._is_due(row, now):
                    result.append(self._public(row))
                    if len(result) >= limit:
                        break
            return result

    def _quiet(self, policy, now):
        local = now.astimezone(self._zone(policy["timezone"]))
        minute, start, end = local.hour*60+local.minute, self._minutes(policy["quiet_start"]), self._minutes(policy["quiet_end"])
        # 相同起止表示没有安静时段；跨午夜按当地民用时间，DST 自动应用。
        return (start <= minute < end if start < end else minute >= start or minute < end) if start != end else False

    def claim_due(self, scope, *, worker_id, limit=1, lease_seconds=60):
        """原子领取到期事项，返回待宿主投递资料；本函数没有外部副作用。"""
        _text(worker_id, "worker_id")
        _integer(limit, "limit", 1, 20); _integer(lease_seconds, "lease_seconds", 5, 600)
        with self.db.transaction():
            policy = self._policy_row(scope)
            now = ensure_utc(self.runtime.clock.now())
            if policy is None or not policy["enabled"]:
                return {"claims": [], "reason": "PROACTIVITY_DISABLED"}
            if self._quiet(policy, now):
                return {"claims": [], "reason": "QUIET_HOURS"}
            due = self.preview_due(scope, limit=100)
            for item in due:
                self.conn.execute("INSERT OR IGNORE INTO proactive_deliveries(delivery_id,intention_id,scope_key,user_id,character_id,"
                    "relationship_id,activity_id,domain,state,policy_version,intention_version,created_at) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)",
                    (new_id("reminder"), item["intention_id"], *_scope_values(scope), policy["version"], item["version"], to_utc_iso(now)))
            rows = self.conn.execute("SELECT * FROM proactive_deliveries WHERE scope_key=? AND "
                "((state='pending' AND available_at<=?) OR (state='claimed' AND lease_until<=?)) ORDER BY created_at,delivery_id",
                (_scope_values(scope)[0], to_utc_iso(now), to_utc_iso(now))).fetchall()
            day = now.astimezone(self._zone(policy["timezone"])).date().isoformat()
            claims = []
            for delivery in rows:
                item = self._load(scope, delivery["intention_id"])
                if not self._valid_row(scope, item) or item["state"] != "open" or not self._is_due(item, now):
                    self.conn.execute("UPDATE proactive_deliveries SET state='dropped',claim_token=NULL WHERE delivery_id=?", (delivery["delivery_id"],))
                    continue
                retry = delivery["reserved_day"] == day
                if not retry:
                    reserved = self.conn.execute("SELECT COUNT(*) FROM proactive_deliveries WHERE scope_key=? AND reserved_day=?",
                                                  (policy["scope_key"], day)).fetchone()[0]
                    if reserved >= policy["max_per_day"]:
                        break
                    last = from_utc_iso(policy["last_claimed_at"])
                    if last is not None and (now-last).total_seconds() < policy["min_interval_seconds"]:
                        break
                token = secrets.token_urlsafe(24)
                until = to_utc_iso(now+timedelta(seconds=lease_seconds))
                self.conn.execute("UPDATE proactive_deliveries SET state='claimed',claim_token=?,worker_id=?,lease_until=?,reserved_day=?,"
                    "policy_version=?,intention_version=?,attempts=attempts+1 WHERE delivery_id=?",
                    (token, worker_id, until, day, policy["version"], item["version"], delivery["delivery_id"]))
                if not retry:
                    self.conn.execute("UPDATE proactive_policies SET last_claimed_at=? WHERE scope_key=?", (to_utc_iso(now), policy["scope_key"]))
                    policy = self._policy_row(scope)
                claims.append({"delivery_id": delivery["delivery_id"], "claim_token": token, "lease_until": until,
                    "intention": self._public(item), "delivery_mode": "trusted_host_claim_only",
                    "guidance": "只在用户允许的渠道与时段给出一次自然提醒，尊重暂停；不要制造情感负担，不将提醒当成任务完成。"})
                if len(claims) >= limit:
                    break
            return {"claims": claims, "reason": "READY" if claims else "NO_ELIGIBLE_OR_RATE_LIMITED"}

    def _validate_claim(self, scope, claim_token):
        """宿主发送前再次核验；已领取资料不能撤销已经完成的外部发送。"""
        _text(claim_token, "claim_token")
        row = self.conn.execute("SELECT * FROM proactive_deliveries WHERE scope_key=? AND claim_token=?",
                                (_scope_values(scope)[0], claim_token)).fetchone()
        if row is None or row["state"] != "claimed" or from_utc_iso(row["lease_until"]) <= ensure_utc(self.runtime.clock.now()):
            raise IntentionError("REMINDER_LEASE_LOST", "提醒领取已失效，不能发送或覆盖新领取结果")
        policy = self._policy_row(scope)
        item = self._load(scope, row["intention_id"])
        if (policy is None or not policy["enabled"] or policy["version"] != row["policy_version"]
                or item["version"] != row["intention_version"] or item["state"] != "open" or not self._valid_row(scope, item)):
            raise IntentionError("REMINDER_REVOKED", "提醒授权、承诺或来源已撤销")
        if self._quiet(policy, ensure_utc(self.runtime.clock.now())):
            raise IntentionError("REMINDER_QUIET_HOURS", "已进入用户安静时段")
        return row, item

    def validate_claim(self, scope, claim_token):
        """宿主在真正发送前调用；只返回JSON安全资料，不暴露存储层对象。"""
        with self.db.transaction():
            delivery, item = self._validate_claim(scope, claim_token)
            return {"valid": True, "delivery_id": delivery["delivery_id"], "claim_token": claim_token,
                    "lease_until": delivery["lease_until"], "intention": self._public(item),
                    "delivery_mode": "trusted_host_claim_only"}

    def ack(self, scope, claim_token, *, delivered, request_id):
        if not isinstance(delivered, bool):
            raise IntentionError("INVALID_ARGUMENT", "delivered 须为布尔值")
        _text(request_id, "request_id"); _text(claim_token, "claim_token")
        key = self._fingerprint([delivered, request_id])
        with self.db.transaction():
            previous = self.conn.execute("SELECT * FROM proactive_ack_receipts WHERE scope_key=? AND claim_token=?",
                                          (_scope_values(scope)[0], claim_token)).fetchone()
            if previous:
                if previous["request_key"] != key:
                    raise IntentionError("IDEMPOTENCY_CONFLICT", "终态投递回执不能改变结果或请求ID")
                return {**json.loads(previous["receipt_json"]), "duplicate": True}
            delivery, _ = self._validate_claim(scope, claim_token)
            state = "delivered" if delivered else "pending"
            now = self.runtime.clock.now()
            available_at = now if delivered else now+timedelta(seconds=min(300, 5*2**min(6, max(0, delivery["attempts"]-1))))
            self.conn.execute("UPDATE proactive_deliveries SET state=?,claim_token=NULL,lease_until=NULL,delivered_at=?,available_at=? WHERE delivery_id=?",
                              (state, to_utc_iso(now) if delivered else None, to_utc_iso(available_at), delivery["delivery_id"]))
            receipt = {"delivery_id": delivery["delivery_id"], "delivered": delivered,
                       "state": state, "intention_completed": False, "duplicate": False}
            self.conn.execute("INSERT INTO proactive_ack_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (claim_token, *_scope_values(scope), key, dumps(receipt), to_utc_iso(self.runtime.clock.now())))
            return receipt

    def invalidate_stale(self, scope=None):
        """遗忘/纠错提交链可主动清理；普通读取/领取也复核，不依赖定时调用。"""
        with self.db.transaction():
            sql = "SELECT * FROM intentions WHERE state!='invalidated'"
            rows = self.conn.execute(sql + (" AND scope_key=?" if scope else ""),
                                     (_scope_values(scope)[0],) if scope else ()).fetchall()
            invalidated = []
            for row in rows:
                actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                if not self._valid_row(actual, row):
                    invalidated.append(row["intention_id"])
            policies = self.conn.execute("SELECT * FROM proactive_policies" + (" WHERE scope_key=?" if scope else ""),
                                         (_scope_values(scope)[0],) if scope else ()).fetchall()
            for row in policies:
                actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                self._policy_row(actual)
            return invalidated
