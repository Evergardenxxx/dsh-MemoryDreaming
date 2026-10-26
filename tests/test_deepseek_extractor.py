from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from memorydreaming.llm_extractor import DeepSeekClient, DeepSeekExtractor, LLMExtractionError
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


if __name__ == "__main__":
    unittest.main()

