"""共享供应商配额、延后计数、请求租约及真实进程崩溃；不访问外网。"""

from dataclasses import replace
from datetime import timedelta
from email.message import Message
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfoNotFoundError

from memorydreaming import DeepSeekRequestScheduler, DeepSeekSchedulingPolicy
from memorydreaming.clock import from_utc_iso
from memorydreaming.errors import StorageError
from memorydreaming.llm_extractor import DeepSeekClient, DeepSeekExtractor, LLMExtractionError
from memorydreaming.portability import export_snapshot
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.scheduling import ExtractionDeferred
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import NOW, RuntimeTestCase


class Response:
    def __init__(self, content):
        self.body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()

    def read(self, limit):
        return self.body[:limit]

    def close(self):
        pass


def good_response():
    return Response(json.dumps({"schema_version": "1.0", "candidates": [{
        "memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
        "candidate_key": "sea", "source_span": [0, 5]}]}))


def limited_response(seconds=60, status=429):
    headers = Message()
    if seconds is not None:
        headers["Retry-After"] = str(seconds)
    return urllib.error.HTTPError("https://private.example", status, "sk-private provider body", headers,
                                  io.BytesIO(b"private response body"))


class RequestSchedulingTest(RuntimeTestCase):
    def scheduler(self, *, db=None, key="sk-scheduled-private", model="deepseek-chat", endpoint="https://api.deepseek.com",
                  policy=None):
        return DeepSeekRequestScheduler(db or self.runtime.db, self.clock,
            DeepSeekClient(key, model=model, base_url=endpoint, scheduling_policy=policy))

    def test_same_database_account_shares_limit_across_connections_and_models(self):
        first = self.scheduler()
        permit = first.acquire()
        other = Database.open(self.db_path, initialize=False)
        self.addCleanup(other.close)
        second = self.scheduler(db=other, model="deepseek-reasoner", endpoint="https://API.DEEPSEEK.com/")
        self.assertEqual(first.provider_key, second.provider_key)
        with self.assertRaises(ExtractionDeferred) as caught:
            second.acquire()
        self.assertEqual(caught.exception.code, "PROVIDER_BUSY")
        self.assertEqual(second.status()["active_requests"], 1)
        first._finish(permit, None)
        permit2 = second.acquire()
        second._finish(permit2, None)

    def test_different_credentials_or_endpoint_have_independent_slots(self):
        schedulers = [self.scheduler(), self.scheduler(key="sk-another"), self.scheduler(endpoint="https://other.example")]
        permits = [scheduler.acquire() for scheduler in schedulers]
        self.assertEqual(len({permit.provider_key for permit in permits}), 3)
        for scheduler, permit in zip(schedulers, permits):
            scheduler._finish(permit, None)

    def test_cooldown_survives_restart_and_older_success_cannot_clear_it(self):
        policy = DeepSeekSchedulingPolicy(max_concurrent=2)
        scheduler = self.scheduler(policy=policy)
        older, limited = scheduler.acquire(), scheduler.acquire()
        scheduler._finish(limited, LLMExtractionError("limited", code="RATE_LIMITED", retry_after_seconds=61))
        scheduler._finish(older, None)
        reopened = Database.open(self.db_path, initialize=False)
        self.addCleanup(reopened.close)
        restarted = self.scheduler(db=reopened, policy=policy)
        self.clock.advance(seconds=60)
        with self.assertRaises(ExtractionDeferred):
            restarted.acquire()
        self.clock.advance(seconds=1)
        permit = restarted.acquire()
        restarted._finish(permit, None)

    def test_server_cooldown_is_monotonic_and_missing_header_uses_fallback(self):
        scheduler = self.scheduler(policy=DeepSeekSchedulingPolicy(max_concurrent=2, cooldown_seconds=7))
        first, second = scheduler.acquire(), scheduler.acquire()
        scheduler._finish(first, LLMExtractionError("rate", code="RATE_LIMITED", retry_after_seconds=100))
        scheduler._finish(second, LLMExtractionError("rate", code="RATE_LIMITED", retry_after_seconds=1))
        self.assertEqual(from_utc_iso(scheduler.status()["not_before"]), NOW + timedelta(seconds=100))
        self.clock.advance(seconds=100)
        permit = scheduler.acquire()
        scheduler._finish(permit, LLMExtractionError("rate", code="RATE_LIMITED"))
        self.assertEqual(from_utc_iso(scheduler.status()["not_before"]), self.clock.now() + timedelta(seconds=7))

    def test_start_interval_and_stricter_policy_cannot_be_bypassed(self):
        strict = self.scheduler(policy=DeepSeekSchedulingPolicy(min_interval_seconds=10))
        permit = strict.acquire()
        strict._finish(permit, None)
        loose = self.scheduler(policy=DeepSeekSchedulingPolicy(max_concurrent=3))
        with self.assertRaises(ExtractionDeferred) as caught:
            loose.acquire()
        self.assertEqual(caught.exception.retry_at, NOW + timedelta(seconds=10))
        self.assertEqual(loose.status()["max_concurrent"], 1)
        self.assertEqual(loose.status()["min_interval_seconds"], 10)
        self.clock.advance(seconds=10)
        permit = loose.acquire()
        with self.assertRaises(StorageError):
            loose.reconfigure()
        loose._finish(permit, None)
        loose.reconfigure()
        self.assertEqual(loose.status()["max_concurrent"], 3)
        self.assertEqual(loose.status()["min_interval_seconds"], 0)
        # 显式放宽也不删除已经预订的时间。
        with self.assertRaises(ExtractionDeferred):
            loose.acquire()

    def test_expired_permit_cannot_release_new_owner_or_publish_result(self):
        scheduler = self.scheduler(policy=DeepSeekSchedulingPolicy(lease_seconds=2))
        old = scheduler.acquire()
        self.clock.advance(seconds=2)
        new = scheduler.acquire()
        with self.assertRaises(LLMExtractionError) as caught:
            scheduler._finish(old, None)
        self.assertEqual(caught.exception.code, "REQUEST_LEASE_LOST")
        self.assertEqual(scheduler.status()["active_requests"], 1)
        scheduler._finish(new, None)

    def test_simultaneous_connections_get_only_one_slot(self):
        barrier = threading.Barrier(2)
        results = queue.Queue()
        def compete():
            db = Database.open(self.db_path, initialize=False)
            try:
                scheduler = self.scheduler(db=db)
                barrier.wait(timeout=3)
                results.put(scheduler.acquire())
            except BaseException as exc:
                results.put(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=compete, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        values = [results.get(timeout=5) for _ in threads]
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(sum(isinstance(item, ExtractionDeferred) for item in values), 1, values)
        self.assertEqual(sum(not isinstance(item, BaseException) for item in values), 1, values)
        permit = next(item for item in values if not isinstance(item, BaseException))
        self.scheduler()._finish(permit, None)

    def test_network_callback_is_outside_transaction_and_interrupt_releases_slot(self):
        scheduler = self.scheduler()
        other = Database.open(self.db_path, initialize=False)
        other.connection.execute("PRAGMA busy_timeout=10")
        self.addCleanup(other.close)
        def request():
            self.assertFalse(self.runtime.db.connection.in_transaction)
            with other.transaction():
                other.meta_set("unlocked-provider", "yes")
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            scheduler.call(request)
        self.assertEqual(scheduler.status()["active_requests"], 0)
        self.assertFalse(self.runtime.db.connection.in_transaction)

    def test_outer_write_transaction_cannot_hide_network_wait(self):
        invoke = Mock()
        with self.runtime.db.transaction(), self.assertRaises(StorageError):
            self.scheduler().call(invoke)
        invoke.assert_not_called()

    def test_heartbeat_renews_request_while_other_connection_respects_slot(self):
        entered, finish = threading.Event(), threading.Event()
        results = queue.Queue()
        policy = DeepSeekSchedulingPolicy(lease_seconds=1)
        def run():
            db = Database.open(self.db_path, initialize=False)
            try:
                def request():
                    entered.set()
                    if not finish.wait(3):
                        raise TimeoutError("test deadline")
                    return "ok"
                results.put(self.scheduler(db=db, policy=policy).call(request))
            except BaseException as exc:
                results.put(exc)
            finally:
                db.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.clock.advance(seconds=0.8)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                row = self.runtime.db.connection.execute("SELECT lease_until FROM provider_requests").fetchone()
                if row and from_utc_iso(row[0]) > NOW + timedelta(seconds=1):
                    break
                time.sleep(0.01)
            else:
                self.fail("请求心跳未续租")
            self.clock.advance(seconds=0.6)
            with self.assertRaises(ExtractionDeferred):
                self.scheduler(policy=policy).acquire()
        finally:
            finish.set()
            thread.join(timeout=3)
        self.assertEqual(results.get(timeout=1), "ok")

    def test_crashed_process_slot_is_reclaimed_only_after_lease_expiry(self):
        script = """
import os, sys
from datetime import datetime, timezone
from memorydreaming import DeepSeekRequestScheduler, DeepSeekSchedulingPolicy
from memorydreaming.clock import FixedClock
from memorydreaming.llm_extractor import DeepSeekClient
from memorydreaming.storage.database import Database
db = Database.open(sys.argv[1], initialize=False)
scheduler = DeepSeekRequestScheduler(db, FixedClock(datetime(2026,10,20,12,tzinfo=timezone.utc)),
    DeepSeekClient('sk-scheduled-private', scheduling_policy=DeepSeekSchedulingPolicy(lease_seconds=3)))
scheduler.acquire()
os._exit(17)
"""
        child = subprocess.run([sys.executable, "-c", script, str(self.db_path)], timeout=10, capture_output=True)
        self.assertEqual(child.returncode, 17, child.stderr)
        scheduler = self.scheduler(policy=DeepSeekSchedulingPolicy(lease_seconds=3))
        with self.assertRaises(ExtractionDeferred):
            scheduler.acquire()
        self.clock.advance(seconds=3)
        permit = scheduler.acquire()
        scheduler._finish(permit, None)

    def test_operational_rows_do_not_invalidate_memory_cache_or_export_secrets(self):
        generation = self.repos.retrieval_cache.generation()
        scheduler = self.scheduler()
        permit = scheduler.acquire()
        snapshot = export_snapshot(self.runtime.db)
        self.assertNotIn("provider_limits", snapshot["tables"])
        self.assertNotIn("provider_requests", snapshot["tables"])
        self.assertEqual(snapshot["manifest"]["operational_tables_skipped"], ["provider_limits", "provider_requests",
            "provider_usage", "provider_budgets", "operational_metrics", "operational_alerts"])
        stored = [dict(row) for table in ("provider_limits", "provider_requests")
                  for row in self.runtime.db.connection.execute(f"SELECT * FROM {table}")]
        payload = json.dumps(stored)
        self.assertNotIn("sk-scheduled-private", payload)
        self.assertNotIn("api.deepseek.com", payload)
        scheduler._finish(permit, None)
        self.assertEqual(self.repos.retrieval_cache.generation(), generation)

    def test_invalid_policy_and_environment_fail_before_requests(self):
        for kwargs in ({"max_concurrent": True}, {"max_concurrent": 0}, {"min_interval_seconds": -1},
                       {"cooldown_seconds": float("nan")}, {"lease_seconds": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DeepSeekSchedulingPolicy(**kwargs)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test", "MEMORYDREAMING_DEEPSEEK_MAX_CONCURRENT": "invalid"}):
            with self.assertRaises(ValueError):
                DeepSeekClient.from_env()

    def test_schema20_upgrade_adds_operational_tables_and_is_repeatable(self):
        with self.runtime.db.transaction():
            self.runtime.db.connection.execute("DROP TABLE provider_requests")
            self.runtime.db.connection.execute("DROP TABLE provider_limits")
            self.runtime.db.meta_set("schema_version", "20")
        for _ in range(2):
            db = Database.open(self.db_path)
            try:
                self.assertEqual(db.meta_get("schema_version"), SCHEMA_VERSION)
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM provider_requests").fetchone()[0], 0)
            finally:
                db.close()

    def test_environment_configures_all_four_scheduling_limits(self):
        values = {"DEEPSEEK_API_KEY": "sk-test", "MEMORYDREAMING_DEEPSEEK_MAX_CONCURRENT": "2",
                  "MEMORYDREAMING_DEEPSEEK_MIN_INTERVAL_SECONDS": "0.5",
                  "MEMORYDREAMING_DEEPSEEK_COOLDOWN_SECONDS": "7",
                  "MEMORYDREAMING_DEEPSEEK_REQUEST_LEASE_SECONDS": "40"}
        with patch.dict(os.environ, values):
            policy = DeepSeekClient.from_env().scheduling_policy
        self.assertEqual(policy, DeepSeekSchedulingPolicy(2, 0.5, 7, 40))


class ScheduledJobTest(RuntimeTestCase):
    def observe(self, key):
        return self.runtime.observe(self.make_turn("我喜欢海边"), self.scope, key)

    def row(self, observed):
        return self.runtime.db.connection.execute("SELECT * FROM consolidation_jobs WHERE job_id=?", (observed.job_id,)).fetchone()

    def test_shared_cooldown_defers_entire_batch_and_new_jobs_without_consuming_attempts(self):
        observations = [self.observe(str(i)) for i in range(4)]
        opener = Mock(side_effect=limited_response(60))
        extractor = DeepSeekExtractor(DeepSeekClient("sk-jobs", opener=opener))
        report = self.runtime.run_consolidation(extractor, limit=10)
        self.assertEqual((report.claimed, report.failed, report.deferred), (4, 1, 3))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sorted(self.row(item)["attempts"] for item in observations), [0, 0, 0, 1])
        new = self.observe("new")
        report = self.runtime.run_consolidation(extractor)
        self.assertEqual((report.failed, report.deferred), (0, 1))
        self.assertEqual(self.row(new)["attempts"], 0)
        self.assertEqual(opener.call_count, 1)
        for item in [*observations, new]:
            self.assertEqual(from_utc_iso(self.row(item)["available_at"]), NOW + timedelta(seconds=60))

    def test_dedupe_work_is_not_blocked_by_provider_cooldown(self):
        self.observe("extract")
        with self.runtime.db.transaction():
            dedupe = self.repos.jobs.enqueue("dedupe", self.scope, {}, NOW)
        opener = Mock(side_effect=limited_response(60))
        report = self.runtime.run_consolidation(DeepSeekExtractor(DeepSeekClient("sk-jobs", opener=opener)))
        self.assertEqual((report.claimed, report.failed), (2, 1))
        self.assertEqual(self.runtime.db.connection.execute("SELECT status FROM consolidation_jobs WHERE job_id=?", (dedupe,)).fetchone()[0], "done")

    def test_min_interval_resumes_pending_tasks_at_exact_boundary(self):
        observations = [self.observe(str(i)) for i in range(2)]
        opener = Mock(side_effect=lambda request, timeout: good_response())
        policy = DeepSeekSchedulingPolicy(min_interval_seconds=10)
        extractor = DeepSeekExtractor(DeepSeekClient("sk-jobs", opener=opener, scheduling_policy=policy))
        report = self.runtime.run_consolidation(extractor)
        self.assertEqual((report.created, report.deferred, report.failed), (1, 1, 0))
        self.clock.advance(seconds=9)
        self.runtime.run_consolidation(extractor)
        self.assertEqual(opener.call_count, 1)
        self.clock.advance(seconds=1)
        report = self.runtime.run_consolidation(extractor)
        self.assertEqual(report.created, 1)
        self.assertEqual(opener.call_count, 2)
        self.assertTrue(all(self.row(item)["attempts"] == 1 for item in observations))

    def test_repair_must_acquire_another_slot_and_real_attempt_is_not_refunded(self):
        observation = self.observe("bad-output")
        opener = Mock(side_effect=lambda request, timeout: Response("not JSON"))
        extractor = DeepSeekExtractor(DeepSeekClient("sk-jobs", opener=opener,
            scheduling_policy=DeepSeekSchedulingPolicy(min_interval_seconds=10)))
        for attempt in range(1, 4):
            report = self.runtime.run_consolidation(extractor)
            self.assertEqual((report.failed, report.deferred), (1, 1))
            self.assertEqual(self.row(observation)["attempts"], attempt)
            self.assertEqual(opener.call_count, attempt)
            self.clock.advance(seconds=30)
        self.assertEqual(self.row(observation)["status"], "failed")
        self.assertEqual(self.count_rows("memories"), 0)

    def test_retry_after_from_server_error_also_closes_shared_gate(self):
        self.observe("a")
        self.observe("b")
        opener = Mock(side_effect=limited_response(120, status=503))
        report = self.runtime.run_consolidation(DeepSeekExtractor(DeepSeekClient("sk-jobs", opener=opener)))
        self.assertEqual((report.failed, report.deferred), (1, 1))
        self.assertEqual(opener.call_count, 1)

    def test_forgotten_event_cannot_be_reopened_by_delayed_release(self):
        observation = self.observe("will-forget")
        client = DeepSeekClient("sk-jobs", opener=Mock())
        scheduler = DeepSeekRequestScheduler(self.runtime.db, self.clock, client)
        permit = scheduler.acquire()
        original = DeepSeekRequestScheduler.acquire
        def race(service, **kwargs):
            try:
                return original(service, **kwargs)
            except ExtractionDeferred:
                self.runtime.forget_event(observation.event.event_id)
                raise
        with patch.object(DeepSeekRequestScheduler, "acquire", race):
            report = self.runtime.run_consolidation(DeepSeekExtractor(client))
        self.assertEqual((report.deferred, report.lease_lost), (1, 1))
        self.assertEqual(self.row(observation)["status"], "done")
        client._opener.assert_not_called()
        scheduler._finish(permit, None)

    def test_background_worker_resumes_after_cooldown_without_network_retry_loop(self):
        opener = Mock(side_effect=[limited_response(60), good_response(), good_response()])
        extractor = DeepSeekExtractor(DeepSeekClient("sk-auto", opener=opener))
        settings = replace(self.runtime.settings, auto_consolidation=True, consolidation_interval_seconds=0.02)
        runtime = MemoryRuntime(settings, clock=self.clock, consolidation_extractor=extractor)
        try:
            for key in ("a", "b"):
                runtime.observe(self.make_turn("我喜欢海边"), self.scope, key)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                stats = runtime.job_stats()
                if stats["scheduled"] == 2:
                    break
                time.sleep(0.01)
            else:
                self.fail("后台任务未延后")
            self.assertEqual(opener.call_count, 1)
            self.clock.advance(seconds=60)
            runtime.consolidator.wake()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                completed = runtime.db.connection.execute("SELECT COUNT(*) FROM consolidation_jobs WHERE job_type='extract' AND status='done'").fetchone()[0]
                if completed == 2:
                    break
                time.sleep(0.01)
            else:
                self.fail("冷却后后台任务未恢复")
            self.assertEqual(opener.call_count, 3)
            self.assertIsNone(runtime.consolidator.last_error)
        finally:
            runtime.close()

    def test_waiting_for_a_slot_cannot_exhaust_max_attempts(self):
        observation = self.observe("waiting")
        with self.runtime.db.transaction():
            self.runtime.db.connection.execute("UPDATE consolidation_jobs SET max_attempts=1 WHERE job_id=?", (observation.job_id,))
        opener = Mock(side_effect=lambda request, timeout: good_response())
        client = DeepSeekClient("sk-jobs", opener=opener)
        scheduler = DeepSeekRequestScheduler(self.runtime.db, self.clock, client)
        permit = scheduler.acquire()
        for _ in range(4):
            report = self.runtime.run_consolidation(DeepSeekExtractor(client))
            self.assertEqual((report.deferred, report.failed), (1, 0))
            self.assertEqual((self.row(observation)["status"], self.row(observation)["attempts"]), ("pending", 0))
            self.clock.advance(seconds=1)
        opener.assert_not_called()
        scheduler._finish(permit, None)
        report = self.runtime.run_consolidation(DeepSeekExtractor(client))
        self.assertEqual(report.created, 1)
        self.assertEqual((self.row(observation)["status"], self.row(observation)["attempts"]), ("done", 1))

    def test_lost_request_lease_discards_result_and_consumes_real_attempt(self):
        observation = self.observe("slow-request")
        def slow_response(request, timeout):
            self.clock.advance(seconds=3)
            return good_response()
        opener = Mock(side_effect=slow_response)
        extractor = DeepSeekExtractor(DeepSeekClient("sk-jobs", opener=opener,
            scheduling_policy=DeepSeekSchedulingPolicy(lease_seconds=2)))
        report = self.runtime.run_consolidation(extractor)
        self.assertEqual((report.failed, report.created), (1, 0))
        self.assertEqual(self.row(observation)["attempts"], 1)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("provider_requests"), 0)

    def test_background_worker_keeps_running_without_iana_timezone_data(self):
        opener = Mock(side_effect=lambda request, timeout: good_response())
        extractor = DeepSeekExtractor(DeepSeekClient("sk-no-tzdata", opener=opener))
        settings = replace(self.runtime.settings, auto_consolidation=True, consolidation_interval_seconds=0.02)
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing tzdata")):
            runtime = MemoryRuntime(settings, clock=self.clock, consolidation_extractor=extractor)
            try:
                observation = runtime.observe(self.make_turn("我喜欢海边"), self.scope, "no-tzdata")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    state = runtime.db.connection.execute("SELECT status FROM consolidation_jobs WHERE job_id=?", (observation.job_id,)).fetchone()[0]
                    if state == "done":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("缺少时区数据库导致后台抽取停止")
                self.assertIsNone(runtime.consolidator.last_error)
                self.assertEqual(opener.call_count, 1)
            finally:
                runtime.close()
