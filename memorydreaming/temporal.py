"""核心字段双时间与摘要版本恢复；历史读取仍应用当前遗忘保护。"""

from dataclasses import asdict, replace
from datetime import datetime
import json

from .assertions import core_scope_key
from .clock import ensure_utc, from_utc_iso, to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .models import CoreKind, MemoryStatus, Scope, Summary


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def record_core_fields(conn, core, previous, changed_fields=None):
    before = previous.payload if previous else {}
    fields = core.field_evidence_ids
    if changed_fields is None:
        names = {name for name in set(before) | set(core.payload)
                 if before.get(name) != core.payload.get(name) or (name in before) != (name in core.payload)
                 or (previous and previous.field_evidence_ids.get(name, []) != fields.get(name, []))}
    else:
        names = set(changed_fields) | (set(before) - set(core.payload))
    for name in sorted(names):
        conn.execute(
            "INSERT OR IGNORE INTO core_field_revisions(scope_key,kind,core_version,field_name,value_json,"
            "evidence_json,deleted,effective_from,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (core_scope_key(core.scope, core.kind), core.kind.value, core.core_version, name,
             dumps(core.payload[name]) if name in core.payload else None,
             dumps(fields.get(name, core.evidence_ids)), int(name not in core.payload),
             to_utc_iso(core.effective_from), to_utc_iso(core.created_at)),
        )


def core_at(runtime, scope, kind, knowledge_at, fact_at):
    for name, value in (("knowledge_at", knowledge_at), ("fact_at", fact_at)):
        if not isinstance(value, datetime):
            raise ValueError(f"{name} 必须为 datetime")
    kind = CoreKind(kind)
    key = core_scope_key(scope, kind)
    from .storage.repository import _core_from_row
    latest = runtime.db.connection.execute(
        "SELECT * FROM core_versions WHERE scope_key=? AND kind=? AND created_at<=? AND status!='rejected' "
        "ORDER BY created_at DESC,core_version DESC LIMIT 1", (key, kind.value, to_utc_iso(knowledge_at)),
    ).fetchone()
    if latest is None:
        return None
    rows = runtime.db.connection.execute(
        "SELECT f.* FROM core_field_revisions f JOIN core_versions c "
        "ON f.scope_key=c.scope_key AND f.kind=c.kind AND f.core_version=c.core_version "
        "WHERE f.scope_key=? AND f.kind=? AND f.recorded_at<=? AND f.effective_from<=? AND c.status!='rejected' "
        "ORDER BY f.effective_from DESC,f.recorded_at DESC,f.core_version DESC",
        (key, kind.value, to_utc_iso(knowledge_at), to_utc_iso(fact_at)),
    ).fetchall()
    payload, fields, seen = {}, {}, set()
    for row in rows:
        name = row["field_name"]
        if name in seen:
            continue
        seen.add(name)
        if not row["deleted"]:
            payload[name] = json.loads(row["value_json"])
            fields[name] = json.loads(row["evidence_json"])
    raw = _core_from_row(latest)
    projected = replace(raw, payload=payload, field_evidence_ids=fields,
                        evidence_ids=sorted({ref for refs in fields.values() for ref in refs}))
    return runtime.db.repositories.forgetting.mask_core(projected)


def summary_from_snapshot(snapshot):
    raw = json.loads(snapshot) if isinstance(snapshot, str) else dict(snapshot)
    raw["scope"] = Scope(**raw["scope"])
    for name in ("created_at", "updated_at", "span_start", "span_end"):
        raw[name] = from_utc_iso(raw[name]) if raw.get(name) else None
    return Summary(**raw)


def record_summary(conn, summary, now, operation):
    if summary is None:
        return 0
    previous = conn.execute(
        "SELECT * FROM summary_revisions WHERE summary_id=? ORDER BY revision DESC LIMIT 1", (summary.summary_id,),
    ).fetchone()
    material = asdict(summary)
    if previous:
        old = json.loads(previous["snapshot_json"])
        current = json.loads(dumps(material))
        old.pop("updated_at", None)
        current.pop("updated_at", None)
        if old == current and not operation.startswith("restore:"):
            return previous["revision"]
    revision = previous["revision"] + 1 if previous else 1
    scope = summary.scope
    conn.execute(
        "INSERT INTO summary_revisions(summary_id,revision,user_id,character_id,relationship_id,activity_id,"
        "domain,snapshot_json,operation,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (summary.summary_id, revision, scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id,
         scope.domain, dumps(material), operation, to_utc_iso(now)),
    )
    return revision


def seed_history(db):
    from .storage.repository import _core_from_row
    previous = {}
    for row in db.connection.execute("SELECT * FROM core_versions ORDER BY scope_key,kind,core_version").fetchall():
        core = _core_from_row(row)
        if core.status == "rejected":
            continue
        key = (row["scope_key"], row["kind"])
        record_core_fields(db.connection, core, previous.get(key))
        previous[key] = core
    for row in db.connection.execute("SELECT summary_id FROM summaries").fetchall():
        summary = db.repositories.summaries.get(row[0])
        record_summary(db.connection, summary, summary.updated_at, "migration_baseline")


def summary_available(runtime, summary, *, require_fresh=False):
    repos = runtime.db.repositories
    if not summary.source_memory_ids:
        return False
    for key in summary.source_memory_ids:
        item = repos.memories.get(key)
        if (item is None or item.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
                or repos.forgetting.is_suppressed(item) or not runtime._scope_matches(item.scope, summary.scope)):
            return False
    for key in summary.source_event_ids:
        event = repos.events.get(key)
        if (event is None or repos.forgetting.is_event_forgotten(key)
                or not runtime._scope_matches(event.scope, summary.scope)):
            return False
    return not require_fresh or (bool(summary.source_digest) and repos.summaries.is_fresh(summary))


def summary_history(runtime, summary_id, scope, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit 必须为 1..200")
    item = runtime.db.repositories.summaries.get(summary_id)
    if item is None or item.scope != scope:
        raise ScopeNotFound("摘要不存在或不属于精确作用域")
    rows = runtime.db.connection.execute(
        "SELECT * FROM summary_revisions WHERE summary_id=? ORDER BY revision DESC LIMIT ?", (summary_id, limit),
    ).fetchall()
    results = []
    for row in rows:
        summary = summary_from_snapshot(row["snapshot_json"])
        available = summary_available(runtime, summary, require_fresh=True)
        results.append({"revision": row["revision"], "recorded_at": row["recorded_at"],
                        "operation": row["operation"], "available": available,
                        "snapshot": asdict(summary) if available else None})
    return results


def restore_summary(runtime, summary_id, revision, scope, *, expected_version, reason, idempotency_key):
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (revision, expected_version)):
        raise ValueError("revision/expected_version 必须为正整数")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            or not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200):
        raise ValueError("必须提供短 reason 与 idempotency_key")
    from .storage.repository import _scope_key
    conn = runtime.db.connection
    key = _scope_key(scope)
    fingerprint = runtime.db.repositories.forgetting.fingerprint(dumps([summary_id, revision, expected_version, reason]))
    with runtime.db.transaction():
        previous = conn.execute("SELECT * FROM summary_restore_receipts WHERE scope_key=? AND idempotency_key=?",
                                (key, idempotency_key)).fetchone()
        if previous:
            if previous["request_key"] != fingerprint:
                raise ValueError("同一回滚幂等键不能对应不同请求")
            return {**json.loads(previous["receipt_json"]), "duplicate": True}
        current = runtime.db.repositories.summaries.get(summary_id)
        if current is None or current.scope != scope:
            raise ScopeNotFound("摘要不存在或不属于精确作用域")
        latest = conn.execute("SELECT MAX(revision) FROM summary_revisions WHERE summary_id=?", (summary_id,)).fetchone()[0] or 0
        if latest != expected_version:
            raise VersionConflict(expected_version, latest, "摘要")
        row = conn.execute("SELECT * FROM summary_revisions WHERE summary_id=? AND revision=?", (summary_id, revision)).fetchone()
        if row is None:
            raise ScopeNotFound("摘要历史版本不存在")
        old = summary_from_snapshot(row["snapshot_json"])
        if old.status != "active" or not summary_available(runtime, old, require_fresh=True):
            raise MemorySuppressed("历史摘要来源已变化、失效或被遗忘，不能重新发布")
        now = runtime.clock.now()
        restored = replace(old, created_at=current.created_at, updated_at=now, status="active")
        runtime.db.repositories.summaries.upsert(restored, now, reactivate=True, operation=f"restore:{revision}")
        version = conn.execute("SELECT MAX(revision) FROM summary_revisions WHERE summary_id=?", (summary_id,)).fetchone()[0]
        receipt = {"summary_id": summary_id, "restored_from": revision, "version": version, "applied": True, "duplicate": False}
        conn.execute("INSERT INTO summary_restore_receipts VALUES(?,?,?,?,?)",
                     (key, idempotency_key, fingerprint, dumps(receipt), to_utc_iso(now)))
        runtime.db.repositories.journal.append("summary.restored", scope=scope, entity_id=summary_id,
                                             payload=receipt, now=now)
        return receipt
