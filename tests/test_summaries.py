"""主题摘要测试：什么时候该生成索引卡，什么时候该失效。

摘要最容易出问题的地方不是「生成不出来」，而是「生成了但没人用」或者
「来源变了卡还在」。所以用例围绕三件事：生成条件、检索接线、失效传播。
"""

from __future__ import annotations

import unittest

from memorydreaming.models import ForgetSelector, MemoryType

from tests.helpers import RuntimeTestCase


class SummaryBuildTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 三条都围绕「海边」，但分属三种记忆类型——主题本来就该跨类型
        self.beach_episode = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "第一次去海边，捡了一整袋贝壳"},
            hours_ago=24 * 20,
            importance=0.7,
        )
        self.beach_plan = self.add_memory(
            MemoryType.WORKING,
            {"goal": "周末去海边", "open_loops": ["带上相机"]},
            hours_ago=24 * 3,
            importance=0.7,
        )
        self.beach_like = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "海边的日落"},
            hours_ago=24 * 15,
            importance=0.6,
        )
        # 两条无关记忆，不该被卷进来
        self.name = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "名字", "obj": "林鸥"},
            hours_ago=24 * 20,
            importance=0.9,
        )

    def test_builds_one_card_from_cross_type_memories(self) -> None:
        count = self.runtime.build_summaries(self.scope)

        self.assertEqual(count, 1)
        card = self.repos.summaries.list_active(self.scope)[0]
        self.assertEqual(card.topic, "海边")
        self.assertIn("海边", card.digest)
        self.assertEqual(
            set(card.source_memory_ids),
            {self.beach_episode.memory_id, self.beach_plan.memory_id, self.beach_like.memory_id},
        )
        self.assertLessEqual(len(card.digest), 200)

    def test_no_card_when_topic_has_too_few_memories(self) -> None:
        """两条记忆凑不成主题：生成的「摘要」只是换个说法重复一遍。"""
        self.runtime.forget(ForgetSelector(memory_ids=[self.beach_like.memory_id]))

        count = self.runtime.build_summaries(self.scope)

        self.assertEqual(count, 0)
        self.assertEqual(self.repos.summaries.list_active(self.scope), [])

    def test_rebuild_keeps_the_same_card_id(self) -> None:
        """重建摘要应当是更新同一张卡，而不是再发一张——外部引用不能失效。"""
        self.runtime.build_summaries(self.scope)
        first = self.repos.summaries.list_active(self.scope)[0]

        self.runtime.build_summaries(self.scope)
        second = self.repos.summaries.list_active(self.scope)[0]

        self.assertEqual(first.summary_id, second.summary_id)
        self.assertEqual(len(self.repos.summaries.list_active(self.scope)), 1)

    def test_topic_disappearing_marks_card_stale(self) -> None:
        self.runtime.build_summaries(self.scope)
        card = self.repos.summaries.list_active(self.scope)[0]

        # 只剩两条时主题不再成立，卡片应失效而不是继续给出过时概述
        self.runtime.forget(
            ForgetSelector(memory_ids=[self.beach_episode.memory_id, self.beach_like.memory_id])
        )
        self.runtime.build_summaries(self.scope)

        self.assertEqual(self.repos.summaries.list_active(self.scope), [])
        self.assertEqual(self.repos.summaries.get(card.summary_id).status, "stale")


class SummaryRetrievalTest(SummaryBuildTest):
    def test_broad_query_returns_card_and_expanded_sources(self) -> None:
        self.runtime.build_summaries(self.scope)

        result = self.runtime.retrieve("海边那次的事还记得吗", self.scope)

        self.assertEqual(result.plan.expansion_mode, "summary_then_expand")
        self.assertEqual([hit.summary.topic for hit in result.summaries], ["海边"])
        # 卡片上的来源应作为候选被带出来，并标明来自摘要展开
        self.assertTrue(any("summary_expand" in item.channels for item in result.items))

    def test_recent_query_does_not_use_summaries(self) -> None:
        """接着说的时候给索引卡是绕远路，应该直接给那几条原始记录。"""
        self.runtime.build_summaries(self.scope)

        result = self.runtime.retrieve("我们接着聊吧", self.scope)

        self.assertEqual(result.plan.expansion_mode, "detail")
        self.assertEqual(result.summaries, [])

    def test_expand_summary_returns_sources(self) -> None:
        self.runtime.build_summaries(self.scope)
        card = self.repos.summaries.list_active(self.scope)[0]

        items = self.runtime.expand_summary(card.summary_id)

        self.assertEqual(len(items), 3)
        self.assertEqual(
            {item.memory_id for item in items},
            {self.beach_episode.memory_id, self.beach_plan.memory_id, self.beach_like.memory_id},
        )
        self.assertTrue(all("summary_expand" in item.channels for item in items))

    def test_bootstrap_carries_topic_index(self) -> None:
        self.runtime.build_summaries(self.scope)

        bootstrap = self.runtime.bootstrap(self.scope.user_id, self.scope.character_id)

        self.assertEqual([card.topic for card in bootstrap.summaries], ["海边"])


if __name__ == "__main__":
    unittest.main()
