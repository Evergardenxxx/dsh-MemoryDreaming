"""承诺的可信确认与主动提醒的授权、频率、租约和撤销边界。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorydreaming.errors import MemorySuppressed, ScopeNotFound, VersionConflict
from memorydreaming.intentions import IntentionError, IntentionsService
from memorydreaming.models import ForgetSelector, Scope, new_id
from memorydreaming.runtime import MemoryRuntime
from tests.helpers import RuntimeTestCase


class IntentionsTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.runtime.db.connection.executescript((Path(__file__).resolve().parents[1]/"memorydreaming/storage/intentions-schema.sql").read_text(encoding="utf-8"))
        self.intentions = IntentionsService(self.runtime)
        self.event = self.observe("明天继续讲灯塔的故事，并允许你每天提醒一次。")

    def observe(self, text, *, speaker="user", scope=None):
        return self.runtime.observe(self.make_turn(text, speaker=speaker), scope or self.scope, new_id("u")).event

    def promise(self, **changes):
        params = {"due_at": self.clock.now(), "request_id": new_id("request")}
        params.update(changes)
        proposed = self.intentions.propose(self.scope, "继续灯塔的故事", [self.event.event_id], **params)
        return self.intentions.confirm(self.scope, proposed["intention_id"], expected_version=1,
            confirmation_refs=[self.event.event_id], reviewer="trusted-host", reason="用户明确确认", request_id=new_id("confirm"))

    def enable(self, **changes):
        params = {"enabled": True, "confirmation_refs": [self.event.event_id], "request_id": new_id("policy"),
                  "quiet_start": "00:00", "quiet_end": "00:00", "min_interval_seconds": 60, "max_per_day": 2,
                  "expected_version": self.intentions.policy(self.scope)["version"]}
        params.update(changes)
        return self.intentions.configure_policy(self.scope, **params)

    def claim(self, **changes):
        return self.intentions.claim_due(self.scope, worker_id="host-a", **changes)

    def test_candidate_does_not_open_or_schedule_until_user_confirmation(self):
        proposed = self.intentions.propose(self.scope, "继续灯塔", [self.event.event_id], due_at=self.clock.now(), request_id="proposal")
        self.assertEqual(proposed["state"], "proposed")
        self.assertEqual(self.intentions.list_open(self.scope), [])
        self.assertEqual(self.intentions.preview_due(self.scope), [])
        self.assertEqual(self.claim()["reason"], "PROACTIVITY_DISABLED")
        self.assertFalse(self.intentions.policy(self.scope)["enabled"])

    def test_character_self_report_cannot_confirm_user_permission(self):
        proposed = self.intentions.propose(self.scope, "下次提醒", [self.event.event_id], request_id="proposal")
        character = self.observe("用户同意了", speaker="character")
        with self.assertRaises(ValueError):
            self.intentions.confirm(self.scope, proposed["intention_id"], expected_version=1, confirmation_refs=[character.event_id],
                reviewer="host", reason="错误来源", request_id="confirm")
        with self.assertRaises(ValueError):
            self.enable(confirmation_refs=[character.event_id])

    def test_open_promise_survives_restart_and_resolves_with_history(self):
        item = self.promise()
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as runtime:
            service = IntentionsService(runtime)
            self.assertEqual(service.list_open(self.scope)[0]["intention_id"], item["intention_id"])
            done = service.resolve(self.scope, item["intention_id"], state="completed", expected_version=2,
                evidence=[self.event.event_id], reviewer="host", reason="用户确认完成", request_id="finish")
            self.assertEqual(done["state"], "completed")
            self.assertEqual(service.list_open(self.scope), [])
            self.assertEqual(len(service.history(self.scope, item["intention_id"])), 3)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_time_and_event_triggers_must_both_be_satisfied(self):
        future = self.clock.now()+timedelta(hours=1)
        item = self.promise(due_at=future, event_trigger="exam-ended")
        self.assertEqual(self.intentions.preview_due(self.scope), [])
        self.clock.advance(hours=2)
        self.assertEqual(self.intentions.preview_due(self.scope), [])
        result = self.intentions.record_trigger(self.scope, "exam-ended", self.event.event_id, request_id="trigger")
        self.assertEqual(result["triggered_intention_ids"], [item["intention_id"]])
        self.assertTrue(self.intentions.record_trigger(self.scope, "exam-ended", self.event.event_id, request_id="trigger")["duplicate"])
        self.assertEqual(self.intentions.preview_due(self.scope)[0]["intention_id"], item["intention_id"])

    def test_untimed_open_promise_is_remembered_but_not_proactively_due(self):
        self.promise(due_at=None)
        self.assertEqual(len(self.intentions.list_open(self.scope)), 1)
        self.assertEqual(self.intentions.preview_due(self.scope), [])

    def test_revision_requires_new_confirmation_and_stale_version_fails(self):
        item = self.promise()
        new = self.intentions.revise(self.scope, item["intention_id"], text="改为讲森林故事", evidence=[self.event.event_id],
                                   expected_version=2, due_at=self.clock.now(), request_id="revise")
        self.assertEqual(new["state"], "proposed")
        self.assertEqual(self.intentions.list_open(self.scope), [])
        with self.assertRaises(VersionConflict):
            self.intentions.confirm(self.scope, item["intention_id"], expected_version=2, confirmation_refs=[self.event.event_id],
                                   reviewer="host", reason="旧版本", request_id="bad-confirm")

    def test_propose_confirm_and_resolve_idempotency(self):
        first = self.intentions.propose(self.scope, "继续灯塔", [self.event.event_id], due_at=self.clock.now(), request_id="p1")
        again = self.intentions.propose(self.scope, "继续灯塔", [self.event.event_id], due_at=self.clock.now(), request_id="p1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["intention_id"], again["intention_id"])
        with self.assertRaises(IntentionError):
            self.intentions.propose(self.scope, "改写承诺", [self.event.event_id], request_id="p1")
        kwargs = dict(expected_version=1, confirmation_refs=[self.event.event_id], reviewer="host", reason="确认", request_id="c1")
        self.intentions.confirm(self.scope, first["intention_id"], **kwargs)
        self.assertTrue(self.intentions.confirm(self.scope, first["intention_id"], **kwargs)["duplicate"])

    def test_foreign_scope_and_fictional_dream_id_cannot_be_sources(self):
        foreign = self.observe("另一个用户的授权", scope=replace(self.scope, user_id="other"))
        for refs in ([foreign.event_id], ["dream_fake"]):
            with self.assertRaises((MemorySuppressed, ScopeNotFound)):
                self.intentions.propose(self.scope, "越权承诺", refs, request_id=new_id("p"))
        item = self.promise()
        with self.assertRaises(ScopeNotFound):
            self.intentions.get(replace(self.scope, relationship_id="other"), item["intention_id"])

    def test_quiet_hours_cross_midnight_and_daylight_saving(self):
        self.promise()
        self.enable(quiet_start="22:00", quiet_end="08:00")
        self.clock.set(self.clock.now().replace(hour=23))
        self.assertEqual(self.claim()["reason"], "QUIET_HOURS")
        self.clock.advance(hours=9)
        self.assertEqual(len(self.claim()["claims"]), 1)
        try:
            zone = self.intentions._zone("America/New_York")
        except IntentionError:
            self.skipTest("tzdata unavailable; UTC quiet-hour path validated")
        self.assertNotEqual(datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(zone).utcoffset(),
                            datetime(2026, 7, 1, tzinfo=timezone.utc).astimezone(zone).utcoffset())

    def test_daily_budget_and_interval_are_shared_by_independent_connections(self):
        self.promise(request_id="p1"); self.promise(request_id="p2")
        self.enable(max_per_day=1)
        first = self.claim()["claims"][0]
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as runtime:
            other = IntentionsService(runtime)
            self.assertEqual(other.claim_due(self.scope, worker_id="host-b")["claims"], [])
            receipt = other.ack(self.scope, first["claim_token"], delivered=True, request_id="sent")
            self.assertFalse(receipt["intention_completed"])
        self.clock.advance(seconds=61)
        self.assertEqual(self.claim()["claims"], [])
        self.assertEqual(len(self.intentions.list_open(self.scope)), 2)

    def test_expired_claim_can_retry_same_delivery_but_old_worker_cannot_ack(self):
        self.promise(); self.enable(max_per_day=1)
        first = self.claim(lease_seconds=5)["claims"][0]
        self.clock.advance(seconds=6)
        second = self.claim(lease_seconds=5)["claims"][0]
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        with self.assertRaises(IntentionError):
            self.intentions.ack(self.scope, first["claim_token"], delivered=True, request_id="stale")
        result = self.intentions.ack(self.scope, second["claim_token"], delivered=True, request_id="sent")
        self.assertTrue(result["delivered"])
        self.assertTrue(self.intentions.ack(self.scope, second["claim_token"], delivered=True, request_id="sent")["duplicate"])
        self.assertEqual(self.claim()["claims"], [])

    def test_failed_send_can_release_and_retry_without_new_daily_budget(self):
        self.promise(); self.enable(max_per_day=1)
        first = self.claim()["claims"][0]
        receipt = self.intentions.ack(self.scope, first["claim_token"], delivered=False, request_id="failed")
        self.assertFalse(receipt["delivered"])
        self.assertEqual(self.claim()["claims"], [])
        self.clock.advance(seconds=5)
        retry = self.claim()["claims"][0]
        self.assertEqual(retry["delivery_id"], first["delivery_id"])
        self.assertTrue(self.intentions.ack(self.scope, first["claim_token"], delivered=False, request_id="failed")["duplicate"])

    def test_cancel_and_pause_revoke_claim_before_host_can_send(self):
        item = self.promise(); policy = self.enable()
        first = self.claim()["claims"][0]
        self.intentions.pause(self.scope, expected_version=policy["version"], request_id="pause")
        with self.assertRaises(IntentionError):
            self.intentions.validate_claim(self.scope, first["claim_token"])
        self.assertEqual(self.claim()["reason"], "PROACTIVITY_DISABLED")
        self.enable()
        fresh = self.claim()["claims"][0]
        self.intentions.resolve(self.scope, item["intention_id"], state="cancelled", expected_version=2,
                               evidence=[self.event.event_id], reviewer="host", reason="用户取消", request_id="cancel")
        with self.assertRaises(IntentionError):
            self.intentions.validate_claim(self.scope, fresh["claim_token"])

    def test_source_forgetting_invalidates_promise_and_policy_and_hides_history(self):
        item = self.promise(); self.enable()
        claim = self.claim()["claims"][0]
        self.runtime.forget_event(self.event.event_id)
        self.intentions.invalidate_stale()
        self.assertEqual(self.intentions.list_open(self.scope), [])
        self.assertFalse(self.intentions.policy(self.scope)["enabled"])
        self.assertEqual(self.intentions.get(self.scope, item["intention_id"])["state"], "invalidated")
        self.assertNotIn("继续灯塔", str(self.intentions.history(self.scope, item["intention_id"])))
        with self.assertRaises(IntentionError):
            self.intentions.ack(self.scope, claim["claim_token"], delivered=True, request_id="no-replay")

    def test_claim_and_confirm_transaction_failures_leave_no_partial_state(self):
        proposed = self.intentions.propose(self.scope, "继续灯塔", [self.event.event_id], request_id="p1")
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.intentions.confirm(self.scope, proposed["intention_id"], expected_version=1, confirmation_refs=[self.event.event_id],
                                       reviewer="host", reason="确认", request_id="c1")
        self.assertEqual(self.intentions.get(self.scope, proposed["intention_id"])["state"], "proposed")
        self.assertEqual(self.count_rows("intention_operations"), 0)

    def test_trigger_confirmation_and_proactivity_do_not_increase_truth_or_usage(self):
        self.promise(event_trigger="ready")
        self.intentions.record_trigger(self.scope, "ready", self.event.event_id, request_id="trigger")
        self.enable(); first = self.claim()["claims"][0]
        self.intentions.ack(self.scope, first["claim_token"], delivered=True, request_id="sent")
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("core_versions"), 0)

    def test_revised_history_and_old_receipts_do_not_reveal_revoked_previous_source(self):
        first = self.intentions.propose(self.scope, "保密的旧约定", [self.event.event_id], request_id="p1")
        self.intentions.confirm(self.scope, first["intention_id"], expected_version=1, confirmation_refs=[self.event.event_id],
            reviewer="host", reason="确认", request_id="c1")
        second = self.observe("以后换成新约定。")
        self.intentions.revise(self.scope, first["intention_id"], text="新的约定", evidence=[second.event_id],
                              expected_version=2, request_id="revise")
        self.runtime.forget_event(self.event.event_id)
        history = self.intentions.history(self.scope, first["intention_id"])
        self.assertNotIn("保密的旧约定", str(history))
        self.assertIn("新的约定", str(history))
        self.assertEqual(self.intentions.get(self.scope, first["intention_id"])["state"], "proposed")

    def test_completed_state_keeps_completion_proof_as_revocable_evidence(self):
        item = self.promise()
        completed_event = self.observe("我确认已经完成这件事。")
        self.intentions.resolve(self.scope, item["intention_id"], state="completed", expected_version=2,
            evidence=[completed_event.event_id], reviewer="host", reason="确认完成", request_id="done")
        self.runtime.forget_event(completed_event.event_id)
        self.assertEqual(self.intentions.get(self.scope, item["intention_id"])["state"], "invalidated")

    def test_public_claim_validation_is_json_safe_and_not_a_storage_row(self):
        import json
        self.promise(); self.enable()
        claim = self.claim()["claims"][0]
        public = self.intentions.validate_claim(self.scope, claim["claim_token"])
        self.assertTrue(json.loads(json.dumps(public))["valid"])
        self.assertEqual(public["delivery_id"], claim["delivery_id"])

    def test_pause_resume_and_clear_preserve_user_control(self):
        configured = self.enable(max_per_day=3)
        paused = self.intentions.pause(self.scope, expected_version=configured["version"], request_id="pause")
        self.assertFalse(paused["enabled"])
        resumed = self.intentions.resume(self.scope, expected_version=paused["version"],
                                        confirmation_refs=[self.event.event_id], request_id="resume")
        self.assertEqual(resumed["max_per_day"], 3)
        cleared = self.intentions.clear_policy(self.scope, expected_version=resumed["version"], request_id="clear")
        self.assertFalse(cleared["enabled"])
        self.assertEqual(cleared["max_per_day"], 1)
        self.assertEqual(cleared["min_interval_seconds"], 86400)
