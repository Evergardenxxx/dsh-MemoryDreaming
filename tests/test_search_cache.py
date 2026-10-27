from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.models import ForgetSelector, MemoryType
from memorydreaming.retrieval.embedding import HashingEmbedder
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import RuntimeTestCase


class SearchCacheTests(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.scope = self.scope.__class__("u1", "c1", "r1")
        self.item = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"},
        )
        self.session = HostAdapter(
            self.runtime, clock=self.clock, retrieval_cache_ttl_seconds=60,
        ).open_session(SessionBinding("u1", "c1", "r1", "cache-window"))
        self.addCleanup(self.session.close)

    def other_runtime(self, **kwargs):
        runtime = MemoryRuntime(settings=self.runtime.settings, clock=self.clock, **kwargs)
        self.addCleanup(runtime.close)
        return runtime

    def open_window(self, *, runtime=None, binding=None, **kwargs):
        session = HostAdapter(runtime or self.runtime, clock=self.clock, **kwargs).open_session(
            binding or SessionBinding("u1", "c1", "r1", "new-window"))
        self.addCleanup(session.close)
        return session

    @staticmethod
    def args(query="喜欢海边故事"):
        return {
            "query": query, "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 20, "max_tokens": 4096,
        }

    def test_repeated_search_reuses_references_without_running_pipeline_again(self):
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            first = self.session.call_tool("memory_search", self.args())
            second = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(first["data"]["evidence"][0]["ref"], second["data"]["evidence"][0]["ref"])

    def test_content_revision_invalidates_cache_even_without_scope_epoch_bump(self):
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            self.session.call_tool("memory_search", self.args())
            self.repos.memories.update_content(
                self.item.memory_id,
                {"subject": "用户", "predicate": "喜欢", "obj": "海边新故事"},
                self.clock.now(),
            )
            result = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 2)
        self.assertIn("海边新故事", result["data"]["evidence"][0]["text"])

    def test_forgetting_bumps_epoch_and_never_returns_cached_evidence(self):
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            self.session.call_tool("memory_search", self.args())
            self.runtime.forget(ForgetSelector(memory_ids=[self.item.memory_id]))
            result = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 2)
        self.assertEqual(result["status"], "not_found")

    def test_second_window_reuses_shared_candidate_cache_without_running_pipeline(self):
        second = HostAdapter(
            self.runtime, clock=self.clock, retrieval_cache_ttl_seconds=60,
        ).open_session(SessionBinding("u1", "c1", "r1", "other-window"))
        try:
            with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
                first = self.session.call_tool("memory_search", self.args())
                replay = second.call_tool("memory_search", self.args())
            self.assertEqual(retrieve.call_count, 1)
            self.assertEqual(first["data"]["evidence"][0]["text"], replay["data"]["evidence"][0]["text"])
            self.assertEqual(second.cache_stats()["shared_hits"], 1)
        finally:
            second.close()

    def test_temporal_search_does_not_reuse_current_projection_cache(self):
        args = self.args()
        args["knowledge_at"] = "2026-10-19T12:00:00+00:00"
        args["fact_at"] = None
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            self.session.call_tool("memory_search", args)
            self.session.call_tool("memory_search", args)
        self.assertEqual(retrieve.call_count, 2)

    def test_negative_cache_is_invalidated_by_new_memory_from_another_connection(self):
        self.runtime.forget(ForgetSelector(memory_ids=[self.item.memory_id]))
        other = self.other_runtime()
        second = self.open_window(runtime=other)
        self.assertEqual(self.session.call_tool("memory_search", self.args())["status"], "not_found")
        self.assertEqual(second.call_tool("memory_search", self.args())["status"], "not_found")
        self.assertEqual(second.cache_stats()["shared_hits"], 1)

        item = replace(self.item, memory_id="mem_new_external", body={
            "subject": "用户", "predicate": "喜欢", "obj": "海边鲸鱼故事"})
        with other.db.transaction():
            other.db.repositories.memories.insert(item)

        for session in (self.session, second):
            result = session.call_tool("memory_search", self.args())
            self.assertEqual(result["status"], "ok", result)
            self.assertIn("鲸鱼", result["data"]["evidence"][0]["text"])
            self.assertEqual(session.cache_stats()["stale"], 1)

    def test_positive_cache_discovers_additional_candidates(self):
        self.session.call_tool("memory_search", self.args())
        self.add_memory(MemoryType.SEMANTIC, {
            "subject": "用户", "predicate": "喜欢", "obj": "海边鲸鱼故事"})
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            result = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(len(result["data"]["evidence"]), 2)

    def test_cache_keys_do_not_store_queries_and_scopes_do_not_overwrite_each_other(self):
        query = "喜欢海边故事：敏感查询测试标记"
        self.session.call_tool("memory_search", self.args(query))
        other_binding = replace(self.session.binding, relationship_id="r2")
        self.open_window(binding=other_binding).call_tool("memory_search", self.args(query))
        replay = self.open_window()
        replay.call_tool("memory_search", self.args(query))
        self.assertEqual(replay.cache_stats()["shared_hits"], 1)
        rows = self.runtime.db.connection.execute("SELECT * FROM retrieval_cache").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertRegex(row["cache_key"], r"^[a-f0-9]{64}$")
            self.assertNotIn("敏感查询测试标记", str(tuple(row)))

    def test_selected_activity_and_resolved_activity_refs_partition_cache(self):
        one = self.runtime.register_activity(self.scope, "活动甲")
        two = self.runtime.register_activity(self.scope, "活动乙")
        for activity, label in ((one, "甲"), (two, "乙")):
            item = replace(self.item, memory_id="mem_activity_" + label,
                           scope=replace(self.scope, activity_id=activity.activity_id),
                           body={"subject": "用户", "predicate": "喜欢", "obj": "海边" + label})
            with self.runtime.db.transaction():
                self.repos.memories.insert(item)
        binding = replace(self.session.binding, allowed_activity_ids=(one.activity_id, two.activity_id),
                          selected_activity_id=one.activity_id)
        first = self.open_window(binding=binding)
        second = self.open_window(binding=replace(binding, selected_activity_id=two.activity_id))
        first_result = first.call_tool("memory_search", self.args())
        second_result = second.call_tool("memory_search", self.args())
        self.assertIn("海边甲", str(first_result))
        self.assertNotIn("海边乙", str(first_result))
        self.assertIn("海边乙", str(second_result))
        self.assertNotIn("海边甲", str(second_result))
        self.assertEqual(second.cache_stats()["shared_hits"], 0)
        # 两个窗口的随机 activity_ref 不同，但指向同一活动时可以共享候选。
        third = self.open_window(binding=binding)
        for session in (first, third):
            args = self.args()
            args["activity_ref"] = next(x["ref"] for x in session.opened["data"]["activities"]
                                        if x["label"] == "活动乙")
            result = session.call_tool("memory_search", args)
            self.assertIn("海边乙", str(result))
        self.assertEqual(third.cache_stats()["shared_hits"], 1)

    def test_allowed_activity_sources_are_not_reused_by_a_narrower_binding(self):
        activity = self.runtime.register_activity(self.scope, "来源活动")
        event_scope = replace(self.scope, activity_id=activity.activity_id)
        event = self.runtime.observe(self.make_turn("我喜欢海边独有故事。"), event_scope, "source").event
        self.add_memory(MemoryType.SEMANTIC, {
            "subject": "用户", "predicate": "喜欢", "obj": "海边独有故事"}, source_event_ids=[event.event_id])
        broad = self.open_window(binding=replace(self.session.binding,
                                                 allowed_activity_ids=(activity.activity_id,)))
        self.assertIn("独有故事", str(broad.call_tool("memory_search", self.args())))
        narrow = self.open_window()
        self.assertNotIn("独有故事", str(narrow.call_tool("memory_search", self.args())))
        self.assertEqual(narrow.cache_stats()["shared_hits"], 0)

    def test_retrieval_settings_and_embedding_configuration_partition_cache(self):
        self.session.call_tool("memory_search", self.args())
        different_settings = MemoryRuntime(settings=replace(self.runtime.settings, rrf_k=10), clock=self.clock)
        self.addCleanup(different_settings.close)
        different_embedding = self.other_runtime(embedder=HashingEmbedder(ngram_sizes=(3,)))
        for runtime in (different_settings, different_embedding):
            session = self.open_window(runtime=runtime)
            with patch.object(runtime, "retrieve", wraps=runtime.retrieve) as retrieve:
                session.call_tool("memory_search", self.args())
            self.assertEqual(retrieve.call_count, 1)
            self.assertEqual(session.cache_stats()["shared_hits"], 0)
        self.assertEqual(self.count_rows("retrieval_cache"), 3)

    def test_shared_hit_cannot_extend_the_original_expiration(self):
        self.session.call_tool("memory_search", self.args())
        self.clock.advance(seconds=59)
        second = self.open_window(retrieval_cache_ttl_seconds=60)
        second.call_tool("memory_search", self.args())
        self.assertEqual(second.cache_stats()["shared_hits"], 1)
        self.clock.advance(seconds=2)
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            second.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 1)

    def test_shared_hit_obeys_local_capacity_and_persistent_cache_is_bounded(self):
        producer = self.open_window(shared_retrieval_cache_capacity=3)
        for suffix in ("甲", "乙", "丙", "丁"):
            producer.call_tool("memory_search", self.args("海边故事" + suffix))
        self.assertEqual(self.count_rows("retrieval_cache"), 3)
        consumer = self.open_window(retrieval_cache_capacity=1)
        for suffix in ("乙", "丙", "丁"):
            consumer.call_tool("memory_search", self.args("海边故事" + suffix))
        self.assertEqual(consumer.cache_stats()["shared_hits"], 3)
        self.assertEqual(len(consumer._search_cache), 1)
        self.clock.advance(seconds=61)
        consumer.call_tool("memory_search", self.args())
        self.assertEqual(self.count_rows("retrieval_cache"), 1)

    def test_generation_is_transactional_and_cache_writes_do_not_invalidate(self):
        generation = self.repos.retrieval_cache.generation()
        with self.assertRaises(RuntimeError):
            with self.runtime.db.transaction():
                self.repos.memories.update_content(self.item.memory_id, {"obj": "临时变化"}, self.clock.now())
                self.assertGreater(self.repos.retrieval_cache.generation(), generation)
                raise RuntimeError("rollback")
        self.assertEqual(self.repos.retrieval_cache.generation(), generation)
        self.session.call_tool("memory_search", self.args())
        self.assertEqual(self.repos.retrieval_cache.generation(), generation)

    def test_an_independent_process_uses_shared_candidates_and_issues_new_refs(self):
        first = self.session.call_tool("memory_search", self.args())
        script = """
import json, sys
from unittest.mock import patch
from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.clock import FixedClock
from memorydreaming.runtime import MemoryRuntime
from tests.helpers import NOW
with MemoryRuntime(db_path=sys.argv[1], clock=FixedClock(NOW)) as runtime:
    session = HostAdapter(runtime).open_session(SessionBinding('u1', 'c1', 'r1', 'child'))
    with patch.object(runtime, 'retrieve', side_effect=AssertionError('cache missed')):
        result = session.call_tool('memory_search', json.loads(sys.argv[2]))
    old_ref = session.call_tool('memory_read', {'references': [sys.argv[3]], 'max_tokens': 4096})
    print(json.dumps({'result': result, 'metrics': session.cache_stats(), 'old_ref': old_ref}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.db_path), json.dumps(self.args()),
             first["data"]["evidence"][0]["ref"]],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replay = json.loads(completed.stdout)
        self.assertEqual(replay["metrics"]["shared_hits"], 1)
        self.assertEqual(replay["result"]["data"]["evidence"][0]["text"],
                         first["data"]["evidence"][0]["text"])
        self.assertNotEqual(replay["result"]["data"]["evidence"][0]["ref"],
                            first["data"]["evidence"][0]["ref"])
        self.assertEqual(replay["old_ref"]["error"]["code"], "NOT_FOUND_OR_FORBIDDEN")

    def test_v18_migration_discards_plaintext_cache_and_preserves_memories(self):
        legacy = self._tmpdir.name + "/legacy.db"
        db = Database.open(legacy)
        try:
            db.repositories.memories.insert(self.item)
            db.connection.execute("DROP TABLE retrieval_cache")
            db.connection.execute("""CREATE TABLE retrieval_cache (
                cache_key TEXT PRIMARY KEY, scope_key TEXT NOT NULL, current_event_id TEXT,
                mutation_epoch INTEGER NOT NULL DEFAULT 0,
                memory_ids_json TEXT NOT NULL DEFAULT '[]', summary_ids_json TEXT NOT NULL DEFAULT '[]',
                memory_revisions_json TEXT NOT NULL DEFAULT '{}', summary_revisions_json TEXT NOT NULL DEFAULT '{}',
                retrieval_complete INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
            db.connection.execute("INSERT INTO retrieval_cache (cache_key,scope_key,created_at,expires_at) "
                                  "VALUES ('legacy private query','scope','2026-10-20','2099-01-01')")
            db.meta_set("schema_version", "18")
        finally:
            db.close()
        for _ in range(2):
            reopened = Database.open(legacy)
            try:
                self.assertEqual(reopened.meta_get("schema_version"), SCHEMA_VERSION)
                self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM retrieval_cache").fetchone()[0], 0)
                self.assertIsNotNone(reopened.repositories.memories.get(self.item.memory_id))
            finally:
                reopened.close()

