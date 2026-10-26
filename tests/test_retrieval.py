"""检索测试：时间解析、多标签路由、48 小时分级、硬时间约束、预算。

这些用例对应架构文档第 8 节的验收场景，能直接回答
「这种问法到底会不会找对」这个问题。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from memorydreaming.models import MemoryType
from memorydreaming.retrieval.timeexpr import parse_time_hint

from tests.helpers import NOW, RuntimeTestCase


class TimeExpressionTest(RuntimeTestCase):
    def test_yesterday_resolves_to_local_day(self) -> None:
        hint = parse_time_hint("昨天我们聊了什么", NOW, self.tz)

        self.assertIsNotNone(hint)
        self.assertTrue(hint.hard)
        self.assertEqual(hint.precision, "day")
        # 昨天（Shanghai 10-19）从当地 00:00 开始，即 UTC 10-18 16:00
        self.assertEqual(hint.start, datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(hint.end, datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc))

    def test_explicit_date_is_hard_constraint(self) -> None:
        hint = parse_time_hint("10月3日那天说的事", NOW, self.tz)

        self.assertIsNotNone(hint)
        self.assertTrue(hint.hard)
        self.assertEqual(hint.start, datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc))

    def test_vague_reference_has_no_range(self) -> None:
        """「上次」不知道是哪次，不能凭空给出范围，否则会漏掉真正相关的旧事。"""
        hint = parse_time_hint("上次答应的那件事", NOW, self.tz)

        self.assertIsNotNone(hint)
        self.assertFalse(hint.hard)
        self.assertIsNone(hint.start)

    def test_no_time_expression(self) -> None:
        self.assertIsNone(parse_time_hint("你喜欢什么", NOW, self.tz))


class PlannerTest(RuntimeTestCase):
    def test_multilabel_scores_are_not_normalised(self) -> None:
        """四类分数表达「有没有用」，不要求和为 1。"""
        result = self.runtime.retrieve("上次答应的睡前故事，今天接着讲吧", self.scope)
        scores = result.plan.type_scores

        self.assertGreater(scores[MemoryType.WORKING], 0.5)   # 答应、接着
        self.assertGreater(scores[MemoryType.EPISODIC], 0.5)  # 上次、故事
        self.assertLess(sum(scores.values()), 4.0)

    def test_prospective_time_word_is_not_a_filter(self) -> None:
        """「今天接着讲」里的今天是说话时刻，不该过滤事件时间。"""
        result = self.runtime.retrieve("上次答应的睡前故事，今天接着讲吧", self.scope)

        self.assertFalse(result.plan.hard_time)
        self.assertEqual(result.plan.expansion_mode, "detail")

    def test_high_score_types_get_more_budget(self) -> None:
        result = self.runtime.retrieve("上次答应的睡前故事", self.scope)
        budgets = result.plan.per_type_budget

        self.assertGreater(budgets[MemoryType.EPISODIC], budgets[MemoryType.PROCEDURAL])


class RetrievalTest(RuntimeTestCase):
    def test_recent_memory_is_preferred_within_48h(self) -> None:
        recent = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "今天聊到海边的约定"},
            hours_ago=2,
        )

        result = self.runtime.retrieve("我们聊了什么", self.scope)

        self.assertEqual(result.memory_ids()[0], recent.memory_id)
        self.assertEqual(result.items[0].memory.memory_type, MemoryType.EPISODIC)

    def test_old_memory_is_still_reachable_by_keywords(self) -> None:
        """十天后问「第一次取名字时的原话」，仍然要能定位到那条旧记忆。"""
        old = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "第一次给你取名字，你说叫小满"},
            hours_ago=24 * 10,
        )
        self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "睡前听故事"},
            hours_ago=1,
        )

        result = self.runtime.retrieve("第一次给你取名字的原话是什么", self.scope)

        self.assertIn(old.memory_id, result.memory_ids())
        # 软时间提示 → 先摘要后回源，而不是精确回查
        self.assertEqual(result.plan.expansion_mode, "summary_then_expand")

    def test_hard_time_range_excludes_other_days(self) -> None:
        """用户说「昨天」就只给昨天，别的日期即使关键词命中也要剔除。"""
        yesterday = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "我们聊到海边的约定"},
            occurred_at=datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc),  # 上海 10-19 10:00
        )
        self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "我们聊到新买的书"},
            occurred_at=NOW,  # 上海 10-20 20:00
        )

        result = self.runtime.retrieve("昨天我们聊了什么", self.scope)

        self.assertEqual(result.memory_ids(), [yesterday.memory_id])
        self.assertTrue(result.plan.hard_time)

    def test_empty_result_reports_gap(self) -> None:
        """没有证据时必须说「没找到」，不能留白让模型自己编。"""
        result = self.runtime.retrieve("量子力学的多世界诠释", self.scope)

        self.assertEqual(result.items, [])
        self.assertTrue(result.gaps)

    def test_budget_limits_number_of_items(self) -> None:
        for index in range(10):
            self.add_memory(
                MemoryType.EPISODIC,
                {"what_happened": f"第{index}次一起去海边"},
                hours_ago=index,
            )

        result = self.runtime.retrieve("海边", self.scope, max_items=3)

        self.assertLessEqual(len(result.items), 3)
        self.assertGreater(result.trace.token_estimate, 0)

    def test_retrieval_does_not_inflate_access_count(self) -> None:
        """检索不等于「被用上」，访问次数只能由宿主显式标记。"""
        item = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "猫"})

        self.runtime.retrieve("猫", self.scope)
        self.assertEqual(self.repos.memories.get(item.memory_id).access_count, 0)

        self.runtime.mark_used([item.memory_id])
        self.assertEqual(self.repos.memories.get(item.memory_id).access_count, 1)


if __name__ == "__main__":
    unittest.main()
