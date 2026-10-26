"""整理流程测试：从「用户说了一句话」到「库里有了一条类型记忆」。

重点验证三件事：
1. observe 只排队，不在同一请求里做抽取；
2. 重跑整理任务不会产生重复记忆；
3. 抽出来的东西保留正确的认识论标记（愿望 ≠ 已发生的事实）。
"""

from __future__ import annotations

import unittest

from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import EpistemicStatus, MemoryType, Modality

from tests.helpers import RuntimeTestCase


class ObserveTest(RuntimeTestCase):
    def test_observe_enqueues_extraction_job(self) -> None:
        result = self.runtime.observe(self.make_turn("我喜欢睡前听故事"), self.scope, "k1")

        self.assertIsNotNone(result.job_id)
        self.assertEqual(self.runtime.pending_jobs(), 1)
        self.assertEqual(self.count_rows("memories"), 0)  # 还没抽取


class ConsolidationTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.extractor = RuleBasedExtractor()

    def test_extracts_semantic_and_working_memories(self) -> None:
        self.runtime.observe(self.make_turn("我叫林鸥"), self.scope, "k1")
        self.runtime.observe(self.make_turn("我们约好周末去海边"), self.scope, "k2")
        self.runtime.observe(self.make_turn("我喜欢睡前听你讲故事"), self.scope, "k3")

        report = self.runtime.run_consolidation(self.extractor)

        self.assertEqual(report.claimed, 3)
        self.assertEqual(report.created, 3)
        self.assertEqual(report.failed, 0)
        # 抽取完会自动排一条去重任务，这是设计使然，不是漏处理
        self.assertEqual(self.runtime.pending_jobs(), 1)
        self.runtime.run_consolidation()  # 不传抽取器也能跑去重
        self.assertEqual(self.runtime.pending_jobs(), 0)

        types = {item.memory_type for item in self.repos.memories.list_by_scope(self.scope)}
        self.assertEqual(types, {MemoryType.SEMANTIC, MemoryType.WORKING})

    def test_rerun_does_not_duplicate_memories(self) -> None:
        observed = self.runtime.observe(self.make_turn("我叫林鸥"), self.scope, "k1")
        self.runtime.run_consolidation(self.extractor)

        # 手工再排一个任务，指向同一条事件——模拟任务重试或抽取器重跑
        with self.runtime.db.transaction():
            self.repos.jobs.enqueue(
                "extract",
                self.scope,
                {"event_id": observed.event.event_id},
                self.clock.now(),
            )
        report = self.runtime.run_consolidation(self.extractor)

        self.assertEqual(report.created, 0)
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(self.count_rows("memories"), 1)

    def test_intention_is_not_promoted_to_occurred_fact(self) -> None:
        """「我们约好去海边」是意愿，不是已经发生的事。"""
        self.runtime.observe(self.make_turn("我们约好周末去海边"), self.scope, "k1")
        self.runtime.run_consolidation(self.extractor)

        commitment = next(
            item
            for item in self.repos.memories.list_by_scope(self.scope)
            if item.memory_type is MemoryType.WORKING
        )
        self.assertEqual(commitment.modality, Modality.INTENTION)
        self.assertEqual(commitment.epistemic_status, EpistemicStatus.USER_REPORT)

    def test_character_speech_is_not_extracted(self) -> None:
        """角色自己说的话不能当成关于用户的事实来源。"""
        self.runtime.observe(
            self.make_turn("我叫小满，我喜欢听你说话", speaker="character"),
            self.scope,
            "k1",
        )
        report = self.runtime.run_consolidation(self.extractor)

        self.assertEqual(report.created, 0)
        self.assertEqual(self.count_rows("memories"), 0)

    def test_evidence_links_back_to_source_event(self) -> None:
        observed = self.runtime.observe(self.make_turn("我喜欢猫"), self.scope, "k1")
        self.runtime.run_consolidation(self.extractor)

        memory = self.repos.memories.list_by_scope(self.scope)[0]
        self.assertEqual(memory.source_event_ids, [observed.event.event_id])
        self.assertEqual(memory.source_speaker, "user")

    def test_negated_preference_is_not_flipped(self) -> None:
        """「我不喜欢猫」不能被抽成「喜欢猫」——这是规则抽取最典型的坑。"""
        self.runtime.observe(self.make_turn("我不喜欢猫"), self.scope, "k1")
        self.runtime.run_consolidation(self.extractor)

        memory = self.repos.memories.list_by_scope(self.scope)[0]
        self.assertEqual(memory.body["predicate"], "不喜欢")


if __name__ == "__main__":
    unittest.main()
