"""可重建实体索引测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memorydreaming.config import Settings
from memorydreaming.models import MemoryItem, MemoryStatus, MemoryType, Scope, new_id
from memorydreaming.runtime import MemoryRuntime

from tests.helpers import RuntimeTestCase


class EntityIndexTest(RuntimeTestCase):
    def _insert(self, memory_type: MemoryType, body: dict, scope: Scope | None = None) -> MemoryItem:
        target = scope or self.scope
        item = MemoryItem(
            memory_id=new_id("mem"), memory_type=memory_type, scope=target,
            body=body, source_speaker="user", occurred_start=self.clock.now(),
            recorded_at=self.clock.now(),
        )
        with self.runtime.db.transaction():
            inserted = self.repos.memories.insert(item)
        assert inserted is not None
        return inserted

    def test_extracts_structured_entities_from_four_memory_types(self) -> None:
        items = [
            self._insert(MemoryType.WORKING, {"goal": "继续故事", "active_entities": ["灯塔"]}),
            self._insert(MemoryType.EPISODIC, {"what_happened": "讲故事", "participants": ["爸爸"]}),
            self._insert(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "海边"}),
            self._insert(MemoryType.PROCEDURAL, {
                "trigger": "睡前", "steps": ["讲海边故事"], "entities": ["小梦"],
            }),
        ]

        mentions = self.runtime.rebuild_entities(self.scope)

        self.assertEqual(mentions, 5)
        for name, item in (("灯塔", items[0]), ("爸爸", items[1]), ("用户", items[2]),
                           ("海边", items[2]), ("小梦", items[3])):
            result = self.runtime.search_entities(name, self.scope)
            self.assertTrue(result, name)
            self.assertIn(item.memory_id, result[0]["memory_ids"])

    def test_same_scope_entities_merge_and_normalize_whitespace_case(self) -> None:
        first = self._insert(MemoryType.SEMANTIC, {"subject": "  Alice  ", "obj": "猫"})
        second = self._insert(MemoryType.EPISODIC, {
            "participants": ["alice", " Alice ", "", "   ", 123],
        })

        self.runtime.rebuild_entities(self.scope)
        rows = self.runtime.search_entities(" alice ", self.scope)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["canonical_name"], "alice")
        self.assertEqual(rows[0]["mention_count"], 2)
        self.assertEqual(set(rows[0]["memory_ids"]), {first.memory_id, second.memory_id})

        self.repos.entities.remove_memory(second.memory_id)
        self.assertEqual(self.runtime.search_entities("alice", self.scope)[0]["mention_count"], 1)

    def test_relationship_and_activity_scopes_do_not_leak(self) -> None:
        relation_one = Scope("u1", "c1", relationship_id="r1")
        relation_two = Scope("u1", "c1", relationship_id="r2")
        activity_one = Scope("u1", "c1", relationship_id="r1", activity_id="a1")
        activity_two = Scope("u1", "c1", relationship_id="r1", activity_id="a2")
        first = self._insert(MemoryType.SEMANTIC, {"obj": "只在一号关系"}, relation_one)
        second = self._insert(MemoryType.SEMANTIC, {"obj": "只在二号关系"}, relation_two)
        third = self._insert(MemoryType.SEMANTIC, {"obj": "只在一号活动"}, activity_one)
        fourth = self._insert(MemoryType.SEMANTIC, {"obj": "只在二号活动"}, activity_two)

        self.runtime.rebuild_entities(Scope("u1", "c1"))
        self.assertEqual(self.runtime.search_entities("关系", relation_one)[0]["memory_ids"], [first.memory_id])
        self.assertEqual(self.runtime.search_entities("关系", relation_two)[0]["memory_ids"], [second.memory_id])
        self.assertEqual(self.runtime.search_entities("活动", activity_one)[0]["memory_ids"], [third.memory_id])
        self.assertEqual(self.runtime.search_entities("活动", activity_two)[0]["memory_ids"], [fourth.memory_id])

    def test_entity_channel_recalls_memory_and_forget_scope_removes_projection(self) -> None:
        item = self._insert(MemoryType.SEMANTIC, {"subject": "用户", "obj": "月光灯塔"})
        self.runtime.rebuild_entities(self.scope)

        result = self.runtime.retrieve("月光灯塔", self.scope)

        self.assertIn(item.memory_id, result.memory_ids())
        self.assertGreater(result.trace.channel_hits.get("entity", 0), 0)
        self.runtime.forget_scope(self.scope, reason="测试")
        self.assertEqual(self.runtime.search_entities("灯塔", self.scope), [])
        self.assertEqual(self.count_rows("entities"), 0)
        self.assertEqual(self.count_rows("entity_mentions"), 0)

    def test_sentence_query_recalls_embedded_entity_and_normalizes_unicode(self) -> None:
        item = self._insert(MemoryType.SEMANTIC, {"subject": "用户", "obj": "月光灯塔"})
        self.assertEqual(self.runtime.search_entities("你还记得月光灯塔吗", self.scope)[0]["memory_ids"],
                         [item.memory_id])
        result = self.runtime.retrieve("你还记得月光灯塔吗", self.scope)
        self.assertGreater(result.trace.channel_hits.get("entity", 0), 0)
        self.assertIn(item.memory_id, result.memory_ids())
        named = self._insert(MemoryType.SEMANTIC, {"obj": "ＡＬＩＣＥ"})
        self.assertEqual(self.runtime.search_entities("你记得Alice吗", self.scope)[0]["memory_ids"],
                         [named.memory_id])

    def test_sql_wildcard_characters_are_literal_entity_names(self) -> None:
        special = self._insert(MemoryType.SEMANTIC, {"obj": "项目_100%"})
        self._insert(MemoryType.SEMANTIC, {"obj": "海边故事"})
        for query in ("_", "%", "项目_100%"):
            with self.subTest(query=query):
                rows = self.runtime.search_entities(query, self.scope)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["memory_ids"], [special.memory_id])

    def test_repository_rewrite_replaces_mentions_and_retains_other_memories(self) -> None:
        first = self._insert(MemoryType.SEMANTIC, {"obj": "旧灯塔"})
        second = self._insert(MemoryType.SEMANTIC, {"obj": "旧灯塔"})
        with self.runtime.db.transaction():
            self.repos.memories.update_content(first.memory_id, {"obj": "新灯塔"}, self.clock.now())
        self.assertEqual(self.runtime.search_entities("旧灯塔", self.scope)[0]["memory_ids"], [second.memory_id])
        self.assertEqual(self.runtime.search_entities("旧灯塔", self.scope)[0]["mention_count"], 1)
        self.assertEqual(self.runtime.search_entities("新灯塔", self.scope)[0]["memory_ids"], [first.memory_id])
        # 去掉所有结构化实体字段也必须清理旧提及。
        with self.runtime.db.transaction():
            self.repos.memories.update_content(first.memory_id, {"scope_note": "不含实体"}, self.clock.now())
        self.assertEqual(self.runtime.search_entities("新灯塔", self.scope), [])

    def test_repository_retraction_delete_and_restore_keep_entities_in_sync(self) -> None:
        item = self._insert(MemoryType.SEMANTIC, {"obj": "月光灯塔"})
        with self.runtime.db.transaction():
            self.repos.memories.update_status(item.memory_id, MemoryStatus.RETRACTED, self.clock.now())
        self.assertEqual(self.runtime.search_entities("月光灯塔", self.scope), [])
        with self.runtime.db.transaction():
            self.repos.memories.update_status(item.memory_id, MemoryStatus.ACTIVE, self.clock.now())
        self.assertTrue(self.runtime.search_entities("月光灯塔", self.scope))
        with self.runtime.db.transaction():
            self.repos.memories.delete(item.memory_id, self.clock.now())
        self.assertEqual(self.runtime.search_entities("月光灯塔", self.scope), [])
        self.assertEqual(self.count_rows("entity_mentions"), 0)

    def test_failed_rewrite_restores_previous_entity_projection(self) -> None:
        item = self._insert(MemoryType.SEMANTIC, {"obj": "旧灯塔"})
        with self.assertRaises(RuntimeError):
            with self.runtime.db.transaction():
                self.repos.memories.update_content(item.memory_id, {"obj": "新灯塔"}, self.clock.now())
                raise RuntimeError("rollback")
        self.assertEqual(self.runtime.search_entities("旧灯塔", self.scope)[0]["memory_ids"], [item.memory_id])
        self.assertEqual(self.runtime.search_entities("新灯塔", self.scope), [])

    def test_scoped_snapshot_preserves_entity_mentions_and_imports_them(self) -> None:
        item = self._insert(MemoryType.SEMANTIC, {"subject": "用户", "obj": "海边"})
        self.runtime.rebuild_entities(self.scope)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scoped.json"
            self.runtime.export_snapshot(str(path), self.scope)
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(snapshot["tables"]["entities"]), 2)
            self.assertEqual(len(snapshot["tables"]["entity_mentions"]), 2)

            target = MemoryRuntime(Settings(db_path=Path(temp) / "target.db", timezone="Asia/Shanghai"), clock=self.clock)
            try:
                target.db.meta_set("forgetting_hmac_key", self.runtime.db.meta_get("forgetting_hmac_key") or "")
                result = target.import_snapshot(str(path))
                self.assertTrue(result["ready"], result)
                self.assertEqual(target.search_entities("海边", self.scope)[0]["memory_ids"], [item.memory_id])
            finally:
                target.close()


if __name__ == "__main__":
    unittest.main()
