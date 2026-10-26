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
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..clock import ensure_utc, from_utc_iso, to_utc_iso
from ..errors import VersionConflict
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
    body_to_text,
    new_id,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _placeholders(count: int) -> str:
    """生成 `?,?,?` 这样的占位符串，供 IN (...) 使用。"""
    return ",".join("?" for _ in range(count))


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
        ),
        kind=CoreKind(row["kind"]),
        core_version=row["core_version"],
        payload=_load_dict(row["payload_json"]),
        change_reason=row["change_reason"],
        effective_from=from_utc_iso(row["effective_from"]) or ensure_utc(datetime.min),
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        evidence_ids=_load_list(row["evidence_ids_json"]),
        status=row["status"],
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


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        job_type=row["job_type"],
        scope=Scope(
            user_id=row["user_id"],
            character_id=row["character_id"],
        ),
        activity_id=row["activity_id"],
        payload=_load_dict(row["payload_json"]),
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=from_utc_iso(row["created_at"]) or ensure_utc(datetime.min),
        updated_at=from_utc_iso(row["updated_at"]) or ensure_utc(datetime.min),
    )


# ---------------------------------------------------------------------------
# 原始事件
# ---------------------------------------------------------------------------


class EventRepository:
    """原始事件读写。事件是 append-only 的：只插入，不更新正文。"""

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
            ON CONFLICT(idempotency_key) DO NOTHING
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

        if cursor.rowcount == 0 and event.idempotency_key:
            existing = self.find_by_idempotency_key(event.idempotency_key)
            if existing is not None:
                return existing, True
        return event, False

    def get(self, event_id: str) -> EventRecord | None:
        row = self._conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return _event_from_row(row) if row else None

    def find_by_idempotency_key(self, key: str) -> EventRecord | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE idempotency_key = ?", (key,)
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
        rows = self._conn.execute(
            """
            SELECT * FROM events
            WHERE user_id = ? AND character_id = ?
              AND occurred_at >= ? AND occurred_at < ?
            ORDER BY occurred_at ASC
            LIMIT ?
            """,
            (scope.user_id, scope.character_id, to_utc_iso(start), to_utc_iso(end), limit),
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
                body_json, body_text, recorded_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            ),
        )
        return item if cursor.rowcount > 0 else None

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

    def list_recent(
        self,
        scope: Scope,
        since: datetime,
        memory_types: list[MemoryType] | None,
        limit: int,
    ) -> list[MemoryItem]:
        """取某个时间点之后出现的记忆，按事件时间倒序。

        用 COALESCE(occurred_start, recorded_at)：有的记忆只有记录时间
        （比如「用户今天提到自己十年前搬过家」），不能因为缺事件时间就漏掉。
        """
        sql = """
            SELECT * FROM memories
            WHERE user_id = ? AND character_id = ?
              AND status = 'active'
              AND COALESCE(occurred_start, recorded_at) >= ?
        """
        params: list[object] = [scope.user_id, scope.character_id, to_utc_iso(since)]

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
    ) -> list[MemoryItem]:
        """按事件时间范围取记忆，用于「明确日期」的精确回查。"""
        sql = """
            SELECT * FROM memories
            WHERE user_id = ? AND character_id = ?
              AND status = 'active'
              AND COALESCE(occurred_start, recorded_at) >= ?
              AND COALESCE(occurred_start, recorded_at) < ?
        """
        params: list[object] = [
            scope.user_id,
            scope.character_id,
            to_utc_iso(start),
            to_utc_iso(end),
        ]

        if memory_types:
            sql += f" AND memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        sql += " ORDER BY occurred_start ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def list_important(self, scope: Scope, limit: int) -> list[MemoryItem]:
        """按重要度取少量「值得先想起来」的经历，用于 bootstrap 摘要。"""
        rows = self._conn.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND character_id = ?
              AND status = 'active'
              AND memory_type IN ('episodic', 'semantic')
            ORDER BY importance DESC, COALESCE(occurred_start, recorded_at) DESC
            LIMIT ?
            """,
            (scope.user_id, scope.character_id, limit),
        ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def update_status(self, memory_id: str, status: MemoryStatus, now: datetime) -> None:
        self._conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE memory_id = ?",
            (status.value, to_utc_iso(now), memory_id),
        )

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
        self._conn.execute(
            "UPDATE memories SET valid_to = ?, status = ?, updated_at = ? WHERE memory_id = ?",
            (to_utc_iso(valid_to), status.value, to_utc_iso(now), memory_id),
        )

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
        sql = """
            SELECT * FROM memories
            WHERE user_id = ? AND character_id = ? AND status != 'deleted'
        """
        params: list[object] = [scope.user_id, scope.character_id]

        if memory_types:
            sql += f" AND memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        if statuses:
            sql += f" AND status IN ({_placeholders(len(statuses))})"
            params.extend(status.value for status in statuses)

        if scope.domain:
            sql += " AND domain = ?"
            params.append(scope.domain)

        sql += " ORDER BY COALESCE(occurred_start, recorded_at) DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def update_content(
        self,
        memory_id: str,
        body: dict[str, Any],
        now: datetime,
        *,
        importance: float | None = None,
        source_event_ids: Sequence[str] | None = None,
    ) -> None:
        """改写一条记忆的正文（以及可选的重要度、证据来源）。

        去重合并用它。注意 body_text 必须一起更新：
        FTS 触发器只在 body_text 变化时重建索引，
        只改 body_json 会让检索用的文本和正文不一致。
        """
        item_type = self._conn.execute(
            "SELECT memory_type FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if item_type is None:
            return

        memory_type = MemoryType(item_type["memory_type"])
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

        params.append(memory_id)
        self._conn.execute(
            f"UPDATE memories SET {', '.join(assignments)} WHERE memory_id = ?", params
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
        sql = """
            SELECT * FROM activities
            WHERE user_id = ? AND character_id = ?
        """
        params: list[object] = [scope.user_id, scope.character_id]

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
# 身份核心 / 关系档案
# ---------------------------------------------------------------------------


class CoreRepository:
    """版本化核心档案。每次变更追加一条记录，旧版本标记为 superseded。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def latest(self, scope: Scope, kind: CoreKind) -> CoreSnapshot | None:
        """取当前（版本号最大的）快照。

        为什么不按 status='active' 过滤？
        因为可能有人手工把某版本标成 rejected 之后没有新的 active，
        这时仍应回退到最后一个可用版本，而不是让角色「失忆」。
        """
        row = self._conn.execute(
            """
            SELECT * FROM core_versions
            WHERE user_id = ? AND character_id = ? AND kind = ?
            ORDER BY core_version DESC
            LIMIT 1
            """,
            (scope.user_id, scope.character_id, kind.value),
        ).fetchone()
        return _core_from_row(row) if row else None

    def history(self, scope: Scope, kind: CoreKind, limit: int = 20) -> list[CoreSnapshot]:
        rows = self._conn.execute(
            """
            SELECT * FROM core_versions
            WHERE user_id = ? AND character_id = ? AND kind = ?
            ORDER BY core_version DESC LIMIT ?
            """,
            (scope.user_id, scope.character_id, kind.value, limit),
        ).fetchall()
        return [_core_from_row(row) for row in rows]

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
    ) -> CoreSnapshot:
        """追加一个新版本。

        expected_version 为 None 时表示「不校验」，否则必须等于当前最新版本；
        这对应架构文档里的 propose_core_update(..., expected_version)。
        """
        current = self.latest(scope, kind)
        current_version = current.core_version if current else 0

        if expected_version is not None and expected_version != current_version:
            raise VersionConflict(expected_version, current_version, f"核心档案({kind.value})")

        next_version = current_version + 1

        if current is not None:
            # 旧版本保留数据，只改状态，方便「这个称呼是什么时候变的」这类追溯
            self._conn.execute(
                """
                UPDATE core_versions SET status = 'superseded'
                WHERE user_id = ? AND character_id = ? AND kind = ? AND core_version = ?
                """,
                (scope.user_id, scope.character_id, kind.value, current.core_version),
            )

        self._conn.execute(
            """
            INSERT INTO core_versions (
                user_id, character_id, kind, core_version, relationship_id,
                payload_json, evidence_ids_json, change_reason, status,
                effective_from, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope.user_id,
                scope.character_id,
                kind.value,
                next_version,
                scope.relationship_id,
                json.dumps(payload, ensure_ascii=False),
                _json_list(evidence_ids),
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


# ---------------------------------------------------------------------------
# 整理任务队列
# ---------------------------------------------------------------------------


class JobRepository:
    """异步整理任务。第一版只是一个可靠的任务表，由外部 worker 拉取执行。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def enqueue(
        self,
        job_type: str,
        scope: Scope,
        payload: dict[str, Any],
        now: datetime,
        activity_id: str | None = None,
    ) -> str:
        job_id = new_id("job")
        self._conn.execute(
            """
            INSERT INTO consolidation_jobs (
                job_id, job_type, user_id, character_id, activity_id,
                payload_json, status, attempts, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                job_type,
                scope.user_id,
                scope.character_id,
                activity_id or scope.activity_id,
                json.dumps(payload, ensure_ascii=False),
                JobStatus.PENDING.value,
                0,
                to_utc_iso(now),
                to_utc_iso(now),
            ),
        )
        return job_id

    def claim(
        self,
        limit: int,
        now: datetime,
        job_types: Sequence[str] | None = None,
    ) -> list[JobRecord]:
        """取出待处理任务并标记为 running。

        参数:
            job_types: 只取这几类任务；None 表示不限。
                       抽取任务需要抽取器，去重任务不需要——
                       调用方可以据此只领自己能处理的那部分。

        注意：这是「单进程」版本。多进程部署时应当换成
        `UPDATE ... RETURNING` + 事务锁，避免两个 worker 抢到同一条任务。
        """
        sql = """
            SELECT * FROM consolidation_jobs
            WHERE status = 'pending'
        """
        params: list[object] = []

        if job_types:
            sql += f" AND job_type IN ({_placeholders(len(job_types))})"
            params.extend(job_types)

        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()

        jobs: list[JobRecord] = []
        for row in rows:
            cursor = self._conn.execute(
                """
                UPDATE consolidation_jobs
                SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (to_utc_iso(now), row["job_id"]),
            )
            if cursor.rowcount:
                jobs.append(_job_from_row(row))
        return jobs

    def has_pending(self, job_type: str, scope: Scope) -> bool:
        """这个角色是否已经有一条同类待处理任务？

        用来避免「每抽取一次就排一条去重任务」把队列堆满。
        """
        row = self._conn.execute(
            """
            SELECT 1 FROM consolidation_jobs
            WHERE job_type = ? AND user_id = ? AND character_id = ? AND status = 'pending'
            LIMIT 1
            """,
            (job_type, scope.user_id, scope.character_id),
        ).fetchone()
        return row is not None

    def release(self, job_id: str, now: datetime) -> None:
        """把任务退回 pending，且不消耗重试次数。

        用于「这次没条件处理」的情况，例如没传抽取器时的 extract 任务。
        和 fail() 的区别：fail 是真出错，release 是「先放着」。
        """
        self._conn.execute(
            """
            UPDATE consolidation_jobs
            SET status = 'pending',
                attempts = MAX(0, attempts - 1),
                updated_at = ?
            WHERE job_id = ?
            """,
            (to_utc_iso(now), job_id),
        )

    def complete(self, job_id: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE consolidation_jobs SET status = 'done', updated_at = ?, last_error = NULL "
            "WHERE job_id = ?",
            (to_utc_iso(now), job_id),
        )

    def fail(self, job_id: str, error: str, now: datetime, max_attempts: int = 3) -> None:
        """失败处理：未超过重试上限就退回 pending，否则标记 failed。"""
        row = self._conn.execute(
            "SELECT attempts FROM consolidation_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        attempts = row["attempts"] if row else max_attempts
        status = JobStatus.PENDING if attempts < max_attempts else JobStatus.FAILED
        self._conn.execute(
            "UPDATE consolidation_jobs SET status = ?, last_error = ?, updated_at = ? "
            "WHERE job_id = ?",
            (status.value, error[:2000], to_utc_iso(now), job_id),
        )

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
    ) -> list[tuple[str, MemoryType, bytes]]:
        """取出某个角色在某模型下的全部向量。

        第一版是全量载入内存做暴力检索：本地几百上千条记忆完全够用，
        而且实现简单、没有近似误差。规模上去再换 ANN 索引。
        """
        sql = """
            SELECT v.memory_id, v.vector, m.memory_type
            FROM memory_vectors v
            JOIN memories m ON m.memory_id = v.memory_id
            WHERE v.model = ?
              AND m.user_id = ? AND m.character_id = ?
              AND m.status IN ('active', 'superseded')
        """
        params: list[object] = [model, scope.user_id, scope.character_id]

        if memory_types:
            sql += f" AND m.memory_type IN ({_placeholders(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        rows = self._conn.execute(sql, params).fetchall()
        return [(row["memory_id"], MemoryType(row["memory_type"]), row["vector"]) for row in rows]

    def count(self, model: str | None = None) -> int:
        if model:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memory_vectors WHERE model = ?", (model,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memory_vectors").fetchone()
        return int(row["n"])


# ---------------------------------------------------------------------------
# 仓储集合
# ---------------------------------------------------------------------------


@dataclass
class Repositories:
    """把各个仓储打包传递，避免运行时到处 new。"""

    events: EventRepository
    memories: MemoryRepository
    activities: ActivityRepository
    cores: CoreRepository
    links: LinkRepository
    jobs: JobRepository
    vectors: VectorRepository


def build_repositories(conn: sqlite3.Connection) -> Repositories:
    return Repositories(
        events=EventRepository(conn),
        memories=MemoryRepository(conn),
        activities=ActivityRepository(conn),
        cores=CoreRepository(conn),
        links=LinkRepository(conn),
        jobs=JobRepository(conn),
        vectors=VectorRepository(conn),
    )
