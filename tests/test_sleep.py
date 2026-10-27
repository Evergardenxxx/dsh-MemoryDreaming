"""睡眠冻结、逐批租约审阅、虚构梦境与可逆巩固/遗忘。"""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from memorydreaming.models import ForgetSelector, MemoryStatus, MemoryType, Scope, new_id
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.sleep import SleepError, SleepService
from memorydreaming.storage.database import SCHEMA_VERSION
from tests.helpers import RuntimeTestCase


class SleepServiceTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        # 发布迁移必须自行创建睡眠表，测试不再额外补建。
        self.assertIsNotNone(self.runtime.db.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sleep_runs'").fetchone())
        self.sleep = SleepService(self.runtime)
        self.conversation = "sleep-conversation"

    def observe(self, text, *, speaker="user", scope=None, conversation=None):
        return self.runtime.observe(self.make_turn(text, speaker=speaker, conversation_id=conversation or self.conversation),
            scope or self.scope, new_id("input")).event

    def memory(self, event, text="海边故事", **changes):
        return self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": text},
            source_event_ids=[event.event_id], source_spans={event.event_id: [[0, len(event.content)]]}, **changes)

    def prepare(self, **kwargs):
        return self.sleep.prepare(self.scope, self.conversation, "sleep-command", **kwargs)

    def review_all(self, prepared):
        cursor = prepared["review"]["next_cursor"]
        covered = []
        while cursor is not None:
            batch = self.sleep.batch(self.scope, prepared["sleep_id"], cursor, request_id=f"batch-{cursor}")
            covered.extend(batch["source_event_ids"])
            result = self.sleep.record_review(self.scope, prepared["sleep_id"], cursor,
                [{"memory_type": "semantic", "text": "只整理用户明确表达的偏好，其他内容保留原认识论。",
                  "source_event_ids": batch["source_event_ids"], "epistemic_status": "candidate_review"}],
                batch["source_event_ids"], claim_token=batch["claim_token"])
            self.assertEqual(result["status"], "reviewed")
            cursor = batch["next_cursor"]
        return sorted(set(covered))

    def test_inventory_covers_entire_local_conversation_in_bounded_fragments(self):
        expected = [self.observe("海边"*1800), self.observe("一个助手故事", speaker="character"), self.observe("我喜欢海边故事")]
        self.observe("别的窗口内容", conversation="other-window")
        prepared = self.prepare(batch_chars=800, fragment_chars=400)
        self.assertEqual(prepared["inventory"]["event_count"], 3)
        self.assertGreater(prepared["review"]["batch_count"], 2)
        covered = self.review_all(prepared)
        self.assertEqual(set(covered), {event.event_id for event in expected})
        result = self.sleep.complete(self.scope, prepared["sleep_id"], reviewed_event_ids=covered)
        self.assertTrue(result["review_complete"])
        self.assertEqual(result["review"]["reviewed_batches"], result["review"]["batch_count"])

    def test_sleep_without_all_reviews_refuses_full_completion_and_partial_is_explicit(self):
        self.observe("我喜欢海边故事")
        prepared = self.prepare()
        with self.assertRaises(SleepError) as caught:
            self.sleep.complete(self.scope, prepared["sleep_id"])
        self.assertEqual(caught.exception.code, "SLEEP_REVIEW_INCOMPLETE")
        result = self.sleep.complete(self.scope, prepared["sleep_id"], allow_partial=True)
        self.assertFalse(result["review_complete"])
        self.assertTrue(result["warnings"])
        self.assertEqual(result["consolidation"]["reinforced_memory_ids"], [])

    def test_dream_is_stable_fiction_and_never_becomes_fact_core_or_usage(self):
        event = self.observe("我喜欢海边故事")
        item = self.memory(event, importance=.9)
        before = self.repos.memories.get(item.memory_id)
        prepared = self.prepare()
        self.review_all(prepared)
        result = self.sleep.complete(self.scope, prepared["sleep_id"], request_id="first-transport")
        retry = self.sleep.complete(self.scope, prepared["sleep_id"], request_id="retry-transport")
        self.assertTrue(retry["duplicate"])
        self.assertEqual(result["dream"], retry["dream"])
        self.assertEqual(result["dream"]["kind"], "dream")
        self.assertEqual(result["dream"]["epistemic_status"], "generated_fiction")
        self.assertEqual(result["dream"]["modality"], "fictional")
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.count_rows("core_versions"), 0)
        after = self.repos.memories.get(item.memory_id)
        self.assertEqual(after.body, before.body)
        self.assertEqual(after.epistemic_status, before.epistemic_status)
        self.assertEqual(after.importance, before.importance)
        self.assertEqual(after.access_count, before.access_count)
        self.assertEqual(after.last_used_at, before.last_used_at)
        self.assertGreater(after.retrieval_strength, before.retrieval_strength)
        self.assertEqual(result["consolidation"]["facts_created"], 0)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_fragment_review_validates_coverage_types_and_epistemic_status(self):
        self.observe("我喜欢海边故事")
        prepared = self.prepare()
        batch = self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="review")
        args = (self.scope, prepared["sleep_id"], 0)
        with self.assertRaises(SleepError):
            self.sleep.record_review(*args, [], ["not-source"], claim_token=batch["claim_token"])
        bad = {"memory_type": "semantic", "text": "错误升级", "source_event_ids": batch["source_event_ids"], "epistemic_status": "user_report"}
        with self.assertRaises(SleepError):
            self.sleep.record_review(*args, [bad], batch["source_event_ids"], claim_token=batch["claim_token"])
        valid = self.sleep.record_review(*args, ["用户原话的待复核整理"], batch["source_event_ids"], claim_token=batch["claim_token"])
        self.assertFalse(valid["duplicate"])
        again = self.sleep.record_review(*args, ["用户原话的待复核整理"], batch["source_event_ids"], claim_token=batch["claim_token"])
        self.assertTrue(again["duplicate"])
        with self.assertRaises(SleepError):
            self.sleep.record_review(*args, ["不能改写已提交审阅"], batch["source_event_ids"], claim_token=batch["claim_token"])

    def test_expired_lease_can_be_reclaimed_and_old_worker_cannot_commit(self):
        self.observe("我喜欢海边故事")
        prepared = self.prepare()
        old = self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="old", lease_seconds=5)
        self.assertEqual(old["claim_token"], self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="old")["claim_token"])
        with self.assertRaises(SleepError) as busy:
            self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="other")
        self.assertEqual(busy.exception.code, "SLEEP_BATCH_BUSY")
        self.clock.advance(seconds=6)
        fresh = self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="other")
        self.assertNotEqual(old["claim_token"], fresh["claim_token"])
        with self.assertRaises(SleepError) as stale:
            self.sleep.record_review(self.scope, prepared["sleep_id"], 0, ["旧结果"], old["source_event_ids"], claim_token=old["claim_token"])
        self.assertEqual(stale.exception.code, "SLEEP_LEASE_LOST")

    def test_restart_keeps_reviewed_batches_and_completed_dream(self):
        self.observe("我喜欢海边故事")
        prepared = self.prepare()
        self.review_all(prepared)
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as reopened:
            service = SleepService(reopened)
            progress = service.prepare(self.scope, self.conversation, "sleep-command")
            self.assertIsNone(progress["review"]["next_cursor"])
            result = service.complete(self.scope, prepared["sleep_id"])
        awake = self.sleep.wake(self.scope, "new-window", "wake-command")
        self.assertEqual(awake["dream"]["dream_id"], result["dream"]["dream_id"])
        self.assertEqual(awake["status"], "awake")
        self.assertEqual(awake["review_summary"]["epistemic_status"], "candidate_review")
        self.assertFalse(awake["review_summary"]["approved_facts"])

    def test_forgetting_source_invalidates_and_erases_dream_and_review(self):
        event = self.observe("我喜欢保密海边故事")
        item = self.memory(event, text="保密海边故事")
        prepared = self.prepare()
        self.review_all(prepared)
        self.sleep.complete(self.scope, prepared["sleep_id"], dream_text="保密海边故事变成一只纸船")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[item.memory_id]))
        result = self.sleep.wake(self.scope, "new-window", "wake")
        self.assertIsNone(result["dream"])
        dream = self.runtime.db.connection.execute("SELECT text,status FROM dreams").fetchone()
        self.assertEqual(tuple(dream), ("", "withdrawn"))
        self.assertEqual(self.runtime.db.connection.execute("SELECT review_json FROM sleep_batches").fetchone()[0], None)

    def test_source_edit_without_epoch_change_is_rejected_before_completion(self):
        event = self.observe("我喜欢海边故事")
        prepared = self.prepare()
        self.review_all(prepared)
        self.runtime.db.connection.execute("UPDATE events SET content='已经改写的原文' WHERE event_id=?", (event.event_id,))
        result = self.sleep.complete(self.scope, prepared["sleep_id"])
        self.assertEqual(result["status"], "invalidated")
        self.assertEqual(self.count_rows("dreams"), 0)

    def test_ordinary_new_messages_do_not_invalidate_frozen_sleep_or_add_unreviewed_coverage(self):
        self.observe("旧的本地资料")
        prepared = self.prepare()
        self.review_all(prepared)
        self.observe("睡眠准备后才到达的新资料")
        result = self.sleep.complete(self.scope, prepared["sleep_id"])
        self.assertTrue(result["review_complete"])
        self.assertEqual(result["inventory"]["event_count"], 1)
        self.assertEqual(self.sleep.status(self.scope, prepared["sleep_id"])["status"], "completed")
        self.assertEqual(self.sleep.wake(self.scope, "new-window", "wake")["sleep_id"], prepared["sleep_id"])

    def test_previously_forgotten_fragment_is_masked_during_local_review(self):
        event = self.observe("我喜欢猫。我喜欢狗。")
        cat = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "猫"},
            source_event_ids=[event.event_id], source_spans={event.event_id: [[0, 4]]})
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "狗"},
            source_event_ids=[event.event_id], source_spans={event.event_id: [[5, 9]]})
        self.runtime.forget_fact(ForgetSelector(memory_ids=[cat.memory_id]))
        prepared = self.prepare()
        batch = self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="review")
        self.assertIn("[已抑制片段]", batch["events"][0]["content"])
        self.assertNotIn("我喜欢猫", batch["events"][0]["content"])
        self.assertIn("我喜欢狗", batch["events"][0]["content"])

    def test_root_delete_cascade_cleans_all_sleep_artifacts(self):
        event = self.observe("本地资料")
        self.memory(event)
        prepared = self.prepare()
        self.review_all(prepared)
        self.sleep.complete(self.scope, prepared["sleep_id"])
        with self.runtime.db.transaction():
            self.runtime.db.connection.execute("DELETE FROM sleep_runs WHERE sleep_id=?", (prepared["sleep_id"],))
        for table in ("sleep_runs", "sleep_sources", "sleep_batches", "dreams", "sleep_weight_changes"):
            self.assertEqual(self.count_rows(table), 0)

    def test_scope_purge_removes_sleep_reviews_and_dreams_through_public_api(self):
        event = self.observe("需一并清除的梦来源")
        self.memory(event)
        prepared = self.prepare()
        self.review_all(prepared)
        self.sleep.complete(self.scope, prepared["sleep_id"])
        self.runtime.forget_scope(self.scope)
        for table in ("sleep_runs", "sleep_sources", "sleep_batches", "dreams", "sleep_weight_changes"):
            self.assertEqual(self.count_rows(table), 0)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_full_snapshot_carries_sleep_and_new_runtime_can_wake(self):
        event = self.observe("可跨库恢复的海边梦")
        self.memory(event)
        prepared = self.prepare()
        self.review_all(prepared)
        completed = self.sleep.complete(self.scope, prepared["sleep_id"])
        path = Path(self._tmpdir.name)/"sleep.snapshot.json"
        self.runtime.export_snapshot(str(path))
        settings = replace(self.runtime.settings, db_path=Path(self._tmpdir.name)/"copy.db")
        with MemoryRuntime(settings, clock=self.clock) as restored:
            restored.db.meta_set("forgetting_hmac_key", self.runtime.db.meta_get("forgetting_hmac_key"))
            restored.import_snapshot(str(path))
            awake = restored.sleep.wake(self.scope, "restored-window", "wake")
            self.assertEqual(awake["dream"]["dream_id"], completed["dream"]["dream_id"])
            self.assertEqual(awake["dream"]["epistemic_status"], "generated_fiction")
            restored.forget_fact(ForgetSelector(memory_ids=[event.event_id]))  # 不存在的记忆ID不撤销无关梦。
            self.assertIsNotNone(restored.sleep.wake(self.scope, "restored-window", "again")["dream"])

    def test_v25_upgrade_creates_sleep_tables_without_changing_old_data(self):
        event = self.observe("旧库原始对话")
        self.memory(event)
        for table in ("sleep_weight_changes", "dreams", "sleep_batches", "sleep_sources", "sleep_runs"):
            self.runtime.db.connection.execute(f"DROP TABLE {table}")
        self.runtime.db.meta_set("schema_version", "25")
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as upgraded:
            self.assertEqual(upgraded.db.meta_get("schema_version"), SCHEMA_VERSION)
            self.assertEqual(upgraded.db.repositories.events.get(event.event_id).content, event.content)
            prepared = upgraded.sleep.prepare(self.scope, self.conversation, "new-sleep")
            self.assertEqual(prepared["inventory"]["event_count"], 1)

    def test_exact_scope_and_conversation_are_enforced(self):
        self.observe("本窗故事")
        self.observe("其他关系", scope=replace(self.scope, relationship_id="other"))
        prepared = self.prepare()
        self.assertEqual(prepared["inventory"]["event_count"], 1)
        for foreign in (replace(self.scope, user_id="other"), replace(self.scope, relationship_id="other"), replace(self.scope, domain="fiction")):
            with self.subTest(scope=foreign), self.assertRaises(SleepError):
                self.sleep.batch(foreign, prepared["sleep_id"], 0)
            self.assertIsNone(self.sleep.wake(foreign, "new-window", "wake")["dream"])

    def test_prepare_is_idempotent_and_rejects_parameter_reuse(self):
        self.observe("本窗故事")
        prepared = self.prepare()
        self.assertEqual(prepared["sleep_id"], self.prepare()["sleep_id"])
        self.assertEqual(self.count_rows("sleep_runs"), 1)
        with self.assertRaises(SleepError):
            self.prepare(batch_chars=1000)

    def test_low_salience_decay_is_reversible_and_no_physical_forgetting(self):
        events = [self.observe(f"旧片段{n}") for n in range(16)]
        items = [self.memory(event, text=f"片段{n}", importance=.1) for n, event in enumerate(events)]
        self.clock.advance(days=3)
        prepared = self.prepare()
        self.review_all(prepared)
        result = self.sleep.complete(self.scope, prepared["sleep_id"])
        self.assertTrue(result["consolidation"]["decayed_memory_ids"])
        self.assertEqual(self.count_rows("memories"), 16)
        self.assertTrue(all(self.repos.memories.get(item.memory_id).status == MemoryStatus.ACTIVE for item in items))
        restored = self.sleep.restore_weights(self.scope, prepared["sleep_id"])
        self.assertTrue(restored["restored_memory_ids"])
        self.assertTrue(all(self.repos.memories.get(item.memory_id).retrieval_strength == item.retrieval_strength for item in items))

    def test_sleep_publication_is_atomic_and_wake_budget_is_bounded(self):
        event = self.observe("我喜欢海边故事")
        self.memory(event)
        prepared = self.prepare()
        self.review_all(prepared)
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("发布故障")):
            with self.assertRaises(RuntimeError):
                self.sleep.complete(self.scope, prepared["sleep_id"])
        self.assertEqual(self.count_rows("dreams"), 0)
        self.assertEqual(self.count_rows("sleep_weight_changes"), 0)
        self.sleep.complete(self.scope, prepared["sleep_id"], dream_text="海"*3000)
        from memorydreaming.llm import EstimatedTokenCounter
        for budget in (128, 200, 400):
            awake = self.sleep.wake(self.scope, "new-window", "wake", max_tokens=budget)
            self.assertLessEqual(EstimatedTokenCounter().count(json.dumps(awake, ensure_ascii=False, separators=(",", ":"))), budget)
            self.assertTrue(awake["truncated"])

    def test_repeated_sleep_cannot_brush_up_replay_strength_within_a_day(self):
        event = self.observe("重复睡眠不能增加事实真实性")
        item = self.memory(event, importance=.9)
        first = self.prepare()
        self.review_all(first)
        self.sleep.complete(self.scope, first["sleep_id"])
        strength = self.repos.memories.get(item.memory_id).retrieval_strength
        second = self.sleep.prepare(self.scope, self.conversation, "different-command")
        self.review_all(second)
        result = self.sleep.complete(self.scope, second["sleep_id"])
        self.assertEqual(self.repos.memories.get(item.memory_id).retrieval_strength, strength)
        self.assertEqual(result["consolidation"]["reinforced_memory_ids"], [])
        self.clock.advance(hours=25)
        third = self.sleep.prepare(self.scope, self.conversation, "another-day")
        self.review_all(third)
        self.sleep.complete(self.scope, third["sleep_id"])
        self.assertGreater(self.repos.memories.get(item.memory_id).retrieval_strength, strength)

    def test_oversize_inventory_is_explicit_and_empty_sleep_does_not_invent_sources(self):
        self.observe("第一条")
        self.observe("第二条")
        with self.assertRaises(SleepError) as caught:
            self.prepare(max_events=1)
        self.assertEqual(caught.exception.code, "SLEEP_INVENTORY_TOO_LARGE")
        self.assertEqual(self.count_rows("sleep_runs"), 0)
        empty = self.sleep.prepare(self.scope, "empty-window", "empty-sleep")
        result = self.sleep.complete(self.scope, empty["sleep_id"])
        self.assertEqual(result["dream"]["source_refs"], [])
        self.assertTrue(result["review_complete"])

    def test_same_content_generation_reuses_validation_without_rescanning_all_sources(self):
        events = [self.observe(f"本地片段{number}") for number in range(30)]
        for number, event in enumerate(events):
            self.memory(event, text=f"片段{number}")
        prepared = self.prepare()
        with patch.object(self.sleep, "_view", wraps=self.sleep._view) as reads:
            batch = self.sleep.batch(self.scope, prepared["sleep_id"], 0, request_id="batch")
            # 只展开本批，不再逐一读全部 60 个冻结来源。
            self.assertEqual(reads.call_count, len(batch["events"]))
            self.sleep.record_review(self.scope, prepared["sleep_id"], 0, ["已审阅"],
                                     batch["source_event_ids"], claim_token=batch["claim_token"])
            self.sleep.status(self.scope, prepared["sleep_id"])
            self.assertEqual(reads.call_count, len(batch["events"]))
        self.runtime.db.connection.execute("UPDATE events SET content='直接 SQL 改写' WHERE event_id=?", (events[0].event_id,))
        with patch.object(self.sleep, "_view", wraps=self.sleep._view) as reads:
            result = self.sleep.status(self.scope, prepared["sleep_id"])
            self.assertGreater(reads.call_count, 0)
        self.assertEqual(result["status"], "invalidated")

    def test_validation_cache_is_not_restored_with_a_new_runtime(self):
        event = self.observe("原始本地资料")
        self.memory(event)
        prepared = self.prepare()
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as reopened:
            service = SleepService(reopened)
            self.assertEqual(len(service._validated), 0)
            with patch.object(service, "_view", wraps=service._view) as reads:
                status = service.status(self.scope, prepared["sleep_id"])
                self.assertEqual(reads.call_count, 2)
            self.assertEqual(status["status"], "prepared")
            self.assertIn(prepared["sleep_id"], service._validated)

    def test_validation_cache_rechecks_at_effective_time_boundary_and_is_bounded(self):
        event = self.observe("带有效期的原话")
        self.memory(event, valid_to=self.clock.now()+timedelta(seconds=5))
        prepared = self.prepare()
        self.clock.advance(seconds=5)
        self.assertEqual(self.sleep.status(self.scope, prepared["sleep_id"])["status"], "invalidated")
        for number in range(65):
            self.sleep.prepare(self.scope, "empty-window", f"empty-{number}")
        self.assertEqual(len(self.sleep._validated), 64)

    def test_selected_important_memory_after_the_first_128_is_still_replayed(self):
        events = [self.observe(f"历史资料{number}") for number in range(140)]
        items = [self.memory(event, text=f"资料{number}", importance=.5) for number, event in enumerate(events)]
        high_event = self.observe("最后一条非常重要的资料")
        important = self.memory(high_event, text="后排重要资料", importance=.99)
        prepared = self.prepare()
        self.review_all(prepared)
        done = self.sleep.complete(self.scope, prepared["sleep_id"])
        self.assertIn(important.memory_id, done["consolidation"]["reinforced_memory_ids"])
        self.assertGreater(self.repos.memories.get(important.memory_id).retrieval_strength, important.retrieval_strength)
        self.assertLessEqual(self.count_rows("sleep_weight_changes"), 128)
