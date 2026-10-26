from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorydreaming.clock import FixedClock
from memorydreaming.config import Settings
from memorydreaming.models import MemoryItem, MemoryType, Scope, new_id
from memorydreaming.runtime import MemoryRuntime


class P0ScopeAndBitemporalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        self.clock = FixedClock(self.now)
        self.runtime = MemoryRuntime(Settings(db_path=Path(self.temp.name) / "memory.db"), clock=self.clock)
        self.addCleanup(self.runtime.close)

    def _insert(self, scope: Scope, body: str):
        item = MemoryItem(new_id("mem"), MemoryType.SEMANTIC, scope,
                          {"subject": "user", "predicate": "likes", "obj": body},
                          occurred_start=self.clock.now(), recorded_at=self.clock.now())
        with self.runtime.db.transaction():
            self.runtime.db.repositories.memories.insert(item)
        return item

    def test_bitemporal_replay_separates_knowledge_and_fact_time(self):
        scope = Scope("u", "c")
        item = self._insert(scope, "茶")
        first = self.clock.now()
        changed = first + timedelta(hours=1)
        self.clock.set(changed)
        with self.runtime.db.transaction():
            self.runtime.db.repositories.memories.close_validity(item.memory_id, changed, changed)
            replacement = MemoryItem(new_id("mem"), MemoryType.SEMANTIC, scope,
                                     {"subject": "user", "predicate": "likes", "obj": "咖啡"},
                                     supersedes_id=item.memory_id,
                                     occurred_start=changed, recorded_at=changed, valid_from=changed)
            self.runtime.db.repositories.memories.insert(replacement)

        old = self.runtime.retrieve("user", scope, knowledge_at=first + timedelta(minutes=1), fact_at=first)
        current = self.runtime.retrieve("user", scope, knowledge_at=changed + timedelta(minutes=1), fact_at=changed)
        self.assertEqual([entry.memory.body["obj"] for entry in old.items], ["茶"])
        self.assertEqual([entry.memory.body["obj"] for entry in current.items], ["咖啡"])
        self.assertEqual(self.runtime.memory_at(item.memory_id, first + timedelta(minutes=1)).body["obj"], "茶")

    def test_scope_purge_removes_local_data_preserves_other_scope_and_blocks_snapshot(self):
        target = Scope("u", "c", "r1")
        other = Scope("u", "c", "r2")
        removed = self._insert(target, "目标")
        kept = self._insert(other, "保留")
        snapshot_path = Path(self.temp.name) / "before.json"
        self.runtime.export_snapshot(str(snapshot_path), scope=target)

        result = self.runtime.forget_scope(target, reason="用户撤回")
        self.assertEqual(result.erasure_level, "active_store_purged")
        self.assertIn(removed.memory_id, result.deleted_ids)
        self.assertIsNone(self.runtime.db.repositories.memories.get(removed.memory_id))
        self.assertIsNotNone(self.runtime.db.repositories.memories.get(kept.memory_id))
        self.assertEqual(self.runtime.verify_journal()["valid"], True)
        self.assertTrue(result.residuals)
        preflight = self.runtime.preflight_snapshot(str(snapshot_path))
        self.assertFalse(preflight["ready"])
        self.assertTrue(any(error["code"] in {"GENERATION_MISMATCH", "PURGED_SCOPE"}
                            for error in preflight["errors"]))


if __name__ == "__main__":
    unittest.main()
