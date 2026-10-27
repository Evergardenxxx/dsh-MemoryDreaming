"""可信宿主重新学习：新来源授权、再次遗忘、原子发布与可迁移回执。"""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
import io
import json
from pathlib import Path
from unittest.mock import patch

from memorydreaming import HostAdapter, Scope, SessionBinding
from memorydreaming.cli import main
from memorydreaming.clock import from_utc_iso
from memorydreaming.errors import MemorySuppressed, ScopeNotFound, SourceRevisionChanged
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.llm import AdapterError
from memorydreaming.models import CoreKind, ForgetSelector, MemoryStatus, new_id
from memorydreaming.references import ViewReader
from memorydreaming.runtime import MemoryRuntime
from tests.helpers import RuntimeTestCase


class RelearningTest(RuntimeTestCase):
    def forgotten(self, content="我喜欢猫。", *, scope=None):
        scope = scope or self.scope
        event = self.runtime.observe(self.make_turn(content), scope, new_id("u")).event
        self.runtime.run_consolidation(RuleBasedExtractor())
        old = next(item for item in self.repos.memories.list_by_scope(scope) if item.body.get("obj") == "猫")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[old.memory_id]))
        return event, old

    def confirmation(self, content="我喜欢猫，现在允许你重新记住。", *, scope=None, speaker="user"):
        self.clock.set(self.clock.now() + timedelta(seconds=1))
        return self.runtime.observe(self.make_turn(content, speaker=speaker), scope or self.scope, new_id("u")).event

    def approve(self, old, event, **kwargs):
        return self.runtime.relearn_fact(
            old.memory_id, old.scope, confirmation_event_id=event.event_id,
            **{"approved_by": "trusted-host", "reason": "用户明确重申并授权", "idempotency_key": "approval-1", **kwargs},
        )

    def test_new_record_survives_restart_while_old_records_and_core_stay_hidden(self):
        event = self.runtime.observe(self.make_turn("我喜欢猫。"), self.scope, "initial").event
        self.runtime.run_consolidation(RuleBasedExtractor())
        old = self.repos.memories.list_by_scope(self.scope)[0]
        self.runtime.apply_core_update(self.scope, "identity", {"偏好": "猫"}, "旧投影",
                                       field_evidence_ids={"偏好": [old.memory_id]})
        self.runtime.forget_fact(ForgetSelector(memory_ids=[old.memory_id]))
        consent = self.confirmation()
        result = self.approve(old, consent)
        self.assertNotEqual(result.memory_id, old.memory_id)
        self.assertFalse(result.old_records_restored)
        item = self.repos.memories.get(result.memory_id)
        self.assertEqual(item.source_event_ids, [consent.event_id])
        self.assertEqual(item.body, old.body)
        self.assertEqual(self.repos.memories.get(old.memory_id).status, MemoryStatus.DELETED)
        self.assertTrue(self.repos.forgetting.is_suppressed(old))
        self.assertNotIn("偏好", self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload)
        self.assertNotIn(event.event_id, item.source_event_ids)
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as reopened:
            reopened.rebuild_vectors(self.scope)
            found = reopened.retrieve("猫", self.scope).items
            self.assertEqual([entry.memory.memory_id for entry in found], [item.memory_id])
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_receipt_is_idempotent_and_key_reuse_cannot_change_request(self):
        _, old = self.forgotten()
        consent = self.confirmation()
        result = self.approve(old, consent)
        self.assertEqual(self.approve(old, consent).grant_id, result.grant_id)
        self.assertTrue(self.approve(old, consent).duplicate)
        with self.assertRaises(ValueError):
            self.approve(old, consent, reason="不同请求")
        with self.assertRaises(ValueError):
            self.approve(old, consent, idempotency_key="another-key")
        self.assertEqual(self.count_rows("relearning_grants"), 1)

    def test_confirmation_must_be_new_real_user_and_exact_scope(self):
        first, old = self.forgotten()
        with self.assertRaises(ValueError):
            self.approve(old, first)
        # 事件发生时间不能替代系统保存时间，旧输入不能冒充新的用户授权。
        future_occurrence = self.runtime.observe(
            replace(self.make_turn("重新记住"), occurred_at=self.clock.now() + timedelta(days=1)),
            self.scope, "same-system-time",
        ).event
        with self.assertRaises(ValueError):
            self.approve(old, future_occurrence)
        activity = self.runtime.register_activity(self.scope, "另一个活动")
        for scope, speaker in ((self.scope, "character"), (self.scope, "system"),
                               (replace(self.scope, relationship_id="other"), "user"),
                               (replace(self.scope, activity_id=activity.activity_id), "user"),
                               (replace(self.scope, domain="fiction"), "user")):
            with self.subTest(scope=scope, speaker=speaker):
                event = self.confirmation(scope=scope, speaker=speaker)
                with self.assertRaises((ValueError, ScopeNotFound)):
                    self.approve(old, event)
        event = self.confirmation()
        self.runtime.forget_event(event.event_id, scope=self.scope)
        with self.assertRaises(ValueError):
            self.approve(old, event)
        self.assertEqual(self.count_rows("relearning_grants"), 0)

    def test_reforget_revokes_old_grant_and_requires_another_new_confirmation(self):
        _, old = self.forgotten()
        consent = self.confirmation()
        result = self.approve(old, consent)
        self.clock.set(self.clock.now() - timedelta(days=1))
        self.runtime.forget_fact(ForgetSelector(memory_ids=[old.memory_id]))
        self.assertEqual(self.repos.memories.get(result.memory_id).status, MemoryStatus.DELETED)
        self.assertEqual(self.runtime.retrieve("猫", self.scope).items, [])
        with self.assertRaises(MemorySuppressed):
            self.approve(old, consent)
        with self.assertRaises(ValueError):
            self.approve(old, consent, idempotency_key="new-key-old-message")
        self.clock.set(consent.recorded_at)
        next_consent = self.confirmation()
        new = self.approve(old, next_consent, idempotency_key="new-approval")
        self.assertNotEqual(new.memory_id, result.memory_id)
        self.assertFalse(self.repos.forgetting.is_suppressed(self.repos.memories.get(new.memory_id)))

    def test_unapproved_reextraction_and_future_restatements_still_cannot_revive_old_data(self):
        _, old = self.forgotten()
        consent = self.confirmation("我喜欢猫。")
        result = self.approve(old, consent)
        report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertGreaterEqual(report.suppressed, 1)
        self.confirmation("我喜欢猫。")
        report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertGreaterEqual(report.suppressed, 1)
        self.assertEqual([m.memory_id for m in self.repos.memories.list_by_scope(self.scope)], [result.memory_id])

    def test_host_can_search_new_evidence_and_read_new_source_without_unmasking_old_source(self):
        scope = Scope("u1", "c1", "r1")
        original, old = self.forgotten(scope=scope)
        session = HostAdapter(self.runtime).open_session(SessionBinding("u1", "c1", "r1", "new-window"))
        self.clock.set(self.clock.now() + timedelta(seconds=1))
        prepared = session.prepare_turn("confirm", "我喜欢猫。允许你重新记住。", max_tokens=0)
        # 模拟后台比用户审批先抽取；审批必须仍能开放被批准的新原话片段。
        self.runtime.run_consolidation(RuleBasedExtractor())
        result = session.approve_relearning(old.memory_id, user_event_id=prepared.user_event_id,
                    reviewer="host", reason="用户确认", idempotency_key="host-1", source_span=(0, 4))
        self.clock.set(self.clock.now() + timedelta(seconds=1))
        session.prepare_turn("next", "还记得我喜欢什么吗", max_tokens=0)
        response = session.call_tool("memory_search", {"query": "猫", "detail": "evidence",
                  "activity_ref": None, "time_range": None, "max_items": 4, "max_tokens": 2048})
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(len(response["data"]["evidence"]), 1)
        evidence = response["data"]["evidence"][0]
        expanded = session.call_tool("memory_read", {"references": evidence["source_refs"], "max_tokens": 2048})
        self.assertIn("我喜欢猫", expanded["data"]["records"][0]["text"])
        reader = ViewReader(self.runtime, session.binding, self.clock)
        self.assertIn("[已抑制片段]", reader.load("event", original.event_id).data["text"])
        with self.assertRaises(AdapterError):
            session.approve_relearning(old.memory_id, user_event_id=prepared.user_event_id,
                    reviewer="host", reason="旧回合", idempotency_key="old-turn")
        self.assertNotIn("approve_relearning", [t["name"] for t in session.adapter.tools])
        self.assertEqual(self.repos.memories.get(result.memory_id).source_event_ids, [prepared.user_event_id])

    def test_partial_approval_keeps_other_forgotten_facts_masked(self):
        scope = Scope("u1", "c1", "r1")
        _, old = self.forgotten("我喜欢猫。我喜欢狗。", scope=scope)
        dog = next(m for m in self.repos.memories.list_by_scope(scope) if m.body.get("obj") == "狗")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[dog.memory_id]))
        consent = self.confirmation("我喜欢猫。我喜欢狗。", scope=scope)
        self.runtime.run_consolidation(RuleBasedExtractor())
        with self.assertRaises(MemorySuppressed):
            self.approve(old, consent)  # 整条原话含其他受抑制事实，不能一起开放。
        result = self.approve(old, consent, source_span=(0, 4))
        binding = SessionBinding("u1", "c1", "r1", "check")
        reader = ViewReader(self.runtime, binding, self.clock)
        shown = reader.load("event", consent.event_id).data["text"]
        self.assertIn("我喜欢猫", shown)
        self.assertNotIn("我喜欢狗", shown)
        self.assertIn("[已抑制片段]", shown)
        self.assertEqual(self.repos.memories.get(result.memory_id).source_spans, {consent.event_id: [[0, 4]]})
        self.assertTrue(self.repos.forgetting.is_suppressed(dog))
        self.assertEqual(self.count_rows("relearning_grants"), 1)

    def test_authorized_record_id_cannot_be_used_to_swap_back_to_old_sources(self):
        original, old = self.forgotten()
        consent = self.confirmation()
        result = self.approve(old, consent)
        item = self.repos.memories.get(result.memory_id)
        forged = replace(item, source_event_ids=[original.event_id], source_spans={})
        self.assertTrue(self.repos.forgetting.is_suppressed(forged))
        self.assertTrue(self.repos.forgetting.is_suppressed(replace(item, body={**item.body, "extra": "旧正文"})))
        self.runtime.db.connection.execute("UPDATE events SET content='用户已经撤回授权' WHERE event_id=?", (consent.event_id,))
        self.assertTrue(self.repos.forgetting.is_suppressed(item))

    def test_failure_rolls_back_grant_memory_vectors_and_journal(self):
        _, old = self.forgotten()
        event = self.confirmation()
        before = {table: self.count_rows(table) for table in ("memories", "memory_vectors", "relearning_grants", "journal_entries")}
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("故障注入")):
            with self.assertRaises(RuntimeError):
                self.approve(old, event)
        self.assertEqual(before, {table: self.count_rows(table) for table in before})
        self.assertFalse(self.approve(old, event).duplicate)

    def test_encoding_runs_outside_writer_and_concurrent_forget_prevents_publication(self):
        _, old = self.forgotten()
        event = self.confirmation()
        encode = self.runtime.vectors._encode_documents
        def concurrent_forget(texts):
            self.assertFalse(self.runtime.db.connection.in_transaction)
            with MemoryRuntime(self.runtime.settings, clock=self.clock) as other:
                other.forget_fact(ForgetSelector(memory_ids=[old.memory_id]))
            return encode(texts)
        with patch.object(self.runtime.vectors, "_encode_documents", side_effect=concurrent_forget):
            with self.assertRaises(SourceRevisionChanged):
                self.approve(old, event)
        self.assertEqual(self.count_rows("relearning_grants"), 0)

    def test_snapshot_roundtrip_keeps_authority_and_scope_purge_removes_it(self):
        _, old = self.forgotten()
        event = self.confirmation()
        result = self.approve(old, event)
        path = Path(self._tmpdir.name) / "relearn.json"
        self.runtime.export_snapshot(str(path))
        target_settings = replace(self.runtime.settings, db_path=Path(self._tmpdir.name) / "import.db")
        with MemoryRuntime(target_settings, clock=self.clock) as target:
            target.db.meta_set("forgetting_hmac_key", self.runtime.db.meta_get("forgetting_hmac_key"))
            target.import_snapshot(str(path))
            self.assertFalse(target.db.repositories.forgetting.is_suppressed(target.db.repositories.memories.get(result.memory_id)))
            self.assertTrue(target.relearn_fact(old.memory_id, self.scope, confirmation_event_id=event.event_id,
                     approved_by="trusted-host", reason="用户明确重申并授权", idempotency_key="approval-1").duplicate)
        self.runtime.forget_scope(self.scope)
        self.assertEqual(self.count_rows("relearning_grants"), 0)
        self.assertEqual(self.count_rows("fact_forgetting_barriers"), 0)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_v21_migration_preserves_suppression_and_creates_fresh_input_barrier(self):
        _, old = self.forgotten()
        conn = self.runtime.db.connection
        conn.execute("DROP TABLE relearning_grants")
        conn.execute("DROP TABLE fact_forgetting_barriers")
        self.runtime.db.meta_set("schema_version", "21")
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as migrated:
            barrier = migrated.db.connection.execute("SELECT * FROM fact_forgetting_barriers").fetchone()
            self.assertIsNotNone(barrier)
            self.assertTrue(migrated.db.repositories.forgetting.is_suppressed(old))
            self.clock.set(from_utc_iso(barrier["forgotten_at"]) + timedelta(seconds=1))
            event = migrated.observe(self.make_turn("我喜欢猫，重新记住。"), self.scope, "new-after-upgrade").event
            result = migrated.relearn_fact(old.memory_id, self.scope, confirmation_event_id=event.event_id,
                        approved_by="host", reason="升级后新确认", idempotency_key="migration")
            self.assertFalse(result.old_records_restored)

    def test_local_cli_returns_receipt(self):
        _, old = self.forgotten()
        event = self.confirmation()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--db", str(self.db_path), "relearn", old.memory_id,
                "--user", "u1", "--character", "c1", "--confirmation-event", event.event_id,
                "--approved-by", "operator", "--reason", "明确授权", "--key", "cli-1"])
        self.assertEqual(code, 0)
        self.assertNotEqual(json.loads(output.getvalue())["memory_id"], old.memory_id)
