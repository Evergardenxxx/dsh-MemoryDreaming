"""真实客户端错误到持久队列状态的集成回归，不访问外网。"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from unittest.mock import Mock

from memorydreaming.clock import from_utc_iso
from memorydreaming.llm_extractor import DeepSeekClient, DeepSeekExtractor
from memorydreaming.runtime import MemoryRuntime
from tests.helpers import RuntimeTestCase


class Response:
    def __init__(self, content):
        self.raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()

    def read(self, limit):
        return self.raw[:limit]

    def close(self):
        pass


def good_response():
    return Response(json.dumps({"schema_version": "1.0", "candidates": [{
        "memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
        "candidate_key": "sea", "source_span": [0, 5]}]}))


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://api.example", status, "secret error body sk-private", headers,
                                  io.BytesIO(b"private raw content"))


class DeepSeekJobTests(RuntimeTestCase):
    def observe(self, key="extract"):
        return self.runtime.observe(self.make_turn("我喜欢海边"), self.scope, key)

    def job(self, job_id):
        return self.runtime.db.connection.execute(
            "SELECT * FROM consolidation_jobs WHERE job_id=?", (job_id,)).fetchone()

    def test_rate_limit_retry_after_is_persisted_across_restart_and_exceeds_local_backoff_cap(self):
        observed = self.observe()
        opener = Mock(side_effect=[http_error(429, 3600), good_response()])
        extractor = DeepSeekExtractor(DeepSeekClient("sk-private", opener=opener))
        report = self.runtime.run_consolidation(extractor)
        self.assertEqual((report.failed, report.created), (1, 0))
        self.assertEqual(self.job(observed.job_id)["status"], "pending")
        self.assertEqual((from_utc_iso(self.job(observed.job_id)["available_at"]) - self.clock.now()).total_seconds(), 3600)
        self.assertNotIn("sk-private", str(report.errors) + self.job(observed.job_id)["last_error"])
        with MemoryRuntime(settings=self.runtime.settings, clock=self.clock) as other:
            self.clock.advance(seconds=3599)
            self.assertEqual(other.run_consolidation(extractor).claimed, 0)
            self.assertEqual(opener.call_count, 1)
            self.clock.advance(seconds=1)
            report = other.run_consolidation(extractor)
            self.assertEqual(report.created, 1)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(self.job(observed.job_id)["status"], "done")
        self.assertEqual(self.count_rows("memories"), 1)
        memory = self.repos.memories.list_by_scope(self.scope)[0]
        self.assertIsNone(memory.occurred_start)
        self.assertEqual(memory.occurred_precision, "unknown")

    def test_authentication_and_invalid_request_fail_permanently_without_spending_all_attempts(self):
        for status in (400, 401, 402, 403, 404, 422):
            with self.subTest(status=status):
                observed = self.observe(str(status))
                opener = Mock(side_effect=http_error(status))
                report = self.runtime.run_consolidation(DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)))
                row = self.job(observed.job_id)
                self.assertEqual(report.failed, 1)
                self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
                self.clock.advance(hours=1)
                self.assertEqual(self.runtime.run_consolidation(DeepSeekExtractor(
                    DeepSeekClient("sk-test", opener=opener))).claimed, 0)
                self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.count_rows("memories"), 0)

    def test_timeout_uses_existing_queue_backoff(self):
        observed = self.observe()
        opener = Mock(side_effect=TimeoutError("private timeout"))
        report = self.runtime.run_consolidation(DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)))
        self.assertEqual(report.failed, 1)
        row = self.job(observed.job_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual((from_utc_iso(row["available_at"]) - self.clock.now()).total_seconds(),
                         self.runtime.settings.job_retry_base_seconds)

    def test_failed_inline_repair_never_writes_a_partial_batch(self):
        observed = self.observe()
        output = {"schema_version": "1.0", "candidates": [
            {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
             "candidate_key": "sea", "source_span": [0, 5]},
            {"memory_type": "semantic", "body": {"subject": "用户"},
             "candidate_key": "broken", "source_span": [0, 5]}]}
        opener = Mock(side_effect=lambda request, timeout: Response(json.dumps(output)))
        report = self.runtime.run_consolidation(DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)))
        self.assertEqual(opener.call_count, 2)
        self.assertEqual((report.created, report.failed), (0, 1))
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)
        self.assertEqual(self.count_rows("entity_mentions"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)
        self.assertEqual(self.job(observed.job_id)["status"], "pending")

    def test_one_rate_limited_event_defers_other_events_on_the_same_account(self):
        first = self.observe("first")
        second = self.observe("second")
        opener = Mock(side_effect=[http_error(429, 60), good_response()])
        report = self.runtime.run_consolidation(DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)))
        self.assertEqual((report.claimed, report.created, report.failed, report.deferred), (2, 0, 1, 1))
        self.assertEqual(opener.call_count, 1)
        states = sorted(self.job(item.job_id)["status"] for item in (first, second))
        self.assertEqual(states, ["pending", "pending"])
        self.assertEqual(sorted(self.job(item.job_id)["attempts"] for item in (first, second)), [0, 1])

    def test_reextracting_same_event_with_random_model_keys_is_idempotent(self):
        observed = self.observe()
        response = good_response()
        changed = json.loads(response.raw)
        content = json.loads(changed["choices"][0]["message"]["content"])
        content["candidates"][0]["candidate_key"] = "different-random-key"
        opener = Mock(side_effect=[response, Response(json.dumps(content))])
        extractor = DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener))
        self.assertEqual(self.runtime.run_consolidation(extractor).created, 1)
        with self.runtime.db.transaction():
            self.repos.jobs.enqueue("extract", self.scope, {"event_id": observed.event.event_id}, self.clock.now())
        report = self.runtime.run_consolidation(extractor)
        self.assertEqual((report.created, report.duplicates), (0, 1))
        self.assertEqual(self.count_rows("memories"), 1)
