"""Trusted desktop management facade, separate from model-facing memory tools.

The native host authenticates the UI channel and constructs ``SessionBinding``.
This module never accepts a database path or a replacement identity from page
arguments. Reads reuse ViewReader's source/forgetting checks; changes reuse the
existing proposal services, their optimistic versions and durable receipts.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json

from .errors import MemorySuppressed, ScopeNotFound, SourceRevisionChanged, VersionConflict
from .host import HostAdapter, MemorySession, SessionBinding
from .llm import AdapterError, dumps, integer, text
from .models import CoreKind, ReadFilter, Turn
from .proposals import ChangeProposals, CoreProposalPolicy
from .references import ViewReader, forbidden, stale
from .storage.repository import _scope_key, retrieval_sql


_READ_ACTIONS = {
    "overview": set(), "memories": {"memory_type", "query", "offset", "limit"},
    "record": {"kind", "id"}, "proposals": {"proposal_type", "offset", "limit"},
    "role": set(), "sleep": {"limit"},
    "role_catalog": {"role_type", "offset", "limit"},
    "role_history": {"role_type", "id", "context", "limit"},
}
_WRITE_ACTIONS = {
    "initialize_role": {"identity_fields", "relationship_fields", "expected_version", "request_id"},
    "proposal_decide": {"proposal_type", "id", "decision", "expected_revision", "request_id", "reason", "expected_version"},
    "memory_forget": {"id", "expected_revision", "request_id", "reason"},
    "memory_correct": {"id", "expected_revision", "request_id", "reason", "body"},
    "role_save": {"role_type", "id", "context", "expected_version", "payload", "reason", "request_id", "allow_recent"},
    "role_transition": {"role_type", "id", "context", "expected_version", "state", "reason", "request_id"},
}
_PROPOSALS = {
    "core": ("core_proposals", "pending_validation"),
    "change": ("change_proposals", "pending_validation"),
    "narrative": ("narrative_proposals", "proposed"),
    "growth": ("growth_proposals", "pending"),
    "companionship": ("companionship_proposals", "pending"),
}


def _digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def _bounded(value, *, chars=4000, depth=0):
    """Bound private page payloads without treating them as prompt material."""
    if depth > 10:
        return None
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + "…"
    if isinstance(value, dict):
        return {key: _bounded(item, chars=chars, depth=depth+1) for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, chars=chars, depth=depth+1) for item in value[:50]]
    return value


class DesktopService:
    """One authenticated page's fixed role scope; never installed as an LLM tool."""

    def __init__(self, adapter: HostAdapter, binding: SessionBinding, *, read_only=False):
        if not isinstance(adapter, HostAdapter) or not isinstance(binding, SessionBinding):
            raise TypeError("DesktopService requires a trusted HostAdapter and SessionBinding")
        if not isinstance(read_only, bool):
            raise ValueError("read_only must be boolean")
        self.adapter, self.binding, self.read_only = adapter, binding, read_only
        self.runtime, self.scope = adapter.runtime, binding.scope
        self.db, self.conn = self.runtime.db, self.runtime.db.connection
        self.scope_key = _scope_key(self.scope)

    def handle(self, action, arguments=None):
        if not isinstance(action, str) or action not in (_READ_ACTIONS | _WRITE_ACTIONS):
            raise AdapterError("UNSUPPORTED", "未知的桌面管理操作")
        args = {} if arguments is None else arguments
        allowed = (_READ_ACTIONS | _WRITE_ACTIONS)[action]
        if not isinstance(args, dict) or set(args) - allowed:
            raise AdapterError("INVALID_ARGUMENT", "管理参数无效，不能覆盖宿主身份或作用域")
        if self.read_only and action in _WRITE_ACTIONS:
            raise AdapterError("READ_ONLY", "当前管理连接只允许查看")
        try:
            result = getattr(self, "_"+action)(args)
            clipped = _bounded(result, chars=16000)
            if clipped != result:
                clipped["truncated"] = True
            if len(dumps(clipped).encode("utf-8")) > 262144:
                raise AdapterError("RESPONSE_TOO_LARGE", "资料超过管理页面单次上限，请减少每页条数")
            return clipped
        except AdapterError:
            raise
        except (VersionConflict, SourceRevisionChanged) as exc:
            raise AdapterError("STALE_REFERENCE", "资料已变化，请刷新后再确认") from exc
        except (ScopeNotFound, MemorySuppressed) as exc:
            raise forbidden() from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("INVALID_ARGUMENT", str(exc)[:300]) from exc

    def _reader(self):
        return ViewReader(self.runtime, self.binding, self.runtime.clock)

    def _role_catalog(self, args):
        from .desktop_roles import DesktopRoles
        return DesktopRoles(self).catalog(args)

    def _role_history(self, args):
        from .desktop_roles import DesktopRoles
        return DesktopRoles(self).history(args)

    def _role_save(self, args):
        from .desktop_roles import DesktopRoles
        return DesktopRoles(self).save(args)

    def _role_transition(self, args):
        from .desktop_roles import DesktopRoles
        return DesktopRoles(self).transition(args)

    @staticmethod
    def _page(args):
        return (integer(args.get("offset", 0), "offset", 0, 1000000),
                integer(args.get("limit", 20), "limit", 1, 50))

    def _visible_sql(self):
        return retrieval_sql(self.scope, read_filter=ReadFilter(
            activity_ids=(None, *self.binding.allowed_activity_ids)))

    def _core(self, kind):
        try:
            view = self._reader().load("core_version", kind)
            return {"state": "recovered", "version": view.data["record_version"],
                    "fields": json.loads(view.data["text"]), "revision": view.revision}
        except AdapterError:
            return {"state": "missing", "version": None, "fields": {}, "revision": None}

    def _cores(self):
        return {kind: self._core(kind) for kind in ("identity", "relationship")}

    def _overview(self, args):
        with self.db.transaction(write=False):
            where, values = self._visible_sql()
            counts = {kind: 0 for kind in ("working", "episodic", "semantic", "procedural")}
            for row in self.conn.execute("SELECT memory_type,COUNT(*) AS n FROM memories WHERE "+where+
                    " AND status='active' GROUP BY memory_type", values):
                counts[row["memory_type"]] = row["n"]
            cores = self._cores()
            # Raw latest cores are checked too: a forgotten source must never
            # turn an existing role into an apparently new, overwriteable role.
            can_initialize = all(self.db.repositories.cores.latest(self.scope, kind) is None for kind in CoreKind)
            binding = asdict(self.binding)
            binding["allowed_activity_ids"] = list(binding["allowed_activity_ids"])
            return {"binding": binding, "cores": cores, "counts": counts,
                    "needs_initialization": can_initialize,
                    "capabilities": {"read_only": self.read_only,
                        "actions": list(_READ_ACTIONS) + ([] if self.read_only else list(_WRITE_ACTIONS)),
                        "proposal_types": list(_PROPOSALS)},
                    "health": self.runtime.memory_health(self.scope)}

    def _view_record(self, view, *, detail=False):
        result = {"id": view.target, "kind": view.kind, "revision": view.revision, **view.data}
        original = result.get("text", "")
        result["text"] = original[:16000 if detail else 1500]
        result["truncated"] = len(original) > len(result["text"])
        if view.kind in ("memory", "core_version"):
            body = json.loads(original)
            result["body"] = _bounded(body, chars=16000 if detail else 1500)
            result["truncated"] |= result["body"] != body
        result["source_count"] = len(view.sources)
        if view.kind == "memory":
            item = self.db.repositories.memories.get(view.target)
            result["status"] = item.status.value
            result["recorded_at"] = item.recorded_at.isoformat()
        if detail:
            result["sources"] = [{"id": source.target, "kind": source.kind,
                "revision": source.revision, "text": source.data.get("text", "")[:2000],
                "truncated": len(source.data.get("text", "")) > 2000} for source in view.sources[:20]]
        return result

    def _memories(self, args):
        offset, limit = self._page(args)
        where, values = self._visible_sql()
        where += " AND status NOT IN ('deleted','retracted')"
        kind = args.get("memory_type")
        if kind not in (None, "working", "episodic", "semantic", "procedural"):
            raise ValueError("memory_type 无效")
        if kind:
            where += " AND memory_type=?"; values.append(kind)
        query = args.get("query", "")
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("query 最多1000字符")
        if query:
            where += " AND body_text LIKE ? ESCAPE '\\'"
            values.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        scan_limit = min(250, max(100, limit*5))
        with self.db.transaction(write=False):
            rows = self.conn.execute("SELECT memory_id FROM memories WHERE "+where+
                " ORDER BY recorded_at DESC,memory_id DESC LIMIT ? OFFSET ?", (*values, scan_limit+1, offset)).fetchall()
            items, scanned = [], 0
            for row in rows[:scan_limit]:
                scanned += 1
                try:
                    items.append(self._view_record(self._reader().load("memory", row[0])))
                except AdapterError:
                    continue
                if len(items) == limit:
                    break
            has_more = len(rows) > scanned
            return {"items": items, "offset": offset,
                    "next_offset": offset+scanned if has_more else None, "has_more": has_more}

    def _record(self, args):
        kind = args.get("kind", "memory")
        if kind not in ("memory", "event", "core_version"):
            raise ValueError("record kind 无效")
        target = text(args["id"], "id", 200)
        with self.db.transaction(write=False):
            return self._view_record(self._reader().load(kind, target), detail=True)

    def _proposal_session(self, row):
        event = self.db.repositories.events.get(row["request_event_id"])
        if event is None or not self._reader().visible(event.scope):
            raise forbidden()
        # Model proposal policy governs creation, not the owner's review of an
        # already persisted proposal. A fresh bridge normally has that model
        # policy closed; inspecting with it would permanently invalidate valid
        # candidates. This narrow operator adapter exposes only fields already
        # present in this proposal and never replaces the model's adapter.
        if "target_kind" in row.keys():
            names = tuple(field["name"] for field in json.loads(row["fields_json"]))
            policy = CoreProposalPolicy(**{row["target_kind"]+"_fields": names})
        else:
            policy = CoreProposalPolicy(change_proposals=True, checkpoint_proposals=True)
        operator = HostAdapter(self.runtime, core_policy=policy)
        return MemorySession(operator, replace(self.binding, conversation_id=event.conversation_id))

    def _proposal(self, proposal_type, proposal_id):
        if proposal_type not in _PROPOSALS:
            raise ValueError("proposal_type 无效")
        table = _PROPOSALS[proposal_type][0]
        row = self.conn.execute("SELECT * FROM "+table+" WHERE proposal_id=? AND scope_key=?",
                                (proposal_id, self.scope_key)).fetchone()
        if row is None:
            raise forbidden()
        if proposal_type in ("core", "change"):
            session = self._proposal_session(row)
            data = (session.get_core_proposal if proposal_type == "core" else session.get_change_proposal)(proposal_id)
            if proposal_type == "change" and data["state"] == "pending_validation":
                # Existing public inspection deliberately hides the patch from
                # model tools; a human must see it before approving a change.
                data["candidate"] = json.loads(row["payload_json"])
        else:
            data = getattr(self.runtime, proposal_type).inspect(self.scope, proposal_id)
            if proposal_type == "growth" and data["state"] == "pending":
                head = self.conn.execute("SELECT version FROM growth_values WHERE scope_key=? AND field=?",
                    (self.scope_key, row["field"])).fetchone()
                data["expected_version"] = head[0] if head else 0
            for name in ("sources", "evidence"):
                self._require_visible_sources(data.get(name, []))
        result = {"id": proposal_id, "proposal_type": proposal_type, "created_at": row["created_at"], **data}
        result["truncated"] = _bounded(result, chars=16000) != result or len(dumps(result).encode("utf-8")) > 65536
        result["revision"] = _digest(result)
        return result

    def _require_visible_sources(self, refs):
        """Domain evidence validity and page activity authorization are separate.

        LifeEvidence's revision format is intentionally not ViewReader's page
        revision. Domain inspection checks the former; this check enforces the
        page's independently fixed allowed activity list.
        """
        for ref in refs:
            self._reader().load(ref["kind"], ref["target"])

    def _sources_visible(self, refs):
        try:
            self._require_visible_sources(refs)
            return True
        except AdapterError:
            return False

    def _proposals(self, args):
        offset, limit = self._page(args)
        category = args.get("proposal_type")
        if category is not None and category not in _PROPOSALS:
            raise ValueError("proposal_type 无效")
        categories = [category] if category else list(_PROPOSALS)
        selects, values = [], []
        for name in categories:
            table, state = _PROPOSALS[name]
            selects.append("SELECT proposal_id,created_at,'"+name+"' AS category FROM "+table+" WHERE scope_key=? AND state=?")
            values.extend((self.scope_key, state))
        rows = self.conn.execute(" UNION ALL ".join(selects)+" ORDER BY created_at DESC,proposal_id DESC LIMIT ? OFFSET ?",
                                 (*values, 251, offset)).fetchall()
        items, scanned = [], 0
        for row in rows[:250]:
            scanned += 1
            try:
                item = self._proposal(row["category"], row["proposal_id"])
                if item["state"] == _PROPOSALS[row["category"]][1]:
                    items.append(item)
            except AdapterError:
                continue
            if len(items) == limit:
                break
        has_more = len(rows) > scanned
        return {"items": items, "offset": offset, "next_offset": offset+scanned if has_more else None, "has_more": has_more}

    def _confirmation(self, action, args):
        request_id = text(args["request_id"], "request_id", 128)
        material = {"action": action, "arguments": {key: value for key, value in args.items() if key != "request_id"}}
        content = "桌面用户明确操作："+dumps(material)
        if len(content) > 20000:
            raise ValueError("管理操作内容过长")
        key = "desktop:"+_digest([self.scope_key, request_id])
        old = self.conn.execute("SELECT event_id,content FROM events WHERE idempotency_key=?", (key,)).fetchone()
        if old is not None:
            if old["content"] != content:
                raise AdapterError("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同的管理操作")
            self._reader().load("event", old["event_id"])
            return old["event_id"], True
        # This event is explicit user authorization, not conversational input.
        # Preserve its evidence chain without extracting facts from form JSON
        # (which can contain shared fiction or changes still awaiting review).
        event = self.runtime.observe(Turn(speaker="user", content=content, occurred_at=self.runtime.clock.now(),
            conversation_id="desktop-management"), self.scope, key, enqueue_extraction=False).event
        return event.event_id, False

    def _proposal_decide(self, args):
        category = args["proposal_type"]
        proposal_id = text(args["id"], "id", 200)
        decision = args["decision"]
        if decision not in ("approve", "reject"):
            raise ValueError("decision 必须为approve或reject")
        expected = text(args["expected_revision"], "expected_revision", 128)
        reason = text(args["reason"], "reason", 500)
        with self.db.transaction():
            candidate = self._proposal(category, proposal_id)
            if candidate.get("truncated") and decision == "approve":
                raise AdapterError("CANDIDATE_TOO_LARGE", "候选内容未完整展示，不能直接批准")
            confirmation, duplicate = self._confirmation("proposal_decide", args)
            pending = candidate["state"] == _PROPOSALS[category][1]
            if candidate["revision"] != expected and (not duplicate or pending):
                raise stale()
        reviewer = "desktop:"+confirmation
        approved = decision == "approve"
        if category in ("core", "change"):
            table = _PROPOSALS[category][0]
            row = self.conn.execute("SELECT * FROM "+table+" WHERE proposal_id=? AND scope_key=?", (proposal_id, self.scope_key)).fetchone()
            session = self._proposal_session(row)
            callback = getattr(session, ("approve_" if approved else "reject_")+category+"_proposal")
            # Original request remains the proposal's evidence. The separate
            # click event authenticates this human decision and is in reviewer.
            receipt = callback(proposal_id, user_event_id=row["request_event_id"], reviewer=reviewer, reason=reason)
        elif category == "narrative":
            service = self.runtime.narrative
            receipt = (service.approve(self.scope, proposal_id, reviewer, reason, user_event_id=confirmation) if approved
                       else service.reject(self.scope, proposal_id, reviewer, reason))
        elif category == "companionship":
            receipt = self.runtime.companionship.feedback(self.scope, proposal_id, approved=approved,
                user_event_id=confirmation, reviewer=reviewer, reason=reason,
                expected_version=integer(args.get("expected_version", candidate.get("expected_version", 0)), "expected_version", 0, 1000000))
        elif category == "growth":
            if approved:
                expected_version = args.get("expected_version", candidate.get("expected_version", 0))
                if duplicate and "expected_version" not in args:
                    prior = self.conn.execute("SELECT receipt_json FROM growth_receipts WHERE scope_key=? AND request_id=?",
                                              (self.scope_key, args["request_id"])).fetchone()
                    receipt = json.loads(prior[0]) if prior else None
                    if receipt and receipt.get("proposal_id") == proposal_id and receipt.get("state") == "applied":
                        # A successful retry must use the original input
                        # version, even after later edits moved the live head.
                        # The domain receipt still verifies its full request
                        # fingerprint, including this version and confirmation.
                        expected_version = receipt["version"]-1
                receipt = self.runtime.growth.approve(self.scope, proposal_id, user_event_id=confirmation,
                    reviewer=reviewer, reason=reason,
                    expected_version=integer(expected_version, "expected_version", 0, 1000000),
                    request_id=args["request_id"])
            else:
                receipt = self.runtime.growth.reject(self.scope, proposal_id, user_event_id=confirmation,
                    reviewer=reviewer, reason=reason, request_id=args["request_id"])
        return {"receipt": receipt, "confirmation_event_id": confirmation}

    def _memory_change(self, action, args):
        target = text(args["id"], "id", 200)
        expected = text(args["expected_revision"], "expected_revision", 128)
        reason = text(args["reason"], "reason", 500)
        body = args.get("body")
        if action == "memory_correct" and (not isinstance(body, dict) or len(body) > 20 or len(dumps(body)) > 12000):
            raise ValueError("纠错需要完整且有界的四类记忆结构体")
        # A management-only policy enables explicit human mutations without
        # changing which tools/proposals the conversation model may access.
        operator = HostAdapter(self.runtime, core_policy=CoreProposalPolicy(change_proposals=True))
        session = MemorySession(operator, replace(self.binding, conversation_id="desktop-management"))
        with self.db.transaction():
            confirmation, duplicate = self._confirmation(action, args)
            session.current_event_id = confirmation
            old = self.conn.execute("SELECT proposal_id FROM change_proposals WHERE scope_key=? AND request_event_id=?",
                                    (self.scope_key, confirmation)).fetchone()
            if old is not None:
                proposal_id = old[0]
            else:
                view = self._reader().load("memory", target)
                if view.revision != expected:
                    raise stale()
                reference = session.registry.issue(view)
                proposal = ChangeProposals(session).create("memory_request_change", {
                    "operation": "correct_fact" if action == "memory_correct" else "forget_fact",
                    "target_refs": [reference], "replacement_text": None, "replacement_body": body,
                    "reason": reason, "evidence_refs": [],
                })
                proposal_id = proposal["proposal_id"]
        receipt = session.approve_change_proposal(proposal_id, user_event_id=confirmation,
                                                  reviewer="desktop:"+confirmation, reason=reason)
        return {"receipt": receipt, "confirmation_event_id": confirmation}

    def _memory_forget(self, args):
        return self._memory_change("memory_forget", args)

    def _memory_correct(self, args):
        return self._memory_change("memory_correct", args)

    def _initialize_role(self, args):
        if integer(args["expected_version"], "expected_version", 0, 0) != 0:
            raise ValueError("只能初始化空角色")
        fields = {}
        for key in ("identity_fields", "relationship_fields"):
            value = args[key]
            if not isinstance(value, dict) or not 1 <= len(value) <= 12:
                raise ValueError(key+"须为1..12个明确字段")
            fields[key] = {text(name, "字段名", 100): text(item, "设定值", 1000) for name, item in value.items()}
        with self.db.transaction():
            confirmation, duplicate = self._confirmation("initialize_role", args)
            existing = [self.db.repositories.cores.latest(self.scope, kind) for kind in CoreKind]
            if any(core is not None for core in existing):
                # Successful transport retries may read the original result;
                # no other existing/hidden/shared core can be overwritten.
                if not duplicate or any(core is None or confirmation not in core.evidence_ids for core in existing):
                    raise AdapterError("ROLE_EXISTS", "已有身份或关系核心，初始化不能覆盖")
            else:
                for kind, key in (("identity", "identity_fields"), ("relationship", "relationship_fields")):
                    self.runtime.apply_core_update(self.scope, kind, fields[key], "桌面用户明确初始化角色",
                                                   expected_version=0, evidence_ids=[confirmation])
            return {"initialized": True, "confirmation_event_id": confirmation, "cores": self._cores()}

    def _role(self, args):
        story = self.runtime.narrative.timeline(self.scope, limit=20, max_tokens=4096)
        story["entries"] = [row for row in story["entries"] if self._sources_visible(row["sources"])]
        intentions = [row for row in self.runtime.intentions.list_open(self.scope, limit=20)
            if self._sources_visible(row["source_refs"]+row["confirmation_refs"])]
        companionship = [row for row in self.runtime.companionship.list_confirmed(self.scope, limit=32)
            if self._sources_visible(row["evidence"]+row["confirmation"])]
        growth = []
        for item in self.runtime.growth.profile(self.scope, limit=20):
            row = self.conn.execute("SELECT sources_json,confirmation_json FROM growth_values WHERE scope_key=? AND field=?",
                                    (self.scope_key, item["field"])).fetchone()
            if row and self._sources_visible(json.loads(row[0])+json.loads(row[1])):
                growth.append(item)
        return {"autobiography": story, "intentions": intentions, "companionship": companionship, "growth": growth}

    def _sleep(self, args):
        limit = integer(args.get("limit", 10), "limit", 1, 20)
        rows = self.conn.execute("SELECT sleep_id,conversation_id,created_at,completed_at FROM sleep_runs "
            "WHERE scope_key=? ORDER BY created_at DESC,sleep_id DESC LIMIT ?", (self.scope_key, limit)).fetchall()
        runs, dreams = [], []
        for row in rows:
            status = self.runtime.sleep.status(self.scope, row["sleep_id"])
            runs.append({**dict(row), **{key: status.get(key) for key in ("status", "review_complete", "review", "inventory")}})
            if status["status"] != "completed":
                continue
            dream = self.conn.execute("SELECT * FROM dreams WHERE sleep_id=? AND scope_key=? AND status='active'",
                                      (row["sleep_id"], self.scope_key)).fetchone()
            if dream is None:
                continue
            sources = []
            valid = True
            for ref in json.loads(dream["source_refs_json"]):
                try:
                    view = self._reader().load(ref["kind"], ref.get("source_id", ref.get("target", ref.get("id"))))
                    if view.revision != ref["revision"]:
                        valid = False; break
                    sources.append({"kind": view.kind, "id": view.target, "revision": view.revision})
                except AdapterError:
                    valid = False; break
            if valid:
                dreams.append({"id": dream["dream_id"], "sleep_id": dream["sleep_id"], "text": dream["text"][:6000],
                    "epistemic_status": "generated_fiction", "created_at": dream["created_at"], "sources": sources[:20]})
        return {"runs": runs, "dreams": dreams}
