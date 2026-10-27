"""七项长程角色能力接入原生生命周期、来源传播及恢复档案。"""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import json

from memorydreaming import HostAdapter, MemoryRuntime, Scope, SessionBinding
from memorydreaming.bridge import BridgeServer
from memorydreaming.life_commands import execute_life_command
from memorydreaming.llm import AdapterError
from memorydreaming.models import CoreKind, ForgetSelector, new_id
from memorydreaming.storage.database import SCHEMA_VERSION
from tests.helpers import RuntimeTestCase


class LifeIntegrationTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦"}, "初始设定")
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "爸爸"}, "初始设定")
        self.session = HostAdapter(self.runtime).open_session(SessionBinding("u1", "c1", "r1", "life-window"))

    def command(self, operation, payload):
        key = new_id("command")
        labels = {"autobiography": "记忆自传", "promise": "记忆承诺", "complete": "记忆完成", "cancel": "记忆取消",
                  "preference": "记忆偏好", "growth": "记忆成长", "proactive": "记忆提醒", "check": "记忆自检"}
        self.session.prepare_turn(key, labels[operation]+" "+json.dumps(payload, ensure_ascii=False), max_tokens=0)
        event = self.session.current_event_id
        return execute_life_command(self.session, operation, payload, key), event

    def fill(self):
        story, first = self.command("autobiography", {"title": "成为小梦", "text": "用户确认称呼为小梦，我们约定从海边故事开始。"})
        promise, second = self.command("promise", {"text": "下次接着讲灯塔故事", "due_at": (self.clock.now()+timedelta(hours=1)).isoformat()})
        preference, third = self.command("preference", {"key": "安慰方式", "value": "先倾听，再给建议"})
        growth, fourth = self.command("growth", {"field": "storytelling", "value": "讲故事慢一点，使用温柔的语气"})
        policy, fifth = self.command("proactive", {"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00", "min_interval_seconds": 60})
        return [first, second, third, fourth, fifth]

    def test_native_commands_add_life_state_and_normal_turn_gets_bounded_context(self):
        self.fill()
        prepared = self.session.prepare_turn("next", "今天继续聊吧", max_tokens=4096)
        context = prepared.messages[1]["content"]
        self.assertIn("role_continuity", context)
        for value in ("成为小梦", "下次接着讲灯塔故事", "先倾听", "温柔的语气"):
            self.assertIn(value, context)
        self.assertLessEqual(prepared.counted_tokens, 4096)
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload, {"名字": "小梦"})

    def test_wake_self_check_covers_identity_story_promise_and_style(self):
        self.fill()
        result, _ = self.command("check", {})
        self.assertEqual(result["recovery_status"], "complete")
        self.assertTrue(result["autobiography"])
        self.assertTrue(result["intentions"])
        self.assertTrue(result["companionship"])
        self.assertTrue(result.get("growth"), result)
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        awake = server.handle({"id": "wake", "op": "wake", "conversation_id": "new-window", "request_id": "wake-new"})
        self.assertTrue(awake["ok"], awake)
        self.assertIn("continuity", awake["data"])
        self.assertEqual(awake["data"]["continuity"]["recovery_status"], "complete")
        server.close()

    def test_forgetting_sources_revokes_each_projection_and_disables_proactive_policy(self):
        sources = self.fill()
        for event in sources:
            self.runtime.forget_event(event)
        self.assertEqual(self.runtime.narrative.timeline(self.scope)["entries"], [])
        self.assertEqual(self.runtime.intentions.list_open(self.scope), [])
        self.assertEqual(self.runtime.companionship.list_confirmed(self.scope), [])
        self.assertEqual(self.runtime.growth.profile(self.scope), [])
        self.assertFalse(self.runtime.intentions.policy(self.scope)["enabled"])

    def test_archive_recovers_all_roles_to_fresh_device_but_pauses_proactivity(self):
        self.fill()
        archive = Path(self._tmpdir.name)/"private-role.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        settings = replace(self.runtime.settings, db_path=Path(self._tmpdir.name)/"new-device.db")
        with MemoryRuntime(settings, clock=self.clock) as other:
            result = other.continuity.restore_archive(archive, self.scope)
            self.assertTrue(result["continuity_verified"], result)
            card = other.continuity.check(self.scope)
            self.assertTrue(card["intentions"])
            self.assertTrue(card["autobiography"])
            self.assertTrue(other.companionship.list_confirmed(self.scope))
            self.assertTrue(other.growth.profile(self.scope))
            self.assertFalse(other.intentions.policy(self.scope)["enabled"])
            self.assertEqual(other.db.repositories.cores.latest(self.scope, CoreKind.IDENTITY).payload["名字"], "小梦")

    def test_scope_purge_clears_life_tables_and_old_archive_cannot_restore(self):
        self.fill()
        archive = Path(self._tmpdir.name)/"old-role.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        self.runtime.forget_scope(self.scope)
        for table in ("narrative_entries", "narrative_proposals", "narrative_versions", "intentions", "intention_revisions",
                      "intention_operations", "proactive_policies", "proactive_deliveries", "proactive_ack_receipts",
                      "intention_trigger_receipts", "companionship_proposals", "companionship_preferences", "companionship_history",
                      "companionship_receipts", "growth_proposals", "growth_values", "growth_history", "growth_receipts"):
            self.assertEqual(self.count_rows(table), 0, table)
        self.assertFalse(self.runtime.continuity.verify_archive(archive, self.scope)["ready"])
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_scoped_import_does_not_pause_another_relationships_proactivity(self):
        self.command("proactive", {"enabled": True})
        other_scope = replace(self.scope, relationship_id="unrelated")
        other_event = self.runtime.observe(self.make_turn("允许另一关系的本地提醒"), other_scope, "unrelated-policy").event
        self.runtime.intentions.configure_policy(other_scope, enabled=True, confirmation_refs=[other_event.event_id], request_id="other-policy")
        path = Path(self._tmpdir.name)/"one-scope.snapshot.json"
        self.runtime.export_snapshot(str(path), scope=self.scope)
        self.runtime.import_snapshot(str(path))
        self.assertFalse(self.runtime.intentions.policy(self.scope)["enabled"])
        self.assertTrue(self.runtime.intentions.policy(other_scope)["enabled"])

    def test_bridge_claim_validate_and_ack_keep_user_approval_and_actual_delivery_separate(self):
        self.command("promise", {"text": "提醒我继续灯塔故事", "due_at": self.clock.now().isoformat()})
        self.command("proactive", {"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00", "min_interval_seconds": 60})
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        result = server.handle({"id": "claim", "op": "proactive_claim", "conversation_id": "host", "worker_id": "test-host"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["claims"], result)
        token = result["data"]["claims"][0]["claim_token"]
        valid = server.handle({"id": "validate", "op": "proactive_validate", "conversation_id": "host", "claim_token": token})
        self.assertTrue(valid["data"]["valid"], valid)
        ack = server.handle({"id": "ack", "op": "proactive_ack", "conversation_id": "host", "claim_token": token,
                             "delivered": True, "request_id": "ack-1"})
        self.assertTrue(ack["ok"], ack)
        self.assertFalse(ack["data"]["intention_completed"])
        self.assertEqual(len(self.runtime.intentions.list_open(self.scope)), 1)
        server.close()

    def test_native_due_time_requires_explicit_timezone(self):
        with self.assertRaises(AdapterError):
            self.command("promise", {"text": "不能偏移八小时", "due_at": "2026-10-27T20:00:00"})
        result, _ = self.command("promise", {"text": "明确上海时间", "due_at": "2026-10-27T20:00:00+08:00"})
        self.assertEqual(result["due_at"], "2026-10-27T12:00:00.000000+00:00")

    def test_internal_command_cannot_apply_payload_not_present_in_current_user_message(self):
        self.session.prepare_turn("ordinary", "现在回答我二加二是多少", max_tokens=0)
        with self.assertRaises(AdapterError):
            execute_life_command(self.session, "promise", {"text": "未被授权的承诺"}, "ordinary")
        self.session.prepare_turn("real-command", '记忆承诺 {"text":"真实承诺"}', max_tokens=0)
        with self.assertRaises(AdapterError):
            execute_life_command(self.session, "promise", {"text": "被替换的承诺"}, "real-command")
        with self.assertRaises(AdapterError):
            execute_life_command(self.session, "promise", {"text": "真实承诺"}, "wrong-message-id")
        self.assertEqual(self.count_rows("intentions"), 0)

    def test_same_native_promise_command_retry_uses_original_confirmation_version(self):
        payload = {"text": "下次继续灯塔故事"}
        self.session.prepare_turn("promise-retry", "记忆承诺 "+json.dumps(payload, ensure_ascii=False), max_tokens=0)
        first = execute_life_command(self.session, "promise", payload, "promise-retry")
        revisions = self.count_rows("intention_revisions")
        again = execute_life_command(self.session, "promise", payload, "promise-retry")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["intention_id"], first["intention_id"])
        self.assertEqual(again["version"], 2)
        self.assertEqual(self.count_rows("intentions"), 1)
        self.assertEqual(self.count_rows("intention_revisions"), revisions)

    def test_old_proactive_command_retry_preserves_later_policy_and_original_receipt(self):
        original = {"enabled": True, "max_per_day": 1}
        message = "记忆提醒 "+json.dumps(original, ensure_ascii=False)
        self.session.prepare_turn("policy-original", message, max_tokens=0)
        first = execute_life_command(self.session, "proactive", original, "policy-original")
        self.assertEqual(first["version"], 1)
        again = execute_life_command(self.session, "proactive", original, "policy-original")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["version"], first["version"])
        changed, _ = self.command("proactive", {"enabled": False, "max_per_day": 2})
        self.assertEqual(changed["version"], 2)
        before = self.runtime.intentions.policy(self.scope)
        self.session.prepare_turn("policy-original", message, max_tokens=0)
        replayed = execute_life_command(self.session, "proactive", original, "policy-original")
        self.assertTrue(replayed["duplicate"])
        self.assertEqual(replayed["version"], 1)
        self.assertEqual(self.runtime.intentions.policy(self.scope), before)
        self.assertFalse(self.runtime.intentions.policy(self.scope)["enabled"])
        self.assertEqual(self.runtime.intentions.policy(self.scope)["max_per_day"], 2)

    def test_retried_commands_reject_changed_payload_and_forgotten_user_source(self):
        for operation, label, payload in (("promise", "记忆承诺", {"text": "真实承诺"}),
                                          ("proactive", "记忆提醒", {"enabled": True})):
            with self.subTest(operation=operation):
                key = "revoke-"+operation
                self.session.prepare_turn(key, label+" "+json.dumps(payload, ensure_ascii=False), max_tokens=0)
                event = self.session.current_event_id
                execute_life_command(self.session, operation, payload, key)
                changed = {**payload, "text": "替换正文"} if operation == "promise" else {"enabled": False}
                with self.assertRaises(AdapterError):
                    execute_life_command(self.session, operation, changed, key)
                self.runtime.forget_event(event)
                with self.assertRaises(AdapterError):
                    execute_life_command(self.session, operation, payload, key)
        self.assertEqual(self.runtime.intentions.list_open(self.scope), [])
        self.assertFalse(self.runtime.intentions.policy(self.scope)["enabled"])

    def test_v26_migration_preserves_sources_and_adds_domains(self):
        self.runtime.db.meta_set("schema_version", "26")
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as other:
            self.assertEqual(other.db.meta_get("schema_version"), SCHEMA_VERSION)
            self.assertEqual(other.narrative.timeline(self.scope)["entries"], [])
            self.assertFalse(other.intentions.policy(self.scope)["enabled"])
