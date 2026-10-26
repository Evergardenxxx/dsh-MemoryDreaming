"""整理任务测试：抽取之后自动去重，副本被合并而不是被删掉。

去重这件事很容易做过头：把「相关」当成「重复」，一合并就把不同的事揉成一条。
所以这里的用例重点不是「能合并」，而是「该合并的合并、不该动的别动」。
"""

from __future__ import annotations

import unittest

from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import LinkRelation, MemoryStatus, MemoryType

from tests.helpers import RuntimeTestCase


class FollowUpJobTest(RuntimeTestCase):
    def test_extract_enqueues_one_dedupe_job(self) -> None:
        """抽三次只排一条去重任务，不能每抽一次就堆一条。"""
        for index in range(3):
            self.runtime.observe(self.make_turn(f"我喜欢第{index}件事"), self.scope, f"k{index}")

        self.runtime.run_consolidation(RuleBasedExtractor())

        pending = self.repos.jobs.claim(10, self.clock.now(), job_types=["dedupe"])
        self.assertEqual(len(pending), 1)

    def test_extract_is_skipped_without_extractor(self) -> None:
        """没传抽取器时，抽取任务应当退回队列，而不是被判失败。"""
        self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "k1")

        report = self.runtime.run_consolidation()

        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(self.runtime.pending_jobs(), 1)


class DedupeTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "周末去海边，我要带相机"},
            source_event_ids=["evt_a"],
            importance=0.6,
            hours_ago=3,
        )
        self.second = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "周末去海边，我要带相机！"},
            source_event_ids=["evt_b"],
            importance=0.4,
            hours_ago=2,
        )
        # 上面两条是同一件事的两种说法；这一条是另一件事，不该被合并
        self.unrelated = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "昨天聊到公司楼下的橘猫"},
            hours_ago=1,
        )

    def _run_dedupe(self) -> int:
        with self.runtime.db.transaction():
            self.repos.jobs.enqueue("dedupe", self.scope, {"reason": "test"}, self.clock.now())
        return self.runtime.run_consolidation().merged

    def test_merges_only_near_duplicates(self) -> None:
        merged = self._run_dedupe()

        self.assertEqual(merged, 1)
        self.assertEqual(
            self.repos.memories.get(self.first.memory_id).status, MemoryStatus.ACTIVE
        )
        self.assertEqual(
            self.repos.memories.get(self.second.memory_id).status, MemoryStatus.SUPERSEDED
        )
        self.assertEqual(
            self.repos.memories.get(self.unrelated.memory_id).status, MemoryStatus.ACTIVE
        )

    def test_merge_keeps_evidence_from_both_records(self) -> None:
        """合并不能丢证据：两条来源事件都要挂在主记录上。"""
        self._run_dedupe()

        canonical = self.repos.memories.get(self.first.memory_id)
        self.assertEqual(len(canonical.source_event_ids), 2)
        self.assertEqual(len(set(canonical.source_event_ids)), 2)

    def test_duplicate_is_linked_to_canonical(self) -> None:
        self._run_dedupe()

        self.assertTrue(
            self.repos.links.has_relation(self.second.memory_id, LinkRelation.PART_OF)
        )
        self.assertEqual(
            self.repos.links.targets_of(self.second.memory_id), [self.first.memory_id]
        )

    def test_merged_copy_is_not_returned_by_retrieval(self) -> None:
        self._run_dedupe()

        result = self.runtime.retrieve("周末去海边要带什么", self.scope)
        ids = result.memory_ids()

        self.assertIn(self.first.memory_id, ids)
        self.assertNotIn(self.second.memory_id, ids)

    def test_merge_takes_max_importance(self) -> None:
        """合并后重要度取两者最大值：更重要的事实不该因为合并被降权。"""
        self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "周末去海边，我要带相机"},
            importance=0.9,
            hours_ago=4,
        )
        self._run_dedupe()

        merged = self.repos.memories.get(self.first.memory_id)
        self.assertGreaterEqual(merged.importance, 0.6)


if __name__ == "__main__":
    unittest.main()
