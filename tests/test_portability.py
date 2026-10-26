"""可移植快照与导入预检测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memorydreaming.models import Scope
from memorydreaming.portability import load_snapshot, preflight_snapshot_file
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.config import Settings

from tests.helpers import RuntimeTestCase


class PortabilityTest(RuntimeTestCase):
    def test_full_snapshot_roundtrip_preflight_is_read_only(self) -> None:
        self.runtime.observe(self.make_turn("我喜欢海边"), self.scope, "portable-turn-1")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "memory.snapshot.json"
            manifest = self.runtime.export_snapshot(str(path))

            before = self.count_rows("events")
            result = self.runtime.preflight_snapshot(str(path))

        self.assertEqual(manifest["format"], "memorydreaming.snapshot")
        self.assertFalse(manifest["partial"])
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(self.count_rows("events"), before)
        self.assertFalse(result["residuals"]["apply_supported"])

    def test_scoped_snapshot_marks_global_journal_as_partial(self) -> None:
        self.runtime.observe(self.make_turn("作用域内消息"), self.scope, "portable-turn-2")
        other = Scope(user_id="u2", character_id="c1")
        self.runtime.observe(self.make_turn("其他用户消息", conversation_id="other"), other, "portable-turn-3")

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scoped.snapshot.json"
            manifest = self.runtime.export_snapshot(str(path), self.scope)
            result = self.runtime.preflight_snapshot(str(path))
            snapshot = load_snapshot(path)

        self.assertTrue(manifest["partial"])
        self.assertTrue(result["ready"], result)
        self.assertTrue(any(item["code"] == "PARTIAL_JOURNAL" for item in result["warnings"]))
        self.assertEqual(len(snapshot["tables"]["events"]), 1)

    def test_tampered_table_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tampered.snapshot.json"
            self.runtime.export_snapshot(str(path))
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot["tables"]["events"].append({"event_id": "forged"})
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            result = self.runtime.preflight_snapshot(str(path))

        self.assertFalse(result["ready"])
        self.assertTrue(any(item["code"] == "TABLE_DIGEST_MISMATCH" for item in result["errors"]))
        self.assertEqual(self.count_rows("events"), 0)

    def test_changed_scope_generation_blocks_scoped_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "generation.snapshot.json"
            self.runtime.export_snapshot(str(path), self.scope)
            with self.runtime.db.transaction():
                self.repos.forgetting.bump(self.scope)

            result = self.runtime.preflight_snapshot(str(path))

        self.assertFalse(result["ready"])
        self.assertTrue(any(item["code"] == "GENERATION_MISMATCH" for item in result["errors"]))

    def test_different_forgetting_key_blocks_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_path = Path(temp) / "source.snapshot.json"
            self.runtime.export_snapshot(str(source_path))
            other_path = Path(temp) / "other.db"
            other = MemoryRuntime(Settings(db_path=other_path, timezone="Asia/Shanghai"))
            try:
                result = other.preflight_snapshot(str(source_path))
            finally:
                other.close()

        self.assertFalse(result["ready"])
        self.assertTrue(any(item["code"] == "FORGETTING_KEY_MISMATCH" for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
