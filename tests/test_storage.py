"""存储层测试：幂等、去重、中文检索、软删除。

这一层不涉及业务判断，测试起来最直接：
给它数据，看它能不能原样取回来、该拦的有没有拦住。
"""

from __future__ import annotations

import unittest

from memorydreaming.models import MemoryStatus, MemoryType, new_id
from memorydreaming.retrieval.keyword import KeywordIndex, tokenize_query

from tests.helpers import NOW, RuntimeTestCase


class EventStoreTest(RuntimeTestCase):
    def test_same_idempotency_key_writes_only_once(self) -> None:
        """同一轮消息重复提交，只应留下一条事件、一个任务。"""
        turn = self.make_turn("我喜欢睡前听故事")

        first = self.runtime.observe(turn, self.scope, idempotency_key="conv-1#0")
        second = self.runtime.observe(turn, self.scope, idempotency_key="conv-1#0")

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.event.event_id, second.event.event_id)
        self.assertIsNone(second.job_id)
        self.assertEqual(self.count_rows("events"), 1)
        self.assertEqual(self.count_rows("consolidation_jobs"), 1)

    def test_different_keys_create_separate_events(self) -> None:
        self.runtime.observe(self.make_turn("第一句"), self.scope, "k1")
        self.runtime.observe(self.make_turn("第二句"), self.scope, "k2")
        self.assertEqual(self.count_rows("events"), 2)


class MemoryStoreTest(RuntimeTestCase):
    def test_dedupe_key_prevents_duplicate_memories(self) -> None:
        """同一个 dedupe_key 只能写入一次——这是重跑抽取任务的安全网。"""
        body = {"subject": "用户", "predicate": "喜欢", "obj": "猫"}

        first = self.add_memory(MemoryType.SEMANTIC, body, dedupe_key="evt1:v1:pref")
        second = self.add_memory(MemoryType.SEMANTIC, body, dedupe_key="evt1:v1:pref")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.count_rows("memories"), 1)

    def test_roundtrip_keeps_scope_and_body(self) -> None:
        item = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "一起去海边", "participants": ["用户", "小满"]},
        )
        loaded = self.repos.memories.get(item.memory_id)

        self.assertEqual(loaded.memory_type, MemoryType.EPISODIC)
        self.assertEqual(loaded.scope.user_id, "u1")
        self.assertEqual(loaded.body["participants"], ["用户", "小满"])
        # body_text 是入库时自动生成的检索投影，读回来应该还在
        self.assertIn("一起去海边", loaded.body_text)


class KeywordSearchTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.index = KeywordIndex(self.runtime.db.connection)
        self.target = self.add_memory(
            MemoryType.EPISODIC,
            {"what_happened": "我们约好周末去海边，我要带相机"},
            hours_ago=1,
        )

    def test_two_char_chinese_term_matches(self) -> None:
        """「海边」两个字走 LIKE 通道，必须能命中。"""
        hits = self.index.search(tokenize_query("海边"), "u1", "c1")
        self.assertEqual([hit.memory_id for hit in hits], [self.target.memory_id])

    def test_three_char_chinese_term_matches(self) -> None:
        """「去海边」三个字走 FTS5 trigram 通道。"""
        hits = self.index.search(tokenize_query("去海边"), "u1", "c1")
        self.assertIn(self.target.memory_id, [hit.memory_id for hit in hits])

    def test_scope_isolation(self) -> None:
        """别的角色不能查到这条记忆——可见性过滤必须在检索前生效。"""
        hits = self.index.search(tokenize_query("海边"), "u1", "c2")
        self.assertEqual(hits, [])

    def test_soft_delete_removes_from_index(self) -> None:
        self.repos.memories.delete(self.target.memory_id, NOW)

        hits = self.index.search(tokenize_query("海边"), "u1", "c1")
        self.assertEqual(hits, [])

        loaded = self.repos.memories.get(self.target.memory_id)
        self.assertEqual(loaded.status, MemoryStatus.DELETED)


if __name__ == "__main__":
    unittest.main()
