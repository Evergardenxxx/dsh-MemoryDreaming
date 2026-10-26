from __future__ import annotations

from datetime import timedelta

from tests.helpers import RuntimeTestCase
from memorydreaming.models import MemoryType


class RetrievalGateTests(RuntimeTestCase):
    def test_trivial_query_skips_retrieval_but_keeps_a_normal_query_available(self):
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"})
        trivial = self.runtime.retrieve("你好", self.scope)
        self.assertEqual(trivial.items, [])
        self.assertTrue(trivial.plan.skip_retrieval)
        self.assertIn("低信息", "".join(trivial.trace.notes))

        normal = self.runtime.retrieve("喜欢海边故事", self.scope)
        self.assertFalse(normal.plan.skip_retrieval)
        self.assertTrue(normal.items)

    def test_explicit_time_constraint_disables_trivial_gate(self):
        interval = (self.clock.now() - timedelta(days=1), self.clock.now())
        result = self.runtime.retrieve("好的", self.scope, time_range=interval)
        self.assertFalse(result.plan.skip_retrieval)

