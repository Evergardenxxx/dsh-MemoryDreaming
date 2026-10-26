"""整理的边界回归：可执行任务、事实归因与乱序历史写入。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import (
    EpistemicStatus,
    EventRecord,
    MemoryDraft,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    Modality,
    new_id,
)

from tests.helpers import RuntimeTestCase


class SupportedJobsTest(RuntimeTestCase):
    def test_unhandled_old_jobs_do_not_starve_executable_jobs(self) -> None:
        for _ in range(3):
            self.repos.jobs.enqueue(
                "extract", self.scope, {}, self.clock.now() - timedelta(minutes=2)
            )
        future_job = self.repos.jobs.enqueue(
            "propose_core", self.scope, {}, self.clock.now() - timedelta(minutes=1)
        )
        dedupe_job = self.repos.jobs.enqueue("dedupe", self.scope, {}, self.clock.now())

        report = self.runtime.run_consolidation(limit=2)

        self.assertEqual(report.claimed, 1)
        self.assertEqual(report.skipped, 4)
        self.assertEqual(report.failed, 0)
        states = {
            row["job_id"]: row
            for row in self.runtime.db.connection.execute(
                "SELECT job_id, status, attempts FROM consolidation_jobs"
            )
        }
        self.assertEqual(states[dedupe_job]["status"], "done")
        self.assertEqual(states[future_job]["status"], "pending")
        self.assertTrue(all(
            row["attempts"] == 0
            for row in states.values() if row["status"] == "pending"
        ))


class ConservativeExtractionTest(RuntimeTestCase):
    def _extract(self, text: str) -> list[MemoryDraft]:
        event = EventRecord(
            event_id="evt-test", scope=self.scope, conversation_id="conversation",
            speaker="user", content=text, occurred_at=self.clock.now(),
        )
        return RuleBasedExtractor().extract(event)

    def test_quoted_hypothetical_questions_and_complex_negation_are_not_facts(self) -> None:
        for text in (
            "如果我喜欢猫呢", "她说：我喜欢猫。", "她说，我喜欢猫。",
            "“我喜欢猫”是她说的。", "我喜欢猫吗？", "我喜欢什么",
            "我不是不喜欢猫", "比如我叫阿鸥。", "如果我叫阿鸥，你会记住吗？",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._extract(text), [])

    def test_multiple_preferences_have_distinct_stable_keys(self) -> None:
        text = "我喜欢猫。我喜欢狗，我不喜欢下雨。"
        drafts = self._extract(text)
        self.assertEqual([draft.body["obj"] for draft in drafts], ["猫", "狗", "下雨"])
        keys = [draft.candidate_key for draft in drafts]
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(keys, [draft.candidate_key for draft in self._extract(text)])
        self.assertEqual(drafts[-1].body["predicate"], "不喜欢")

    def test_preferred_address_does_not_also_change_name(self) -> None:
        drafts = self._extract("我叫林鸥，你以后叫我阿鸥就行")
        self.assertEqual(
            [(draft.body["predicate"], draft.body["obj"]) for draft in drafts],
            [("称呼", "林鸥"), ("希望被称呼为", "阿鸥")],
        )

    def test_direct_preference_can_mention_someone_speaking(self) -> None:
        drafts = self._extract("我喜欢听你说话。")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].body["obj"], "听你说话")

    def test_multiple_preferences_survive_storage_and_retry(self) -> None:
        event = self.runtime.observe(
            self.make_turn("我喜欢猫。我喜欢狗。"), self.scope, "multi-preference"
        ).event
        self.runtime.run_consolidation(RuleBasedExtractor())
        self.repos.jobs.enqueue(
            "extract", self.scope, {"event_id": event.event_id}, self.clock.now()
        )
        report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual(report.created, 0)
        self.assertEqual(report.duplicates, 2)
        self.assertEqual(self.count_rows("memories"), 2)


class TemporalConflictTest(RuntimeTestCase):
    def test_importing_older_event_preserves_current_fact_and_bounds_history(self) -> None:
        self.runtime.observe(self.make_turn("我叫新名字"), self.scope, "new")
        self.runtime.run_consolidation(RuleBasedExtractor())
        current = self.repos.memories.list_by_scope(self.scope)[0]

        old_turn = self.make_turn("我叫旧名字")
        old_turn.occurred_at -= timedelta(days=10)
        self.runtime.observe(old_turn, self.scope, "historical")
        self.runtime.run_consolidation(RuleBasedExtractor())

        current = self.repos.memories.get(current.memory_id)
        self.assertEqual(current.status, MemoryStatus.ACTIVE)
        self.assertIsNone(current.valid_to)
        history = next(
            item for item in self.repos.memories.list_by_scope(self.scope)
            if item.body.get("obj") == "旧名字"
        )
        self.assertEqual(history.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(history.valid_to, self.clock.now())
        self.assertLess(history.occurred_start, history.valid_to)

    def test_nonfactual_or_inferred_candidate_cannot_expire_current_fact(self) -> None:
        current = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "称呼", "obj": "真实名字"},
            hours_ago=2,
        )
        for index, (modality, epistemic_status) in enumerate((
            (Modality.HYPOTHETICAL, EpistemicStatus.USER_REPORT),
            (Modality.INTENTION, EpistemicStatus.USER_REPORT),
            (Modality.OCCURRED, EpistemicStatus.INFERRED),
        )):
            class CandidateExtractor:
                version = "candidate-test"

                def extract(self, event: EventRecord) -> list[MemoryDraft]:
                    return [MemoryDraft(
                        memory_type=MemoryType.SEMANTIC,
                        body={"subject": "用户", "predicate": "称呼", "obj": "其他名字"},
                        candidate_key="name", modality=modality,
                        epistemic_status=epistemic_status,
                    )]

            self.runtime.observe(self.make_turn("候选输入"), self.scope, f"candidate-{index}")
            self.runtime.run_consolidation(CandidateExtractor())
            reloaded = self.repos.memories.get(current.memory_id)
            self.assertEqual(reloaded.status, MemoryStatus.ACTIVE)
            self.assertIsNone(reloaded.valid_to)

    def test_old_fact_beyond_two_hundred_records_is_still_checked(self) -> None:
        current = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "称呼", "obj": "旧名字"},
            hours_ago=48,
        )
        for index in range(205):
            self.add_memory(
                MemoryType.SEMANTIC,
                {"subject": "其他实体", "predicate": "属性", "obj": str(index)},
                hours_ago=1,
            )
        self.runtime.observe(self.make_turn("我叫新名字"), self.scope, "new-name")
        self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual(
            self.repos.memories.get(current.memory_id).status, MemoryStatus.SUPERSEDED
        )


class DedupeIsolationTest(RuntimeTestCase):
    def _insert(self, memory_type: MemoryType, body: dict, **changes: object) -> MemoryItem:
        memory = MemoryItem(
            memory_id=new_id("mem"), memory_type=memory_type, scope=self.scope,
            body=body, source_speaker="user", occurred_start=self.clock.now(),
            recorded_at=self.clock.now(),
        )
        memory = replace(memory, **changes)
        self.repos.memories.insert(memory)
        return memory

    def _assert_separate(self, first: MemoryItem, second: MemoryItem) -> None:
        self.repos.jobs.enqueue("dedupe", self.scope, {}, self.clock.now())
        report = self.runtime.run_consolidation()
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.merged, 0)
        for memory in (first, second):
            self.assertEqual(
                self.repos.memories.get(memory.memory_id).status, MemoryStatus.ACTIVE
            )

    def test_identical_working_notes_in_different_activities_do_not_merge(self) -> None:
        body = {"goal": "继续给用户讲睡前故事", "open_loops": ["接着讲下一章"]}
        first = self._insert(
            MemoryType.WORKING, body, scope=replace(self.scope, activity_id="story-a")
        )
        second = self._insert(
            MemoryType.WORKING, body, scope=replace(self.scope, activity_id="story-b")
        )
        self._assert_separate(first, second)

    def test_intention_does_not_merge_into_occurred_event(self) -> None:
        body = {"what_happened": "周末一起去海边带相机看日落"}
        first = self._insert(MemoryType.EPISODIC, body)
        second = self._insert(MemoryType.EPISODIC, body, modality=Modality.INTENTION)
        self._assert_separate(first, second)

    def test_near_identical_opposite_preferences_do_not_merge(self) -> None:
        body = {
            "subject": "用户", "predicate": "喜欢", "obj": "周末在安静的阳台上看书喝茶听音乐",
            "scope_note": "这是用户明确表述的一项长期生活偏好",
        }
        first = self._insert(MemoryType.SEMANTIC, body)
        second = self._insert(MemoryType.SEMANTIC, {**body, "predicate": "不喜欢"})
        self._assert_separate(first, second)
