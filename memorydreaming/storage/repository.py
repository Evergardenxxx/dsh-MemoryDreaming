"""存储层：把领域模型读写成 SQLite 行。

为什么 SQL 直接写在这里，不用 ORM？
新手最容易在 ORM 的「魔法」里迷路：不知道一条语句最终发出的是什么 SQL，
也不知道为什么改了一个字段会连带更新别的表。这里每个方法对应一条（或几条）
能一眼看懂的 SQL，出问题时可以把语句直接贴到 sqlite3 命令行里验证。

约定：
- 本层只负责「存什么、取什么」，不做业务判断（例如是否该撤回一条记忆）。
  业务规则放在 runtime.py，这样存储层可以独立测试。
- 所有写操作都由调用方开启事务（见 database.py 的 transaction()），
  仓储方法内部不 commit，方便把「事件 + 检查点 + 任务」放进同一个原子操作。
"""

from __future__ import annotations

import json
import hashlib
import hmac
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from ..clock import ensure_utc, from_utc_iso, to_utc_iso
from ..errors import JobLeaseLost, MemorySuppressed, VersionConflict
from ..assertions import assertion_material, core_scope_key, normalize
from ..models import (
    Activity,
    ActivityStatus,
    CoreKind,
    CoreSnapshot,
    EpistemicStatus,
    EventRecord,
    JobStatus,
    LinkRelation,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    Modality,
    Scope,
    ReadFilter,
    Summary,
    SessionHandoff,
    body_to_text,
    memory_source_digest,
    new_id,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _placeholders(count: int) -> str:
    """生成 `?,?,?` 这样的占位符串，供 IN (...) 使用。"""
    return ",".join("?" for _ in range(count))


def scope_sql(scope: Scope, alias: str = "") -> tuple[str, list[object]]:
    """SQL 可见性边界：领域必须相同，给定关系/活动时再收窄。

    只可传程序中固定的表别名。调用者必须在排序和 LIMIT 之前应用此条件。
    核心档案使用独立的 core_scope_key，不使用此记忆/活动范围过滤器。
    """
    prefix = f"{alias}." if alias else ""
    clauses = [f"{prefix}user_id = ?", f"{prefix}character_id = ?", f"{prefix}domain = ?"]
    params: list[object] = [scope.user_id, scope.character_id, scope.domain]
    if scope.relationship_id is not None:
        clauses.append(f"{prefix}relationship_id = ?")
        params.append(scope.relationship_id)
    if scope.activity_id is not None:
        clauses.append(f"{prefix}activity_id = ?")
        params.append(scope.activity_id)
    return " AND ".join(clauses), params


def _scope_key(scope: Scope) -> str:
    """摘要的精确生成范围；宽范围查询不会覆盖窄范围卡片。"""
    return json.dumps(
        [scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id, scope.domain],
        ensure_ascii=False, separators=(",", ":"),
    )


def retrieval_sql(scope: Scope, alias: str = "", read_filter: ReadFilter | None = None) -> tuple[str, list[object]]:
    """在候选 LIMIT 前应用共享/活动联合范围及本轮事件排除。"""
    visible, params = scope_sql(scope, alias)
    if read_filter is None:
        return visible, params
    prefix = f"{alias}." if alias else ""
    clauses = [visible]
    if read_filter.activity_ids is not None:
        ids = list(dict.fromkeys(item for item in read_filter.activity_ids if item is not None))
        alternatives = []
        if None in read_filter.activity_ids:
            alternatives.append(f"{prefix}activity_id IS NULL")
        if ids:
            alternatives.append(f"{prefix}activity_id IN ({_placeholders(len(ids))})")
            params.extend(ids)
        clauses.append("(" + " OR ".join(alternatives) + ")" if alternatives else "0")
    for event_id in read_filter.exclude_event_ids:
        # 精确匹配 JSON 引号包围的事件 ID；不要求 SQLite 编译 JSON 扩展。
        token = json.dumps(event_id, ensure_ascii=False).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"{prefix}source_event_ids_json NOT LIKE ? ESCAPE '\\'")
        params.append(f"%{token}%")
    return " AND ".join(clauses), params


def _json_list(values: Iterable[str] | None) -> str:
    return json.dumps(list(values or []), ensure_ascii=False)


def _load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _load_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _scope_from_row(row: sqlite3.Row) -> Scope:
    """从任意「带作用域列」的行还原 Scope。"""
    return Scope(
        user_id=row["user_id"],
        character_id=row["character_id"],
        relationship_id=row["relationship_id"],
        activity_id=row["activity_id"] if "activity_id" in row.keys() else None,
        domain=row["domain"] if "domain" in row.keys() else "real",
    )


def _memory_from_row(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        memory_id=row["memory_id"],
        memory_type=MemoryType(row["memory_type"]),
        scope=_scope_from_row(row),
        body=_load_dict(row["body_json"]),
        source_event_ids=_load_list(row["source_event_ids_json"]),
        source_speaker=row["source_speaker"],
        epistemic_status=EpistemicStatus(row["epistemic_status"]),
        modality=Modality(row["modality"]),
        occurred_start=from_utc_iso(row["occurred_start"]),
        occurred_end=from_utc_iso(row["occurred_end"]),
        occurred_precision=row["occurred_precision"],
        valid_from=from_utc_iso(row["valid_from"]),
        valid_to=from_utc_iso(row["valid_to"]),
        supersedes_id=row["supersedes_id"],
        status=MemoryStatus(row["status"]),
        schema_version=row["schema_version"],
        importance=row["importance"],
        retrieval_strength=row["retrieval_strength"],
        last_used_at=from_utc_iso(row["last_used_at"]),
        access_count=row["access_count"],
        recorded_at=from_utc_iso(row["recorded_at"]) or ensure_utc(datetime.min),
        body_text=row["body_text"],
        source_spans=_load_dict(row["source_spans_json"]),
    )


def _memory_snapshot(item: MemoryItem) -> dict[str, Any]:
    """只保存事实状态所需字段；访问次数等召回强化也保留，便于完整回放。"""
    return {
        "memory_id": item.memory_id,
        "memory_type": item.memory_type.value,
        "schema_version": item.schema_version,
        "body": item.body,
        "source_event_ids": list(item.source_event_ids),
        "source_spans": item.source_spans,
        "source_speaker": item.source_speaker,
        "epistemic_status": item.epistemic_status.value,
        "modality": item.modality.value,
        "occurred_start": to_utc_iso(item.occurred_start),
        "occurred_end": to_utc_iso(item.occurred_end),
        "occurred_precision": item.occurred_precision,
        "valid_from": to_utc_iso(item.valid_from),
        "valid_to": to_utc_iso(item.valid_to),
        "supersedes_id": item.supersedes_id,
        "status": item.status.value,
        "importance": item.importance,
        "retrieval_strength": item.retrieval_strength,
        "last_used_at": to_utc_iso(item.last_used_at),
        "access_count": item.access_count,
        "recorded_at": to_utc_iso(item.recorded_at),
        "updated_at": to_utc_iso(item.recorded_at),
    }


def _memory_from_snapshot(
    snapshot: dict[str, Any], scope: Scope, revision: int | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=str(snapshot["memory_id"]), memory_type=MemoryType(snapshot["memory_type"]),
        scope=scope, body=dict(snapshot.get("body") or {}),
        source_event_ids=list(snapshot.get("source_event_ids") or []),
        source_spans=dict(snapshot.get("source_spans") or {}),
        source_speaker=snapshot.get("source_speaker"),
        epistemic_status=EpistemicStatus(snapshot["epistemic_status"]),
        modality=Modality(snapshot["modality"]),
        occurred_start=from_utc_iso(snapshot.get("occurred_start")),
        occurred_end=from_utc_iso(snapshot.get("occurred_end")),
        occurred_precision=snapshot.get("occurred_precision", "unknown"),
        valid_from=from_utc_iso(snapshot.get("valid_from")),
        valid_to=from_utc_iso(snapshot.get("valid_to")),
        supersedes_id=snapshot.get("supersedes_id"),
        status=MemoryStatus(snapshot["status"]),
        schema_version=int(snapshot.get("schema_version", 1)),
        system_revision=revision,
        importance=float(snapshot.get("importance", 0.5)),
        retrieval_strength=float(snapshot.get("retrieval_strength", 0.5)),
        last_used_at=from_utc_iso(snapshot.get("last_used_at")),
        access_count=int(snapshot.get("access_count", 0)),
        recorded_at=from_utc_iso(snapshot.get("recorded_at")) or ensure_utc(datetime.min),
        body_text=body_to_text(MemoryType(snapshot["memory_type"]), dict(snapshot.get("body") or {})),
    )


def _activity_from_row(row: sqlite3.Row) -> Activity:
    return Activity(
        activity_id=row["activity_id"],
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
            relationship_id=row["relationship_id"],
            activity_id=row["activity_id"],
            domain=row["domain"],
        ),
        title=row["title"],
        status=ActivityStatus(row["status"]),
        goal=row["goal"],
        open_loops=_load_list(row["open_loops_json"]),
        active_entities=_load_list(row["active_entities_json"]),
        checkpoint=_load_dict(row["checkpoint_json"]),
        checkpoint_version=row["checkpoint_version"],
        last_event_at=from_utc_iso(row["last_event_at"]),
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        updated_at=from_utc_iso(row["updated_at"]) or ensure_utc(datetime.min),
    )


def _core_from_row(row: sqlite3.Row) -> CoreSnapshot:
    return CoreSnapshot(
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
            relationship_id=row["relationship_id"],
            domain=row["domain"],
        ),
        kind=CoreKind(row["kind"]),
        core_version=row["core_version"],
        payload=_load_dict(row["payload_json"]),
        change_reason=row["change_reason"],
        effective_from=from_utc_iso(row["effective_from"]) or ensure_utc(datetime.min),
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        evidence_ids=_load_list(row["evidence_ids_json"]),
        status=row["status"],
        field_evidence_ids=_load_dict(row["field_evidence_json"]),
    )


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=row["event_id"],
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
            relationship_id=row["relationship_id"],
            activity_id=row["activity_id"],
            domain=row["domain"],
        ),
        conversation_id=row["conversation_id"],
        speaker=row["speaker"],
        content=row["content"],
        occurred_at=from_utc_iso(row["occurred_at"]) or ensure_utc(datetime.min),
        source_timezone=row["source_timezone"],
        occurred_precision=row["occurred_precision"],
        recorded_at=from_utc_iso(row["recorded_at"]) or ensure_utc(datetime.min),
        turn_index=row["turn_index"],
        idempotency_key=row["idempotency_key"],
    )


@dataclass
class JobRecord:
    """整理任务。放在存储层是因为它描述的是「一行任务记录」，不是领域概念。"""

    job_id: str
    job_type: str
    scope: Scope
    activity_id: str | None
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    max_attempts: int = 3
    available_at: datetime | None = None
    claim_token: str | None = None
    lease_until: datetime | None = None


def _summary_from_row(row: sqlite3.Row) -> Summary:
    return Summary(
        summary_id=row["summary_id"],
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
            relationship_id=row["relationship_id"],
            activity_id=row["activity_id"],
            domain=row["domain"],
        ),
        topic=row["topic"],
        digest=row["digest"],
        source_memory_ids=_load_list(row["source_memory_ids_json"]),
        source_event_ids=_load_list(row["source_event_ids_json"]),
        source_digest=row["source_digest"] if "source_digest" in row.keys() else "",
        algorithm_version=row["algorithm_version"] if "algorithm_version" in row.keys() else "extractive-v1",
        span_start=from_utc_iso(row["span_start"]),
        span_end=from_utc_iso(row["span_end"]),
        importance=row["importance"],
        status=row["status"],
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        updated_at=from_utc_iso(row["updated_at"]) or ensure_utc(datetime.min),
    )


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        job_type=row["job_type"],
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
            relationship_id=row["relationship_id"],
            activity_id=row["activity_id"],
            domain=row["domain"],
        ),
        activity_id=row["activity_id"],
        payload=_load_dict(row["payload_json"]),
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        updated_at=from_utc_iso(row["updated_at"]) or ensure_utc(datetime.min),
        max_attempts=row["max_attempts"],
        available_at=from_utc_iso(row["available_at"]),
        claim_token=row["claim_token"],
        lease_until=from_utc_iso(row["lease_until"]),
    )


# ---------------------------------------------------------------------------
# 原始事件
# ---------------------------------------------------------------------------


class EventRepository:
    """原始事件读写。正常事件只追加；事件级遗忘允许逻辑擦除正文。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, event: EventRecord) -> tuple[EventRecord, bool]:
        """写入一条事件。

        返回 (事件, 是否重复)。幂等键命中时返回已存在的那条，
        调用方据此跳过后续的整理任务入队，避免重复抽取。
        """
        cursor = self._conn.execute(
            """
            INSERT INTO events (
                event_id, user_id, character_id, relationship_id, conversation_id,
                activity_id, domain, speaker, content, occurred_at,
                occurred_precision, source_timezone, recorded_at, turn_index,
                idempotency_key, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
            """,
            (
                event.event_id,
                event.scope.user_id,
                event.scope.character_id,
                event.scope.relationship_id,
                event.conversation_id,
                event.scope.activity_id,
                event.scope.domain,
                event.speaker,
                event.content,
                to_utc_iso(event.occurred_at),
                event.occurred_precision,
                event.source_timezone,
                to_utc_iso(event.recorded_at),
                event.turn_index,
                event.idempotency_key,
                to_utc_iso(event.recorded_at),
            ),
        )

        if cursor.rowcount == 0 and event.idempotency_key is not None:
            existing = self.find_by_idempotency_key(event.idempotency_key, event.scope)
            if existing is not None:
                if (existing.content, existing.speaker, existing.conversation_id) != (
                    event.content, event.speaker, event.conversation_id
                ):
                    raise ValueError("同一作用域的幂等键已用于不同的消息内容或会话")
                return existing, True
        if cursor.rowcount == 0:
            raise ValueError("事件 ID 冲突，消息未保存")
        return event, False

    def get(self, event_id: str) -> EventRecord | None:
        row = self._conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return _event_from_row(row) if row else None

    def redact(self, event_id: str) -> bool:
        """逻辑擦除事件正文，保留事件 ID 以维护来源和投递外键。"""
        cursor = self._conn.execute(
            "UPDATE events SET content = ? WHERE event_id = ?",
            ("[已删除事件]", event_id),
        )
        return cursor.rowcount > 0

    def find_by_idempotency_key(self, key: str, scope: Scope) -> EventRecord | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE idempotency_key = ? "
            "AND user_id = ? AND character_id = ? AND domain = ? "
            "AND COALESCE(relationship_id, '') = ? AND COALESCE(activity_id, '') = ?",
            (key, scope.user_id, scope.character_id, scope.domain,
             scope.relationship_id or "", scope.activity_id or ""),
        ).fetchone()
        return _event_from_row(row) if row else None

    def list_for_activity(self, activity_id: str, limit: int = 50) -> list[EventRecord]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE activity_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (activity_id, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_between(
        self,
        scope: Scope,
        start: datetime,
        end: datetime,
        limit: int = 50,
    ) -> list[EventRecord]:
        """按事件时间取原始对话。用于「回到原话」的精确回查。"""
        visible, params = scope_sql(scope)
        rows = self._conn.execute(
            f"""
            SELECT * FROM events
            WHERE {visible}
              AND occurred_at >= ? AND occurred_at < ?
            ORDER BY occurred_at ASC
            LIMIT ?
            """,
            [*params, to_utc_iso(start), to_utc_iso(end), limit],
        ).fetchall()
        return [_event_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------


class MemoryRepository:
    """四类记忆的读写。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self,
        item: MemoryItem,
        *,
        dedupe_key: str | None = None,
        extraction_version: str = "v1",
        candidate_key: str | None = None,
    ) -> MemoryItem | None:
        """插入一条记忆。

        返回 None 表示 dedupe_key 命中（同一事件、同一抽取器版本、同一个候选），
        这是幂等保护：重跑整理任务不会产生重复记忆。
        """
        if ForgetRepository(self._conn).is_suppressed(item):
            raise MemorySuppressed("记忆匹配持久遗忘标记")
        cursor = self._conn.execute(
            """
            INSERT INTO memories (
                memory_id, memory_type, schema_version,
                user_id, character_id, relationship_id, activity_id, domain,
                source_event_ids_json, source_speaker,
                extraction_version, candidate_key, dedupe_key,
                epistemic_status, modality,
                occurred_start, occurred_end, occurred_precision,
                valid_from, valid_to, supersedes_id,
                status, importance, retrieval_strength,
                last_used_at, access_count,
                body_json, body_text, recorded_at, updated_at, source_spans_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                item.memory_id,
                item.memory_type.value,
                item.schema_version,
                item.scope.user_id,
                item.scope.character_id,
                item.scope.relationship_id,
                item.scope.activity_id,
                item.scope.domain,
                _json_list(item.source_event_ids),
                item.source_speaker,
                extraction_version,
                candidate_key,
                dedupe_key,
                item.epistemic_status.value,
                item.modality.value,
                to_utc_iso(item.occurred_start),
                to_utc_iso(item.occurred_end),
                item.occurred_precision,
                to_utc_iso(item.valid_from),
                to_utc_iso(item.valid_to),
                item.supersedes_id,
                item.status.value,
                item.importance,
                item.retrieval_strength,
                to_utc_iso(item.last_used_at),
                item.access_count,
                json.dumps(item.body, ensure_ascii=False),
                item.body_text or body_to_text(item.memory_type, item.body),
                to_utc_iso(item.recorded_at),
                to_utc_iso(item.recorded_at),
                json.dumps(item.source_spans, ensure_ascii=False),
            ),
        )
        if cursor.rowcount > 0:
            self._record_revision(item.memory_id, "insert", item.recorded_at)
            EntityRepository(self._conn).index_memory(item, item.recorded_at)
            return item
        return None

    def get(self, memory_id: str) -> MemoryItem | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return _memory_from_row(row) if row else None

    def fetch_many(self, memory_ids: Sequence[str]) -> dict[str, MemoryItem]:
        """批量取记忆，返回 {id: item}。保持一次查询，避免 N+1。"""
        if not memory_ids:
            return {}
        rows = self._conn.execute(
            f"SELECT * FROM memories WHERE memory_id IN ({_placeholders(len(memory_ids))})",
            list(memory_ids),
        ).fetchall()
        return {row["memory_id"]: _memory_from_row(row) for row in rows}

    def history(self, memory_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """返回按系统时间倒序的修订摘要；snapshot 是内部完整状态。"""
        rows = self._conn.execute(
            "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY revision DESC LIMIT ?",
            (memory_id, limit),
        ).fetchall()
        return [
            {
                "revision": row["revision"], "revision_id": row["revision_id"],
                "operation": json.loads(row["snapshot_json"]).get("operation", ""),
                "system_from": row["system_from"], "system_to": row["system_to"],
                "status": row["status"], "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "snapshot": json.loads(row["snapshot_json"]),
            }
            for row in rows
        ]

    def at(
        self,
        memory_id: str,
        knowledge_at: datetime,
        *,
        fact_at: datetime | None = None,
    ) -> MemoryItem | None:
        """按系统知识时间恢复一条记忆，再按事实时间检查有效期。"""
        moment = to_utc_iso(knowledge_at)
        row = self._conn.execute(
            """
            SELECT * FROM memory_revisions
            WHERE memory_id=? AND system_from <= ?
              AND (system_to IS NULL OR system_to > ?)
            ORDER BY revision DESC LIMIT 1
            """,
            (memory_id, moment, moment),
        ).fetchone()
        if row is None:
            return None
        scope = Scope(row["user_id"], row["character_id"], row["relationship_id"],
                      row["activity_id"], row["domain"])
        item = _memory_from_snapshot(json.loads(row["snapshot_json"]), scope, row["revision"])
        if fact_at is not None and not item.is_current(fact_at):
            return None
        return item

    def list_at(
        self,
        scope: Scope,
        knowledge_at: datetime,
        *,
        fact_at: datetime | None = None,
        memory_types: list[MemoryType] | None = None,
        read_filter: ReadFilter | None = None,
        limit: int = 200,
    ) -> list[MemoryItem]:
        """按系统时间恢复每条记忆的唯一版本，供双时间回放使用。"""
        visible, params = scope_sql(scope, "r")
        rows = self._conn.execute(
            "SELECT * FROM memory_revisions r WHERE " + visible
            + " AND r.system_from <= ? AND (r.system_to IS NULL OR r.system_to > ?)"
            + " ORDER BY r.memory_id, r.revision DESC",
            [*params, to_utc_iso(knowledge_at), to_utc_iso(knowledge_at)],
        ).fetchall()
        result: list[MemoryItem] = []
        seen: set[str] = set()
        for row in rows:
            if row["memory_id"] in seen:
                continue
            seen.add(row["memory_id"])
            if memory_types and MemoryType(row["memory_type"]) not in memory_types:
                continue
            item = _memory_from_snapshot(
                json.loads(row["snapshot_json"]),
                Scope(row["user_id"], row["character_id"], row["relationship_id"],
                      row["activity_id"], row["domain"]),
                row["revision"],
            )
            if item.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED):
                continue
            if fact_at is not None:
                # 事实轴只判断有效期。修订快照中的 ``superseded`` 表示
                # 当前投影已被新版本取代，但在 fact_at 这个过去时刻它
                # 可能仍是有效值。
                if item.valid_from is not None and item.valid_from > fact_at:
                    continue
                if item.valid_to is not None and item.valid_to <= fact_at:
                    continue
            if read_filter is not None and (
                (read_filter.activity_ids is not None and item.scope.activity_id not in read_filter.activity_ids)
                or set(item.source_event_ids).intersection(read_filter.exclude_event_ids)
            ):
                continue
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def list_recent(
        self,
        scope: Scope,
        since: datetime,
        memory_types: list[MemoryType] | None,
        limit: int,
        *, read_filter: ReadFilter | None = None,
    ) -> list[MemoryItem]:
        """取某个时间点之后出现的记忆，按事件时间倒序。

        用 COALESCE(occurred_start, recorded_at)：有的记忆只有记录时间
        （比如「用户今天提到自己十年前搬过家」），不能因为缺事件时间就漏掉。
        """
        visible, params = retrieval_sql(scope, read_filter=read_filter)
        sql = f"""
            SELECT * FROM memories
            WHERE {visible}
              AND status = 'active'
              AND COALESCE(occurred_start, recorded_at) >= ?
        """
        params.append(to_utc_iso(since))

        if memory_types:
            sql += f" AND memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        sql += " ORDER BY COALESCE(occurred_start, recorded_at) DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def list_between(
        self,
        scope: Scope,
        start: datetime,
        end: datetime,
        memory_types: list[MemoryType] | None,
        limit: int,
        *, read_filter: ReadFilter | None = None,
    ) -> list[MemoryItem]:
        """按事件时间范围取记忆，用于「明确日期」的精确回查。"""
        visible, params = retrieval_sql(scope, read_filter=read_filter)
        sql = f"""
            SELECT * FROM memories
            WHERE {visible}
              AND status = 'active'
              AND COALESCE(occurred_start, recorded_at) >= ?
              AND COALESCE(occurred_start, recorded_at) < ?
        """
        params.extend([to_utc_iso(start), to_utc_iso(end)])

        if memory_types:
            sql += f" AND memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        sql += " ORDER BY occurred_start ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def list_important(self, scope: Scope, limit: int) -> list[MemoryItem]:
        """按重要度取少量「值得先想起来」的经历，用于 bootstrap 摘要。"""
        visible, params = scope_sql(scope)
        rows = self._conn.execute(
            f"""
            SELECT * FROM memories
            WHERE {visible}
              AND status = 'active'
              AND memory_type IN ('episodic', 'semantic')
            ORDER BY importance DESC, COALESCE(occurred_start, recorded_at) DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def count_active(self, scope: Scope) -> int:
        """这个角色名下有多少条有效记忆。用来决定「够不够生成摘要」。"""
        visible, params = scope_sql(scope)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM memories
            WHERE {visible} AND status = 'active'
            """,
            params,
        ).fetchone()
        return int(row["n"])

    def update_status(self, memory_id: str, status: MemoryStatus, now: datetime) -> None:
        cursor = self._conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ?",
            (status.value, to_utc_iso(now), memory_id),
        )
        if cursor.rowcount:
            self._record_revision(memory_id, f"status:{status.value}", now)
            EntityRepository(self._conn).index_memory(self.get(memory_id), now)
        SummaryRepository(self._conn).invalidate_for_memories([memory_id], now)

    def close_validity(
        self,
        memory_id: str,
        valid_to: datetime,
        now: datetime,
        status: MemoryStatus = MemoryStatus.SUPERSEDED,
    ) -> None:
        """结束一条事实的有效期。

        「当时正确、现在改变」用它：记录仍然可查，只是不再是当前值。
        """
        cursor = self._conn.execute(
            "UPDATE memories SET valid_to = ?, status = ?, updated_at = ? WHERE memory_id = ?",
            (to_utc_iso(valid_to), status.value, to_utc_iso(now), memory_id),
        )
        if cursor.rowcount:
            self._record_revision(memory_id, "close_validity", now)
            EntityRepository(self._conn).index_memory(self.get(memory_id), now)
        SummaryRepository(self._conn).invalidate_for_memories([memory_id], now)

    def mark_used(self, memory_ids: Sequence[str], now: datetime) -> None:
        """记录「这些记忆被真正用上了」。

        架构文档第 4 节：只有实际用于回答、并得到明确有用反馈时才增强使用价值，
        不能让反复检索本身把一条错误记忆顶上高位。所以这个方法由宿主显式调用，
        而不是 retrieve() 内部自动调用。
        """
        if not memory_ids:
            return
        self._conn.execute(
            f"""
            UPDATE memories
            SET access_count = access_count + 1,
                last_used_at = ?,
                retrieval_strength = MIN(1.0, retrieval_strength + 0.05)
            WHERE memory_id IN ({_placeholders(len(memory_ids))})
            """,
            [to_utc_iso(now), *memory_ids],
        )

    def delete(self, memory_id: str, now: datetime) -> bool:
        """软删除：保留行以便审计，但把状态改为 deleted。

        索引由 FTS 触发器同步；检索 SQL 都会排除 deleted。
        """
        cursor = self._conn.execute(
            "UPDATE memories SET status = 'deleted', updated_at = ? "
            "WHERE memory_id = ? AND status != 'deleted'",
            (to_utc_iso(now), memory_id),
        )
        if cursor.rowcount:
            self._record_revision(memory_id, "delete", now)
            EntityRepository(self._conn).remove_memory(memory_id)
            SummaryRepository(self._conn).invalidate_for_memories([memory_id], now)
        return cursor.rowcount > 0

    def list_by_scope(
        self,
        scope: Scope,
        memory_types: list[MemoryType] | None = None,
        limit: int = 200,
        statuses: tuple[MemoryStatus, ...] | None = None,
    ) -> list[MemoryItem]:
        """按作用域列出记忆。

        参数:
            statuses: 只取这些状态的记录；None 表示「除已删除之外全部」。
                      去重要求只看 active，纠错/删除则希望覆盖历史记录。
        """
        visible, params = scope_sql(scope)
        sql = f"""
            SELECT * FROM memories
            WHERE {visible} AND status != 'deleted'
        """

        if memory_types:
            sql += f" AND memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        if statuses:
            sql += f" AND status IN ({_placeholders(len(statuses))})"
            params.extend(status.value for status in statuses)

        sql += " ORDER BY COALESCE(occurred_start, recorded_at) DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def list_ids_for_scope(
        self, scope: Scope, memory_types: list[MemoryType] | None = None
    ) -> list[str]:
        """枚举删除范围的全部 ID，不应用检索候选上限。"""
        visible, params = scope_sql(scope)
        sql = f"SELECT memory_id FROM memories WHERE {visible} AND status != 'deleted'"
        if memory_types:
            sql += f" AND memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(value.value for value in memory_types)
        return [row[0] for row in self._conn.execute(sql, params).fetchall()]

    def update_content(
        self,
        memory_id: str,
        body: dict[str, Any],
        now: datetime,
        *,
        importance: float | None = None,
        source_event_ids: Sequence[str] | None = None,
        source_spans: dict[str, list[list[int]]] | None = None,
    ) -> None:
        """改写一条记忆的正文（以及可选的重要度、证据来源）。

        去重合并用它。注意 body_text 必须一起更新：
        FTS 触发器只在 body_text 变化时重建索引，
        只改 body_json 会让检索用的文本和正文不一致。
        """
        old = self.get(memory_id)
        if old is None:
            return
        candidate = replace(old, body=body,
                            source_event_ids=list(source_event_ids) if source_event_ids is not None else old.source_event_ids,
                            source_spans=source_spans if source_spans is not None else old.source_spans)
        if ForgetRepository(self._conn).is_suppressed(candidate):
            raise MemorySuppressed("更新正文会重新写入被抑制的事实")
        memory_type = old.memory_type
        new_text = body_to_text(memory_type, body)

        assignments = ["body_json = ?", "body_text = ?", "updated_at = ?"]
        params: list[object] = [
            json.dumps(body, ensure_ascii=False),
            new_text,
            to_utc_iso(now),
        ]

        if importance is not None:
            assignments.append("importance = ?")
            params.append(importance)
        if source_event_ids is not None:
            assignments.append("source_event_ids_json = ?")
            params.append(_json_list(source_event_ids))
        if source_spans is not None:
            assignments.append("source_spans_json = ?")
            params.append(json.dumps(source_spans, ensure_ascii=False))

        params.append(memory_id)
        self._conn.execute(
            f"UPDATE memories SET {', '.join(assignments)} WHERE memory_id = ?", params
        )
        self._record_revision(memory_id, "content", now)
        VectorRepository(self._conn).delete(memory_id)
        EntityRepository(self._conn).index_memory(self.get(memory_id), now)
        SummaryRepository(self._conn).invalidate_for_memories([memory_id], now)

    def _record_revision(self, memory_id: str, operation: str, now: datetime) -> None:
        item = self.get(memory_id)
        if item is None:
            return
        previous = self._conn.execute(
            "SELECT revision FROM memory_revisions WHERE memory_id=? ORDER BY revision DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
        next_revision = int(previous["revision"]) + 1 if previous else 1
        self._conn.execute(
            "UPDATE memory_revisions SET system_to=? WHERE memory_id=? AND system_to IS NULL",
            (to_utc_iso(now), memory_id),
        )
        snapshot = _memory_snapshot(item)
        snapshot["operation"] = operation
        self._conn.execute(
            """
            INSERT INTO memory_revisions (
                revision_id, memory_id, revision, user_id, character_id, relationship_id,
                activity_id, domain, memory_type, status, valid_from, valid_to,
                snapshot_json, system_from, system_to, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                new_id("mrev"), memory_id, next_revision, item.scope.user_id,
                item.scope.character_id, item.scope.relationship_id, item.scope.activity_id,
                item.scope.domain, item.memory_type.value, item.status.value,
                to_utc_iso(item.valid_from), to_utc_iso(item.valid_to),
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                to_utc_iso(now), None, to_utc_iso(now),
            ),
        )


# ---------------------------------------------------------------------------
# 活动与检查点
# ---------------------------------------------------------------------------


class ActivityRepository:
    """活动的读写，以及带版本号的工作检查点更新。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, activity: Activity) -> Activity:
        self._conn.execute(
            """
            INSERT INTO activities (
                activity_id, user_id, character_id, relationship_id, domain,
                title, status, goal, open_loops_json, active_entities_json,
                checkpoint_json, checkpoint_version, last_event_at,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                activity.activity_id,
                activity.scope.user_id,
                activity.scope.character_id,
                activity.scope.relationship_id,
                activity.scope.domain,
                activity.title,
                activity.status.value,
                activity.goal,
                _json_list(activity.open_loops),
                _json_list(activity.active_entities),
                json.dumps(activity.checkpoint, ensure_ascii=False),
                activity.checkpoint_version,
                to_utc_iso(activity.last_event_at),
                to_utc_iso(activity.created_at),
                to_utc_iso(activity.updated_at),
            ),
        )
        return activity

    def get(self, activity_id: str) -> Activity | None:
        row = self._conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        return _activity_from_row(row) if row else None

    def list_for_scope(
        self,
        scope: Scope,
        statuses: tuple[ActivityStatus, ...] = (ActivityStatus.ACTIVE,),
        limit: int = 20,
    ) -> list[Activity]:
        visible, params = scope_sql(scope)
        sql = f"""
            SELECT * FROM activities
            WHERE {visible}
        """

        if statuses:
            sql += f" AND status IN ({_placeholders(len(statuses))})"
            params.extend(status.value for status in statuses)

        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_activity_from_row(row) for row in rows]

    def update_checkpoint(
        self,
        activity_id: str,
        expected_version: int,
        now: datetime,
        *,
        goal: str | None = None,
        open_loops: list[str] | None = None,
        active_entities: list[str] | None = None,
        checkpoint: dict[str, Any] | None = None,
        status: ActivityStatus | None = None,
    ) -> int:
        """条件更新检查点，返回新版本号。

        这是「两个窗口同时继续同一段活动」的防线：
        UPDATE 的 WHERE 里带上版本号，版本对不上就一行也改不到，
        我们据此抛 VersionConflict，让调用方重新 bootstrap。
        `None` 表示该字段不改，这样调用方可以只提交变化的字段。
        """
        assignments = ["checkpoint_version = checkpoint_version + 1", "updated_at = ?"]
        params: list[object] = [to_utc_iso(now)]

        if goal is not None:
            assignments.append("goal = ?")
            params.append(goal)
        if open_loops is not None:
            assignments.append("open_loops_json = ?")
            params.append(_json_list(open_loops))
        if active_entities is not None:
            assignments.append("active_entities_json = ?")
            params.append(_json_list(active_entities))
        if checkpoint is not None:
            assignments.append("checkpoint_json = ?")
            params.append(json.dumps(checkpoint, ensure_ascii=False))
        if status is not None:
            assignments.append("status = ?")
            params.append(status.value)

        params.extend([activity_id, expected_version])
        cursor = self._conn.execute(
            f"UPDATE activities SET {', '.join(assignments)} "
            "WHERE activity_id = ? AND checkpoint_version = ?",
            params,
        )

        if cursor.rowcount == 0:
            current = self.get(activity_id)
            actual = current.checkpoint_version if current else -1
            raise VersionConflict(expected_version, actual, "活动检查点")

        row = self._conn.execute(
            "SELECT checkpoint_version FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        return row["checkpoint_version"]

    def touch_last_event(self, activity_id: str, when: datetime) -> None:
        """记录这个活动最近一次有对话的时间，不动版本号。"""
        self._conn.execute(
            "UPDATE activities SET last_event_at = ?, updated_at = ? WHERE activity_id = ?",
            (to_utc_iso(when), to_utc_iso(when), activity_id),
        )


# ---------------------------------------------------------------------------
# 窗口交接 / 上下文压缩检查点
# ---------------------------------------------------------------------------


def _handoff_from_row(row: sqlite3.Row) -> SessionHandoff:
    return SessionHandoff(
        handoff_id=row["handoff_id"],
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
            relationship_id=row["relationship_id"],
            activity_id=row["activity_id"],
            domain=row["domain"],
        ),
        conversation_id=row["conversation_id"],
        activity_id=row["activity_id"],
        payload=_load_dict(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        source_event_id=row["source_event_id"],
        activity_version=row["activity_version"],
        status=row["status"],
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        updated_at=from_utc_iso(row["updated_at"]) or ensure_utc(datetime.min),
    )


class HandoffRepository:
    """持久化压缩前交接，并保证重试不会产生重复记录。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, handoff: SessionHandoff) -> tuple[SessionHandoff, bool]:
        """写入交接；返回 (记录, duplicate)。

        同一个 scope + idempotency_key 只能成功一次。新交接只会使同一
        活动的旧记录失效，其他活动的交接不会被覆盖。
        """
        scope_key = _scope_key(handoff.scope)
        row = self._conn.execute(
            "SELECT * FROM session_handoffs WHERE scope_key = ? AND idempotency_key = ?",
            (scope_key, handoff.idempotency_key),
        ).fetchone()
        if row is not None:
            existing = _handoff_from_row(row)
            if existing.payload != handoff.payload or existing.activity_version != handoff.activity_version:
                raise ValueError("同一交接幂等键对应了不同的工作状态")
            return existing, True

        now = handoff.updated_at
        if handoff.activity_id is None:
            self._conn.execute(
                "UPDATE session_handoffs SET status='superseded', updated_at=? "
                "WHERE scope_key=? AND activity_id IS NULL AND status='active'",
                (to_utc_iso(now), scope_key),
            )
        else:
            self._conn.execute(
                "UPDATE session_handoffs SET status='superseded', updated_at=? "
                "WHERE scope_key=? AND activity_id=? AND status='active'",
                (to_utc_iso(now), scope_key, handoff.activity_id),
            )
        self._conn.execute(
            """
            INSERT INTO session_handoffs (
                handoff_id, scope_key, user_id, character_id, relationship_id, domain,
                conversation_id, activity_id, source_event_id, activity_version,
                payload_json, idempotency_key, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                handoff.handoff_id, scope_key, handoff.scope.user_id, handoff.scope.character_id,
                handoff.scope.relationship_id, handoff.scope.domain, handoff.conversation_id,
                handoff.activity_id, handoff.source_event_id, handoff.activity_version,
                json.dumps(handoff.payload, ensure_ascii=False, separators=(",", ":")),
                handoff.idempotency_key, handoff.status, to_utc_iso(handoff.created_at),
                to_utc_iso(handoff.updated_at),
            ),
        )
        return handoff, False

    def get_by_key(self, scope: Scope, idempotency_key: str) -> SessionHandoff | None:
        scope_key = _scope_key(scope)
        row = self._conn.execute(
            "SELECT * FROM session_handoffs WHERE scope_key=? AND idempotency_key=?",
            (scope_key, idempotency_key),
        ).fetchone()
        return _handoff_from_row(row) if row else None

    def latest(self, scope: Scope, *, activity_id: str | None = None) -> SessionHandoff | None:
        visible, params = scope_sql(scope)
        clauses = [visible, "status = 'active'"]
        if activity_id is not None:
            clauses.append("activity_id = ?")
            params.append(activity_id)
        row = self._conn.execute(
            "SELECT * FROM session_handoffs WHERE " + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT 1",
            params,
        ).fetchone()
        return _handoff_from_row(row) if row else None


# ---------------------------------------------------------------------------
# 身份核心 / 关系档案
# ---------------------------------------------------------------------------


class ForgetRepository:
    """持久断言抑制与内容代次。NULL 区间表示旧来源无法精确定位。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def fingerprint(self, value: str) -> str:
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key='forgetting_hmac_key'").fetchone()
        if row is None:
            raise RuntimeError("遗忘指纹密钥未初始化")
        return hmac.new(bytes.fromhex(row[0]), value.encode("utf-8"), hashlib.sha256).hexdigest()

    def epoch(self, scope: Scope) -> int:
        row = self._conn.execute("SELECT epoch FROM scope_mutations WHERE scope_key=?", (_scope_key(scope),)).fetchone()
        return int(row[0]) if row else 0

    def bump(self, scope: Scope) -> None:
        self._conn.execute(
            "INSERT INTO scope_mutations(scope_key,epoch) VALUES (?,1) "
            "ON CONFLICT(scope_key) DO UPDATE SET epoch=epoch+1", (_scope_key(scope),),
        )

    def mark_event(self, event_id: str, scope: Scope, now: datetime, reason: str = "") -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO forgotten_events(event_id,scope_key,reason,created_at) VALUES (?,?,?,?)",
            (event_id, _scope_key(scope), reason[:500], to_utc_iso(now)),
        )

    def is_event_forgotten(self, event_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM forgotten_events WHERE event_id = ?", (event_id,)
        ).fetchone() is not None

    def suppress(self, item: MemoryItem, now: datetime) -> str:
        key = self.fingerprint(assertion_material(item))
        self._conn.execute(
            "INSERT INTO memory_tombstones(tombstone_id,scope_key,assertion_key,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(scope_key,assertion_key) DO NOTHING",
            (new_id("tomb"), _scope_key(item.scope), key, to_utc_iso(now)),
        )
        row = self._conn.execute(
            "SELECT tombstone_id FROM memory_tombstones WHERE scope_key=? AND assertion_key=?",
            (_scope_key(item.scope), key),
        ).fetchone()
        tombstone_id = row[0]
        for event_id in set(item.source_event_ids):
            intervals = item.source_spans.get(event_id) or [[None, None]]
            for start, end in intervals:
                # SQLite UNIQUE 中 NULL 不去重，故先用 IS 显式判断。
                self._conn.execute(
                    "INSERT INTO tombstone_sources(tombstone_id,event_id,span_start,span_end) "
                    "SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM tombstone_sources "
                    "WHERE tombstone_id=? AND event_id=? AND span_start IS ? AND span_end IS ?)",
                    (tombstone_id, event_id, start, end, tombstone_id, event_id, start, end),
                )
        return tombstone_id

    def assertion_tombstone(self, item: MemoryItem) -> str | None:
        """仅匹配稳定断言，供同一断言的其他来源补入现有账本。"""
        key = self.fingerprint(assertion_material(item))
        row = self._conn.execute(
            "SELECT tombstone_id FROM memory_tombstones WHERE scope_key=? AND assertion_key=?",
            (_scope_key(item.scope), key),
        ).fetchone()
        return row[0] if row else None

    def is_suppressed(self, item: MemoryItem) -> bool:
        if self.assertion_tombstone(item) is not None:
            return True
        if not item.source_event_ids:
            return False
        # 独立来源采用 OR 语义；所有来源都受抑制才隔离不同断言。
        for event_id in item.source_event_ids:
            rows = self._conn.execute(
                "SELECT s.span_start,s.span_end FROM tombstone_sources s "
                "JOIN memory_tombstones t USING(tombstone_id) WHERE t.scope_key=? AND s.event_id=?",
                (_scope_key(item.scope), event_id),
            ).fetchall()
            if not rows:
                return False
            spans = item.source_spans.get(event_id)
            blocked = not spans or any(
                row[0] is None or any(start < row[1] and end > row[0] for start, end in spans)
                for row in rows
            )
            if not blocked:
                return False
        return True

    def core_field_blocked(self, core: CoreSnapshot, name: str, value: Any, refs: Sequence[str]) -> bool:
        key = self.fingerprint(json.dumps([name, normalize(value)], ensure_ascii=False, sort_keys=True))
        if self._conn.execute(
            "SELECT 1 FROM core_field_tombstones WHERE scope_key=? AND kind=? AND field_key=?",
            (core_scope_key(core.scope, core.kind), core.kind.value, key),
        ).fetchone():
            return True
        for ref in refs:
            if self.is_event_forgotten(ref):
                return True
            memory = MemoryRepository(self._conn).get(ref)
            if memory is not None:
                if memory.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED) or self.is_suppressed(memory):
                    return True
                continue  # 精确记忆引用优于整条事件的粗粒度引用
            rows = self._conn.execute(
                "SELECT t.scope_key FROM tombstone_sources s JOIN memory_tombstones t USING(tombstone_id) "
                "WHERE s.event_id=?", (ref,),
            ).fetchall()
            for row in rows:
                user, character, relationship, activity, domain = json.loads(row[0])
                source_scope = Scope(user, character, relationship, activity, domain)
                if core_scope_key(source_scope, core.kind) == core_scope_key(core.scope, core.kind):
                    return True
        return False

    def mask_core(self, core: CoreSnapshot) -> CoreSnapshot:
        fields = core.field_evidence_ids
        payload = {
            name: value for name, value in core.payload.items()
            if not self.core_field_blocked(core, name, value, fields.get(name, core.evidence_ids))
        }
        evidence = {name: list(fields.get(name, core.evidence_ids)) for name in payload}
        return replace(core, payload=payload, field_evidence_ids=evidence,
                       evidence_ids=sorted({ref for refs in evidence.values() for ref in refs}))

    def redact_cores(self, now: datetime) -> list[str]:
        """屏蔽所有历史版本中的受影响字段，再给当前核心追加清理版本。"""
        changed: set[str] = set()
        current: dict[tuple[str, str], CoreSnapshot] = {}
        for row in self._conn.execute("SELECT * FROM core_versions ORDER BY core_version DESC").fetchall():
            core = _core_from_row(row)
            core_key = core_scope_key(core.scope, core.kind)
            if core.status != "rejected":
                current.setdefault((core_key, core.kind.value), core)
            for name, value in core.payload.items():
                if self.core_field_blocked(core, name, value, core.field_evidence_ids.get(name, core.evidence_ids)):
                    key = self.fingerprint(json.dumps([name, normalize(value)], ensure_ascii=False, sort_keys=True))
                    cursor = self._conn.execute(
                        "INSERT OR IGNORE INTO core_field_tombstones(scope_key,kind,field_key,created_at) VALUES (?,?,?,?)",
                        (core_key, core.kind.value, key, to_utc_iso(now)),
                    )
                    if cursor.rowcount:
                        changed.add(f"{core.kind.value}:{name}")
        for raw in current.values():
            visible = self.mask_core(raw)
            if visible.payload != raw.payload:
                CoreRepository(self._conn).append(
                    raw.scope, raw.kind, visible.payload, "遗忘后移除受影响字段", now, now,
                    evidence_ids=visible.evidence_ids, expected_version=raw.core_version,
                    field_evidence_ids=visible.field_evidence_ids,
                )
        return sorted(changed)


class CoreRepository:
    """版本化核心档案。每次变更追加一条记录，旧版本标记为 superseded。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def latest(self, scope: Scope, kind: CoreKind) -> CoreSnapshot | None:
        """取当前（版本号最大的）快照。

        为什么不按 status='active' 过滤？
        因为某版本被标成 rejected 后可能没有新的 active，
        这时跳过 rejected，回退到最后一个可用版本。
        """
        row = self._conn.execute(
            """
            SELECT * FROM core_versions
            WHERE scope_key = ? AND kind = ?
              AND status != 'rejected'
            ORDER BY core_version DESC
            LIMIT 1
            """,
            (core_scope_key(scope, kind), kind.value),
        ).fetchone()
        return ForgetRepository(self._conn).mask_core(_core_from_row(row)) if row else None

    def history(self, scope: Scope, kind: CoreKind, limit: int = 20) -> list[CoreSnapshot]:
        rows = self._conn.execute(
            """
            SELECT * FROM core_versions
            WHERE scope_key = ? AND kind = ?
            ORDER BY core_version DESC LIMIT ?
            """,
            (core_scope_key(scope, kind), kind.value, limit),
        ).fetchall()
        return [ForgetRepository(self._conn).mask_core(_core_from_row(row)) for row in rows]

    def append(
        self,
        scope: Scope,
        kind: CoreKind,
        payload: dict[str, Any],
        change_reason: str,
        effective_from: datetime,
        now: datetime,
        evidence_ids: Sequence[str] | None = None,
        expected_version: int | None = None,
        field_evidence_ids: dict[str, list[str]] | None = None,
    ) -> CoreSnapshot:
        """追加一个新版本。

        expected_version 为 None 时表示「不校验」，否则必须等于当前最新版本；
        这对应架构文档里的 propose_core_update(..., expected_version)。
        """
        current = self.latest(scope, kind)
        current_version = current.core_version if current else 0
        scope = replace(scope, activity_id=None,
                        relationship_id=scope.relationship_id if kind is CoreKind.RELATIONSHIP else None)
        field_evidence_ids = field_evidence_ids if field_evidence_ids is not None else {
            name: list(evidence_ids or []) for name in payload
        }
        candidate = CoreSnapshot(scope, kind, 0, payload, change_reason, effective_from,
                                 field_evidence_ids=field_evidence_ids, evidence_ids=list(evidence_ids or []))
        if ForgetRepository(self._conn).mask_core(candidate).payload != payload:
            raise MemorySuppressed("核心字段引用已撤回的来源或匹配遗忘标记")

        if expected_version is not None and expected_version != current_version:
            raise VersionConflict(expected_version, current_version, f"核心档案({kind.value})")

        # 被否决的版本仍占据版本号，不能从回退后的可用版本号重新分配。
        max_row = self._conn.execute(
            "SELECT COALESCE(MAX(core_version), 0) FROM core_versions "
            "WHERE scope_key = ? AND kind = ?",
            (core_scope_key(scope, kind), kind.value),
        ).fetchone()
        next_version = int(max_row[0]) + 1

        if current is not None:
            # 旧版本保留数据，只改状态，方便「这个称呼是什么时候变的」这类追溯
            self._conn.execute(
                """
                UPDATE core_versions SET status = 'superseded'
                WHERE scope_key = ? AND kind = ? AND core_version = ?
                """,
                (core_scope_key(scope, kind), kind.value, current.core_version),
            )

        self._conn.execute(
            """
            INSERT INTO core_versions (
                user_id, character_id, kind, core_version, relationship_id, scope_key, domain,
                payload_json, evidence_ids_json, field_evidence_json, change_reason, status,
                effective_from, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope.user_id,
                scope.character_id,
                kind.value,
                next_version,
                scope.relationship_id,
                core_scope_key(scope, kind),
                scope.domain,
                json.dumps(payload, ensure_ascii=False),
                _json_list(evidence_ids),
                json.dumps(field_evidence_ids, ensure_ascii=False),
                change_reason,
                "active",
                to_utc_iso(effective_from),
                to_utc_iso(now),
            ),
        )

        return CoreSnapshot(
            scope=scope,
            kind=kind,
            core_version=next_version,
            payload=payload,
            change_reason=change_reason,
            effective_from=effective_from,
            created_at=now,
            evidence_ids=list(evidence_ids or []),
            status="active",
            field_evidence_ids=field_evidence_ids,
        )


# ---------------------------------------------------------------------------
# 记忆链接
# ---------------------------------------------------------------------------


class LinkRepository:
    """记忆之间的关系。纠正和删除要靠它向下传播。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(
        self,
        from_memory_id: str,
        to_memory_id: str,
        relation: LinkRelation,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_links (link_id, from_memory_id, to_memory_id, relation, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(from_memory_id, to_memory_id, relation) DO NOTHING
            """,
            (new_id("lnk"), from_memory_id, to_memory_id, relation.value, to_utc_iso(now)),
        )

    def dependents(self, memory_id: str) -> list[str]:
        """找出「依赖这条记忆」的派生记忆。

        关系方向：派生记忆 --derived_from--> 源记忆。
        所以查 to_memory_id = 源 的链接。
        """
        rows = self._conn.execute(
            """
            SELECT from_memory_id FROM memory_links
            WHERE to_memory_id = ? AND relation = 'derived_from'
            """,
            (memory_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def parents_of(self, memory_id: str) -> list[str]:
        """这条记忆是从哪些记忆派生出来的。

        删除时用它回答一个关键问题：「删掉这个来源之后，还有别的来源撑着它吗？」
        参考 Zep 的 remove_episode 规则——只有所有父来源都没了，派生事实才该消失。
        """
        rows = self._conn.execute(
            """
            SELECT to_memory_id FROM memory_links
            WHERE from_memory_id = ? AND relation = 'derived_from'
            """,
            (memory_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def has_relation(self, from_memory_id: str, relation: LinkRelation) -> bool:
        """这条记忆是否挂着某种关系。

        检索时用它识别「被去重合并掉的副本」：
        这类记录状态是 superseded，且有一条 part_of 指向主记录。
        """
        row = self._conn.execute(
            "SELECT 1 FROM memory_links WHERE from_memory_id = ? AND relation = ? LIMIT 1",
            (from_memory_id, relation.value),
        ).fetchone()
        return row is not None

    def targets_of(self, memory_id: str) -> list[str]:
        """这条记忆指向了哪些记忆（不管关系类型）。

        主要用途是校验：例如「新记录是否真的指向了被取代的旧记录」。
        需要按关系过滤时请直接用 SQL，别在这里猜。
        """
        rows = self._conn.execute(
            "SELECT to_memory_id FROM memory_links WHERE from_memory_id = ?",
            (memory_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def neighbors(self, memory_id: str, *, relation: LinkRelation | None = None) -> list[tuple[str, str]]:
        """返回一跳图邻居 ``(target_id, relation)``，方向为 from → to。"""
        sql = "SELECT to_memory_id, relation FROM memory_links WHERE from_memory_id = ?"
        params: list[object] = [memory_id]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation.value)
        rows = self._conn.execute(sql, params).fetchall()
        return [(row["to_memory_id"], row["relation"]) for row in rows]

    def incoming(self, memory_id: str, *, relation: LinkRelation | None = None) -> list[tuple[str, str]]:
        """返回一跳反向邻居 ``(source_id, relation)``，用于派生链回溯。"""
        sql = "SELECT from_memory_id, relation FROM memory_links WHERE to_memory_id = ?"
        params: list[object] = [memory_id]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation.value)
        rows = self._conn.execute(sql, params).fetchall()
        return [(row["from_memory_id"], row["relation"]) for row in rows]


# ---------------------------------------------------------------------------
# 实体索引（可重建派生投影）
# ---------------------------------------------------------------------------


def _entity_values(body: Any) -> list[tuple[str, str]]:
    """从四类结构化正文提取保守实体候选，不扫描整段自由文本。"""
    if not isinstance(body, dict):
        return []
    candidates: list[tuple[str, str]] = []
    for key in ("subject", "obj", "participants", "active_entities", "entities", "person", "location", "project", "name"):
        value = body.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                surface = " ".join(item.strip().split())
                if 1 <= len(surface) <= 200:
                    candidates.append((key, surface))
    return list(dict.fromkeys(candidates))


class EntityRepository:
    """实体名称到记忆的可重建索引；不改变事实正文和证据等级。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._forgetting = ForgetRepository(conn)

    @staticmethod
    def canonical(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    def _index_values(self, memory: MemoryItem, now: datetime) -> int:
        """增量写入一条记忆的实体提及；调用方必须已在事务中。"""
        values = _entity_values(memory.body)
        if (
            not values
            or memory.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
            or self._forgetting.is_suppressed(memory)
        ):
            return 0
        scope_key = _scope_key(memory.scope)
        indexed = 0
        touched: set[str] = set()
        seen: set[tuple[str, str]] = set()
        for role, surface in values:
            canonical = self.canonical(surface)
            if not canonical or (canonical, role) in seen:
                continue
            seen.add((canonical, role))
            entity_id = new_id("ent")
            self._conn.execute(
                """INSERT INTO entities (
                    entity_id,scope_key,user_id,character_id,relationship_id,activity_id,domain,
                    canonical_name,display_name,entity_type,mention_count,first_seen,last_seen
                ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(scope_key,canonical_name) DO UPDATE SET
                    display_name=CASE WHEN excluded.last_seen >= entities.last_seen
                                      THEN excluded.display_name ELSE entities.display_name END,
                    first_seen=MIN(entities.first_seen, excluded.first_seen),
                    last_seen=MAX(entities.last_seen, excluded.last_seen)""",
                (entity_id, scope_key, memory.scope.user_id, memory.scope.character_id,
                 memory.scope.relationship_id, memory.scope.activity_id, memory.scope.domain,
                 canonical, surface, role, to_utc_iso(memory.recorded_at), to_utc_iso(memory.recorded_at)),
            )
            entity = self._conn.execute(
                "SELECT entity_id FROM entities WHERE scope_key=? AND canonical_name=?",
                (scope_key, canonical),
            ).fetchone()
            if entity is None:
                continue
            entity_id = entity[0]
            touched.add(entity_id)
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO entity_mentions(entity_id,memory_id,role,surface,created_at) VALUES (?,?,?,?,?)",
                (entity_id, memory.memory_id, role, surface, to_utc_iso(now)),
            )
            indexed += max(cursor.rowcount, 0)
        for entity_id in touched:
            self._conn.execute(
                "UPDATE entities SET mention_count=(SELECT COUNT(*) FROM entity_mentions WHERE entity_mentions.entity_id=entities.entity_id) WHERE entity_id=?",
                (entity_id,),
            )
        return indexed

    def index_memory(self, memory: MemoryItem, now: datetime) -> int:
        """用当前正文替换提及，避免改写/去重后旧实体残留。"""
        self.remove_memory(memory.memory_id)
        return self._index_values(memory, now)

    def remove_memory(self, memory_id: str) -> None:
        """移除一条记忆的实体提及，并清理没有剩余提及的实体。"""
        rows = self._conn.execute(
            "SELECT DISTINCT entity_id FROM entity_mentions WHERE memory_id=?", (memory_id,)
        ).fetchall()
        self._conn.execute("DELETE FROM entity_mentions WHERE memory_id=?", (memory_id,))
        for row in rows:
            self._conn.execute(
                "UPDATE entities SET mention_count=(SELECT COUNT(*) FROM entity_mentions "
                "WHERE entity_mentions.entity_id=entities.entity_id) WHERE entity_id=?",
                (row[0],),
            )
            self._conn.execute(
                "DELETE FROM entities WHERE entity_id=? AND NOT EXISTS "
                "(SELECT 1 FROM entity_mentions WHERE entity_id=?)", (row[0], row[0]),
            )

    def rebuild(self, scope: Scope, now: datetime) -> int:
        visible, params = scope_sql(scope, "e")
        entity_rows = self._conn.execute("SELECT entity_id FROM entities e WHERE " + visible, params).fetchall()
        entity_ids = [row[0] for row in entity_rows]
        if entity_ids:
            placeholders = _placeholders(len(entity_ids))
            self._conn.execute(f"DELETE FROM entity_mentions WHERE entity_id IN ({placeholders})", entity_ids)
            self._conn.execute(f"DELETE FROM entities WHERE entity_id IN ({placeholders})", entity_ids)

        memory_visible, memory_params = retrieval_sql(scope, "m")
        rows = self._conn.execute(
            "SELECT m.* FROM memories m WHERE " + memory_visible + " AND m.status IN ('active','superseded')",
            memory_params,
        ).fetchall()
        count = 0
        for row in rows:
            memory = _memory_from_row(row)
            count += self._index_values(memory, now)
        self._conn.execute(
            "UPDATE entities SET mention_count=(SELECT COUNT(*) FROM entity_mentions WHERE entity_mentions.entity_id=entities.entity_id)"
        )
        return count

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        canonical = self.canonical(query)
        if not canonical:
            return []
        visible, params = scope_sql(scope, "e")
        rows = self._conn.execute(
            "SELECT e.* FROM entities e WHERE " + visible +
            " AND (instr(e.canonical_name, ?) > 0 OR instr(?, e.canonical_name) > 0) "
            "ORDER BY (e.canonical_name = ?) DESC, e.mention_count DESC,e.last_seen DESC LIMIT ?",
            [*params, canonical, canonical, canonical, limit],
        ).fetchall()
        result = []
        for row in rows:
            mentions = self._conn.execute(
                "SELECT DISTINCT memory_id FROM entity_mentions WHERE entity_id=? LIMIT 100",
                (row["entity_id"],),
            ).fetchall()
            result.append({"entity_id": row["entity_id"], "canonical_name": row["canonical_name"],
                           "display_name": row["display_name"], "entity_type": row["entity_type"],
                           "mention_count": row["mention_count"],
                           "memory_ids": [item[0] for item in mentions]})
        return result


# ---------------------------------------------------------------------------
# 整理任务队列
# ---------------------------------------------------------------------------


class JobRepository:
    """持久任务队列。多步写入由调用方放在 Database.transaction() 内。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def enqueue(
        self,
        job_type: str,
        scope: Scope,
        payload: dict[str, Any],
        now: datetime,
        activity_id: str | None = None,
        *,
        max_attempts: int = 3,
    ) -> str:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts 必须为正整数")
        job_id = new_id("job")
        self._conn.execute(
            """
            INSERT INTO consolidation_jobs (
                job_id, job_type, user_id, character_id, relationship_id, activity_id, domain,
                payload_json, status, attempts, created_at, updated_at, max_attempts, available_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                job_type,
                scope.user_id,
                scope.character_id,
                scope.relationship_id,
                activity_id or scope.activity_id,
                scope.domain,
                json.dumps(payload, ensure_ascii=False),
                JobStatus.PENDING.value,
                0,
                to_utc_iso(now),
                to_utc_iso(now),
                max_attempts,
                to_utc_iso(now),
            ),
        )
        return job_id

    def recover_expired(self, now: datetime) -> int:
        """回收失联任务；包括升级前没有租约的 running 行。"""
        return self._conn.execute(
            """UPDATE consolidation_jobs
               SET status = CASE WHEN attempts < max_attempts THEN 'pending' ELSE 'failed' END,
                   claim_token = NULL, lease_until = NULL, available_at = ?, updated_at = ?,
                   last_error = 'worker lease expired'
               WHERE status = 'running' AND (lease_until IS NULL OR lease_until <= ?)""",
            (to_utc_iso(now), to_utc_iso(now), to_utc_iso(now)),
        ).rowcount

    @staticmethod
    def _eligible(
        now: datetime, job_types: Sequence[str] | None, job_ids: Sequence[str] | None = None,
    ) -> tuple[str, list[object]]:
        sql = "status = 'pending' AND available_at <= ? AND attempts < max_attempts"
        params: list[object] = [to_utc_iso(now)]
        for column, values in (("job_type", job_types), ("job_id", job_ids)):
            if values is not None:
                if not values:
                    return "0", []
                sql += f" AND {column} IN ({_placeholders(len(values))})"
                params.extend(values)
        return sql, params

    def ready_ids(self, limit: int, now: datetime, job_types: Sequence[str]) -> list[str]:
        """冻结本批候选集合；真正执行前才领取，避免排队期间租约耗尽。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须为正整数")
        where, params = self._eligible(now, job_types)
        rows = self._conn.execute(
            f"SELECT job_id FROM consolidation_jobs WHERE {where} "
            "ORDER BY created_at, job_id LIMIT ?", [*params, limit],
        ).fetchall()
        return [row["job_id"] for row in rows]

    def claim(
        self,
        limit: int,
        now: datetime,
        job_types: Sequence[str] | None = None,
        *,
        job_ids: Sequence[str] | None = None,
        lease_seconds: float = 120,
    ) -> list[JobRecord]:
        """原子领取；每行生成新凭证。失联任务须先 recover_expired。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or not 0 < lease_seconds < float("inf"):
            raise ValueError("limit 和有限的 lease_seconds 必须大于 0")
        where, filters = self._eligible(now, job_types, job_ids)
        sql = """
            UPDATE consolidation_jobs
            SET status = 'running',
                attempts = attempts + 1,
                updated_at = ?, claim_token = lower(hex(randomblob(16))), lease_until = ?
            WHERE job_id IN (
                SELECT job_id FROM consolidation_jobs
        """
        sql += f" WHERE {where} "
        params: list[object] = [
            to_utc_iso(now), to_utc_iso(now + timedelta(seconds=lease_seconds)), *filters,
        ]

        sql += """
                ORDER BY created_at, job_id
                LIMIT ?
            )
            RETURNING *
        """
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_job_from_row(row) for row in rows]

    def has_pending(self, job_type: str, scope: Scope) -> bool:
        """这个角色是否已经有一条同类待处理任务？

        用来避免「每抽取一次就排一条去重任务」把队列堆满。
        """
        row = self._conn.execute(
            """
            SELECT 1 FROM consolidation_jobs
            WHERE job_type = ? AND user_id = ? AND character_id = ? AND domain = ?
              AND relationship_id IS ? AND activity_id IS ? AND status = 'pending'
            LIMIT 1
            """,
            [job_type, scope.user_id, scope.character_id, scope.domain,
             scope.relationship_id, scope.activity_id],
        ).fetchone()
        return row is not None

    def cancel_event(self, event_id: str, now: datetime) -> int:
        """取消指定事件的抽取任务；运行中的 worker 会被租约校验隔离。"""
        rows = self._conn.execute(
            "SELECT job_id,payload_json,status FROM consolidation_jobs "
            "WHERE job_type='extract' AND status IN ('pending','running')"
        ).fetchall()
        cancelled = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("event_id") != event_id:
                continue
            cursor = self._conn.execute(
                "UPDATE consolidation_jobs SET status='done', claim_token=NULL, lease_until=NULL, "
                "last_error=?, updated_at=? WHERE job_id=? AND status IN ('pending','running')",
                ("event forgotten", to_utc_iso(now), row["job_id"]),
            )
            cancelled += cursor.rowcount
        return cancelled

    def assert_owned(self, job_id: str, claim_token: str, now: datetime) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM consolidation_jobs WHERE job_id = ? AND status = 'running' "
            "AND claim_token = ? AND lease_until > ?",
            (job_id, claim_token, to_utc_iso(now)),
        ).fetchone()
        if row is None:
            raise JobLeaseLost(f"任务租约失效：{job_id}")

    def _owned_update(
        self, job_id: str, claim_token: str, now: datetime,
        assignments: str, params: Sequence[object] = (),
    ) -> None:
        cursor = self._conn.execute(
            f"UPDATE consolidation_jobs SET {assignments}, updated_at = ? "
            "WHERE job_id = ? AND status = 'running' AND claim_token = ? AND lease_until > ?",
            [*params, to_utc_iso(now), job_id, claim_token, to_utc_iso(now)],
        )
        if cursor.rowcount != 1:
            raise JobLeaseLost(f"任务租约失效：{job_id}")

    def renew(self, job_id: str, claim_token: str, now: datetime, lease_seconds: float = 120) -> None:
        if not 0 < lease_seconds < float("inf"):
            raise ValueError("lease_seconds 必须是有限正数")
        self._owned_update(
            job_id, claim_token, now, "lease_until = MAX(lease_until, ?)",
            [to_utc_iso(now + timedelta(seconds=lease_seconds))],
        )

    def release(self, job_id: str, claim_token: str, now: datetime, *, available_at: datetime | None = None) -> None:
        """主动退回，不消耗尝试次数；旧持有者不能释放新持有者的任务。"""
        if available_at is not None and ensure_utc(available_at) < ensure_utc(now):
            raise ValueError("任务释放后的可用时间不能早于当前时间")
        self._owned_update(
            job_id, claim_token, now,
            "status = 'pending', attempts = MAX(0, attempts - 1), "
            "claim_token = NULL, lease_until = NULL, available_at = ?", [to_utc_iso(available_at or now)],
        )

    def complete(self, job_id: str, claim_token: str, now: datetime) -> None:
        self._owned_update(
            job_id, claim_token, now,
            "status = 'done', last_error = NULL, claim_token = NULL, lease_until = NULL",
        )

    def fail(
        self, job_id: str, claim_token: str, error: str, now: datetime,
        *, retry_base_seconds: float = 5, retry_max_seconds: float = 300,
        retryable: bool = True, retry_after_seconds: float | None = None,
    ) -> None:
        """带凭证确认失败，重试使用有上限的指数退避。需在事务内调用。"""
        if not 0 < retry_base_seconds <= retry_max_seconds < float("inf"):
            raise ValueError("重试间隔必须为有限正数，且上限不小于基础间隔")
        if not isinstance(retryable, bool):
            raise ValueError("retryable 必须为布尔值")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, (int, float))
            or not 0 <= retry_after_seconds <= 86400
        ):
            raise ValueError("retry_after_seconds 必须在 0..86400 秒之间")
        self.assert_owned(job_id, claim_token, now)
        row = self._conn.execute(
            "SELECT attempts FROM consolidation_jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
        delay = min(retry_max_seconds, retry_base_seconds * 2 ** min(row["attempts"] - 1, 30))
        # 服务端的最早重试时间可长于本地退避上限，持久保存而非阻塞 worker。
        delay = max(delay, retry_after_seconds or 0) if retryable else 0
        self._owned_update(
            job_id, claim_token, now,
            "status = CASE WHEN ? AND attempts < max_attempts THEN 'pending' ELSE 'failed' END, "
            "last_error = ?, available_at = ?, claim_token = NULL, lease_until = NULL",
            [int(retryable), error[:2000], to_utc_iso(now + timedelta(seconds=delay))],
        )

    def stats(self, now: datetime) -> dict[str, int]:
        """只读快照；expired 是 running 的子集，scheduled 是 pending 的子集。"""
        # 单条 SELECT 保证各计数对应同一快照，避免并发完成时统计互相矛盾。
        row = self._conn.execute(
            """SELECT SUM(status = 'pending') AS pending, SUM(status = 'running') AS running,
                      SUM(status = 'done') AS done, SUM(status = 'failed') AS failed,
                      SUM(status = 'pending' AND available_at > ?) AS scheduled,
                      SUM(status = 'pending' AND available_at <= ? AND attempts < max_attempts) AS ready,
                      SUM(status = 'running' AND (lease_until IS NULL OR lease_until <= ?)) AS expired
               FROM consolidation_jobs""",
            (to_utc_iso(now), to_utc_iso(now), to_utc_iso(now)),
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def pending_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM consolidation_jobs WHERE status = 'pending'"
        ).fetchone()
        return int(row["n"])


# ---------------------------------------------------------------------------
# 向量索引
# ---------------------------------------------------------------------------


class VectorRepository:
    """向量索引的读写。

    这里只做「存取字节」，不算余弦、不管模型——那些属于 retrieval/vector.py。
    分开的好处：换存储（比如以后用 sqlite-vec 扩展）时只改这一层。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(
        self,
        memory_id: str,
        model: str,
        dim: int,
        vector: bytes,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_vectors (memory_id, model, dim, vector, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(memory_id) DO UPDATE SET
                model = excluded.model,
                dim = excluded.dim,
                vector = excluded.vector,
                updated_at = excluded.updated_at
            """,
            (memory_id, model, dim, vector, to_utc_iso(now)),
        )

    def delete(self, memory_id: str) -> None:
        """记忆被删除时一并清掉向量，避免它换个形式又被检索到。"""
        self._conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))

    def load_scope(
        self,
        scope: Scope,
        model: str,
        memory_types: list[MemoryType] | None = None,
        *, read_filter: ReadFilter | None = None,
    ) -> list[tuple[str, MemoryType, bytes]]:
        """取出某个角色在某模型下的全部向量。

        第一版是全量载入内存做暴力检索：本地几百上千条记忆完全够用，
        而且实现简单、没有近似误差。规模上去再换 ANN 索引。
        """
        visible, params = retrieval_sql(scope, "m", read_filter)
        sql = f"""
            SELECT v.memory_id, v.vector, m.memory_type
            FROM memory_vectors v
            JOIN memories m ON m.memory_id = v.memory_id
            WHERE v.model = ?
              AND {visible}
              AND m.status IN ('active', 'superseded')
        """
        params = [model, *params]

        if memory_types:
            sql += f" AND m.memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        rows = self._conn.execute(sql, params).fetchall()
        return [(row["memory_id"], MemoryType(row["memory_type"]), row["vector"]) for row in rows]

    def unindexed_scope(self, scope: Scope, model: str, memory_types: list[MemoryType] | None = None,
                        *, read_filter: ReadFilter | None = None, include_missing: bool = True) -> list[str]:
        visible, params = retrieval_sql(scope, "m", read_filter)
        condition = "(v.model != ? OR v.memory_id IS NULL)" if include_missing else "v.model != ?"
        sql = (f"SELECT m.memory_id FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.memory_id "
               f"WHERE {visible} AND m.status IN ('active','superseded') AND {condition}")
        params = [*params, model]
        if memory_types:
            sql += f" AND m.memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)
        return [row[0] for row in self._conn.execute(sql, params).fetchall()]

    def count(self, model: str | None = None) -> int:
        if model:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memory_vectors WHERE model = ?", (model,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memory_vectors").fetchone()
        return int(row["n"])


# ---------------------------------------------------------------------------
# 主题摘要
# ---------------------------------------------------------------------------


class SummaryRepository:
    """主题索引卡的读写。

        upsert 的语义是「更新同一张卡」：主题在精确生成范围内唯一，
    重建摘要不会产生新的 summary_id，外部引用不会失效。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, summary_id: str) -> Summary | None:
        row = self._conn.execute(
            "SELECT * FROM summaries WHERE summary_id = ?", (summary_id,)
        ).fetchone()
        return _summary_from_row(row) if row else None

    def get_by_topic(self, scope: Scope, topic: str) -> Summary | None:
        row = self._conn.execute(
            "SELECT * FROM summaries WHERE scope_key = ? AND topic = ?",
            (_scope_key(scope), topic),
        ).fetchone()
        return _summary_from_row(row) if row else None

    def list_active(self, scope: Scope, limit: int = 50, *, read_filter: ReadFilter | None = None) -> list[Summary]:
        visible, params = retrieval_sql(scope, read_filter=read_filter)
        rows = self._conn.execute(
            f"""
            SELECT * FROM summaries
            WHERE {visible} AND status = 'active'
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        summaries = [_summary_from_row(row) for row in rows]
        result = []
        for summary in summaries:
            sources = MemoryRepository(self._conn).fetch_many(summary.source_memory_ids)
            if (len(sources) != len(set(summary.source_memory_ids))
                    or any(item.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
                           or ForgetRepository(self._conn).is_suppressed(item) for item in sources.values())
                    or (summary.source_digest and memory_source_digest(
                        list(sources.values()), summary.algorithm_version) != summary.source_digest)):
                continue
            result.append(summary)
        return result

    def is_fresh(self, summary: Summary) -> bool:
        """逐跳读取摘要时再次核对来源，避免旧派生物绕过列表过滤。"""
        if not summary.source_digest:
            return True
        sources = MemoryRepository(self._conn).fetch_many(summary.source_memory_ids)
        return (len(sources) == len(set(summary.source_memory_ids))
                and all(item.status not in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
                        and not ForgetRepository(self._conn).is_suppressed(item)
                        for item in sources.values())
                and memory_source_digest(list(sources.values()), summary.algorithm_version)
                == summary.source_digest)

    def upsert(self, summary: Summary, now: datetime, *, reactivate: bool = False) -> Summary:
        """写入主题投影；撤回状态只能由可信宿主显式重新激活。"""
        existing = self.get_by_topic(summary.scope, summary.topic)

        if existing is not None:
            if existing.status in {"withdrawn", "rolled_back"} and not reactivate:
                return existing
            self._conn.execute(
                """
                UPDATE summaries SET
                    digest = ?,
                    source_memory_ids_json = ?,
                    source_event_ids_json = ?,
                    source_digest = ?,
                    algorithm_version = ?,
                    span_start = ?,
                    span_end = ?,
                    importance = ?,
                    status = 'active',
                    updated_at = ?
                WHERE summary_id = ?
                """,
                (
                    summary.digest,
                    _json_list(summary.source_memory_ids),
                    _json_list(summary.source_event_ids),
                    summary.source_digest,
                    summary.algorithm_version,
                    to_utc_iso(summary.span_start),
                    to_utc_iso(summary.span_end),
                    summary.importance,
                    to_utc_iso(now),
                    existing.summary_id,
                ),
            )
            return Summary(
                summary_id=existing.summary_id,
                scope=summary.scope,
                topic=summary.topic,
                digest=summary.digest,
                source_memory_ids=list(summary.source_memory_ids),
                source_event_ids=list(summary.source_event_ids),
                source_digest=summary.source_digest,
                algorithm_version=summary.algorithm_version,
                span_start=summary.span_start,
                span_end=summary.span_end,
                importance=summary.importance,
                status="active",
                created_at=existing.created_at,
                updated_at=now,
            )

        self._conn.execute(
            """
            INSERT INTO summaries (
                summary_id, user_id, character_id, relationship_id, activity_id, domain, scope_key,
                topic, digest,
                source_memory_ids_json, source_event_ids_json,
                source_digest, algorithm_version,
                span_start, span_end, importance, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                summary.summary_id,
                summary.scope.user_id,
                summary.scope.character_id,
                summary.scope.relationship_id,
                summary.scope.activity_id,
                summary.scope.domain,
                _scope_key(summary.scope),
                summary.topic,
                summary.digest,
                _json_list(summary.source_memory_ids),
                _json_list(summary.source_event_ids),
                summary.source_digest,
                summary.algorithm_version,
                to_utc_iso(summary.span_start),
                to_utc_iso(summary.span_end),
                summary.importance,
                "active",
                to_utc_iso(now),
                to_utc_iso(now),
            ),
        )
        return summary

    def invalidate_for_memories(self, memory_ids: Sequence[str], now: datetime) -> int:
        """来源变化立即隐藏摘要；重建前不能继续恢复旧摘要正文。"""
        changed = set(memory_ids)
        if not changed:
            return 0
        rows = self._conn.execute(
            "SELECT summary_id, source_memory_ids_json FROM summaries WHERE status = 'active'"
        ).fetchall()
        affected = [row["summary_id"] for row in rows
                    if changed.intersection(_load_list(row["source_memory_ids_json"]))]
        return self.mark_stale(affected, now)

    def mark_stale(self, summary_ids: Sequence[str], now: datetime) -> int:
        """把不再对应任何主题的旧卡标成 stale。

        为什么不直接删？因为外部可能正引用它，
        标成 stale 至少还能解释「这张卡为什么没内容了」。
        """
        if not summary_ids:
            return 0
        cursor = self._conn.execute(
            f"UPDATE summaries SET status = 'stale', updated_at = ? "
            f"WHERE summary_id IN ({_placeholders(len(summary_ids))}) AND status='active'",
            [to_utc_iso(now), *summary_ids],
        )
        return cursor.rowcount

    def mark_withdrawn(self, summary_ids: Sequence[str], now: datetime) -> int:
        """撤回派生卡，直到可信宿主显式重新激活；保留来源记忆。"""
        return self._mark_lifecycle(summary_ids, "withdrawn", now)

    def mark_rolled_back(self, summary_ids: Sequence[str], now: datetime) -> int:
        """标记算法或人工重建被回滚的派生卡，等待后续显式重建。"""
        return self._mark_lifecycle(summary_ids, "rolled_back", now)

    def _mark_lifecycle(self, summary_ids: Sequence[str], status: str, now: datetime) -> int:
        if status not in {"withdrawn", "rolled_back"}:
            raise ValueError("未知的摘要生命周期状态")
        ids = list(dict.fromkeys(summary_ids))
        if not ids:
            return 0
        cursor = self._conn.execute(
            f"UPDATE summaries SET status=?, updated_at=? "
            f"WHERE summary_id IN ({_placeholders(len(ids))}) AND status<>?",
            [status, to_utc_iso(now), *ids, status],
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# 跨进程检索缓存（只保存候选 ID 与版本指纹）
# ---------------------------------------------------------------------------


class RetrievalCacheRepository:
    """SQLite 共享检索缓存；正文和会话引用永远不进入缓存表。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def generation(self) -> int:
        return int(self._conn.execute(
            "SELECT generation FROM retrieval_state WHERE singleton=1"
        ).fetchone()[0])

    def get(
        self,
        cache_key: str,
        scope_key: str,
        current_event_id: str | None,
        mutation_epoch: int,
        content_generation: int,
        now: datetime,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM retrieval_cache WHERE cache_key=? AND scope_key=? "
            "AND current_event_id IS ? AND mutation_epoch=? AND content_generation=? AND expires_at>?",
            (cache_key, scope_key, current_event_id, mutation_epoch, content_generation, to_utc_iso(now)),
        ).fetchone()
        if row is None:
            # 过期项是派生数据，可以惰性清理，不影响主记忆。
            self._conn.execute(
                "DELETE FROM retrieval_cache WHERE cache_key=? AND (expires_at<=? OR content_generation!=?)",
                (cache_key, to_utc_iso(now), content_generation),
            )
            return None
        return {
            "cache_key": row["cache_key"],
            "memory_ids": _load_list(row["memory_ids_json"]),
            "summary_ids": _load_list(row["summary_ids_json"]),
            "memory_revisions": _load_dict(row["memory_revisions_json"]),
            "summary_revisions": _load_dict(row["summary_revisions_json"]),
            "retrieval_complete": bool(row["retrieval_complete"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def put(
        self,
        *,
        cache_key: str,
        scope_key: str,
        current_event_id: str | None,
        mutation_epoch: int,
        content_generation: int,
        memory_ids: Sequence[str],
        summary_ids: Sequence[str],
        memory_revisions: dict[str, str],
        summary_revisions: dict[str, str],
        retrieval_complete: bool,
        created_at: datetime,
        expires_at: datetime,
        capacity: int = 1024,
    ) -> None:
        self._conn.execute(
            """INSERT INTO retrieval_cache (
                cache_key,scope_key,current_event_id,mutation_epoch,content_generation,
                memory_ids_json,summary_ids_json,memory_revisions_json,summary_revisions_json,
                retrieval_complete,created_at,expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                scope_key=excluded.scope_key,
                current_event_id=excluded.current_event_id,
                mutation_epoch=excluded.mutation_epoch,
                content_generation=excluded.content_generation,
                memory_ids_json=excluded.memory_ids_json,
                summary_ids_json=excluded.summary_ids_json,
                memory_revisions_json=excluded.memory_revisions_json,
                summary_revisions_json=excluded.summary_revisions_json,
                retrieval_complete=excluded.retrieval_complete,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at""",
            (
                cache_key, scope_key, current_event_id, mutation_epoch, content_generation,
                _json_list(memory_ids), _json_list(summary_ids),
                json.dumps(memory_revisions, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(summary_revisions, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                int(bool(retrieval_complete)), to_utc_iso(created_at), to_utc_iso(expires_at),
            ),
        )
        # 全局过期/旧代次清理和容量限制，避免只查询新键时无限积累。
        self._conn.execute(
            "DELETE FROM retrieval_cache WHERE expires_at<=? OR content_generation!=?",
            (to_utc_iso(created_at), content_generation),
        )
        self._conn.execute(
            "DELETE FROM retrieval_cache WHERE cache_key IN "
            "(SELECT cache_key FROM retrieval_cache ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (capacity,),
        )

    def delete(self, cache_key: str) -> None:
        self._conn.execute("DELETE FROM retrieval_cache WHERE cache_key=?", (cache_key,))

    def delete_scopes(self, scope_keys: Sequence[str]) -> int:
        keys = list(dict.fromkeys(scope_keys))
        if not keys:
            return 0
        cursor = self._conn.execute(
            f"DELETE FROM retrieval_cache WHERE scope_key IN ({_placeholders(len(keys))})", keys,
        )
        return cursor.rowcount

    def stats(self, scope: Scope, now: datetime) -> dict[str, int]:
        """返回指定精确绑定的缓存计数，不读取缓存正文。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(expires_at > ? AND content_generation=s.generation) AS active, "
            "SUM(expires_at <= ?) AS expired, "
            "SUM(expires_at > ? AND content_generation!=s.generation) AS invalidated "
            "FROM retrieval_cache CROSS JOIN retrieval_state s WHERE s.singleton=1 AND scope_key=?",
            (to_utc_iso(now), to_utc_iso(now), to_utc_iso(now), _scope_key(scope)),
        ).fetchone()
        return {"total": int(row["total"] or 0), "active": int(row["active"] or 0),
                "expired": int(row["expired"] or 0), "invalidated": int(row["invalidated"] or 0)}


# ---------------------------------------------------------------------------
# Durable journal / hash chain
# ---------------------------------------------------------------------------


class JournalRepository:
    """追加式状态审计链。

    journal 只保存事件元数据和 payload 指纹，避免把原始对话再复制一份。
    所有调用者须把 append 放在自己的业务事务里；这样链上的记录与状态变更
    要么一起提交，要么一起回滚。
    """

    GENESIS = "0" * 64

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def payload_digest(payload: dict[str, Any]) -> str:
        material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _scope_values(scope: Scope | None) -> tuple[str, str | None, str | None, str | None, str | None, str | None]:
        if scope is None:
            return "", None, None, None, None, None
        return (
            _scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id,
            scope.activity_id, scope.domain,
        )

    @classmethod
    def _entry_material(
        cls,
        *,
        journal_id: str,
        sequence: int,
        scope_key: str,
        event_type: str,
        entity_id: str | None,
        payload_json: str,
        prev_hash: str,
        created_at: str,
    ) -> str:
        return json.dumps(
            [journal_id, sequence, scope_key, event_type, entity_id,
             json.loads(payload_json), prev_hash, created_at],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    def append(
        self,
        event_type: str,
        *,
        scope: Scope | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """追加一条链记录，返回可用于日志的元数据。

        ``idempotency_key`` 仅用于同一个作用域和事件类型的业务重试；没有键的
        记录允许无限追加。SQLite 写事务保证 sequence 与 prev_hash 不会分叉。
        """
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("journal event_type 必须为非空字符串")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("journal payload 必须为对象")
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise ValueError("journal idempotency_key 必须为字符串或 null")

        scope_key, user_id, character_id, relationship_id, activity_id, domain = self._scope_values(scope)
        if idempotency_key is not None:
            existing = self._conn.execute(
                "SELECT * FROM journal_entries WHERE scope_key=? AND event_type=? AND idempotency_key=?",
                (scope_key, event_type, idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(existing["payload_json"] or "{}")
                if existing["entity_id"] != entity_id or existing_payload != payload:
                    raise ValueError("同一 journal 幂等键对应了不同的实体或 payload")
                return dict(existing)

        row = self._conn.execute(
            "SELECT sequence, entry_hash FROM journal_entries ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = int(row["sequence"]) + 1 if row else 1
        prev_hash = row["entry_hash"] if row else self.GENESIS
        journal_id = new_id("journal")
        created_at = to_utc_iso(now)
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        material = self._entry_material(
            journal_id=journal_id, sequence=sequence, scope_key=scope_key,
            event_type=event_type, entity_id=entity_id, payload_json=payload_json,
            prev_hash=prev_hash, created_at=created_at,
        )
        entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        self._conn.execute(
            """
            INSERT INTO journal_entries (
                journal_id, sequence, scope_key, user_id, character_id, relationship_id,
                activity_id, domain, event_type, entity_id, idempotency_key,
                payload_json, prev_hash, entry_hash, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                journal_id, sequence, scope_key, user_id, character_id, relationship_id,
                activity_id, domain, event_type, entity_id, idempotency_key,
                payload_json, prev_hash, entry_hash, created_at,
            ),
        )
        return {
            "journal_id": journal_id,
            "sequence": sequence,
            "scope_key": scope_key,
            "event_type": event_type,
            "entity_id": entity_id,
            "payload": payload,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
            "created_at": created_at,
        }

    def list(self, *, scope: Scope | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("journal limit 必须为正整数")
        if scope is None:
            rows = self._conn.execute(
                "SELECT * FROM journal_entries ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            visible, params = scope_sql(scope)
            rows = self._conn.execute(
                "SELECT * FROM journal_entries WHERE " + visible
                + " ORDER BY sequence DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._public(row) for row in rows]

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        try:
            value["payload"] = json.loads(value.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            value["payload"] = None
            value.pop("payload_json", None)
        return value

    def verify(self) -> dict[str, Any]:
        """从 genesis 重算整条链，报告首个不一致位置。"""
        rows = self._conn.execute("SELECT * FROM journal_entries ORDER BY sequence ASC").fetchall()
        previous = self.GENESIS
        expected_sequence = 1
        for row in rows:
            if row["sequence"] != expected_sequence:
                return {"valid": False, "checked": expected_sequence - 1,
                        "error": "sequence_gap", "sequence": row["sequence"]}
            if row["prev_hash"] != previous:
                return {"valid": False, "checked": expected_sequence - 1,
                        "error": "prev_hash_mismatch", "sequence": row["sequence"]}
            try:
                payload_json = json.dumps(json.loads(row["payload_json"] or "{}"),
                                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                material = self._entry_material(
                    journal_id=row["journal_id"], sequence=row["sequence"],
                    scope_key=row["scope_key"], event_type=row["event_type"],
                    entity_id=row["entity_id"], payload_json=payload_json,
                    prev_hash=row["prev_hash"], created_at=row["created_at"],
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"valid": False, "checked": expected_sequence - 1,
                        "error": "payload_invalid", "sequence": row["sequence"]}
            actual = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["entry_hash"] != actual:
                return {"valid": False, "checked": expected_sequence - 1,
                        "error": "entry_hash_mismatch", "sequence": row["sequence"]}
            previous = row["entry_hash"]
            expected_sequence += 1
        return {"valid": True, "checked": len(rows),
                "head_sequence": len(rows), "head_hash": previous}

    def redact_scope(self, scope: Scope, *, now: datetime, reason: str = "", generation: int | None = None) -> int:
        """擦除作用域审计载荷并重建链，不删除审计行。"""
        visible, params = scope_sql(scope)
        rows = self._conn.execute(
            "SELECT * FROM journal_entries WHERE " + visible + " ORDER BY sequence ASC", params
        ).fetchall()
        if not rows:
            return 0
        reason_digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        for row in rows:
            payload: dict[str, Any] = {"redacted": True, "reason_digest": reason_digest}
            if generation is not None:
                payload["purge_generation"] = generation
            self._conn.execute(
                "UPDATE journal_entries SET entity_id=NULL, idempotency_key=NULL, payload_json=? WHERE journal_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), row["journal_id"]),
            )

        previous = self.GENESIS
        all_rows = self._conn.execute("SELECT * FROM journal_entries ORDER BY sequence ASC").fetchall()
        for row in all_rows:
            material = self._entry_material(
                journal_id=row["journal_id"], sequence=int(row["sequence"]),
                scope_key=row["scope_key"], event_type=row["event_type"],
                entity_id=row["entity_id"], payload_json=row["payload_json"],
                prev_hash=previous, created_at=row["created_at"],
            )
            entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            self._conn.execute(
                "UPDATE journal_entries SET prev_hash=?, entry_hash=? WHERE journal_id=?",
                (previous, entry_hash, row["journal_id"]),
            )
            previous = entry_hash
        return len(rows)


# ---------------------------------------------------------------------------
# 仓储集合
# ---------------------------------------------------------------------------


@dataclass
class Repositories:
    """把各个仓储打包传递，避免运行时到处 new。"""

    events: EventRepository
    memories: MemoryRepository
    activities: ActivityRepository
    handoffs: HandoffRepository
    cores: CoreRepository
    links: LinkRepository
    jobs: JobRepository
    vectors: VectorRepository
    summaries: SummaryRepository
    retrieval_cache: RetrievalCacheRepository
    entities: EntityRepository
    forgetting: ForgetRepository
    journal: JournalRepository


def build_repositories(conn: sqlite3.Connection) -> Repositories:
    return Repositories(
        events=EventRepository(conn),
        memories=MemoryRepository(conn),
        activities=ActivityRepository(conn),
        handoffs=HandoffRepository(conn),
        cores=CoreRepository(conn),
        links=LinkRepository(conn),
        jobs=JobRepository(conn),
        vectors=VectorRepository(conn),
        summaries=SummaryRepository(conn),
        retrieval_cache=RetrievalCacheRepository(conn),
        entities=EntityRepository(conn),
        forgetting=ForgetRepository(conn),
        journal=JournalRepository(conn),
    )
