"""持久无正文指标、状态告警、有界预取和跨库供应商配额。"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memorydreaming import DeepSeekSchedulingPolicy, HostAdapter, MemoryRuntime, Scope, SessionBinding
from memorydreaming.llm import AdapterError
from memorydreaming.llm_extractor import DeepSeekClient, LLMExtractionError
from memorydreaming.models import ForgetSelector, MemoryType
from memorydreaming.operations import record_metric
from memorydreaming.scheduling import DeepSeekRequestScheduler, ExtractionDeferred
from memorydreaming.storage.database import Database
from tests.helpers import RuntimeTestCase


class OperationsTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.binding = SessionBinding("u1", "c1", "r1", "operations")

    def test_cache_counters_and_latency_persist_without_query_text(self):
        session = HostAdapter(self.runtime).open_session(self.binding)
        session.prefetch("保密查询正文")
        session.prefetch("保密查询正文")
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as other:
            trend = other.operational_trend(self.scope)
        names = {row["metric"] for row in trend}
        self.assertTrue({"misses", "local_hits", "search", "prefetches"} <= names)
        self.assertNotIn("保密查询正文", str(trend))
        self.assertEqual(self.runtime.operational_trend(replace(self.scope, relationship_id="other")), [])

    def test_alerts_deduplicate_and_resolve_after_failure_is_fixed(self):
        with self.runtime.db.transaction():
            key = self.repos.jobs.enqueue("dedupe", self.scope, {}, self.clock.now())
            self.runtime.db.connection.execute("UPDATE consolidation_jobs SET status='failed' WHERE job_id=?", (key,))
        first = self.runtime.check_alerts(self.scope)
        again = self.runtime.check_alerts(self.scope)
        self.assertEqual(first, again)
        self.assertEqual(next(a for a in first if a["code"] == "FAILED_JOBS")["state"], "active")
        self.runtime.db.connection.execute("UPDATE consolidation_jobs SET status='done' WHERE job_id=?", (key,))
        resolved = self.runtime.check_alerts(self.scope)
        self.assertEqual(next(a for a in resolved if a["code"] == "FAILED_JOBS")["state"], "resolved")

    def test_background_prefetch_warms_shared_cache_with_a_separate_connection(self):
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"})
        adapter = HostAdapter(self.runtime, background_prefetch=True)
        session = adapter.open_session(self.binding)
        self.addCleanup(adapter.close)
        self.assertTrue(session.schedule_prefetch("海边故事")["scheduled"])
        self.assertTrue(adapter.prefetch_scheduler.drain(5))
        session.prefetch("海边故事")
        self.assertEqual(session.cache_stats()["shared_hits"], 1)
        self.assertNotIn("海边故事", str(self.runtime.operational_trend(self.scope)))
        self.assertIsNone(adapter.prefetch_scheduler.last_error)

    def test_closed_scheduler_and_memory_database_do_not_start_background_work(self):
        adapter = HostAdapter(self.runtime, background_prefetch=True)
        session = adapter.open_session(self.binding)
        adapter.close()
        self.assertEqual(session.schedule_prefetch("查询")["reason"], "SCHEDULER_CLOSED")
        with MemoryRuntime(db_path=":memory:", clock=self.clock) as runtime:
            adapter = HostAdapter(runtime, background_prefetch=True)
            session = adapter.open_session(self.binding)
            self.assertEqual(session.schedule_prefetch("查询")["reason"], "FILE_DATABASE_REQUIRED")

    def test_forgetting_prevents_queued_delivery_from_restoring_old_answer(self):
        session = HostAdapter(self.runtime).open_session(self.binding)
        turn = session.prepare_turn("u1", "我喜欢海边故事", max_tokens=0)
        session.after_delivery("a1", "记住了", user_event_id=turn.user_event_id, expected_guard=turn.delivery_guard)
        self.repos.forgetting.bump(self.scope)
        with self.assertRaises(AdapterError) as caught:
            session.after_delivery("a2", "含有旧内容的回答", user_event_id=turn.user_event_id, expected_guard=turn.delivery_guard)
        self.assertEqual(caught.exception.code, "STALE_DELIVERY")
        self.runtime.forget_event(turn.user_event_id)
        with self.assertRaises(AdapterError):
            session.after_delivery("a3", "不能写回", user_event_id=turn.user_event_id)

    def test_scope_purge_clears_metrics_and_alerts(self):
        record_metric(self.runtime, self.scope, "prefetch_failed")
        self.runtime.check_alerts(self.scope)
        self.runtime.forget_scope(self.scope)
        self.assertEqual(self.count_rows("operational_metrics"), 0)
        self.assertEqual(self.count_rows("operational_alerts"), 0)

    def scheduler(self, runtime, **kwargs):
        policy = DeepSeekSchedulingPolicy(quota_db_path=str(Path(self._tmpdir.name)/"quota.db"), **kwargs)
        scheduler = DeepSeekRequestScheduler(runtime.db, self.clock, DeepSeekClient("local-test-key", scheduling_policy=policy))
        self.addCleanup(scheduler.close)
        return scheduler

    def test_separate_memory_databases_share_provider_concurrency(self):
        settings = replace(self.runtime.settings, db_path=Path(self._tmpdir.name)/"second.db")
        with MemoryRuntime(settings, clock=self.clock) as other:
            first = self.scheduler(self.runtime)
            second = self.scheduler(other)
            permit = first.acquire()
            with self.assertRaises(ExtractionDeferred):
                second.acquire()
            first._finish(permit, None)
            second._finish(second.acquire(), None)

    def test_sliding_rpm_and_token_reservations_survive_process_completion(self):
        scheduler = self.scheduler(self.runtime, requests_per_minute=2, tokens_per_minute=100)
        first = scheduler.acquire(token_reservation=60)
        scheduler._finish(first, None)
        with self.assertRaises(ExtractionDeferred):
            scheduler.acquire(token_reservation=41)
        second = scheduler.acquire(token_reservation=40)
        scheduler._finish(second, None)
        with self.assertRaises(ExtractionDeferred):
            scheduler.acquire()
        with self.assertRaises(LLMExtractionError):
            scheduler.acquire(token_reservation=101)
        self.clock.advance(seconds=61)
        scheduler._finish(scheduler.acquire(token_reservation=100), None)

    def test_shared_quota_does_not_allow_network_inside_origin_write_transaction(self):
        scheduler = self.scheduler(self.runtime)
        from memorydreaming.errors import StorageError
        with self.runtime.db.transaction(), self.assertRaises(StorageError):
            scheduler.acquire()
