"""跨用户恢复和删除传播的行为回归，不依赖真实模型服务。"""

from dataclasses import replace
from datetime import timedelta

from memorydreaming.errors import ScopeNotFound
from memorydreaming.models import (
    Correction, ForgetSelector, LinkRelation, MemoryStatus, MemoryType, Scope,
)
from tests.helpers import RuntimeTestCase


class ActivityIsolationTest(RuntimeTestCase):
    def test_same_character_does_not_allow_another_users_activity(self):
        other = Scope("another-user", self.scope.character_id)
        activity = self.runtime.register_activity(other, "另一个用户的私密故事")
        result = self.runtime.bootstrap(
            self.scope.user_id, self.scope.character_id, activity.activity_id
        )
        self.assertIsNone(result.selected_activity)

    def test_observe_rejects_foreign_or_unknown_activity_before_writing(self):
        other = Scope("another-user", self.scope.character_id)
        activity = self.runtime.register_activity(other, "私密故事")
        for activity_id in (activity.activity_id, "missing-activity"):
            with self.subTest(activity_id=activity_id), self.assertRaises(ScopeNotFound):
                self.runtime.observe(
                    self.make_turn("不应写入", activity_id=activity_id), self.scope, activity_id
                )
        self.assertEqual(self.count_rows("events"), 0)
        self.assertEqual(self.count_rows("consolidation_jobs"), 0)
        self.assertIsNone(self.repos.activities.get(activity.activity_id).last_event_at)

    def test_observe_rejects_conflicting_activity_ids(self):
        first = self.runtime.register_activity(self.scope, "故事")
        second = self.runtime.register_activity(self.scope, "学习")
        with self.assertRaises(ValueError):
            self.runtime.observe(
                self.make_turn("继续", activity_id=first.activity_id),
                replace(self.scope, activity_id=second.activity_id), "turn",
            )
        self.assertEqual(self.count_rows("events"), 0)


class DeletionGraphTest(RuntimeTestCase):
    def _node(self, name):
        return self.add_memory(MemoryType.EPISODIC, {"what_happened": name})

    def _derive(self, child, *parents):
        with self.runtime.db.transaction():
            for parent in parents:
                self.repos.links.add(
                    child.memory_id, parent.memory_id,
                    LinkRelation.DERIVED_FROM, self.clock.now(),
                )

    def test_unequal_paths_do_not_leave_unsupported_descendant(self):
        root, a, b, c, d = [self._node(name) for name in ("root", "a", "b", "c", "d")]
        self._derive(a, root)
        self._derive(d, root)
        self._derive(b, d)
        self._derive(c, a, b)
        result = self.runtime.forget(ForgetSelector(memory_ids=[root.memory_id]))
        self.assertEqual(set(result.invalidated_derived), {x.memory_id for x in (a, b, c, d)})
        for item in (a, b, c, d):
            self.assertEqual(self.repos.memories.get(item.memory_id).status, MemoryStatus.RETRACTED)

    def test_cycle_cannot_support_itself_after_external_source_is_deleted(self):
        source, a, b = [self._node(name) for name in ("source", "a", "b")]
        self._derive(a, source, b)
        self._derive(b, a)
        result = self.runtime.forget(ForgetSelector(memory_ids=[source.memory_id]))
        self.assertEqual(set(result.invalidated_derived), {a.memory_id, b.memory_id})

    def test_cycle_remains_when_an_independent_source_survives(self):
        source, independent, a, b = [self._node(name) for name in ("source", "independent", "a", "b")]
        self._derive(a, source, b)
        self._derive(b, a, independent)
        result = self.runtime.forget(ForgetSelector(memory_ids=[source.memory_id]))
        self.assertEqual(result.invalidated_derived, [])
        self.assertEqual(self.repos.memories.get(a.memory_id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.repos.memories.get(b.memory_id).status, MemoryStatus.ACTIVE)

    def test_scope_forget_is_not_limited_by_retrieval_candidate_count(self):
        with self.runtime.db.transaction():
            for index in range(1001):
                self._node(f"event-{index}")
        result = self.runtime.forget(ForgetSelector(scope=self.scope))
        self.assertEqual(len(result.deleted_ids), 1001)
        self.assertEqual(self.repos.memories.count_active(self.scope), 0)

    def test_explicit_ids_still_respect_supplied_scope(self):
        item = self._node("private")
        result = self.runtime.forget(ForgetSelector(
            memory_ids=[item.memory_id], scope=Scope("other-user", self.scope.character_id)
        ))
        self.assertEqual(result.deleted_ids, [])
        self.assertEqual(self.repos.memories.get(item.memory_id).status, MemoryStatus.ACTIVE)


class SummaryInvalidationTest(RuntimeTestCase):
    def _build_topic(self):
        items = [self.add_memory(MemoryType.EPISODIC, {"what_happened": text}) for text in (
            "第一次去海边捡贝壳", "周末去海边带相机", "晚上在海边看日落",
        )]
        self.runtime.build_summaries(self.scope)
        card = self.repos.summaries.list_active(self.scope)[0]
        return items, card

    def test_forget_immediately_hides_digest_without_rebuild(self):
        items, card = self._build_topic()
        self.runtime.forget(ForgetSelector(memory_ids=[items[0].memory_id]))
        self.assertEqual(self.repos.summaries.get(card.summary_id).status, "stale")
        self.assertEqual(self.runtime.expand_summary(card.summary_id), [])
        restored = self.runtime.bootstrap(self.scope.user_id, self.scope.character_id)
        self.assertEqual(restored.summaries, [])

    def test_fix_and_change_immediately_invalidate_digest(self):
        for kind in ("fix", "change"):
            with self.subTest(kind=kind):
                items, card = self._build_topic()
                self.runtime.correct(items[-1].memory_id, Correction(
                    kind=kind, statement="纠正经历", corrected_body={"what_happened": "在家看书"}
                ))
                self.assertEqual(self.repos.summaries.get(card.summary_id).status, "stale")


class ValidityTest(RuntimeTestCase):
    def test_future_fact_is_not_current(self):
        item = self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "称呼", "obj": "新名字"},
            valid_from=self.clock.now() + timedelta(days=1),
        )
        self.assertFalse(item.is_current(self.clock.now()))
