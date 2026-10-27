"""耗时模型计算不占写锁；快照过期、遗忘及租约丢失不得发布旧结果。"""

from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

from memorydreaming.errors import SourceRevisionChanged, StorageError
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.models import ForgetSelector, MemoryStatus, MemoryType, Scope
from memorydreaming.retrieval.vector import VectorIndex
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database
from tests.helpers import NOW, RuntimeTestCase


class ObservedEmbedder:
    name = "observed"
    dim = 2

    def __init__(self):
        self.on_documents = None
        self.on_query = None
        self.document_calls = 0
        self.query_calls = 0

    def embed(self, text):
        return [1.0, 0.0]

    def embed_documents(self, texts):
        self.document_calls += 1
        if self.on_documents:
            self.on_documents()
        return [self.embed(text) for text in texts]

    def embed_query(self, text):
        self.query_calls += 1
        if self.on_query:
            self.on_query()
        return self.embed(text)


class ReadSnapshotTest(RuntimeTestCase):
    def test_snapshot_allows_other_writer_but_sees_consistent_old_version(self):
        memory = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边原记录"})
        other = Database.open(self.db_path, initialize=False)
        self.addCleanup(other.close)
        other.connection.execute("PRAGMA busy_timeout=10")
        with self.runtime.db.read_snapshot():
            self.assertEqual(self.runtime.db.connection.execute("PRAGMA query_only").fetchone()[0], 1)
            generation = self.repos.retrieval_cache.generation()
            with other.transaction():
                other.repositories.memories.update_content(memory.memory_id, {"what_happened": "高山新记录"}, NOW)
            self.assertEqual(self.repos.memories.get(memory.memory_id).body["what_happened"], "海边原记录")
            self.assertEqual(self.repos.retrieval_cache.generation(), generation)
        self.assertGreater(self.repos.retrieval_cache.generation(), generation)
        self.assertEqual(self.repos.memories.get(memory.memory_id).body["what_happened"], "高山新记录")

    def test_snapshot_blocks_accidental_writes_and_restores_connection_after_error(self):
        with self.assertRaises(sqlite3.OperationalError):
            with self.runtime.db.read_snapshot():
                self.runtime.db.meta_set("accidental_write", "forbidden")
        self.assertFalse(self.runtime.db.connection.in_transaction)
        self.assertEqual(self.runtime.db.connection.execute("PRAGMA query_only").fetchone()[0], 0)
        with self.runtime.db.transaction():
            self.runtime.db.meta_set("after_error", "works")
        self.assertEqual(self.runtime.db.meta_get("after_error"), "works")

    def test_snapshot_cannot_silently_inherit_an_outer_write_transaction(self):
        with self.runtime.db.transaction():
            with self.assertRaises(StorageError), self.runtime.db.read_snapshot():
                pass

    def test_keyboard_interrupt_rolls_back_and_leaves_connection_usable(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.runtime.db.transaction():
                self.runtime.db.meta_set("interrupted", "must-rollback")
                raise KeyboardInterrupt
        self.assertIsNone(self.runtime.db.meta_get("interrupted"))
        self.assertFalse(self.runtime.db.connection.in_transaction)


class IncrementalComputationTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.embedder = ObservedEmbedder()
        self.runtime2 = MemoryRuntime(self.runtime.settings, clock=self.clock, embedder=self.embedder)
        self.addCleanup(self.runtime2.close)
        self.other = Database.open(self.db_path, initialize=False)
        self.addCleanup(self.other.close)
        self.other.connection.execute("PRAGMA busy_timeout=10")

    def test_extraction_encodes_outside_transaction_then_commits_vectors_atomically(self):
        self.runtime2.observe(self.make_turn("我喜欢海边故事。"), self.scope, "source")

        def check_unlocked():
            self.assertFalse(self.runtime2.db.connection.in_transaction)
            with self.other.transaction():
                self.other.meta_set("concurrent-operation", "succeeded")
            # 计算期间另一连接看不到候选半成品。
            self.assertEqual(self.other.connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)

        self.embedder.on_documents = check_unlocked
        report = self.runtime2.run_consolidation(RuleBasedExtractor(), limit=1)
        self.assertEqual((report.created, report.failed, report.stale_results), (1, 0, 0))
        self.assertEqual(self.embedder.document_calls, 1)
        self.assertEqual(self.count_rows("memory_vectors"), 1)

    def test_source_change_during_extraction_encoding_discards_plan_without_consuming_retry(self):
        self.runtime2.observe(self.make_turn("我喜欢海边故事。"), self.scope, "source")
        old = self.add_memory(MemoryType.EPISODIC, {"what_happened": "旧日散步"})

        def concurrent_revision():
            self.assertFalse(self.runtime2.db.connection.in_transaction)
            with self.other.transaction():
                self.other.repositories.memories.update_content(old.memory_id, {"what_happened": "修订后的旧日散步"}, NOW)

        self.embedder.on_documents = concurrent_revision
        report = self.runtime2.run_consolidation(RuleBasedExtractor(), limit=1)
        self.assertEqual((report.created, report.failed, report.stale_results), (0, 0, 1))
        job = self.runtime2.db.connection.execute("SELECT * FROM consolidation_jobs").fetchone()
        self.assertEqual((job["status"], job["attempts"]), ("pending", 0))
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.embedder.on_documents = None
        retried = self.runtime2.run_consolidation(RuleBasedExtractor(), limit=1)
        self.assertEqual((retried.created, retried.failed), (1, 0))

    def test_lease_expiry_during_encoding_writes_no_candidate_or_followup(self):
        self.runtime2.observe(self.make_turn("我喜欢海边故事。"), self.scope, "source")
        self.embedder.on_documents = lambda: self.clock.advance(seconds=121)
        report = self.runtime2.run_consolidation(RuleBasedExtractor(), limit=1)
        self.assertEqual((report.created, report.lease_lost), (0, 1))
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)

    def test_forgetting_source_event_during_encoding_cannot_publish_candidate(self):
        observed = self.runtime2.observe(self.make_turn("我喜欢海边故事。"), self.scope, "source")
        def forget_source():
            self.runtime.forget_event(observed.event.event_id, scope=self.scope)
        self.embedder.on_documents = forget_source
        report = self.runtime2.run_consolidation(RuleBasedExtractor(), limit=1)
        self.assertEqual(report.created, 0)
        self.assertEqual(report.lease_lost + report.stale_results, 1)
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)

    def dedupe_pair(self):
        one = self.add_memory(MemoryType.EPISODIC, {"what_happened": "周末海边散步拍照", "participants": ["用户"]},
                              importance=0.9)
        two = self.add_memory(MemoryType.EPISODIC, {"what_happened": "周末海边散步拍照！", "participants": ["用户"]},
                              importance=0.5)
        self.repos.jobs.enqueue("dedupe", self.scope, {}, NOW)
        return one, two

    def test_dedupe_prepares_merged_body_before_commit(self):
        one, two = self.dedupe_pair()
        def check_unlocked():
            self.assertFalse(self.runtime2.db.connection.in_transaction)
            with self.other.transaction():
                self.other.meta_set("dedupe-unlocked", "yes")
        self.embedder.on_documents = check_unlocked
        report = self.runtime2.run_consolidation(limit=1)
        self.assertEqual((report.merged, report.failed), (1, 0))
        self.assertEqual(self.repos.memories.get(two.memory_id).status, MemoryStatus.SUPERSEDED)
        self.assertEqual(self.runtime2.vectors.count(), 1)
        self.assertEqual(self.embedder.document_calls, 1)
        self.assertEqual(self.runtime2.vectors.search(self.scope, "海边")[0].memory_id, one.memory_id)

    def test_dedupe_race_does_not_merge_revised_record_using_old_cluster(self):
        one, two = self.dedupe_pair()
        def change_duplicate():
            with self.other.transaction():
                self.other.repositories.memories.update_content(two.memory_id, {"what_happened": "完全无关的医院检查"}, NOW)
        self.embedder.on_documents = change_duplicate
        report = self.runtime2.run_consolidation(limit=1)
        self.assertEqual((report.merged, report.stale_results), (0, 1))
        self.assertEqual(self.repos.memories.get(two.memory_id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.count_rows("memory_links"), 0)
        self.embedder.on_documents = None
        self.assertEqual(self.runtime2.run_consolidation(limit=1).merged, 0)

    def test_prepared_vectors_reject_different_text_or_model(self):
        memory = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边散步"})
        prepared = self.runtime2.vectors.prepare_memories([memory])
        changed = replace(memory, body={"what_happened": "山里露营"})
        with self.assertRaises(SourceRevisionChanged):
            self.runtime2.vectors.index_memory(changed, NOW, prepared=prepared)
        with self.assertRaises(SourceRevisionChanged):
            self.runtime.vectors.index_memory(memory, NOW, prepared=prepared)
        self.assertEqual(self.count_rows("memory_vectors"), 0)


class HostOptimisticSearchTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.runtime.observe(self.make_turn("我喜欢海边故事。"), self.scope, "seed")
        self.runtime.run_consolidation(RuleBasedExtractor(), limit=1)
        self.memory = self.repos.memories.list_by_scope(self.scope)[0]
        self.embedder = ObservedEmbedder()
        self.runtime2 = MemoryRuntime(self.runtime.settings, clock=self.clock, embedder=self.embedder)
        self.addCleanup(self.runtime2.close)
        self.runtime2.rebuild_vectors(self.scope)
        self.session = HostAdapter(self.runtime2, clock=self.clock).open_session(SessionBinding("u1", "c1", "r1", "window"))
        self.addCleanup(self.session.close)

    def search(self, **kwargs):
        return self.session.call_tool("memory_search", {"query": "海边故事", "activity_ref": None,
            "time_range": None, "detail": "evidence", "max_items": 4, "max_tokens": 2048}, **kwargs)

    def test_host_embedding_allows_other_writer_and_discards_forgotten_snapshot(self):
        def forget_once():
            self.assertEqual(self.runtime2.db.connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.embedder.on_query = None
            self.runtime.db.connection.execute("PRAGMA busy_timeout=10")
            self.runtime.forget(ForgetSelector(memory_ids=[self.memory.memory_id]))
        self.embedder.on_query = forget_once
        response = self.search()
        self.assertEqual(response["status"], "not_found", response)
        self.assertEqual(response["data"]["evidence"], [])
        self.assertNotIn("我喜欢海边故事", json.dumps(response, ensure_ascii=False))
        self.assertEqual(self.embedder.query_calls, 2)
        self.assertEqual(self.session.cache_stats()["revision_retries"], 1)

    def test_prepare_turn_restores_core_updated_during_query(self):
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "旧名"}, "设定")
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "旧称呼"}, "设定")
        def change_core_once():
            self.embedder.on_query = None
            self.runtime.apply_core_update(self.scope, "identity", {"名字": "新名"}, "更新")
            self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "新称呼"}, "更新")
        self.embedder.on_query = change_core_once
        prepared = self.session.prepare_turn("new-user", "海边故事", max_tokens=4096)
        payload = prepared.messages[1]["content"]
        self.assertIn("新名", payload)
        self.assertIn("新称呼", payload)
        self.assertNotIn("旧名", payload)
        self.assertNotIn("旧称呼", payload)
        self.assertEqual(self.session.cache_stats()["revision_retries"], 1)

    def test_continuous_revisions_fail_boundedly_without_caching_old_results(self):
        def change_each_time():
            with self.runtime.db.transaction():
                self.repos.memories.mark_used([self.memory.memory_id], NOW)
        self.embedder.on_query = change_each_time
        response = self.search()
        self.assertEqual(response["status"], "error", response)
        self.assertEqual(response["error"]["code"], "UNAVAILABLE")
        self.assertTrue(response["error"]["retryable"])
        self.assertIsNone(response["data"])
        self.assertEqual(self.embedder.query_calls, 2)
        self.assertEqual(self.count_rows("retrieval_cache"), 0)

    def test_deadline_after_model_calculation_cannot_cache_or_return_evidence(self):
        self.embedder.on_query = lambda: self.clock.advance(seconds=2)
        response = self.search(deadline=NOW + timedelta(seconds=1))
        self.assertEqual(response["status"], "error", response)
        self.assertEqual(response["error"]["code"], "TIMEOUT")
        self.assertEqual(self.count_rows("retrieval_cache"), 0)

    def test_prefetch_uses_same_snapshot_validation(self):
        def revise_once():
            self.embedder.on_query = None
            with self.runtime.db.transaction():
                self.repos.memories.mark_used([self.memory.memory_id], NOW)
        self.embedder.on_query = revise_once
        result = self.session.prefetch("海边故事", detail="evidence", max_items=4, max_tokens=2048)
        self.assertTrue(result["cached"])
        response = self.search()
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(self.embedder.query_calls, 2)
