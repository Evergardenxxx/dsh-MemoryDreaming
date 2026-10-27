"""用户确认的场景陪伴偏好：待审、版本、拒绝、撤销和来源保护。"""

from dataclasses import replace
from unittest.mock import patch

from memorydreaming.companionship import CompanionshipService
from memorydreaming.errors import MemorySuppressed, ScopeNotFound, VersionConflict
from memorydreaming.models import Scope, new_id
from tests.helpers import RuntimeTestCase


class CompanionshipTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.assertIsNotNone(self.runtime.db.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='companionship_preferences'").fetchone())
        self.service = CompanionshipService(self.runtime)

    def event(self, text="我难过时请先听我说，再问要不要建议。", *, scope=None, speaker="user"):
        self.clock.advance(seconds=1)
        return self.runtime.observe(self.make_turn(text, speaker=speaker), scope or self.scope, new_id("input")).event

    def propose(self, event, *, key="安慰方式", value="先倾听，再询问是否需要建议", context="default", **changes):
        return self.service.propose(self.scope, key, value, context=context,
            **{"evidence": [event.event_id], "reason": "从用户原话提出相处方式候选", "idempotency_key": new_id("proposal-key"), **changes})

    def approve(self, proposal, event, **changes):
        return self.service.confirm(self.scope, proposal["proposal_id"], **{
            "user_event_id": event.event_id, "reviewer": "trusted-host", "reason": "已核对用户确认",
            "expected_version": proposal["expected_version"], **changes})

    def test_pending_guess_does_not_change_profile_until_real_user_confirmation(self):
        observed = self.event("角色猜测用户可能希望被安慰", speaker="character")
        proposal = self.propose(observed)
        self.assertEqual(proposal["state"], "pending")
        self.assertEqual(self.service.read_for_context(self.scope), [])
        with self.assertRaises(ScopeNotFound):
            self.approve(proposal, observed)
        user = self.event("你说得对，我难过时先听我说。")
        receipt = self.approve(proposal, user)
        self.assertTrue(receipt["applied"])
        actual = self.service.read_for_context(self.scope)[0]
        self.assertEqual(actual["value"], "先倾听，再询问是否需要建议")
        self.assertEqual(actual["epistemic_status"], "user_confirmed_preference")
        self.assertEqual(actual["confirmation"][0]["target"], user.event_id)
        self.assertEqual(self.count_rows("core_versions"), 0)

    def test_confirm_and_proposal_retries_are_idempotent(self):
        event = self.event()
        proposal = self.propose(event, idempotency_key="same-request")
        self.assertEqual(self.propose(event, idempotency_key="same-request")["proposal_id"], proposal["proposal_id"])
        self.assertEqual(self.propose(event, idempotency_key="new-key-same-evidence")["proposal_id"], proposal["proposal_id"])
        receipt = self.approve(proposal, event)
        self.assertEqual(receipt, self.approve(proposal, event))
        with self.assertRaises(ValueError):
            self.approve(proposal, event, reason="重试改了原因")
        with self.assertRaises(ValueError):
            self.propose(event, idempotency_key="same-request", value="相同键不同正文")
        self.assertEqual(self.count_rows("companionship_history"), 1)
        self.assertEqual(self.count_rows("companionship_receipts"), 1)

    def test_context_overrides_default_and_revocation_blocks_silent_fallback(self):
        default = self.event("平时可以直接给建议。")
        self.approve(self.propose(default, value="直接给建议"), default)
        sad = self.event("难过时先倾听。")
        self.approve(self.propose(sad, value="先倾听", context="sad"), sad)
        self.assertEqual(self.service.read_for_context(self.scope, "sad")[0]["value"], "先倾听")
        self.assertEqual(self.service.read_for_context(self.scope, "celebration")[0]["value"], "直接给建议")
        no = self.event("撤销难过场景这条偏好，先不要用默认建议补回来。")
        receipt = self.service.revoke(self.scope, "安慰方式", context="sad", user_event_id=no.event_id,
            reviewer="host", reason="用户撤销", expected_version=1, idempotency_key="revoke-sad")
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(self.service.read_for_context(self.scope, "sad"), [])
        self.assertEqual(self.service.read_for_context(self.scope, "default")[0]["value"], "直接给建议")

    def test_correction_is_new_pending_version_and_history_preserves_confirmed_changes(self):
        one = self.event("先听我说。")
        self.approve(self.propose(one, value="先倾听"), one)
        two = self.event("现在我希望你直接给建议。")
        next_proposal = self.service.correct(self.scope, "安慰方式", "直接建议", evidence=[two.event_id],
            reason="用户调整偏好", idempotency_key="correct")
        self.assertEqual(next_proposal["state"], "pending")
        self.assertEqual(self.service.read_for_context(self.scope)[0]["value"], "先倾听")
        self.approve(next_proposal, two)
        self.assertEqual(self.service.read_for_context(self.scope)[0]["version"], 2)
        self.assertEqual([h["value"] for h in self.service.history(self.scope, "安慰方式")], ["直接建议", "先倾听"])

    def test_competing_candidates_must_not_overwrite_newer_version(self):
        one = self.event()
        first = self.propose(one, value="偏好甲")
        second = self.propose(one, value="偏好乙")
        self.approve(first, one)
        with self.assertRaises(VersionConflict):
            self.approve(second, one)
        self.assertEqual(self.service.read_for_context(self.scope)[0]["value"], "偏好甲")

    def test_user_rejection_is_remembered_and_new_key_or_reason_cannot_repropose_same_guess(self):
        event = self.event()
        proposal = self.propose(event)
        no = self.event("不，我不希望这样安慰。")
        args = {"user_event_id": no.event_id, "reviewer": "host", "reason": "用户拒绝"}
        receipt = self.service.reject(self.scope, proposal["proposal_id"], **args)
        self.assertEqual(receipt, self.service.reject(self.scope, proposal["proposal_id"], **args))
        duplicate = self.propose(event, reason="换一个措辞继续建议")
        self.assertEqual(duplicate["state"], "rejected")
        self.assertNotIn("value", duplicate)
        self.assertEqual(self.count_rows("companionship_proposals"), 1)
        with self.assertRaises((ValueError, MemorySuppressed)):
            self.approve(proposal, no)
        changed_mind = self.event("我改主意了，后来愿意先倾听。")
        new = self.propose(changed_mind)
        self.assertNotEqual(new["proposal_id"], proposal["proposal_id"])
        self.assertEqual(new["state"], "pending")

    def test_reordering_same_sources_cannot_bypass_user_rejection(self):
        one, two = self.event("偏好依据甲"), self.event("偏好依据乙")
        first = self.propose(one, evidence=[one.event_id, two.event_id])
        self.service.reject(self.scope, first["proposal_id"], user_event_id=two.event_id, reviewer="host", reason="用户拒绝")
        retried = self.propose(one, evidence=[two.event_id, one.event_id], reason="换理由不构成新依据")
        self.assertEqual(retried["state"], "rejected")
        self.assertEqual(retried["proposal_id"], first["proposal_id"])
        self.assertEqual(self.count_rows("companionship_proposals"), 1)

    def test_non_user_or_foreign_confirmation_cannot_apply_preference(self):
        event = self.event()
        proposal = self.propose(event)
        sources = [self.event(speaker="character"), self.event(scope=replace(self.scope, user_id="other")),
                   self.event(scope=replace(self.scope, relationship_id="other")),
                   self.event(scope=replace(self.scope, domain="fiction"))]
        for source in sources:
            with self.subTest(source=source.event_id), self.assertRaises(ScopeNotFound):
                self.approve(proposal, source)
        self.assertEqual(self.service.list_confirmed(self.scope), [])

    def test_relationship_domain_activity_and_user_profile_isolation(self):
        event = self.event()
        self.approve(self.propose(event), event)
        for scope in (replace(self.scope, user_id="other"), replace(self.scope, relationship_id="other"),
                      replace(self.scope, activity_id="activity"), replace(self.scope, domain="fiction")):
            with self.subTest(scope=scope):
                self.assertEqual(self.service.list_confirmed(scope), [])
                self.assertEqual(self.service.history(scope, "安慰方式"), [])

    def test_forgotten_confirmation_hides_payload_and_does_not_restore_old_preference(self):
        first = self.event("最初希望先倾听")
        self.approve(self.propose(first, value="先倾听"), first)
        second = self.event("现在希望直接给建议")
        current = self.propose(second, value="直接建议")
        self.approve(current, second)
        self.runtime.forget_event(second.event_id, scope=self.scope)
        self.assertEqual(self.service.read_for_context(self.scope), [])
        history = self.service.history(self.scope, "安慰方式")
        self.assertEqual(history[0]["value"], None)
        self.assertTrue(history[1]["available"])
        changed = self.service.invalidate_stale(self.scope)
        self.assertIn(changed["preferences"], (0, 1))  # 主运行时可在遗忘事务内已经完成清理。
        self.assertIsNone(self.runtime.db.connection.execute("SELECT value_json FROM companionship_preferences").fetchone()[0])

    def test_source_rewrite_and_candidate_expiration_invalidate_without_revealing_old_value(self):
        event = self.event()
        proposal = self.propose(event)
        self.runtime.db.connection.execute("UPDATE events SET content='用户已改写' WHERE event_id=?", (event.event_id,))
        inspected = self.service.inspect(self.scope, proposal["proposal_id"])
        self.assertEqual(inspected["state"], "invalidated")
        self.assertNotIn("value", inspected)
        new_event = self.event()
        expiring = self.propose(new_event, ttl_seconds=1)
        self.clock.advance(seconds=2)
        self.assertEqual(self.service.inspect(self.scope, expiring["proposal_id"])["state"], "invalidated")

    def test_dream_or_unknown_record_cannot_become_preference_evidence(self):
        with self.assertRaises(MemorySuppressed):
            self.service.propose(self.scope, "安慰方式", "梦中的偏好", evidence=["dream_fake"],
                                 reason="虚构不能背书", idempotency_key="dream")
        with self.assertRaises(MemorySuppressed):
            self.service.propose(self.scope, "安慰方式", "核心也不能为自己背书", evidence=[{"kind": "core_version", "target": "identity", "revision": "0"*64}],
                                 reason="拒绝未知来源", idempotency_key="core")

    def test_failure_rolls_back_profile_history_receipt_and_proposal_state(self):
        event = self.event()
        proposal = self.propose(event)
        count = self.count_rows("journal_entries")
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("故障注入")):
            with self.assertRaises(RuntimeError):
                self.approve(proposal, event)
        for table in ("companionship_preferences", "companionship_history", "companionship_receipts"):
            self.assertEqual(self.count_rows(table), 0)
        self.assertEqual(self.service.inspect(self.scope, proposal["proposal_id"])["state"], "pending")
        self.assertEqual(self.count_rows("journal_entries"), count)

    def test_revoke_receipt_is_idempotent_and_new_head_remains_revoked(self):
        event = self.event()
        self.approve(self.propose(event), event)
        revoke_event = self.event("撤销这条相处偏好")
        args = {"user_event_id": revoke_event.event_id, "reviewer": "host", "reason": "用户撤回",
                "expected_version": 1, "idempotency_key": "revoke"}
        receipt = self.service.revoke(self.scope, "安慰方式", **args)
        self.assertEqual(receipt, self.service.revoke(self.scope, "安慰方式", **args))
        self.assertEqual(self.service.read_for_context(self.scope), [])
        self.assertEqual([row["state"] for row in self.service.history(self.scope, "安慰方式")], ["revoked", "confirmed"])

    def test_limits_and_feedback_boolean_are_checked(self):
        event = self.event()
        for changes in ({"value": ""}, {"key": "x"*81}, {"context": ""}, {"ttl_seconds": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.propose(event, **changes)
        proposal = self.propose(event)
        with self.assertRaises(ValueError):
            self.service.feedback(self.scope, proposal["proposal_id"], approved="yes", user_event_id=event.event_id,
                                  reviewer="host", reason="不能把字符串当批准")
        self.service.feedback(self.scope, proposal["proposal_id"], approved=True, user_event_id=event.event_id,
                              reviewer="host", reason="用户同意", expected_version=0)
        self.assertTrue(self.service.list_confirmed(self.scope))
