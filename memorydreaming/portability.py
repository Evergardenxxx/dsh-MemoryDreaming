"""可移植快照与导入预检。

快照是给可信宿主/运维使用的文件格式，不会被自动注入 LLM 上下文。
它保留事件、记忆、作用域、遗忘账本和审计链；向量索引等派生数据明确跳过，
导入后可以从主记忆重新构建。当前版本只负责导出和 dry-run 预检，真正切换
数据库仍由宿主显式执行，避免一次误操作覆盖正在使用的记忆库。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .clock import to_utc_iso, utcnow
from .storage.database import Database, SCHEMA_VERSION
from .models import Scope


FORMAT_NAME = "memorydreaming.snapshot"
FORMAT_VERSION = 1

# FTS、向量和 scope_mutations 之外的表都属于可移植主数据或审计数据。
# scope_mutations 必须保留，因为它是缓存/在途整理结果的代次防线。
TABLES = (
    "core_versions",
    "activities",
    "session_handoffs",
    "events",
    "forgotten_events",
    "memories",
    "memory_revisions",
    "memory_links",
    "summaries",
    "consolidation_jobs",
    "memory_tombstones",
    "tombstone_sources",
    "scope_mutations",
    "scope_purges",
    "health_snapshots",
    "entities",
    "entity_mentions",
    "core_field_tombstones",
    "host_deliveries",
    "core_proposals",
    "change_proposals",
    "journal_entries",
)
DERIVED_TABLES = ("memory_vectors", "memories_fts", "retrieval_cache")

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "core_versions": ("scope_key", "kind", "core_version"),
    "activities": ("activity_id",),
    "session_handoffs": ("handoff_id",),
    "events": ("event_id",),
    "forgotten_events": ("event_id",),
    "memories": ("memory_id",),
    "memory_revisions": ("revision_id",),
    "memory_links": ("link_id",),
    "summaries": ("summary_id",),
    "consolidation_jobs": ("job_id",),
    "memory_tombstones": ("tombstone_id",),
    "tombstone_sources": ("tombstone_id", "event_id", "span_start", "span_end"),
    "scope_mutations": ("scope_key",),
    "scope_purges": ("scope_key",),
    "health_snapshots": ("snapshot_id",),
    "entities": ("entity_id",),
    "entity_mentions": ("entity_id", "memory_id", "role"),
    "core_field_tombstones": ("scope_key", "kind", "field_key"),
    "host_deliveries": ("event_id",),
    "core_proposals": ("proposal_id",),
    "change_proposals": ("proposal_id",),
    "journal_entries": ("journal_id",),
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    return value


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _jsonable(row[key]) for key in row.keys()}


def _scope_matches_row(row: dict[str, Any], scope: Scope) -> bool:
    """和运行时相同的联合可见性：未指定关系/活动时包含其子范围。"""
    if row.get("user_id") != scope.user_id or row.get("character_id") != scope.character_id:
        return False
    if row.get("domain", "real") != scope.domain:
        return False
    if scope.relationship_id is not None and row.get("relationship_id") != scope.relationship_id:
        return False
    if scope.activity_id is not None and row.get("activity_id") != scope.activity_id:
        return False
    return True


def _row_scope_matches(row: dict[str, Any], scope: Scope) -> bool:
    if "user_id" in row and "character_id" in row:
        return _scope_matches_row(row, scope)
    scope_value = row.get("scope_key")
    if scope_value is None:
        return False
    try:
        user, character, relationship, activity, domain = json.loads(scope_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return _scope_matches_row({
        "user_id": user, "character_id": character, "relationship_id": relationship,
        "activity_id": activity, "domain": domain,
    }, scope)


def _table_fingerprint(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def export_snapshot(db: Database, *, scope: Scope | None = None) -> dict[str, Any]:
    """导出 JSON 可序列化快照。

    ``scope=None`` 导出整个数据库；指定作用域时只导出该作用域及其子活动，
    并在 manifest 中标记为 partial。向量索引不导出，避免把模型版本/维度
    当成主事实带走。
    """
    conn = db.connection
    tables: dict[str, list[dict[str, Any]]] = {}
    selected_memory_ids: set[str] = set()
    selected_event_ids: set[str] = set()
    for table in TABLES:
        rows = [_row_dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        if scope is not None and table not in {"memory_links", "tombstone_sources", "entity_mentions"}:
            rows = [row for row in rows if _row_scope_matches(row, scope)]
        if table == "memories":
            selected_memory_ids = {str(row["memory_id"]) for row in rows}
        elif table == "events":
            selected_event_ids = {str(row["event_id"]) for row in rows}
        tables[table] = rows

    if scope is not None:
        tables["memory_links"] = [
            row for row in tables["memory_links"]
            if row.get("from_memory_id") in selected_memory_ids
            or row.get("to_memory_id") in selected_memory_ids
        ]
        tables["tombstone_sources"] = [
            row for row in tables["tombstone_sources"]
            if row.get("event_id") in selected_event_ids
        ]
        selected_entity_ids = {str(row["entity_id"]) for row in tables["entities"]}
        tables["entity_mentions"] = [
            row for row in tables["entity_mentions"]
            if row.get("entity_id") in selected_entity_ids
            and row.get("memory_id") in selected_memory_ids
        ]

    forgetting_key = db.meta_get("forgetting_hmac_key") or ""
    journal = db.repositories.journal.verify()
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "schema_version": int(db.meta_get("schema_version") or SCHEMA_VERSION),
        "exported_at": to_utc_iso(utcnow()),
        "partial": scope is not None,
        "scope": None if scope is None else {
            "user_id": scope.user_id, "character_id": scope.character_id,
            "relationship_id": scope.relationship_id, "activity_id": scope.activity_id,
            "domain": scope.domain,
        },
        "source_generation": None if scope is None else db.repositories.forgetting.epoch(scope),
        "forgetting_key_digest": hashlib.sha256(forgetting_key.encode("utf-8")).hexdigest(),
        "journal": journal,
        "tables": {name: {"count": len(rows), "sha256": _table_fingerprint(rows)}
                   for name, rows in tables.items()},
        "derived_tables_skipped": list(DERIVED_TABLES),
    }
    return {"manifest": manifest, "tables": tables}


def write_snapshot(db: Database, path: str | Path, *, scope: Scope | None = None) -> dict[str, Any]:
    """导出并写入 UTF-8 JSON 文件，返回 manifest。"""
    snapshot = export_snapshot(db, scope=scope)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return snapshot["manifest"]


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """读取快照并做最基本的格式校验，不连接目标数据库。"""
    try:
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取记忆快照：{exc}") from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("manifest"), dict):
        raise ValueError("快照必须包含 manifest 对象")
    manifest = snapshot["manifest"]
    if manifest.get("format") != FORMAT_NAME or manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("不支持的记忆快照格式")
    if not isinstance(snapshot.get("tables"), dict):
        raise ValueError("快照必须包含 tables 对象")
    return snapshot


def validate_snapshot(db: Database, snapshot: dict[str, Any]) -> dict[str, Any]:
    """对快照执行只读导入预检，绝不写入目标数据库。"""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
    tables = snapshot.get("tables") if isinstance(snapshot, dict) else None
    if not isinstance(manifest, dict) or not isinstance(tables, dict):
        return {"ready": False, "mode": "dry_run", "errors": [{"code": "INVALID_SNAPSHOT", "message": "manifest/tables 缺失"}], "warnings": []}
    try:
        source_schema = int(manifest.get("schema_version", 0))
    except (TypeError, ValueError):
        source_schema = 0
    target_schema = int(db.meta_get("schema_version") or 0)
    if source_schema > int(SCHEMA_VERSION):
        errors.append({"code": "FUTURE_SCHEMA", "message": f"快照 schema {source_schema} 高于当前支持的 {SCHEMA_VERSION}"})
    if target_schema < source_schema:
        errors.append({"code": "TARGET_SCHEMA_OLD", "message": f"目标库 schema {target_schema} 低于快照 {source_schema}"})

    for table in TABLES:
        rows = tables.get(table, [])
        if not isinstance(rows, list):
            errors.append({"code": "TABLE_INVALID", "message": f"表 {table} 不是数组"})
            continue
        expected = manifest.get("tables", {}).get(table, {})
        if expected.get("count") != len(rows) or expected.get("sha256") != _table_fingerprint(rows):
            errors.append({"code": "TABLE_DIGEST_MISMATCH", "message": f"表 {table} 的数量或摘要不匹配"})
        key_columns = PRIMARY_KEYS.get(table, ())
        seen_keys: set[tuple[Any, ...]] = set()
        for row in rows:
            key = tuple(row.get(column) for column in key_columns)
            if key in seen_keys:
                errors.append({"code": "DUPLICATE_PRIMARY_KEY", "message": f"表 {table} 存在重复主键"})
            seen_keys.add(key)
            if not key_columns or any(value is None for value in key):
                continue
            where = " AND ".join(f"{column} IS ?" for column in key_columns)
            existing = db.connection.execute(
                f"SELECT * FROM {table} WHERE {where} LIMIT 1", list(key)
            ).fetchone()
            if existing is not None and dict(existing) != row:
                errors.append({"code": "ID_CONFLICT", "message": f"表 {table} 的主键已存在但内容不同"})

    event_ids = {row.get("event_id") for row in tables.get("events", []) if isinstance(row, dict)}
    memory_ids = {row.get("memory_id") for row in tables.get("memories", []) if isinstance(row, dict)}
    for row in tables.get("memories", []):
        try:
            sources = json.loads(row.get("source_event_ids_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append({"code": "INVALID_SOURCE_LIST", "message": "记忆来源列表不是有效 JSON"})
            continue
        missing = [source for source in sources if source not in event_ids]
        if missing:
            errors.append({"code": "MISSING_EVENT_SOURCE", "message": f"记忆 {row.get('memory_id')} 缺少事件来源"})
    for row in tables.get("memory_links", []):
        if row.get("from_memory_id") not in memory_ids or row.get("to_memory_id") not in memory_ids:
            errors.append({"code": "MISSING_LINK_TARGET", "message": f"链接 {row.get('link_id')} 指向不存在的记忆"})

    source_journal = manifest.get("journal") or {}
    if not manifest.get("partial") and source_journal.get("valid") is not True:
        errors.append({"code": "JOURNAL_INVALID", "message": "快照来源审计链未通过完整性校验"})
    elif manifest.get("partial"):
        warnings.append({"code": "PARTIAL_JOURNAL", "message": "作用域快照只包含审计链子集，无法单独验证全局链"})

    target_key = db.meta_get("forgetting_hmac_key") or ""
    if manifest.get("forgetting_key_digest") != hashlib.sha256(target_key.encode("utf-8")).hexdigest():
        errors.append({"code": "FORGETTING_KEY_MISMATCH", "message": "遗忘账本密钥不同，阻止直接导入以免旧事实复活"})
    if manifest.get("partial") and isinstance(manifest.get("scope"), dict):
        raw_scope = manifest["scope"]
        try:
            snapshot_scope = Scope(
                user_id=str(raw_scope["user_id"]), character_id=str(raw_scope["character_id"]),
                relationship_id=raw_scope.get("relationship_id"),
                activity_id=raw_scope.get("activity_id"), domain=str(raw_scope.get("domain", "real")),
            )
            source_generation = manifest.get("source_generation")
            target_generation = db.repositories.forgetting.epoch(snapshot_scope)
            if source_generation is not None and int(source_generation) != target_generation:
                errors.append({"code": "GENERATION_MISMATCH", "message": "作用域遗忘代次已变化，阻止直接导入"})
        except (KeyError, TypeError, ValueError):
            errors.append({"code": "INVALID_SCOPE", "message": "快照作用域字段无效"})

    # 目标库的 purge marker 是单调的防复活屏障。即使旧快照的表摘要和
    # forgetting key 都匹配，只要其中包含已经清除作用域的正文，就必须拒绝。
    purge_rows = db.connection.execute("SELECT * FROM scope_purges").fetchall()
    snapshot_rows = [row for table, rows in tables.items() if table != "scope_purges"
                     for row in rows if isinstance(row, dict)]
    for marker in purge_rows:
        marker_scope = Scope(marker["user_id"], marker["character_id"], marker["relationship_id"],
                              marker["activity_id"], marker["domain"])
        for row in snapshot_rows:
            if _row_scope_matches(row, marker_scope):
                errors.append({"code": "PURGED_SCOPE", "message": "快照包含已物理清除作用域的数据，阻止重新导入"})
                break

    for table in TABLES:
        for row in tables.get(table, []):
            primary = tuple(row.get(column) for column in PRIMARY_KEYS.get(table, ()))
            if (not primary or any(value is None for value in primary)) and table not in {
                "core_versions", "tombstone_sources", "core_field_tombstones",
            }:
                errors.append({"code": "MISSING_PRIMARY_KEY", "message": f"表 {table} 存在缺少主键的记录"})
    return {
        "ready": not errors,
        "mode": "dry_run",
        "source_schema_version": source_schema,
        "target_schema_version": target_schema,
        "errors": errors,
        "warnings": warnings,
        "residuals": {"derived_tables_to_rebuild": list(DERIVED_TABLES),
                       "apply_supported": False},
    }


def preflight_snapshot_file(db: Database, path: str | Path) -> dict[str, Any]:
    """读取文件并执行 validate_snapshot 的便捷入口。"""
    return validate_snapshot(db, load_snapshot(path))


def _decode_jsonable(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__bytes__"}:
        try:
            return base64.b64decode(value["__bytes__"])
        except (TypeError, ValueError):
            raise ValueError("快照包含无效二进制字段")
    if isinstance(value, dict):
        return {key: _decode_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_jsonable(item) for item in value]
    return value


# 外键依赖决定的写入顺序。派生表不会出现在此列表中，导入完成后由
# 当前数据库的触发器/重建流程产生。
_IMPORT_ORDER = (
    "events", "activities", "core_versions", "session_handoffs", "memories",
    "memory_revisions", "memory_links", "summaries", "consolidation_jobs",
    "forgotten_events", "scope_mutations", "scope_purges", "memory_tombstones",
    "tombstone_sources", "core_field_tombstones", "health_snapshots", "entities", "entity_mentions", "host_deliveries",
    "core_proposals", "change_proposals", "journal_entries",
)


def import_snapshot(db: Database, snapshot: dict[str, Any], *, allow_partial: bool = True) -> dict[str, Any]:
    """在预检通过后原子导入主数据快照。

    导入只接受本项目生成的、遗忘密钥一致的快照；相同主键的相同记录幂等
    跳过，不同内容会在 validate_snapshot 阶段拒绝。任何一张表写入失败都会
    回滚整个事务，向量/FTS 等派生数据列入 residuals 供宿主重建。
    """
    manifest = snapshot.get("manifest") or {}
    if manifest.get("partial") and not allow_partial:
        raise ValueError("当前调用方禁止导入作用域快照")
    check = validate_snapshot(db, snapshot)
    if not check.get("ready"):
        raise ValueError(json.dumps(check, ensure_ascii=False, sort_keys=True))

    tables = snapshot.get("tables") or {}
    counts: dict[str, int] = {}
    with db.transaction():
        for table in _IMPORT_ORDER:
            rows = tables.get(table, [])
            if not rows:
                counts[table] = 0
                continue
            columns = [row["name"] for row in db.connection.execute(f"PRAGMA table_info({table})").fetchall()]
            if not columns:
                raise ValueError(f"目标数据库缺少表：{table}")
            placeholders = ",".join("?" for _ in columns)
            sql = f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
            inserted = 0
            for raw in rows:
                if not isinstance(raw, dict):
                    raise ValueError(f"快照表 {table} 包含非对象记录")
                values = [_decode_jsonable(raw.get(column)) for column in columns]
                cursor = db.connection.execute(sql, values)
                inserted += max(cursor.rowcount, 0)
            counts[table] = inserted

        # 导入后重建 FTS 触发器无法覆盖历史版本的手工差异，显式重建最稳妥。
        db.connection.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    return {
        "ready": True,
        "mode": "applied",
        "partial": bool(manifest.get("partial")),
        "counts": counts,
        "residuals": {"derived_tables_to_rebuild": list(DERIVED_TABLES),
                       "vectors_imported": False},
    }

