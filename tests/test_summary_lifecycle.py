"""撤回状态、原子构建及旧摘要表迁移的回归。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from unittest.mock import patch

from memorydreaming.errors import ScopeNotFound, StorageError
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import MemoryType
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import RuntimeTestCase


class SummaryLifecycleTests(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.items = [self.add_memory(MemoryType.EPISODIC, {"what_happened": text}) for text in (
            "第一次去海边捡贝壳", "第二次去海边看日落", "第三次去海边看鲸鱼")]
        self.runtime.build_summaries(self.scope)
        self.card = self.repos.summaries.list_active(self.scope)[0]

    def test_background_summary_job_cannot_reactivate_a_withdrawn_card(self):
        self.runtime.withdraw_summaries([self.card.summary_id], self.scope)
        with self.runtime.db.transaction():
            job_id = self.repos.jobs.enqueue("summarize", self.scope, {}, self.clock.now())
        self.runtime.run_consolidation(RuleBasedExtractor(), limit=4)
        state = self.runtime.db.connection.execute(
            "SELECT status FROM consolidation_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        self.assertEqual(state, "done")
        self.assertEqual(self.repos.summaries.get(self.card.summary_id).status, "withdrawn")
        self.assertEqual(self.repos.summaries.list_active(self.scope), [])

    def test_only_explicit_reactivation_restores_a_blocked_card_after_restart(self):
        for status in ("withdrawn", "rolled_back"):
            with self.subTest(status=status):
                transition = (self.runtime.withdraw_summaries if status == "withdrawn"
                              else self.runtime.rollback_summaries)
                transition([self.card.summary_id], self.scope)
                with MemoryRuntime(settings=self.runtime.settings, clock=self.clock) as other:
                    self.assertEqual(other.build_summaries(self.scope), 0)
                    self.assertEqual(other.db.repositories.summaries.get(self.card.summary_id).status, status)
                    self.assertEqual(other.build_summaries(self.scope, reactivate=True), 1)
                    self.assertEqual(other.db.repositories.summaries.list_active(self.scope)[0].summary_id,
                                     self.card.summary_id)

    def test_source_update_and_stale_marking_preserve_withdrawal(self):
        self.runtime.withdraw_summaries([self.card.summary_id], self.scope)
        self.repos.memories.update_content(self.items[0].memory_id,
                                          {"what_happened": "第一次去海边寻找小螃蟹"}, self.clock.now())
        self.assertEqual(self.repos.summaries.mark_stale([self.card.summary_id], self.clock.now()), 0)
        self.runtime.build_summaries(self.scope)
        blocked = self.repos.summaries.get(self.card.summary_id)
        self.assertEqual(blocked.status, "withdrawn")
        self.assertEqual(blocked.digest, self.card.digest)
        self.runtime.build_summaries(self.scope, reactivate=True)
        refreshed = self.repos.summaries.get(self.card.summary_id)
        self.assertEqual(refreshed.status, "active")
        self.assertNotEqual(refreshed.source_digest, self.card.source_digest)

    def test_failed_batch_or_malformed_ids_change_no_status(self):
        with self.assertRaises(ScopeNotFound):
            self.runtime.withdraw_summaries([self.card.summary_id, "missing"], self.scope)
        with self.assertRaises(ScopeNotFound):
            self.runtime.withdraw_summaries([self.card.summary_id], replace(self.scope, user_id="other"))
        for ids in ([{}], [1], [""], []):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.runtime.withdraw_summaries(ids, self.scope)
        self.assertEqual(self.repos.summaries.get(self.card.summary_id).status, "active")

    def test_repeated_withdrawal_is_a_noop_without_advancing_generation_or_journal(self):
        ids = [self.card.summary_id, self.card.summary_id]
        self.assertEqual(self.runtime.withdraw_summaries(ids, self.scope)["changed"], 1)
        generation = self.repos.retrieval_cache.generation()
        entries = self.count_rows("journal_entries")
        self.clock.advance(seconds=1)
        self.assertEqual(self.runtime.withdraw_summaries(ids, self.scope)["changed"], 0)
        self.assertEqual(self.repos.retrieval_cache.generation(), generation)
        self.assertEqual(self.count_rows("journal_entries"), entries)

    def test_summary_build_failure_rolls_back_projection_and_content_generation(self):
        generation = self.repos.retrieval_cache.generation()
        original = self.repos.summaries.upsert

        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulate projection failure")

        with patch.object(self.repos.summaries, "upsert", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                self.runtime.build_summaries(self.scope)
        self.assertEqual(self.repos.retrieval_cache.generation(), generation)
        self.assertEqual(self.repos.summaries.get(self.card.summary_id), self.card)

    def legacy_schema_17(self):
        # 真正重建只有 active/stale CHECK 的旧表，保留原 ID/正文/来源。
        conn = self.runtime.db.connection
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='summaries'").fetchone()[0]
        legacy = sql.replace("summaries", "summaries_legacy", 1).replace(
            "'active', 'stale', 'withdrawn', 'rolled_back'", "'active', 'stale'")
        with self.runtime.db.transaction():
            conn.execute(legacy)
            conn.execute("INSERT INTO summaries_legacy SELECT * FROM summaries")
            conn.execute("DROP TABLE summaries")
            conn.execute("ALTER TABLE summaries_legacy RENAME TO summaries")
            self.runtime.db.meta_set("schema_version", "17")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE summaries SET status='withdrawn'")

    def test_schema_17_check_migrates_with_sources_and_can_be_opened_twice(self):
        self.legacy_schema_17()
        for _ in range(2):
            db = Database.open(self.db_path)
            try:
                self.assertEqual(db.meta_get("schema_version"), SCHEMA_VERSION)
                self.assertEqual(db.repositories.summaries.get(self.card.summary_id).source_memory_ids,
                                 self.card.source_memory_ids)
                db.repositories.summaries.mark_withdrawn([self.card.summary_id], self.clock.now())
                self.assertEqual(db.repositories.summaries.get(self.card.summary_id).status, "withdrawn")
            finally:
                db.close()

    def test_failed_schema_17_migration_restores_original_table_and_can_retry(self):
        self.legacy_schema_17()
        with patch.object(Database, "_ensure_retrieval_triggers", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(StorageError):
                Database.open(self.db_path)
        self.assertEqual(self.runtime.db.meta_get("schema_version"), "17")
        self.assertEqual(self.repos.summaries.get(self.card.summary_id), self.card)
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.db.connection.execute("UPDATE summaries SET status='withdrawn'")
        self.assertIsNone(self.runtime.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='summaries_v18'").fetchone())
        db = Database.open(self.db_path)
        db.close()
