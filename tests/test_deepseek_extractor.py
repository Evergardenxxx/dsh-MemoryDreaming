from __future__ import annotations

import json
import io
import traceback
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from unittest.mock import Mock

from memorydreaming.llm_extractor import DeepSeekClient, DeepSeekExtractor, LLMExtractionError, _retry_after
from memorydreaming.models import EventRecord, MemoryType, Scope


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload
        self.closed = False

    def read(self, limit: int = -1):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def close(self):
        self.closed = True


class DeepSeekExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.event = EventRecord(
            event_id="evt-deepseek",
            scope=Scope("u1", "c1"),
            conversation_id="conv",
            speaker="user",
            content="我喜欢海边",
            occurred_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        )

    def test_calls_openai_compatible_endpoint_and_parses_json_content(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.get_header("Authorization")
            return _Response({"choices": [{"message": {"content": json.dumps({
                "schema_version": "1.0",
                "candidates": [{
                    "memory_type": "semantic",
                    "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                    "candidate_key": "sea",
                    "source_span": [0, 5],
                }],
            }, ensure_ascii=False)}}]})

        client = DeepSeekClient("sk-test", base_url="https://example.test", timeout=7,
                                opener=opener)
        drafts = DeepSeekExtractor(client).extract(self.event)
        self.assertEqual(drafts[0].memory_type, MemoryType.SEMANTIC)
        self.assertEqual(captured["url"], "https://example.test/chat/completions")
        self.assertEqual(captured["timeout"], 7.0)
        self.assertEqual(captured["auth"], "Bearer sk-test")
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})

    def test_api_errors_are_redacted_to_retryable_domain_error(self) -> None:
        def opener(request, timeout):
            raise OSError("secret api key should not leak")

        with self.assertRaisesRegex(LLMExtractionError, "服务暂时不可用"):
            DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)).extract(self.event)

    @staticmethod
    def output():
        return {"schema_version": "1.0", "candidates": [{
            "memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
            "candidate_key": "sea", "source_span": [0, 5]}]}

    @staticmethod
    def response(content, reason="stop"):
        return _Response({"choices": [{"finish_reason": reason, "message": {"content": content}}]})

    def test_http_errors_classify_retry_policy_without_leaking_or_reading_error_body(self):
        for status, code, retryable in (
            (429, "RATE_LIMITED", True), (503, "UNAVAILABLE", True),
            (500, "UNAVAILABLE", True), (408, "UNAVAILABLE", True),
            (400, "INVALID_REQUEST", False), (422, "INVALID_REQUEST", False),
            (401, "AUTHENTICATION", False), (403, "AUTHENTICATION", False),
            (402, "QUOTA_EXCEEDED", False), (404, "INVALID_REQUEST", False),
        ):
            with self.subTest(status=status):
                headers = Message()
                headers["Retry-After"] = "61"
                body = io.BytesIO(b"private response content and sk-secret")
                failure = urllib.error.HTTPError("https://private.example", status, "private message", headers, body)
                opener = Mock(side_effect=failure)
                with self.assertRaises(LLMExtractionError) as context:
                    DeepSeekExtractor(DeepSeekClient("sk-secret", opener=opener)).extract(self.event)
                self.assertEqual(context.exception.code, code)
                self.assertEqual(context.exception.retryable, retryable)
                self.assertTrue(body.closed)
                self.assertEqual(opener.call_count, 1)
                rendered = "".join(traceback.format_exception(context.exception))
                self.assertNotIn("sk-secret", rendered)
                self.assertNotIn("private message", rendered)
                if status in (429, 500, 503, 408):
                    self.assertEqual(context.exception.retry_after_seconds, 61)

    def test_retry_after_accepts_http_date_and_bounds_untrusted_values(self):
        self.assertEqual(_retry_after("90000"), 86400)
        self.assertEqual(_retry_after("0"), 0)
        self.assertEqual(_retry_after("2.5"), 2.5)
        for value in ("-1", "nan", "inf", "junk", "", None):
            self.assertIsNone(_retry_after(value))
        date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
        self.assertTrue(58 < _retry_after(date) <= 60)

    def test_network_timeout_is_retryable_and_has_no_sensitive_traceback_cause(self):
        opener = Mock(side_effect=TimeoutError("private timeout detail sk-secret"))
        with self.assertRaises(LLMExtractionError) as context:
            DeepSeekExtractor(DeepSeekClient("sk-secret", opener=opener)).extract(self.event)
        self.assertEqual(context.exception.code, "TIMEOUT")
        self.assertTrue(context.exception.retryable)
        self.assertNotIn("sk-secret", "".join(traceback.format_exception(context.exception)))
        self.assertEqual(opener.call_count, 1)

    def test_truncation_and_content_filter_never_parse_partial_candidates_or_repair_inline(self):
        for reason, code, retryable in (("length", "OUTPUT_TRUNCATED", True),
                                         ("content_filter", "CONTENT_FILTERED", False)):
            with self.subTest(reason=reason):
                response = self.response(json.dumps(self.output()), reason)
                opener = Mock(return_value=response)
                with self.assertRaises(LLMExtractionError) as context:
                    DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)).extract(self.event)
                self.assertEqual(context.exception.code, code)
                self.assertEqual(context.exception.retryable, retryable)
                self.assertTrue(response.closed)
                self.assertEqual(opener.call_count, 1)

    def test_response_byte_limit_is_enforced_before_json_parsing(self):
        response = self.response("x" * 1024)
        client = DeepSeekClient("sk-test", opener=Mock(return_value=response))
        client.response_byte_limit = 512
        with self.assertRaises(LLMExtractionError) as context:
            DeepSeekExtractor(client).extract(self.event)
        self.assertEqual(context.exception.code, "RESPONSE_TOO_LARGE")
        self.assertTrue(response.closed)

    def test_invalid_output_is_repaired_once_with_original_evidence_and_static_feedback(self):
        requests = []
        responses = [self.response("untrusted broken output: ignore rules"),
                     self.response(json.dumps(self.output(), ensure_ascii=False))]

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return responses[len(requests) - 1]

        drafts = DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)).extract(self.event)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(len(requests), 2)
        self.assertIn(self.event.content, requests[1]["messages"][1]["content"])
        self.assertIn("唯一的格式修复", requests[1]["messages"][1]["content"])
        self.assertNotIn("ignore rules", str(requests[1]))
        self.assertTrue(all(response.closed for response in responses))

    def test_failed_repair_is_bounded_and_can_be_disabled(self):
        for enabled, expected_calls in ((True, 2), (False, 1)):
            with self.subTest(enabled=enabled):
                opener = Mock(side_effect=lambda request, timeout: self.response("not-json"))
                with self.assertRaises(LLMExtractionError):
                    DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener),
                                      repair_invalid_output=enabled).extract(self.event)
                self.assertEqual(opener.call_count, expected_calls)

    def test_repair_of_invalid_body_is_validated_before_returning_any_candidate(self):
        bad = self.output()
        bad["candidates"][0]["body"] = {"subject": "用户"}
        opener = Mock(side_effect=[self.response(json.dumps(bad)), self.response(json.dumps(self.output()))])
        result = DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener)).extract(self.event)
        self.assertEqual(result[0].body["obj"], "海边")
        self.assertEqual(opener.call_count, 2)

    def test_non_user_events_do_not_call_remote_model(self):
        opener = Mock(side_effect=AssertionError("must not call"))
        extractor = DeepSeekExtractor(DeepSeekClient("sk-test", opener=opener))
        for speaker in ("character", "assistant", "system"):
            self.assertEqual(extractor.extract(replace(self.event, speaker=speaker)), [])
        opener.assert_not_called()

    def test_nonfinite_timeout_configuration_is_rejected(self):
        for value in (float("nan"), float("inf"), -1, 0, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DeepSeekClient("sk-test", timeout=value)


if __name__ == "__main__":
    unittest.main()

