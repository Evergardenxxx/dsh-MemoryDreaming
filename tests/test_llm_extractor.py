from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
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

    def test_candidate_keys_numbers_reserved_fields_and_ranges_are_strictly_validated(self):
        good = {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                "candidate_key": "sea", "source_span": [0, 5]}
        changes = (
            {"importance": True}, {"importance": "0.7"}, {"importance": -0.1},
            {"importance": 2}, {"importance": float("nan")}, {"importance": float("inf")},
            {"scope": {"user_id": "other"}}, {"source_span": [True, 5]}, {"source_span": [0, 99]},
            {"modality": {}}, {"epistemic_status": []},
            {"occurred_start": "2026-10-27T12:00:00Z", "occurred_end": "2026-10-26T12:00:00Z"},
            {"valid_from": "2026-10-27T12:00:00Z", "valid_to": "2026-10-26T12:00:00Z"},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(LLMExtractionError):
                output = {"schema_version": "1.0", "candidates": [{**deepcopy(good), **change}]}
                LLMExtractor(lambda prompt: output).extract(self.event)
        duplicate = {"schema_version": "1.0", "candidates": [deepcopy(good), deepcopy(good)]}
        with self.assertRaises(LLMExtractionError):
            LLMExtractor(lambda prompt: duplicate).extract(self.event)

    def test_duplicate_json_keys_nonfinite_numbers_and_reserved_top_level_are_rejected(self):
        for output in (
            '{"schema_version":"1.0","candidates":[],"candidates":[]}',
            '{"schema_version":"1.0","candidates":[],"value":NaN}',
            '{"schema_version":"1.0","candidates":[],"value":1e999}',
            {"schema_version": "1.0", "candidates": [], "event_id": "forged"},
        ):
            with self.subTest(output=output), self.assertRaises(LLMExtractionError):
                LLMExtractor(lambda prompt: output).extract(self.event)

    def test_unknown_provider_exception_is_redacted(self):
        def provider(prompt):
            raise RuntimeError("sensitive key sk-secret")
        with self.assertRaises(LLMExtractionError) as context:
            LLMExtractor(provider).extract(self.event)
        self.assertEqual(context.exception.code, "PROVIDER_FAILURE")
        self.assertNotIn("sk-secret", str(context.exception))

    def test_backend_candidate_key_is_stable_despite_random_model_keys_and_body_key_order(self):
        candidate = {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                     "candidate_key": "random-model-key", "source_span": [0, 5]}
        payload = {"schema_version": "1.0", "candidates": [candidate]}
        first = LLMExtractor(lambda prompt: payload).extract(self.event)[0]
        candidate["candidate_key"] = "a-different-model-key"
        candidate["body"] = dict(reversed(list(candidate["body"].items())))
        second = LLMExtractor(lambda prompt: payload).extract(self.event)[0]
        self.assertEqual(first.candidate_key, second.candidate_key)
        self.assertRegex(first.candidate_key, r"^llm:[0-9a-f]{64}$")

    def test_duplicate_content_with_distinct_model_keys_is_rejected(self):
        candidate = {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                     "candidate_key": "one", "source_span": [0, 5]}
        payload = {"schema_version": "1.0", "candidates": [candidate, {**candidate, "candidate_key": "two"}]}
        with self.assertRaises(LLMExtractionError):
            LLMExtractor(lambda prompt: payload).extract(self.event)

    def test_working_and_procedural_body_arrays_require_strings(self):
        bad_bodies = (("working", {"goal": {}, "open_loops": [], "active_entities": [], "checkpoint_note": ""}),
                      ("working", {"goal": "继续", "open_loops": [None], "active_entities": [], "checkpoint_note": ""}),
                      ("working", {"goal": "继续", "open_loops": [], "active_entities": [123], "checkpoint_note": ""}),
                      ("procedural", {"trigger": "睡前", "steps": [None]}),
                      ("procedural", {"trigger": "睡前", "steps": [{}]}))
        for memory_type, body in bad_bodies:
            payload = {"schema_version": "1.0", "candidates": [{"memory_type": memory_type, "body": body,
                       "candidate_key": "invalid-body", "source_span": [0, 5]}]}
            with self.subTest(body=body), self.assertRaises(LLMExtractionError):
                LLMExtractor(lambda prompt: payload).extract(self.event)

    def test_source_quote_aligns_unicode_offsets_but_rejects_ambiguous_or_invented_quotes(self):
        event = replace(self.event, content="🌙 我喜欢海边。")
        candidate = {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                     "candidate_key": "sea", "source_span": [3, 8], "source_quote": "我喜欢海边"}
        payload = {"schema_version": "1.0", "candidates": [candidate]}
        draft = LLMExtractor(lambda prompt: payload).extract(event)[0]
        self.assertEqual(draft.source_span, (2, 7))
        for quote in ("没说过的原话", "海边"):
            repeated = replace(event, content="海边。我喜欢海边。")
            candidate["source_quote"] = quote
            candidate["source_span"] = [0, 1]
            with self.subTest(quote=quote), self.assertRaises(LLMExtractionError):
                LLMExtractor(lambda prompt: payload).extract(repeated)

    def test_missing_event_time_has_unknown_precision_and_explicit_time_is_normalized(self):
        candidate = {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": "海边"},
                     "candidate_key": "sea", "source_span": [0, 5]}
        payload = {"schema_version": "1.0", "candidates": [candidate]}
        draft = LLMExtractor(lambda prompt: payload).extract(self.event)[0]
        self.assertIsNone(draft.occurred_start)
        self.assertEqual(draft.occurred_precision, "unknown")
        candidate["occurred_start"] = "2026-10-26T12:00:00Z"
        first = LLMExtractor(lambda prompt: payload).extract(self.event)[0]
        candidate["occurred_start"] = "2026-10-26T20:00:00+08:00"
        second = LLMExtractor(lambda prompt: payload).extract(self.event)[0]
        self.assertEqual(first.candidate_key, second.candidate_key)
        self.assertEqual(first.occurred_precision, "exact")

    def test_published_deepseek_schema_matches_runtime_contract(self):
        path = Path(__file__).resolve().parents[1] / "docs/contracts/deepseek-extraction.schema.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), EXTRACTION_SCHEMA)


if __name__ == "__main__":
    unittest.main()

