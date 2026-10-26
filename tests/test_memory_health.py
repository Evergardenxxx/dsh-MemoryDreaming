from __future__ import annotations

from tests.helpers import RuntimeTestCase
from memorydreaming.models import EpistemicStatus, MemoryType


class MemoryHealthTests(RuntimeTestCase):
    def test_health_reports_stale_summaries_failed_work_and_source_gaps_without_body(self):
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "海边"})
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "推测", "obj": "未知"},
                        source_event_ids=[], epistemic_status=EpistemicStatus.INFERRED)
        activity = self.runtime.register_activity(self.scope, "故事", open_loops=["补结尾"])
        self.runtime.build_summaries(self.scope)
        health = self.runtime.memory_health(self.scope)
        self.assertEqual(health["status"], "ok")
        self.assertGreaterEqual(health["memories"]["semantic"]["active"], 2)
        self.assertEqual(health["working"]["open_loops"], 1)
        self.assertEqual(health["inferred_without_source"], 1)
        self.assertNotIn("海边", str(health))

