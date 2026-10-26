from __future__ import annotations

from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import ForgetSelector, MemoryStatus, MemoryType

from tests.helpers import RuntimeTestCase


class ForgetEventTest(RuntimeTestCase):
    def test_event_forgetting_redacts_source_and_blocks_reextraction(self) -> None:
        observed = self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "event-forget")
        self.runtime.run_consolidation(RuleBasedExtractor())
        memory = self.repos.memories.list_by_scope(self.scope)[0]

        result = self.runtime.forget_event(observed.event.event_id, scope=self.scope)

        self.assertEqual(result.forgotten_event_ids, [observed.event.event_id])
        self.assertEqual(result.erasure_level, "event_redacted")
        self.assertEqual(self.repos.memories.get(memory.memory_id).status, MemoryStatus.DELETED)
        self.assertEqual(self.repos.events.get(observed.event.event_id).content, "[已删除事件]")
        self.assertEqual(self.runtime.retrieve("喜欢猫", self.scope).items, [])

        # 即便旧任务被重新排队，事件账本也会让它在模型调用前完成而不复活。
        with self.runtime.db.transaction():
            self.repos.jobs.enqueue(
                "extract", self.scope, {"event_id": observed.event.event_id}, self.clock.now()
            )
        report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual((report.created, report.failed), (0, 0))
        self.assertEqual(self.repos.memories.get(memory.memory_id).status, MemoryStatus.DELETED)

    def test_event_forgetting_removes_one_source_but_keeps_independent_source(self) -> None:
        first = self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "source-a").event
        second = self.runtime.observe(self.make_turn("我还是喜欢猫"), self.scope, "source-b").event
        item = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "猫"},
            source_event_ids=[first.event_id, second.event_id],
            source_spans={first.event_id: [[0, 4]], second.event_id: [[0, 6]]},
        )

        self.runtime.forget_event(first.event_id)
        remaining = self.repos.memories.get(item.memory_id)
        self.assertEqual(remaining.status, MemoryStatus.ACTIVE)
        self.assertEqual(remaining.source_event_ids, [second.event_id])
        self.assertNotIn(first.event_id, remaining.source_spans)

        self.runtime.forget_event(second.event_id)
        self.assertEqual(self.repos.memories.get(item.memory_id).status, MemoryStatus.DELETED)

    def test_legacy_forget_selector_can_dispatch_one_event(self) -> None:
        event = self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "selector-event").event
        result = self.runtime.forget(ForgetSelector(event_ids=[event.event_id], scope=self.scope))
        self.assertEqual(result.forgotten_event_ids, [event.event_id])


if __name__ == "__main__":
    import unittest
    unittest.main()

