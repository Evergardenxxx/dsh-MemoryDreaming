from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from memorydreaming.llm_extractor import EXTRACTION_SCHEMA, LLMExtractionError, LLMExtractor
from memorydreaming.models import EventRecord, MemoryType, Scope


class LLMExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.event = EventRecord(
            event_id="evt-1",
            scope=Scope("u1", "c1"),
            conversation_id="conv",
            speaker="user",
            content="我喜欢海边",
            occurred_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        )

    def test_provider_can_receive_schema_and_returns_memory_draft(self) -> None:
        seen = {}

        def provider(prompt, *, schema):
            seen["prompt"] = prompt
            seen["schema"] = schema
            return {
                "schema_version": "1.0",
                "candidates": [{
                    "memory_type": "semantic",
                    "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                    "candidate_key": "preference:sea",
                    "source_span": [0, 5],
                    "importance": 0.7,
                }],
            }

        drafts = LLMExtractor(provider).extract(self.event)
        self.assertEqual(drafts[0].memory_type, MemoryType.SEMANTIC)
        self.assertEqual(drafts[0].source_span, (0, 5))
        self.assertIs(seen["schema"], EXTRACTION_SCHEMA)
        self.assertIn(self.event.content, seen["prompt"])

    def test_single_argument_provider_and_json_string_are_supported(self) -> None:
        def provider(prompt):
            return json.dumps({"schema_version": "1.0", "candidates": []})

        self.assertEqual(LLMExtractor(provider).extract(self.event), [])

    def test_malformed_output_fails_closed(self) -> None:
        for output in ("not-json", {"schema_version": "0.9", "candidates": []},
                       {"schema_version": "1.0", "candidates": [{"memory_type": "semantic"}]}):
            with self.subTest(output=output):
                with self.assertRaises(LLMExtractionError):
                    LLMExtractor(lambda prompt, output=output: output).extract(self.event)

    def test_candidate_without_source_span_is_rejected(self) -> None:
        output = {
            "schema_version": "1.0",
            "candidates": [{
                "memory_type": "semantic",
                "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                "candidate_key": "preference:sea",
            }],
        }
        with self.assertRaises(LLMExtractionError):
            LLMExtractor(lambda prompt: output).extract(self.event)


if __name__ == "__main__":
    unittest.main()

