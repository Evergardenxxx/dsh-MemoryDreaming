"""作用域、幂等与升级旧库的回归场景。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from memorydreaming.errors import StorageError
from memorydreaming.models import (
    CoreKind, EventRecord, MemoryItem, MemoryStatus, MemoryType, Scope, Summary, new_id,
)
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import NOW, RuntimeTestCase


class ScopedStorageTest(RuntimeTestCase):
    def _event(self, scope: Scope, content: str = "消息", key: str = "turn-1") -> EventRecord:
        return EventRecord(
            new_id("evt"), scope, "conversation", "user", content, NOW,
            recorded_at=NOW, idempotency_key=key,
        )

    def _memory(self, scope: Scope, *, importance: float = 0.5) -> MemoryItem:
        item = MemoryItem(
            new_id("mem"), MemoryType.SEMANTIC, scope,
            {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
            occurred_start=NOW, recorded_at=NOW, importance=importance,
        )
        self.repos.memories.insert(item)
        return item

    def test_idempotency_key_is_private_to_exact_scope(self) -> None:
        scopes = [
            self.scope,
            Scope(user_id="u2", character_id="c1"),
            Scope(user_id="u1", character_id="c2"),
            Scope(user_id="u1", character_id="c1", domain="fiction"),
            Scope(user_id="u1", character_id="c1", relationship_id="r1"),
            Scope(user_id="u1", character_id="c1", activity_id="a1"),
        ]
        for i, scope in enumerate(scopes):
            event = self._event(scope, content=f"消息 {i}")
            stored, duplicate = self.repos.events.insert(event)
            self.assertFalse(duplicate)
            self.assertEqual(stored.scope, scope)
            again, duplicate = self.repos.events.insert(event)
            self.assertTrue(duplicate)
            self.assertEqual(again.event_id, event.event_id)
        self.assertEqual(self.count_rows("events"), len(scopes))

    def test_same_key_with_different_message_is_rejected(self) -> None:
        self.repos.events.insert(self._event(self.scope, "第一条"))
        with self.assertRaises(ValueError):
            self.repos.events.insert(self._event(self.scope, "第二条"))

    def test_all_memory_reads_filter_before_candidate_limit(self) -> None:
        selected = Scope(user_id="u1", character_id="c1", relationship_id="r1", activity_id="a1")
        target = self._memory(selected, importance=0.1)
        for scope in [
            Scope(user_id="u2", character_id="c1", relationship_id="r1", activity_id="a1"),
            Scope(user_id="u1", character_id="c2", relationship_id="r1", activity_id="a1"),
            Scope(user_id="u1", character_id="c1", relationship_id="r2", activity_id="a1"),
            Scope(user_id="u1", character_id="c1", relationship_id="r1", activity_id="a2"),
            Scope(user_id="u1", character_id="c1", relationship_id="r1", activity_id="a1", domain="fiction"),
        ]:
            self._memory(scope, importance=1.0)
        reads = [
            self.repos.memories.list_recent(selected, NOW - timedelta(days=1), None, 1),
            self.repos.memories.list_between(selected, NOW - timedelta(days=1), NOW + timedelta(days=1), None, 1),
            self.repos.memories.list_important(selected, 1),
            self.repos.memories.list_by_scope(selected, limit=1),
        ]
        for items in reads:
            self.assertEqual([item.memory_id for item in items], [target.memory_id])
        self.assertEqual(self.repos.memories.count_active(selected), 1)
        self.assertEqual(self.repos.memories.list_ids_for_scope(selected), [target.memory_id])
        # 未指定活动/关系时可以查看该领域内的其他活动，虚构领域仍然隔离。
        self.assertEqual(self.repos.memories.count_active(self.scope), 3)

    def test_vectors_and_events_honor_full_query_scope(self) -> None:
        target_scope = Scope(user_id="u1", character_id="c1", activity_id="a1", domain="fiction")
        other_scope = Scope(user_id="u1", character_id="c1", activity_id="a2", domain="fiction")
        for scope in [self.scope, target_scope, other_scope]:
            self.runtime.vectors.index_memory(self._memory(scope), NOW)
            self.repos.events.insert(self._event(scope))
        vectors = self.repos.vectors.load_scope(target_scope, self.runtime.vectors.model_name)
        self.assertEqual(len(vectors), 1)
        events = self.repos.events.list_between(target_scope, NOW - timedelta(days=1), NOW + timedelta(days=1))
        self.assertEqual([event.scope for event in events], [target_scope])

    def test_summary_scope_roundtrip_and_topic_uniqueness(self) -> None:
        scopes = [
            self.scope,
            Scope(user_id="u1", character_id="c1", relationship_id="r1", activity_id="a1", domain="fiction"),
        ]
        for i, scope in enumerate(scopes):
            card = Summary(new_id("sum"), scope, "海边", f"独立摘要 {i}", created_at=NOW, updated_at=NOW)
            self.repos.summaries.upsert(card, NOW)
            loaded = self.repos.summaries.get_by_topic(scope, "海边")
            self.assertEqual(loaded.scope, scope)
            self.assertEqual(loaded.digest, f"独立摘要 {i}")
            self.assertEqual(self.repos.summaries.list_active(scope, limit=1)[0].summary_id, card.summary_id)
        self.assertEqual(self.count_rows("summaries"), 2)

    def test_source_changes_immediately_hide_summary(self) -> None:
        operations = [
            lambda memory_id: self.repos.memories.update_status(memory_id, MemoryStatus.RETRACTED, NOW),
            lambda memory_id: self.repos.memories.close_validity(memory_id, NOW, NOW),
            lambda memory_id: self.repos.memories.delete(memory_id, NOW),
            lambda memory_id: self.repos.memories.update_content(memory_id, {"obj": "山间"}, NOW),
        ]
        for i, operation in enumerate(operations):
            with self.subTest(operation=i):
                memory = self._memory(self.scope)
                summary = Summary(
                    new_id("sum"), self.scope, f"主题{i}", "已变化的内容",
                    source_memory_ids=[memory.memory_id], created_at=NOW, updated_at=NOW,
                )
                self.repos.summaries.upsert(summary, NOW)
                with self.runtime.db.transaction():
                    operation(memory.memory_id)
                self.assertEqual(self.repos.summaries.get(summary.summary_id).status, "stale")
                self.assertNotIn(summary.summary_id, [card.summary_id for card in self.repos.summaries.list_active(self.scope)])

    def test_job_scope_survives_enqueue_claim_and_coalescing(self) -> None:
        scope = Scope(user_id="u1", character_id="c1", relationship_id="r1", activity_id="a1", domain="fiction")
        job_id = self.repos.jobs.enqueue("dedupe", scope, {}, NOW)
        self.assertTrue(self.repos.jobs.has_pending("dedupe", scope))
        self.assertFalse(self.repos.jobs.has_pending("dedupe", self.scope))
        jobs = self.repos.jobs.claim(1, NOW)
        self.assertEqual(jobs[0].job_id, job_id)
        self.assertEqual(jobs[0].scope, scope)

    def test_rejected_core_is_not_restored_and_does_not_reuse_version(self) -> None:
        self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"name": "accepted"}, "initial")
        second = self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"name": "rejected"}, "candidate")
        self.runtime.db.connection.execute("UPDATE core_versions SET status = 'rejected' WHERE core_version = ?", (second.core_version,))
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).get("name"), "accepted")
        third = self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"name": "next"}, "accepted update")
        self.assertEqual(third.core_version, 3)


# 上一版真实表结构的最小夹具，包含迁移需要保留的所有事件列。
LEGACY_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '3');
CREATE TABLE events (
 event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, character_id TEXT NOT NULL,
 relationship_id TEXT, conversation_id TEXT NOT NULL, activity_id TEXT,
 domain TEXT NOT NULL DEFAULT 'real', speaker TEXT NOT NULL, content TEXT NOT NULL,
 occurred_at TEXT NOT NULL, occurred_precision TEXT NOT NULL DEFAULT 'exact',
 source_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', recorded_at TEXT NOT NULL,
 turn_index INTEGER, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE summaries (
 summary_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, character_id TEXT NOT NULL,
 activity_id TEXT, topic TEXT NOT NULL, digest TEXT NOT NULL,
 source_memory_ids_json TEXT NOT NULL DEFAULT '[]', source_event_ids_json TEXT NOT NULL DEFAULT '[]',
 span_start TEXT, span_end TEXT, importance REAL NOT NULL DEFAULT 0.5,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(user_id, character_id, topic)
);
CREATE TABLE consolidation_jobs (
 job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, user_id TEXT NOT NULL, character_id TEXT NOT NULL,
 activity_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class SchemaMigrationTest(unittest.TestCase):
    def test_legacy_data_preserved_scopes_restored_and_reopen_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(LEGACY_SCHEMA)
            stamp = NOW.isoformat()
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("evt-old", "u1", "c1", "r1", "conv", "a1", "fiction", "user", "旧消息",
                 stamp, "exact", "Asia/Shanghai", stamp, None, "turn-1", stamp),
            )
            conn.execute(
                "INSERT INTO summaries (summary_id,user_id,character_id,topic,digest,created_at,updated_at) "
                "VALUES ('sum-old','u1','c1','海边','旧摘要',?,?)", (stamp, stamp),
            )
            conn.execute(
                "INSERT INTO consolidation_jobs (job_id,job_type,user_id,character_id,payload_json,created_at,updated_at) "
                "VALUES ('job-old','extract','u1','c1','{\"event_id\":\"evt-old\"}',?,?)", (stamp, stamp),
            )
            conn.commit()
            conn.close()
            db = Database.open(path)
            try:
                self.assertEqual(db.meta_get("schema_version"), SCHEMA_VERSION)
                old = db.repositories.events.get("evt-old")
                self.assertEqual(old.content, "旧消息")
                self.assertEqual(db.repositories.summaries.get("sum-old").status, "stale")
                job = db.repositories.jobs.claim(1, NOW)[0]
                self.assertEqual(job.scope, old.scope)
                other = EventRecord("evt-new", Scope(user_id="u2", character_id="c1"), "conv", "user", "新消息", NOW,
                                    recorded_at=NOW, idempotency_key="turn-1")
                self.assertFalse(db.repositories.events.insert(other)[1])
            finally:
                db.close()
            again = Database.open(path)
            try:
                self.assertEqual(again.repositories.events.get("evt-old").content, "旧消息")
                self.assertEqual(again.repositories.events.get("evt-new").content, "新消息")
                self.assertEqual(again.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
            finally:
                again.close()

    def test_future_schema_is_rejected_without_overwriting_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "future.db"
            conn = sqlite3.connect(path)
            conn.executescript("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                               "INSERT INTO schema_meta VALUES ('schema_version','999');")
            conn.close()
            with self.assertRaises(StorageError):
                Database.open(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "999")
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='memories'").fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
