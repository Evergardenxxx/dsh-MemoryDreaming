"""有来源、版本及用户确认的桌面核心编辑，不向模型增加写入权限。

字段移除是版本修订，不是遗忘；字段日志保留明确的删除依据，而当前值
永远不因删除依据失效而回退旧值。遗忘继续由原有账本和来源规则控制。
"""

from __future__ import annotations

import json
import math

from .assertions import core_scope_key
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .life_evidence import LifeEvidence
from .llm import AdapterError, dumps, integer, text
from .models import CoreKind
from .storage.repository import _core_from_row


MAX_FIELDS = 32
MAX_JSON_BYTES = 12000
MAX_SAFE_INTEGER = 2**53 - 1


def _json_value(value, depth=0):
    """只接受浏览器能够无损往返的有界 JSON；bool 与整数保持不同类型。"""
    if depth > 8:
        raise ValueError("核心字段嵌套超过8层")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError("核心字段字符串最多4000字符")
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("核心字段数字须为有限数，整数不能超过JavaScript安全范围")
        return
    if isinstance(value, float):
        if not math.isfinite(value) or (value.is_integer() and abs(value) > MAX_SAFE_INTEGER):
            raise ValueError("核心字段数字须为有限数，整数不能超过JavaScript安全范围")
        return
    if isinstance(value, list) and len(value) <= MAX_FIELDS:
        for item in value:
            _json_value(item, depth+1)
        return
    if isinstance(value, dict) and len(value) <= MAX_FIELDS:
        for name, item in value.items():
            text(name, "JSON字段名", 100)
            _json_value(item, depth+1)
        return
    raise ValueError("核心字段须为有界的JSON值")


class DesktopCore:
    def __init__(self, desktop):
        self.page, self.runtime, self.scope = desktop, desktop.runtime, desktop.scope
        self.db, self.conn = desktop.db, desktop.conn
        self.evidence = LifeEvidence(self.runtime)

    @staticmethod
    def _kind(args):
        name = args.get("core_kind")
        if not isinstance(name, str) or name not in ("identity", "relationship"):
            raise ValueError("core_kind 须为identity或relationship")
        return CoreKind(name)

    def _raw(self, kind):
        row = self.conn.execute("SELECT * FROM core_versions WHERE scope_key=? AND kind=? AND status!='rejected' "
            "ORDER BY core_version DESC LIMIT 1", (core_scope_key(self.scope, kind), kind.value)).fetchone()
        return _core_from_row(row) if row else None

    def _source_views(self, refs):
        if not isinstance(refs, list) or len(refs) > 20:
            raise MemorySuppressed("核心字段来源超过管理页上限")
        reader = self.page._reader()
        views = []
        for ref in refs:
            view = reader.source(ref)
            # ViewReader 可展示部分遮蔽后的原话；整个原话若有抑制片段，
            # 就不能再作为完整的编辑依据。活动权限先由页面reader独立验证。
            obj = (self.db.repositories.memories.get(ref) if view.kind == "memory"
                   else self.db.repositories.events.get(ref))
            if obj is None:
                raise MemorySuppressed("核心字段来源已不存在")
            self.evidence.capture(obj.scope, [ref])
            views.append(view)
        return views

    def _project(self, core):
        visible = self.db.repositories.forgetting.mask_core(core)
        fields, sources = {}, {}
        hidden = set(core.payload) != set(visible.payload)
        for name, value in visible.payload.items():
            try:
                views = self._source_views(visible.field_evidence_ids.get(name, visible.evidence_ids))
            except (AdapterError, MemorySuppressed, ScopeNotFound, ValueError):
                hidden = True
                continue
            fields[name] = value
            sources[name] = [{"kind": view.kind, "id": view.target, "revision": view.revision,
                              "text": view.data.get("text", "")[:2000],
                              "truncated": len(view.data.get("text", "")) > 2000} for view in views]
        return fields, sources, hidden

    def _field_rows(self, kind, version=None):
        sql = "SELECT f.* FROM core_field_revisions f JOIN core_versions c USING(scope_key,kind,core_version) "
        sql += "WHERE f.scope_key=? AND f.kind=? AND c.status!='rejected'"
        values = [core_scope_key(self.scope, kind), kind.value]
        if version is not None:
            sql += " AND f.core_version=?"; values.append(version)
        sql += " ORDER BY f.effective_from DESC,f.recorded_at DESC,f.core_version DESC"
        return self.conn.execute(sql, values).fetchall()

    def _revision(self, kind, raw, fields, sources, hidden):
        from .desktop import _digest
        # Include exact current projection and lineage, not just public fields:
        # another relation's source change must still invalidate a stale form.
        return _digest([kind.value, raw.core_version if raw else 0,
            raw.payload if raw else {}, raw.field_evidence_ids if raw else {}, fields, sources, hidden,
            self.db.repositories.forgetting.epoch(self.scope),
            [dict(row) for row in self._field_rows(kind)]])

    @staticmethod
    def _complete_fields(fields):
        try:
            if len(fields) > MAX_FIELDS or len(dumps(fields).encode("utf-8")) > MAX_JSON_BYTES:
                return False
            for name, value in fields.items():
                text(name, "核心字段名", 100)
                _json_value(value)
            return True
        except (AdapterError, ValueError, TypeError, OverflowError):
            return False

    def _state(self, kind):
        raw = self._raw(kind)
        fields, sources, hidden = self._project(raw) if raw else ({}, {}, False)
        revision = self._revision(kind, raw, fields, sources, hidden)
        complete = (self._complete_fields(fields)
                    and len(dumps({"fields": fields, "field_sources": sources}).encode("utf-8")) <= 80000)
        # 不返回半个JSON值供编辑；超出可安全往返的范围时隐藏整个字段集合。
        if not complete:
            fields, sources = {}, {}
        return raw, {"core_kind": kind.value, "exists": raw is not None,
            "version": raw.core_version if raw else 0, "revision": revision,
            "fields": fields, "field_sources": sources, "redacted_fields": hidden,
            "editable": bool(raw) and not hidden and complete and not self.page.read_only,
            "truncated": not complete}

    def editor(self, args):
        kind = self._kind(args)
        limit = integer(args.get("limit", 20), "limit", 1, 50)
        with self.db.transaction(write=False):
            raw, result = self._state(kind)
            rows = self.conn.execute("SELECT * FROM core_versions WHERE scope_key=? AND kind=? "
                "ORDER BY core_version DESC LIMIT ?", (core_scope_key(self.scope, kind), kind.value, limit+1)).fetchall()
            history, size, clipped = [], 0, False
            for row in rows[:limit]:
                item = _core_from_row(row)
                fields, _, redacted = self._project(item)
                # 操作理由也可能含私密原话；其本版本修改/删除的授权已失效
                # 时应隐藏理由，不能因仍有其他可见字段而泄露。
                authorized = row["status"] != "rejected"
                for changed in self._field_rows(kind, item.core_version):
                    try:
                        self._source_views(json.loads(changed["evidence_json"]))
                    except (AdapterError, MemorySuppressed, ScopeNotFound, ValueError):
                        authorized = False
                redacted |= not authorized
                complete = self._complete_fields(fields)
                entry = {"version": item.core_version, "fields": fields if complete else {},
                    "reason": item.change_reason if not redacted else None,
                    "created_at": row["created_at"], "effective_from": row["effective_from"],
                    "redacted": redacted, "truncated": not complete}
                cost = len(dumps(entry).encode("utf-8"))
                if size + cost > 96000:
                    clipped = True; break
                history.append(entry); size += cost
            result["history"] = {"items": history, "has_more": clipped or len(rows) > limit,
                                 "truncated": clipped}
            if len(dumps(result).encode("utf-8")) > 192000:
                result.update(fields={}, field_sources={}, editable=False, truncated=True)
            return result

    def _guard_old_names(self, kind, names, current):
        # A forgotten value's name can disappear from the current redacted
        # head. Guessed patches must not use that apparent absence as new auth.
        latest = {}
        for row in self._field_rows(kind):
            latest.setdefault(row["field_name"], row)
        for name in names:
            row = latest.get(name)
            if row is None or name in current:
                continue
            try:
                self._source_views(json.loads(row["evidence_json"]))
                # 遗忘清理也会留下删除修订；其删除行可能只带其余字段的
                # 来源。再核对删除前最后一个非删除值，禁止猜名字改值绕过。
                if row["deleted"]:
                    previous = next((old for old in self._field_rows(kind)
                                     if old["field_name"] == name and not old["deleted"]), None)
                    if previous is not None:
                        self._source_views(json.loads(previous["evidence_json"]))
            except (AdapterError, MemorySuppressed, ScopeNotFound, ValueError):
                raise MemorySuppressed("旧字段依据不可用，不能通过编辑恢复") from None

    def save(self, args):
        from .desktop import _digest
        kind = self._kind(args)
        expected = integer(args.get("expected_version"), "expected_version", 1, 2**31-1)
        expected_revision = text(args.get("expected_revision"), "expected_revision", 128)
        reason, request_id = text(args.get("reason"), "reason", 500), text(args.get("request_id"), "request_id", 128)
        patch, removed = args.get("patch"), args.get("remove_fields", [])
        if not isinstance(patch, dict) or len(patch) > MAX_FIELDS or not isinstance(removed, list) or len(removed) > MAX_FIELDS:
            raise ValueError("patch和remove_fields须为至多32项的字段对象与字段名列表")
        for name, value in patch.items():
            text(name, "核心字段名", 100); _json_value(value)
        for name in removed:
            text(name, "核心字段名", 100)
        if len(set(removed)) != len(removed) or set(patch).intersection(removed) or not (patch or removed):
            raise ValueError("核心修改不能为空，patch与remove_fields不能重叠或重复")
        if len(dumps(patch).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("核心修改超过12000字节")
        operation_id = "desktop-core:"+_digest([self.page.scope_key, request_id])
        with self.db.transaction():
            confirmation, _ = self.page._confirmation("core_save", args)
            self.evidence.capture(self.scope, [confirmation], require_user=True)
            prior = self.conn.execute("SELECT payload_json FROM journal_entries WHERE scope_key=? "
                "AND event_type='desktop.core_operation' AND idempotency_key=?",
                (self.page.scope_key, operation_id)).fetchone()
            if prior:
                return json.loads(prior["payload_json"])
            raw, state = self._state(kind)
            if raw is None:
                raise ScopeNotFound("空角色请使用初始化")
            if raw.core_version != expected:
                raise VersionConflict(expected, raw.core_version, "核心资料")
            if state["revision"] != expected_revision:
                raise AdapterError("STALE_REFERENCE", "核心或来源已变化，请刷新后重新核对")
            if not state["editable"]:
                raise MemorySuppressed("核心字段未完整显示或来源不可用，不能编辑")
            self._guard_old_names(kind, set(patch) | set(removed), state["fields"])
            merged = {name: value for name, value in state["fields"].items() if name not in removed}
            merged.update(patch)
            if any(name not in state["fields"] for name in removed):
                raise ValueError("只能移除当前已显示的字段")
            if not self._complete_fields(merged):
                raise ValueError("修改后的核心超过完整编辑上限")
            if dumps(merged) == dumps(state["fields"]):
                raise ValueError("核心内容没有变化")
            core = self.runtime.apply_core_update(self.scope, kind, patch, reason, expected_version=expected,
                evidence_ids=[confirmation], remove_fields=removed)
            response = {"receipt": {"core_kind": kind.value, "version": core.core_version, "applied": True},
                        "confirmation_event_id": confirmation}
            self.db.repositories.journal.append("desktop.core_operation", scope=self.scope, entity_id=kind.value,
                payload=response, now=self.runtime.clock.now(), idempotency_key=operation_id)
            return response
