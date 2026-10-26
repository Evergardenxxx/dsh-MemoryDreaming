"""持久审计链测试：覆盖状态变更、幂等和篡改检测。"""

from __future__ import annotations

import unittest

from memorydreaming.extractors import RuleBasedExtractor

from tests.helpers import RuntimeTestCase


class JournalTest(RuntimeTestCase):
    def test_runtime_operations_append_metadata_only_and_verify_chain(self) -> None:
        activity = self.runtime.register_activity(self.scope, "海边故事", goal="讲完第一章")
        observed = self.runtime.observe(self.make_turn("我喜欢海边"), self.scope, "journal-turn-1")
        self.runtime.checkpoint(
            activity.activity_id,
            {"open_loops": ["第二章"], "checkpoint": {"chapter": 1}},
            expected_version=1,
        )
        handoff, duplicate = self.runtime.save_handoff(
            self.scope,
            "window-1",
            {"goal": "讲完第一章", "open_loops": ["第二章"]},
            activity_id=activity.activity_id,
            source_event_id=observed.event.event_id,
            activity_version=2,
            idempotency_key="handoff-1",
        )
        self.assertFalse(duplicate)
        self.assertEqual(self.runtime.save_handoff(
            self.scope,
            "window-1",
            {"goal": "讲完第一章", "open_loops": ["第二章"]},
            activity_id=activity.activity_id,
            source_event_id=observed.event.event_id,
            activity_version=2,
            idempotency_key="handoff-1",
        )[0].handoff_id, handoff.handoff_id)

        entries = self.runtime.journal_entries(self.scope)
        event_types = {entry["event_type"] for entry in entries}
        self.assertTrue({
            "activity.created", "event.recorded", "consolidation.enqueued",
            "activity.checkpoint", "handoff.created",
        }.issubset(event_types))
        event_entry = next(entry for entry in entries if entry["event_type"] == "event.recorded")
        self.assertNotIn("content", event_entry["payload"])
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_consolidation_lifecycle_is_journaled(self) -> None:
        self.runtime.observe(self.make_turn("我喜欢睡前听故事"), self.scope, "journal-turn-2")

        report = self.runtime.run_consolidation(RuleBasedExtractor())

        self.assertGreaterEqual(report.claimed, 1)
        types = {entry["event_type"] for entry in self.runtime.journal_entries()}
        self.assertIn("consolidation.claimed", types)
        self.assertIn("consolidation.completed", types)

    def test_tampering_is_detected(self) -> None:
        self.runtime.observe(self.make_turn("一条消息"), self.scope, "journal-turn-3")
        row = self.runtime.db.connection.execute(
            "SELECT sequence FROM journal_entries ORDER BY sequence LIMIT 1"
        ).fetchone()
        self.runtime.db.connection.execute(
            "UPDATE journal_entries SET payload_json='{}' WHERE sequence=?",
            (row["sequence"],),
        )

        result = self.runtime.verify_journal()

        self.assertFalse(result["valid"])
        self.assertEqual(result["error"], "entry_hash_mismatch")

    def test_reusing_journal_key_with_different_payload_is_rejected(self) -> None:
        with self.runtime.db.transaction():
            self.repos.journal.append(
                "test.event", scope=self.scope, entity_id="one",
                payload={"value": 1}, now=self.clock.now(), idempotency_key="same-key",
            )
            with self.assertRaises(ValueError):
                self.repos.journal.append(
                    "test.event", scope=self.scope, entity_id="two",
                    payload={"value": 2}, now=self.clock.now(), idempotency_key="same-key",
                )

    def test_journal_rolls_back_with_business_transaction(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.runtime.db.transaction():
                self.repos.journal.append(
                    "test.rollback", scope=self.scope, entity_id="one",
                    payload={}, now=self.clock.now(),
                )
                raise RuntimeError("abort")

        self.assertEqual(self.runtime.verify_journal()["checked"], 0)


if __name__ == "__main__":
    unittest.main()
