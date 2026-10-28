"""Human-operated role forms over the existing, evidence-backed domain services.

The page supplies values, never scope or source references. A saved user operation
is the explicit confirmation and evidence for its new projection. Domain writes,
that confirmation, and a small durable receipt share one outer transaction.
"""

from __future__ import annotations

import json

from .companionship import _label
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .growth import GROWTH_FIELDS
from .life_evidence import LifeEvidence
from .llm import AdapterError, dumps, integer, text, timestamp


# SQL identifiers are code-owned; no page argument is interpolated into SQL.
_HEADS = {
    "narrative": ("narrative_entries", "entry_id"),
    "intention": ("intentions", "intention_id"),
    "companionship": ("companionship_preferences", "preference_key"),
    "growth": ("growth_values", "field"),
}
_PAYLOADS = {
    "narrative": {"title", "text", "perspective", "modality", "occurred_at"},
    "intention": {"text", "owner", "category", "due_at", "event_trigger"},
    "companionship": {"value"}, "growth": {"value"},
}


def _fit_items(items, *, maximum=192*1024):
    """Keep complete records under the desktop envelope's byte ceiling.

    Character limits alone do not bound UTF-8 JSON size. Keeping a prefix also
    lets catalog cursors advance by the actual visible records without gaps.
    """
    result, used = [], 0
    for item in items:
        size = len(dumps(item).encode("utf-8"))+1
        if used+size > maximum:
            break
        result.append(item)
        used += size
    return result


class DesktopRoles:
    """A fixed-scope helper, deliberately absent from the LLM tool catalog."""

    def __init__(self, desktop):
        self.page = desktop
        self.runtime, self.scope = desktop.runtime, desktop.scope
        self.db, self.conn = desktop.db, desktop.conn
        self.evidence = LifeEvidence(self.runtime)

    @staticmethod
    def _kind(args):
        kind = args.get("role_type")
        if not isinstance(kind, str) or kind not in _HEADS:
            raise ValueError("role_type 须为 narrative/intention/companionship/growth")
        if kind != "companionship" and "context" in args:
            raise ValueError("只有相处偏好支持 context")
        return kind

    @staticmethod
    def _target(kind, args, *, optional=False):
        target = args.get("id")
        if optional and target is None and kind in ("narrative", "intention"):
            return None, None
        target = text(target, "id", 200)
        context = None
        if kind == "companionship":
            target, context = _label(target, "id"), _label(args.get("context", "default"), "context")
        if kind == "growth" and target not in GROWTH_FIELDS:
            raise ValueError("未知成长字段")
        return target, context

    def _head(self, kind, target, context):
        if target is None:
            return None
        table, key = _HEADS[kind]
        sql = "SELECT * FROM "+table+" WHERE scope_key=? AND "+key+"=?"
        values = [self.page.scope_key, target]
        if kind == "companionship":
            sql += " AND context=?"; values.append(context)
        return self.conn.execute(sql, values).fetchone()

    @staticmethod
    def _refs(kind, row):
        columns = {
            "narrative": ("sources_json",),
            "intention": ("sources_json", "confirmation_json", "trigger_evidence_json"),
            "companionship": ("evidence_json", "approval_event_json"),
            "growth": ("sources_json", "confirmation_json"),
        }
        return [ref for column in columns[kind] for ref in json.loads(row[column] or "[]")]

    def _usable(self, kind, row):
        """Head existence and authorization are independent of body visibility.

        Revocation deliberately retains a versioned tombstone. Its new user
        confirmation remains required; forgetting that event cannot be bypassed
        by saving over the apparently empty head with expected_version=0.
        """
        if row["state"] == "invalidated":
            return False
        service = getattr(self.runtime, "intentions" if kind == "intention" else kind)
        if kind == "narrative":
            valid = self.evidence.valid(self.scope, json.loads(row["sources_json"]))
        elif kind == "intention":
            valid = service._sources_valid(self.scope, row)
        elif kind == "companionship":
            valid = (service._valid(self.scope, row) if row["state"] != "revoked" else
                     self.evidence.valid(self.scope, json.loads(row["approval_event_json"]), require_user=True))
        else:
            valid = self.evidence.valid(self.scope, json.loads(row["confirmation_json"]), require_user=True)
            if row["state"] != "revoked":
                valid = (valid and row["source_epoch"] == self.db.repositories.forgetting.epoch(self.scope)
                         and self.evidence.valid(self.scope, json.loads(row["sources_json"])))
        return valid and self.page._sources_visible(self._refs(kind, row))

    @staticmethod
    def _payload(kind, row):
        if kind == "narrative":
            return {key: row[key] for key in ("title", "text", "perspective", "modality", "occurred_at")}
        if kind == "intention":
            return {key: row[key] for key in ("text", "owner", "category", "due_at", "event_trigger")}
        value = json.loads(row["value_json"]) if kind == "companionship" and row["value_json"] is not None else None
        if kind == "growth":
            value = row["value"] if row["state"] == "active" else None
        return {"value": value}

    def catalog(self, args):
        kind = self._kind(args)
        offset, limit = self.page._page(args)
        table, key = _HEADS[kind]
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT * FROM "+table+" WHERE scope_key=? ORDER BY updated_at DESC,"+key+
                (",context" if kind == "companionship" else "")+" LIMIT ? OFFSET ?",
                (self.page.scope_key, limit+1, offset)).fetchall()
            items = []
            for row in rows[:limit]:
                visible = self._usable(kind, row)
                item = {"role_type": kind, "id": row[key], "version": row["version"], "state": row["state"],
                        "payload": self._payload(kind, row) if visible else {}, "truncated": False, "redacted": not visible}
                if kind == "companionship":
                    item["context"] = row["context"]
                items.append(item)
            fitted = _fit_items(items)
            clipped = len(fitted) < len(items)
            more = len(rows) > limit or clipped
            return {"items": fitted, "offset": offset, "next_offset": offset+len(fitted) if more else None,
                    "has_more": more, "truncated": clipped}

    def history(self, args):
        kind = self._kind(args)
        target, context = self._target(kind, args)
        limit = integer(args.get("limit", 20), "limit", 1, 50)
        # Intention history may persist source redactions, so use a write-capable
        # transaction even though this page action changes no user-owned value.
        with self.db.transaction():
            head = self._head(kind, target, context)
            if head is None:
                raise ScopeNotFound("角色资料不存在或不属于此窗口")
            head_visible = self.page._sources_visible(self._refs(kind, head))
            service = getattr(self.runtime, "intentions" if kind == "intention" else kind)
            if kind == "growth":
                records = service.history(self.scope, target)[:limit+1]
                raw = self.conn.execute("SELECT * FROM growth_history WHERE scope_key=? AND field=? ORDER BY version DESC LIMIT ?",
                                        (self.page.scope_key, target, limit+1)).fetchall()
            elif kind == "companionship":
                records = service.history(self.scope, target, context=context, limit=limit+1)
                raw = self.conn.execute("SELECT * FROM companionship_history WHERE scope_key=? AND preference_key=? AND context=? ORDER BY version DESC LIMIT ?",
                                        (self.page.scope_key, target, context, limit+1)).fetchall()
            elif kind == "narrative":
                records = service.history(self.scope, target, limit=limit+1)
                raw = self.conn.execute("SELECT * FROM narrative_versions WHERE entry_id=? ORDER BY version DESC LIMIT ?",
                                        (target, limit+1)).fetchall()
            else:
                records = service.history(self.scope, target, limit=limit+1)
                raw = self.conn.execute("SELECT * FROM intention_revisions WHERE intention_id=? ORDER BY version DESC LIMIT ?",
                                        (target, limit+1)).fetchall()
            rows = {row["version"]: row for row in raw}
            for item in records[:limit]:
                row = rows[item["version"]]
                if kind == "intention":
                    snapshot = item.get("snapshot") or {}
                    refs = snapshot.get("source_refs", [])+snapshot.get("confirmation_refs", [])
                else:
                    refs = self._refs(kind, row)
                denied = not head_visible or not self.page._sources_visible(refs)
                if "snapshot" in item:
                    if denied:
                        item["snapshot"] = None
                    item["redacted"] = not item["snapshot"] or bool(item["snapshot"].get("redacted"))
                else:
                    if denied:
                        item["value"] = None
                        if "available" in item:
                            item["available"] = False
                    item["redacted"] = denied or (item["value"] is None and item["state"] != "revoked")
            fitted = _fit_items(records[:limit])
            more = len(records) > limit or len(fitted) < min(len(records), limit)
            return {"items": fitted, "has_more": more, "truncated": more}

    def _validate_payload(self, kind, payload):
        if not isinstance(payload, dict) or set(payload)-_PAYLOADS[kind]:
            raise ValueError("角色资料字段无效")
        if kind == "narrative":
            text(payload.get("title"), "title", 150); text(payload.get("text"), "text", 2000)
            if payload.get("perspective") not in ("user", "character", "shared"):
                raise ValueError("perspective 无效")
            if payload.get("modality") not in ("occurred", "shared_fiction"):
                raise ValueError("modality 无效")
        elif kind == "intention":
            text(payload.get("text"), "text", 1000)
            if payload.get("owner") not in ("user", "character", "shared") or payload.get("category") not in ("promise", "continuation", "creative"):
                raise ValueError("owner/category 无效")
            if payload.get("event_trigger") is not None:
                text(payload["event_trigger"], "event_trigger", 100)
        else:
            text(payload.get("value"), "value", 1000)
        field = "occurred_at" if kind == "narrative" else "due_at"
        result = dict(payload)
        if result.get(field) is not None:
            result[field] = timestamp(result[field])
        return result

    def _write(self, args, *, transition):
        from .desktop import _digest

        kind = self._kind(args)
        target, context = self._target(kind, args, optional=not transition)
        expected = integer(args.get("expected_version"), "expected_version", 0, 2**31-1)
        reason = text(args.get("reason"), "reason", 500)
        request_id = text(args.get("request_id"), "request_id", 128)
        if "allow_recent" in args and (kind != "growth" or not isinstance(args["allow_recent"], bool)):
            raise ValueError("allow_recent 仅支持成长资料且须为布尔值")
        payload = None if transition else self._validate_payload(kind, args.get("payload"))
        states = {"narrative": ("withdrawn",), "intention": ("completed", "cancelled"),
                  "companionship": ("revoked",), "growth": ("revoked",)}
        if transition and args.get("state") not in states[kind]:
            raise ValueError("不支持此角色状态变更")
        action = "role_transition" if transition else "role_save"
        operation_id = "desktop-role:"+_digest([self.page.scope_key, request_id])
        with self.db.transaction():
            confirmation, _ = self.page._confirmation(action, args)
            # Revalidate independently of page rendering: partial source
            # suppression may produce a displayable, redacted event view.
            self.evidence.capture(self.scope, [confirmation], require_user=True)
            prior = self.conn.execute("SELECT payload_json FROM journal_entries WHERE scope_key=? AND event_type='desktop.role_operation' AND idempotency_key=?",
                                      (self.page.scope_key, operation_id)).fetchone()
            if prior:
                return json.loads(prior["payload_json"])
            head = self._head(kind, target, context)
            actual = head["version"] if head else 0
            if actual != expected:
                raise VersionConflict(expected, actual, "角色资料")
            if head is not None and not self._usable(kind, head):
                raise MemorySuppressed("原资料来源已失效或此窗口无权访问，不能通过编辑恢复")
            if head is None and (transition or (target is not None and kind in ("narrative", "intention"))):
                raise ScopeNotFound("角色资料不存在或不属于此窗口")
            reviewer = "desktop:"+confirmation
            common = {"user_event_id": confirmation, "reviewer": reviewer, "reason": reason}
            service = getattr(self.runtime, "intentions" if kind == "intention" else kind)
            if transition:
                if kind == "narrative":
                    if head["state"] != "accepted":
                        raise ValueError("只能撤回当前有效自传")
                    result = service.withdraw(self.scope, target, expected_version=expected, reason=reason)
                elif kind == "intention":
                    result = service.resolve(self.scope, target, state=args["state"], expected_version=expected,
                        evidence=[confirmation], reviewer=reviewer, reason=reason, request_id=operation_id)
                elif kind == "companionship":
                    if head["state"] != "confirmed":
                        raise ValueError("只能撤销当前已确认偏好")
                    result = service.revoke(self.scope, target, context=context, expected_version=expected,
                                            idempotency_key=operation_id, **common)
                else:
                    if head["state"] != "active":
                        raise ValueError("只能撤销当前有效成长资料")
                    result = service.revoke(self.scope, target, expected_version=expected, request_id=operation_id, **common)
            elif kind == "narrative":
                proposal = service.propose(self.scope, payload["title"], payload["text"], [confirmation],
                    perspective=payload["perspective"], modality=payload["modality"], occurred_at=payload.get("occurred_at"),
                    entry_id=target, expected_version=expected, request_id=operation_id)
                result = service.approve(self.scope, proposal["proposal_id"], **common)
                target = result.get("entry_id")
            elif kind == "intention":
                if head:
                    if head["owner"] != payload["owner"] or head["category"] != payload["category"]:
                        raise ValueError("修订不能改变承诺的 owner/category，请另建承诺")
                    proposal = service.revise(self.scope, target, text=payload["text"], evidence=[confirmation],
                        expected_version=expected, due_at=payload.get("due_at"), event_trigger=payload.get("event_trigger"),
                        request_id=operation_id+":revise")
                else:
                    proposal = service.propose(self.scope, payload["text"], [confirmation], owner=payload["owner"],
                        category=payload["category"], due_at=payload.get("due_at"), event_trigger=payload.get("event_trigger"),
                        request_id=operation_id+":propose")
                    target = proposal["intention_id"]
                result = service.confirm(self.scope, target, expected_version=proposal["version"],
                    confirmation_refs=[confirmation], reviewer=reviewer, reason=reason, request_id=operation_id+":confirm")
            elif kind == "companionship":
                proposal = service.propose(self.scope, target, payload["value"], context=context,
                    evidence=[confirmation], reason=reason, idempotency_key=operation_id)
                result = service.confirm(self.scope, proposal["proposal_id"], expected_version=expected, **common)
            else:
                proposal = service.propose(self.scope, target, payload["value"], [confirmation], reason=reason, request_id=operation_id)
                result = service.approve(self.scope, proposal["proposal_id"], expected_version=expected,
                    request_id=operation_id+":approve", allow_recent=args.get("allow_recent", False), **common)
            if result.get("applied") is False or result.get("changed") is False:
                raise MemorySuppressed("资料审批未生效，请刷新后重试")
            current = self._head(kind, target, context)
            receipt = {"role_type": kind, "id": target, "version": current["version"], "state": current["state"], "applied": True}
            if context is not None:
                receipt["context"] = context
            response = {"receipt": receipt, "confirmation_event_id": confirmation}
            # No role body in the journal: old receipts must not retain erased
            # narratives or values. Return this exact receipt on later retries.
            self.db.repositories.journal.append("desktop.role_operation", scope=self.scope, entity_id=target,
                payload=response, now=self.runtime.clock.now(), idempotency_key=operation_id)
            return response

    def save(self, args):
        return self._write(args, transition=False)

    def transition(self, args):
        return self._write(args, transition=True)
