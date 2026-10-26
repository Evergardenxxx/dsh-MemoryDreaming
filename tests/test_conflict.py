"""写时冲突消解与删除传播。

这两块直接借鉴了两个成熟项目的做法：

- **写时失效**（Zep / Graphiti）：新事实与旧事实冲突时，不删除旧的，
  只关闭它的有效期。历史仍然可查，但不再是「当前值」。
- **删除前检查其它来源**（Zep 的 remove_episode）：一条派生事实只有在
  **所有**父来源都消失后才应该失效，不能因为删了一个来源就误伤。

同时验证一条边界：不是所有「同一谓词不同宾语」都算冲突。
「喜欢猫」和「喜欢狗」可以同时成立，历史是时间线，不是一份可覆盖的档案
（Mem0 现行实现的说法）。
"""

from __future__ import annotations

import unittest

from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import (
    ForgetSelector,
    LinkRelation,
    MemoryStatus,
    MemoryType,
)

from tests.helpers import RuntimeTestCase


class ConflictResolutionTest(RuntimeTestCase):
    def _say(self, text: str, key: str) -> None:
        self.runtime.observe(self.make_turn(text), self.scope, key)
        self.runtime.run_consolidation(RuleBasedExtractor())

    def _semantic_by_predicate(self, predicate: str) -> list:
        return [
            item
            for item in self.repos.memories.list_by_scope(self.scope, statuses=None)
            if item.memory_type is MemoryType.SEMANTIC and item.body.get("predicate") == predicate
        ]

    def test_single_valued_predicate_closes_the_old_window(self) -> None:
        self._say("我叫林鸥", "k1")
        old = self._semantic_by_predicate("称呼")[0]

        self._say("我叫林小鸥", "k2")
        records = self._semantic_by_predicate("称呼")

        self.assertEqual(len(records), 2)
        reloaded_old = self.repos.memories.get(old.memory_id)
        new = next(item for item in records if item.memory_id != old.memory_id)

        self.assertEqual(reloaded_old.status, MemoryStatus.SUPERSEDED)
        self.assertIsNotNone(reloaded_old.valid_to)  # 有效期被关闭，而不是被删除
        self.assertEqual(new.status, MemoryStatus.ACTIVE)
        self.assertEqual(new.supersedes_id, old.memory_id)
        self.assertEqual(self.repos.links.targets_of(new.memory_id), [old.memory_id])

    def test_parallel_preferences_are_not_conflicts(self) -> None:
        """喜欢猫和喜欢狗可以同时为真，不能互相顶掉。"""
        self._say("我喜欢猫", "k1")
        self._say("我喜欢狗", "k2")

        records = self._semantic_by_predicate("喜欢")
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item.status is MemoryStatus.ACTIVE for item in records))

    def test_negation_supersedes_the_positive_statement(self) -> None:
        self._say("我喜欢猫", "k1")
        self._say("我不喜欢猫", "k2")

        positive = [
            item for item in self._semantic_by_predicate("喜欢") if item.body.get("obj") == "猫"
        ]
        negative = [
            item for item in self._semantic_by_predicate("不喜欢") if item.body.get("obj") == "猫"
        ]

        self.assertEqual(len(positive), 1)
        self.assertEqual(len(negative), 1)
        self.assertEqual(
            self.repos.memories.get(positive[0].memory_id).status, MemoryStatus.SUPERSEDED
        )

    def test_history_is_still_visible_after_supersede(self) -> None:
        """新值生效后，旧值仍然能查到，只是标成历史。"""
        self._say("我叫林鸥", "k1")
        self._say("我叫林小鸥", "k2")

        # 用「称呼」而不是「名字」：记忆里存的谓词是「称呼」，
        # 而当前的分词/嵌入还做不到跨同义词召回（见 docs/open-problems.md）
        result = self.runtime.retrieve("我的称呼是什么", self.scope)
        current = [item for item in result.items if item.valid_now]
        history = [item for item in result.items if not item.valid_now]

        self.assertEqual([item.memory.body["obj"] for item in current], ["林小鸥"])
        self.assertEqual([item.memory.body["obj"] for item in history], ["林鸥"])


class DeletionSupportTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_a = self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "养了", "obj": "一只橘猫"}
        )
        self.source_b = self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "在楼下遇到一只橘猫"}
        )
        self.derived = self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "橘猫"}
        )
        with self.runtime.db.transaction():
            self.repos.links.add(
                self.derived.memory_id, self.source_a.memory_id, LinkRelation.DERIVED_FROM, self.clock.now()
            )
            self.repos.links.add(
                self.derived.memory_id, self.source_b.memory_id, LinkRelation.DERIVED_FROM, self.clock.now()
            )

    def test_derived_survives_while_another_source_exists(self) -> None:
        self.runtime.forget(ForgetSelector(memory_ids=[self.source_a.memory_id]))

        self.assertEqual(
            self.repos.memories.get(self.derived.memory_id).status, MemoryStatus.ACTIVE
        )

    def test_derived_is_retracted_when_last_source_goes(self) -> None:
        self.runtime.forget(ForgetSelector(memory_ids=[self.source_a.memory_id]))
        self.runtime.forget(ForgetSelector(memory_ids=[self.source_b.memory_id]))

        self.assertEqual(
            self.repos.memories.get(self.derived.memory_id).status, MemoryStatus.RETRACTED
        )

    def test_batch_delete_removes_derived_only_when_all_sources_go(self) -> None:
        result = self.runtime.forget(
            ForgetSelector(
                memory_ids=[self.source_a.memory_id, self.source_b.memory_id]
            )
        )

        self.assertEqual(len(result.deleted_ids), 2)
        self.assertIn(self.derived.memory_id, result.invalidated_derived)


if __name__ == "__main__":
    unittest.main()
