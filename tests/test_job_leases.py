"""任务所有权、事务回滚、真实进程崩溃和跨连接心跳的回归测试。"""

from __future__ import annotations

import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memorydreaming.clock import FixedClock, from_utc_iso
from memorydreaming.config import Settings
from memorydreaming.errors import JobLeaseLost
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import MemoryStatus, MemoryType
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from memorydreaming.worker import JobHeartbeat

from tests.helpers import NOW, RuntimeTestCase


class JobLeaseTest(RuntimeTestCase):
    def enqueue(self, max_attempts: int = 3) -> str:
        return self.repos.jobs.enqueue("dedupe", self.scope, {}, self.clock.now(), max_attempts=max_attempts)

    def claim(self, seconds: float = 10):
        with self.runtime.db.transaction():
            return self.repos.jobs.claim(1, self.clock.now(), lease_seconds=seconds)[0]

    def test_second_connection_cannot_claim_owned_job(self) -> None:
        job_id = self.enqueue()
        owner = self.claim()
        other = Database.open(self.db_path)
        self.addCleanup(other.close)
        with other.transaction():
            self.assertEqual(other.repositories.jobs.claim(1, self.clock.now()), [])
        self.assertEqual(owner.job_id, job_id)
        self.assertTrue(owner.claim_token)

    def test_simultaneous_workers_claim_each_task_only_once(self) -> None:
        expected = {self.enqueue() for _ in range(8)}
        barrier = threading.Barrier(2)
        results: queue.Queue[object] = queue.Queue()

        def compete():
            db = Database.open(self.db_path, initialize=False)
            try:
                barrier.wait(timeout=5)
                results.put(db.repositories.jobs.claim(8, NOW))
            except Exception as exc:
                results.put(exc)
            finally:
                db.close()

        workers = [threading.Thread(target=compete, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        claimed = []
        for _ in workers:
            result = results.get(timeout=10)
            if isinstance(result, Exception):
                raise result
            claimed.extend(result)
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual({job.job_id for job in claimed}, expected)
        self.assertEqual(len(claimed), 8)
        self.assertEqual(len({job.claim_token for job in claimed}), 8)

    def test_expired_claim_is_recovered_with_new_token_and_old_owner_is_fenced(self) -> None:
        self.enqueue()
        old = self.claim()
        self.clock.advance(seconds=10)  # 到期边界立即失去权限
        with self.runtime.db.transaction():
            self.assertEqual(self.repos.jobs.recover_expired(self.clock.now()), 1)
        current = self.claim()
        self.assertNotEqual(old.claim_token, current.claim_token)
        self.assertEqual(current.attempts, 2)
        actions = (
            lambda: self.repos.jobs.complete(old.job_id, old.claim_token, self.clock.now()),
            lambda: self.repos.jobs.fail(old.job_id, old.claim_token, "stale", self.clock.now()),
            lambda: self.repos.jobs.release(old.job_id, old.claim_token, self.clock.now()),
            lambda: self.repos.jobs.renew(old.job_id, old.claim_token, self.clock.now()),
        )
        for action in actions:
            with self.assertRaises(JobLeaseLost), self.runtime.db.transaction():
                action()
        with self.runtime.db.transaction():
            self.repos.jobs.complete(current.job_id, current.claim_token, self.clock.now())
        self.assertEqual(self.runtime.job_stats()["done"], 1)

    def test_renewed_lease_survives_original_deadline(self) -> None:
        self.enqueue()
        job = self.claim()
        self.clock.advance(seconds=8)
        with self.runtime.db.transaction():
            self.repos.jobs.renew(job.job_id, job.claim_token, self.clock.now(), 10)
        self.clock.advance(seconds=3)
        self.assertEqual(self.repos.jobs.recover_expired(self.clock.now()), 0)
        self.repos.jobs.complete(job.job_id, job.claim_token, self.clock.now())

    def test_expiry_at_max_attempts_is_terminal(self) -> None:
        self.enqueue(max_attempts=1)
        self.claim()
        self.clock.advance(seconds=10)
        self.assertEqual(self.runtime.job_stats()["expired"], 1)
        report = self.runtime.run_consolidation()
        self.assertEqual((report.recovered, report.claimed), (1, 0))
        self.assertEqual(self.runtime.job_stats()["failed"], 1)

    def test_retry_backoff_is_persisted_capped_and_ready_at_boundary(self) -> None:
        self.enqueue()
        for attempt, delay in ((1, 5), (2, 6), (3, 6)):
            job = self.claim()
            self.assertEqual(job.attempts, attempt)
            with self.runtime.db.transaction():
                self.repos.jobs.fail(
                    job.job_id, job.claim_token, "transient", self.clock.now(),
                    retry_base_seconds=5, retry_max_seconds=6,
                )
            stats = self.runtime.job_stats()
            if attempt == 3:
                self.assertEqual(stats["failed"], 1)
                break
            self.assertEqual((stats["pending"], stats["scheduled"], stats["ready"]), (1, 1, 0))
            reopened = Database.open(self.db_path)
            try:
                self.assertEqual(reopened.repositories.jobs.claim(1, self.clock.now()), [])
            finally:
                reopened.close()
            self.clock.advance(seconds=delay - 0.001)
            self.assertEqual(self.repos.jobs.claim(1, self.clock.now()), [])
            self.clock.advance(seconds=0.001)
        self.clock.advance(days=1)
        self.assertEqual(self.repos.jobs.claim(1, self.clock.now()), [])

    def test_release_returns_attempt_and_allows_new_owner(self) -> None:
        self.enqueue()
        first = self.claim()
        self.repos.jobs.release(first.job_id, first.claim_token, self.clock.now())
        second = self.claim()
        self.assertEqual(second.attempts, 1)
        self.assertNotEqual(first.claim_token, second.claim_token)

    def test_empty_filters_never_claim_all_jobs_and_invalid_limit_is_rejected(self) -> None:
        self.enqueue()
        self.assertEqual(self.repos.jobs.claim(10, self.clock.now(), job_types=[]), [])
        self.assertEqual(self.repos.jobs.claim(10, self.clock.now(), job_ids=[]), [])
        for limit in (0, -1):
            with self.assertRaises(ValueError):
                self.repos.jobs.claim(limit, self.clock.now())
            with self.assertRaises(ValueError):
                self.runtime.run_consolidation(limit=limit)
        for seconds in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.repos.jobs.claim(1, self.clock.now(), lease_seconds=seconds)


class AtomicConsolidationTest(RuntimeTestCase):
    def observe(self):
        return self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "source")

    def test_index_failure_rolls_back_memory_and_delays_retry(self) -> None:
        self.observe()
        with patch.object(self.runtime.vectors, "index_memory", side_effect=RuntimeError("index offline")):
            report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual((report.failed, report.created), (1, 0))
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)
        self.assertEqual(self.runtime.run_consolidation(RuleBasedExtractor()).claimed, 0)
        self.clock.advance(seconds=5)
        retried = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual((retried.claimed, retried.created), (1, 1))
        self.assertEqual(self.count_rows("memory_vectors"), 1)
        self.assertEqual(self.runtime.job_stats()["done"], 1)
        self.assertEqual(self.runtime.job_stats()["pending"], 1)  # 只有一条后续任务

    def test_followup_enqueue_failure_also_rolls_back_memory(self) -> None:
        self.observe()
        with patch.object(self.repos.jobs, "enqueue", side_effect=RuntimeError("queue offline")):
            report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual(report.failed, 1)
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)

    def test_completion_failure_rolls_back_memory_vectors_and_followup(self) -> None:
        self.observe()
        with patch.object(self.repos.jobs, "complete", side_effect=RuntimeError("ack offline")):
            report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual((report.failed, report.created), (1, 0))
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)

    def test_dedupe_completion_failure_restores_sources_links_and_status(self) -> None:
        first = self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "周末去海边，我要带相机"},
            source_event_ids=["source-a"], importance=0.8,
        )
        second = self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "周末去海边，我要带相机！"},
            source_event_ids=["source-b"], importance=0.5,
        )
        self.repos.jobs.enqueue("dedupe", self.scope, {}, self.clock.now())
        with patch.object(self.repos.jobs, "complete", side_effect=RuntimeError("ack offline")):
            report = self.runtime.run_consolidation()
        self.assertEqual((report.failed, report.merged), (1, 0))
        self.assertEqual(self.repos.memories.get(first.memory_id).source_event_ids, ["source-a"])
        self.assertEqual(self.repos.memories.get(second.memory_id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.count_rows("memory_links"), 0)
        self.clock.advance(seconds=5)
        self.assertEqual(self.runtime.run_consolidation().merged, 1)

    def test_summary_completion_failure_restores_previous_card(self) -> None:
        for text in ("第一次去海边，捡贝壳", "海边拍摄日落", "海边散步吹风"):
            self.add_memory(MemoryType.EPISODIC, {"what_happened": text})
        self.assertEqual(self.runtime.build_summaries(self.scope), 1)
        original = self.repos.summaries.list_active(self.scope)[0]
        self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边露营看星星"})
        self.repos.jobs.enqueue("summarize", self.scope, {}, self.clock.now())
        with patch.object(self.repos.jobs, "complete", side_effect=RuntimeError("ack offline")):
            report = self.runtime.run_consolidation()
        self.assertEqual((report.failed, report.summarized), (1, 0))
        unchanged = self.repos.summaries.get(original.summary_id)
        self.assertEqual(unchanged.digest, original.digest)
        self.assertEqual(unchanged.source_memory_ids, original.source_memory_ids)
        self.clock.advance(seconds=5)
        self.assertEqual(self.runtime.run_consolidation().summarized, 1)
        self.assertEqual(len(self.repos.summaries.get(original.summary_id).source_memory_ids), 4)

    def test_lease_expiring_during_writes_rolls_everything_back(self) -> None:
        self.observe()
        index = self.runtime.vectors.index_memory

        def expire_after_index(*args, **kwargs):
            result = index(*args, **kwargs)
            self.clock.advance(seconds=121)
            return result

        with patch.object(self.runtime.vectors, "index_memory", side_effect=expire_after_index):
            report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual((report.lease_lost, report.created), (1, 0))
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)
        recovered = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual((recovered.recovered, recovered.created), (1, 1))

    def test_extractor_does_not_hold_write_lock_and_stale_result_is_discarded(self) -> None:
        observed = self.observe()
        other = Database.open(self.db_path)
        self.addCleanup(other.close)
        clock = self.clock
        scope = self.scope

        class TakeoverExtractor(RuleBasedExtractor):
            def extract(self, event):
                # 另一个连接可写，证明外部抽取没有占用写事务。
                with other.transaction():
                    other.repositories.jobs.enqueue("propose_core", scope, {}, clock.now())
                clock.advance(seconds=121)
                with other.transaction():
                    other.repositories.jobs.recover_expired(clock.now())
                    current = other.repositories.jobs.claim(1, clock.now(), job_types=["extract"])[0]
                    other.repositories.jobs.complete(current.job_id, current.claim_token, clock.now())
                return super().extract(event)

        report = self.runtime.run_consolidation(TakeoverExtractor())
        self.assertEqual(report.lease_lost, 1)
        self.assertEqual(self.count_rows("memories"), 0)
        row = self.runtime.db.connection.execute(
            "SELECT status,attempts FROM consolidation_jobs WHERE job_id = ?", (observed.job_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("done", 2))

    def test_later_batch_jobs_are_only_leased_when_execution_begins(self) -> None:
        self.observe()
        self.runtime.observe(self.make_turn("我喜欢狗"), self.scope, "source-2")
        clock = self.clock

        class SlowExtractor(RuleBasedExtractor):
            calls = 0

            def extract(self, event):
                if self.calls == 0:
                    clock.advance(seconds=121)
                self.calls += 1
                return super().extract(event)

        report = self.runtime.run_consolidation(SlowExtractor())
        # 第一条因租约到期被拒；第二条这时才领取，拥有完整的租约。
        self.assertEqual((report.claimed, report.lease_lost, report.created), (2, 1, 1))

    def test_configured_attempt_budget_applies_to_observed_and_followup_jobs(self) -> None:
        runtime = MemoryRuntime(replace(self.runtime.settings, job_max_attempts=7), clock=self.clock)
        self.addCleanup(runtime.close)
        runtime.observe(self.make_turn("我喜欢猫"), self.scope, "budget")
        runtime.run_consolidation(RuleBasedExtractor())
        budgets = [row[0] for row in runtime.db.connection.execute("SELECT max_attempts FROM consolidation_jobs")]
        self.assertEqual(budgets, [7, 7])

    def test_in_memory_database_uses_fenced_synchronous_execution(self) -> None:
        with MemoryRuntime(Settings().with_db(":memory:"), clock=self.clock) as runtime:
            runtime.observe(self.make_turn("我喜欢猫"), self.scope, "in-memory")
            report = runtime.run_consolidation(RuleBasedExtractor())
            self.assertEqual((report.created, report.failed), (1, 0))


class WorkerRecoveryTest(RuntimeTestCase):
    def test_heartbeat_renews_over_separate_connection_until_stopped(self) -> None:
        clock = FixedClock(NOW)
        self.repos.jobs.enqueue("dedupe", self.scope, {}, clock.now())
        job = self.repos.jobs.claim(1, clock.now(), lease_seconds=3)[0]
        with JobHeartbeat(self.runtime.db, job, clock, lease_seconds=3):
            clock.advance(seconds=2)
            deadline = time.monotonic() + 5
            renewed = False
            while time.monotonic() < deadline:
                row = self.runtime.db.connection.execute(
                    "SELECT lease_until FROM consolidation_jobs WHERE job_id = ?", (job.job_id,),
                ).fetchone()
                if from_utc_iso(row[0]) > job.lease_until:
                    renewed = True
                    break
                threading.Event().wait(0.02)
            self.assertTrue(renewed, "心跳未在独立连接上续租")
            clock.advance(seconds=1.5)
            self.assertEqual(self.repos.jobs.recover_expired(clock.now()), 0)
        clock.advance(seconds=4)
        self.assertEqual(self.repos.jobs.recover_expired(clock.now()), 1)

    def test_process_killed_mid_transaction_recovers_without_partial_memory(self) -> None:
        self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "crash")
        script = """
import sys
from datetime import datetime
from pathlib import Path
from memorydreaming.clock import FixedClock
from memorydreaming.config import Settings
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.runtime import MemoryRuntime
runtime = MemoryRuntime(Settings(db_path=Path(sys.argv[1])), clock=FixedClock(datetime.fromisoformat(sys.argv[2])))
def interrupt_index(*args, **kwargs):
    print('memory-in-uncommitted-transaction', flush=True)
    sys.stdin.read(1)
runtime.vectors.index_memory = interrupt_index
runtime.run_consolidation(RuleBasedExtractor())
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(self.db_path), NOW.isoformat()],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        signals: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=lambda: signals.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(signals.get(timeout=10).strip(), "memory-in-uncommitted-transaction")
            self.assertEqual(self.count_rows("memories"), 0)  # 其他连接看不到未提交结果
            process.kill()  # 真正强制结束，不走 close/finally
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            reader.join(timeout=2)
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.runtime.run_consolidation(RuleBasedExtractor()).claimed, 0)
        self.clock.advance(seconds=121)
        # 模拟重启后新进程的运行时，沿用同一个磁盘数据库。
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as restarted:
            report = restarted.run_consolidation(RuleBasedExtractor())
            self.assertEqual((report.recovered, report.created, report.failed), (1, 1, 0))
            self.assertEqual(restarted.job_stats()["done"], 1)
            self.assertEqual(restarted.job_stats()["pending"], 1)
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.count_rows("memory_vectors"), 1)

    def test_v4_migration_recovers_legacy_running_and_is_idempotent(self) -> None:
        path = self.db_path.with_name("v4.db")
        schema = (Path(__file__).resolve().parents[1] / "memorydreaming/storage/schema.sql").read_text(encoding="utf-8")
        # 构造上一版真实任务表；其他表无需伪造，初始化会自动创建。
        start = schema.index("CREATE TABLE IF NOT EXISTS consolidation_jobs")
        end = schema.index(";", start) + 1
        job_schema = schema[start:end]
        for line in job_schema.splitlines():
            if any(name in line for name in ("max_attempts", "available_at", "claim_token", "lease_until")):
                job_schema = job_schema.replace(line + "\n", "")
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                           "INSERT INTO schema_meta VALUES ('schema_version','4');" + job_schema)
        stamp = NOW.isoformat(timespec="microseconds")
        for job_id, attempts in (("recoverable", 1), ("exhausted", 3)):
            conn.execute(
                "INSERT INTO consolidation_jobs(job_id,job_type,user_id,character_id,status,attempts,created_at,updated_at) "
                "VALUES (?,'dedupe','u1','c1','running',?,?,?)", (job_id, attempts, stamp, stamp),
            )
        conn.commit()
        conn.close()
        for iteration in range(2):
            db = Database.open(path)
            try:
                self.assertEqual(db.meta_get("schema_version"), SCHEMA_VERSION)
                if iteration == 0:
                    self.assertEqual(db.repositories.jobs.recover_expired(NOW), 2)
                else:
                    self.assertEqual(db.repositories.jobs.recover_expired(NOW), 0)
                stats = db.repositories.jobs.stats(NOW)
                self.assertEqual((stats["pending"], stats["failed"]), (1, 1))
                available = db.connection.execute(
                    "SELECT available_at FROM consolidation_jobs WHERE job_id='recoverable'",
                ).fetchone()[0]
                self.assertEqual(from_utc_iso(available), NOW)
            finally:
                db.close()


class QueueConfigTest(unittest.TestCase):
    def test_invalid_configuration_is_rejected(self) -> None:
        for kwargs in (
            {"job_lease_seconds": 0}, {"job_max_attempts": -1}, {"job_retry_base_seconds": 0},
            {"job_retry_base_seconds": 10, "job_retry_max_seconds": 5}, {"job_lease_seconds": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_environment_overrides_queue_settings(self) -> None:
        with patch.dict(os.environ, {
            "MEMORYDREAMING_JOB_LEASE_SECONDS": "45", "MEMORYDREAMING_JOB_MAX_ATTEMPTS": "6",
            "MEMORYDREAMING_JOB_RETRY_BASE_SECONDS": "2", "MEMORYDREAMING_JOB_RETRY_MAX_SECONDS": "30",
        }):
            settings = Settings.from_env()
        self.assertEqual((settings.job_lease_seconds, settings.job_max_attempts,
                          settings.job_retry_base_seconds, settings.job_retry_max_seconds), (45, 6, 2, 30))


if __name__ == "__main__":
    unittest.main()
