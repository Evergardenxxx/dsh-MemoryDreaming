from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from memorydreaming.clock import FixedClock
from memorydreaming.config import Settings
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import MemoryType, Scope, Turn
from memorydreaming.runtime import MemoryRuntime

from tests.helpers import NOW


class AutomaticConsolidationTest(unittest.TestCase):
    def test_file_runtime_wakes_background_worker_after_observe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                db_path=Path(directory) / "memory.db",
                auto_consolidation=True,
                consolidation_interval_seconds=0.02,
            )
            clock = FixedClock(NOW)
            runtime = MemoryRuntime(settings, clock=clock, consolidation_extractor=RuleBasedExtractor())
            try:
                scope = Scope("u1", "c1")
                runtime.observe(
                    Turn("user", "我喜欢睡前听故事", NOW, "conversation"),
                    scope,
                    "auto-1",
                )
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    items = runtime.db.repositories.memories.list_by_scope(scope)
                    if any(item.memory_type is MemoryType.SEMANTIC for item in items):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("后台整理器未在截止时间内形成语义记忆")
                self.assertTrue(runtime.consolidator is not None)
                self.assertTrue(runtime.consolidator.last_report is not None)
                self.assertIsNone(runtime.consolidator.last_error)
            finally:
                worker = runtime.consolidator
                runtime.close()
                self.assertFalse(worker.running if worker is not None else True)

    def test_in_memory_runtime_keeps_explicit_mode(self) -> None:
        settings = Settings(db_path=Path(":memory:"), auto_consolidation=True)
        runtime = MemoryRuntime(settings, consolidation_extractor=RuleBasedExtractor())
        try:
            self.assertIsNone(runtime.consolidator)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()

