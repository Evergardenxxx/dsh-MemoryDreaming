"""纠错与删除测试：区分「当时错了」和「后来变了」。

这两个动作的差别不在于记录怎么改，而在于**历史还能不能被解释**：
    fix    → 旧记录撤回，派生内容失效（它本来就不该存在）
    change → 旧记录留着并标上截止时间（它当时是对的）
"""

from __future__ import annotations

import unittest

from memorydreaming.models import (
    Correction,
    ForgetSelector,
    LinkRelation,
    MemoryStatus,
    MemoryType,
)

from tests.helpers import RuntimeTestCase


class CorrectionTest(RuntimeTestCase):
    def test_fix_retracts_source_and_derived(self) -> None:
        source = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "养了", "obj": "一只猫"},
        )
        derived = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "需要买", "obj": "猫粮"},
        )
        with self.runtime.db.transaction():
            self.repos.links.add(
                derived.memory_id, source.memory_id, LinkRelation.DERIVED_FROM, self.clock.now()
            )

        result = self.runtime.correct(
            source.memory_id,
            Correction(kind="fix", statement="其实我没养猫，之前说错了"),
        )

        self.assertEqual(
            self.repos.memories.get(source.memory_id).status, MemoryStatus.RETRACTED
        )
        self.assertEqual(
            self.repos.memories.get(derived.memory_id).status, MemoryStatus.RETRACTED
        )
        self.assertEqual(result.invalidated_derived, [derived.memory_id])

    def test_change_closes_validity_and_creates_new_record(self) -> None:
        old = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "猫"},
        )

        result = self.runtime.correct(
            old.memory_id,
            Correction(
                kind="change",
                statement="现在更喜欢狗",
                corrected_body={"subject": "用户", "predicate": "喜欢", "obj": "狗"},
            ),
        )

        reloaded = self.repos.memories.get(old.memory_id)
        self.assertEqual(reloaded.status, MemoryStatus.SUPERSEDED)
        self.assertIsNotNone(reloaded.valid_to)  # 旧事实有了截止时间

        new = result.new_memory
        self.assertIsNotNone(new)
        self.assertEqual(new.status, MemoryStatus.ACTIVE)
        self.assertEqual(new.supersedes_id, old.memory_id)
        self.assertEqual(new.body["obj"], "狗")

        # 取代关系要落到链接表，便于以后追溯
        self.assertEqual(self.repos.links.targets_of(new.memory_id), [old.memory_id])

    def test_old_value_is_labelled_as_history_when_retrieved(self) -> None:
        old = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "猫"},
        )
        self.runtime.correct(
            old.memory_id,
            Correction(
                kind="change",
                statement="现在更喜欢狗",
                corrected_body={"subject": "用户", "predicate": "喜欢", "obj": "狗"},
            ),
        )

        result = self.runtime.retrieve("我喜欢什么", self.scope)
        notes = {item.memory_id: (item.valid_now, item.note) for item in result.items}

        self.assertIn(old.memory_id, notes)
        valid_now, note = notes[old.memory_id]
        self.assertFalse(valid_now)
        self.assertIn("已被更新", note)

        new_item = next(item for item in result.items if item.memory_id != old.memory_id)
        self.assertTrue(new_item.valid_now)

    def test_invalid_kind_is_rejected(self) -> None:
        item = self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "猫"}
        )
        with self.assertRaises(Exception):
            self.runtime.correct(item.memory_id, Correction(kind="update"))


class ForgetTest(RuntimeTestCase):
    def test_forget_removes_from_search_and_invalidates_derived(self) -> None:
        source = self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "海边"}
        )
        derived = self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "想去", "obj": "海边"}
        )
        with self.runtime.db.transaction():
            self.repos.links.add(
                derived.memory_id, source.memory_id, LinkRelation.DERIVED_FROM, self.clock.now()
            )

        result = self.runtime.forget(
            ForgetSelector(memory_ids=[source.memory_id], reason="用户要求删除")
        )

        self.assertEqual(result.deleted_ids, [source.memory_id])
        self.assertEqual(result.invalidated_derived, [derived.memory_id])
        self.assertEqual(
            self.repos.memories.get(derived.memory_id).status, MemoryStatus.RETRACTED
        )

        search = self.runtime.retrieve("海边", self.scope)
        self.assertNotIn(source.memory_id, search.memory_ids())

    def test_forget_by_scope_and_type(self) -> None:
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "猫"})
        self.add_memory(MemoryType.EPISODIC, {"what_happened": "一起去海边"})

        result = self.runtime.forget(
            ForgetSelector(scope=self.scope, memory_types=[MemoryType.SEMANTIC])
        )

        self.assertEqual(len(result.deleted_ids), 1)
        remaining = self.repos.memories.list_by_scope(self.scope)
        self.assertEqual([item.memory_type for item in remaining], [MemoryType.EPISODIC])


if __name__ == "__main__":
    unittest.main()
