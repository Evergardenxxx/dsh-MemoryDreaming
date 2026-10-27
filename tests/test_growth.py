"""表达成长需要用户确认，版本/来源/日频率受控且不修改核心。"""

from pathlib import Path
from unittest.mock import patch

from memorydreaming.errors import MemorySuppressed, VersionConflict
from memorydreaming.growth import GrowthService
from memorydreaming.models import ForgetSelector, MemoryType, new_id
from tests.helpers import RuntimeTestCase


class GrowthTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.assertIsNotNone(self.runtime.db.connection.execute("SELECT 1 FROM sqlite_master WHERE name='growth_proposals'").fetchone())
        self.growth = GrowthService(self.runtime)
        self.source = self.event("以后讲故事慢一点，语气温柔一些。")
        self.confirmation = self.event("我确认这个讲故事的变化。")

    def event(self, content, speaker="user"):
        return self.runtime.observe(self.make_turn(content, speaker=speaker), self.scope, new_id("event")).event

    def propose(self, **changes):
        return self.growth.propose(self.scope, changes.pop("field", "storytelling"), changes.pop("value", "讲故事慢一点，语气温柔"),
             [self.source.event_id], reason="用户相处反馈", request_id=changes.pop("request_id", "propose"), **changes)

    def approve(self, proposal, **changes):
        return self.growth.approve(self.scope, proposal["proposal_id"], **{
            "user_event_id": self.confirmation.event_id, "reviewer": "trusted-host", "reason": "核对用户确认",
            "expected_version": 0, "request_id": "approve", **changes})

    def test_proposal_does_not_change_style_until_confirmation(self):
        proposal = self.propose()
        self.assertEqual(self.growth.profile(self.scope), [])
        result = self.approve(proposal)
        self.assertTrue(result["applied"])
        self.assertFalse(result["core_changed"])
        self.assertEqual(self.growth.profile(self.scope)[0]["value"], "讲故事慢一点，语气温柔")
        self.assertEqual(self.count_rows("core_versions"), 0)
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_idempotency_and_changed_parameters(self):
        proposal = self.propose()
        self.assertEqual(proposal, self.propose())
        with self.assertRaises(ValueError):
            self.propose(value="改变同一请求")
        first = self.approve(proposal)
        second = self.approve(proposal)
        self.assertEqual(first["version"], second["version"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.count_rows("growth_history"), 1)
        with self.assertRaises(ValueError):
            self.approve(proposal, reason="改写审批")

    def test_unknown_fields_and_non_user_confirmation_rejected(self):
        for field in ("identity", "名字", "用户心理诊断"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.propose(field=field)
        proposal = self.propose()
        assistant = self.event("我批准自己变化", "character")
        with self.assertRaises(ValueError):
            self.approve(proposal, user_event_id=assistant.event_id)
        self.assertEqual(self.growth.profile(self.scope), [])

    def test_source_retraction_hides_style_and_history(self):
        proposal = self.propose()
        self.approve(proposal)
        self.runtime.forget_event(self.source.event_id)
        self.assertEqual(self.growth.profile(self.scope), [])
        self.assertEqual(self.growth.history(self.scope, "storytelling")[0]["value"], None)
        self.growth.invalidate_stale()
        self.assertEqual(self.runtime.db.connection.execute("SELECT value FROM growth_values").fetchone()[0], "")

    def test_source_changed_before_approval_invalidates_proposal(self):
        proposal = self.propose()
        self.runtime.db.connection.execute("UPDATE events SET content='已改写' WHERE event_id=?", (self.source.event_id,))
        result = self.approve(proposal)
        self.assertEqual(result["state"], "invalidated")
        self.assertNotIn("value", self.growth.inspect(self.scope, proposal["proposal_id"]))

    def test_daily_change_limit_and_explicit_correction(self):
        self.approve(self.propose())
        second = self.propose(value="更轻快的讲述", request_id="second")
        with self.assertRaises(VersionConflict):
            self.approve(second, expected_version=0, request_id="second-approve")
        with self.assertRaises(ValueError):
            self.approve(second, expected_version=1, request_id="second-approve")
        self.approve(second, expected_version=1, request_id="second-approve", allow_recent=True)
        self.assertEqual(self.growth.profile(self.scope)[0]["version"], 2)
        self.assertEqual(len(self.growth.history(self.scope, "storytelling")), 2)

    def test_revoke_prevents_old_style_from_returning(self):
        self.approve(self.propose())
        result = self.growth.revoke(self.scope, "storytelling", user_event_id=self.confirmation.event_id,
            reviewer="host", reason="用户撤销", expected_version=1, request_id="revoke")
        self.assertEqual(result["version"], 2)
        self.assertEqual(self.growth.profile(self.scope), [])
        self.assertEqual(self.growth.history(self.scope, "storytelling")[0]["state"], "revoked")

    def test_publish_failure_rolls_back_projection_history_and_receipt(self):
        proposal = self.propose()
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("故障")):
            with self.assertRaises(RuntimeError):
                self.approve(proposal)
        self.assertEqual(self.growth.profile(self.scope), [])
        self.assertEqual(self.count_rows("growth_history"), 0)
        self.assertEqual(self.count_rows("growth_receipts"), 0)
        self.assertEqual(self.growth.inspect(self.scope, proposal["proposal_id"])["state"], "pending")

    def test_same_content_reuses_pending_and_rejected_candidate_even_with_reordered_sources(self):
        refs = [self.source.event_id, self.confirmation.event_id]
        first = self.growth.propose(self.scope, "response_style", "温柔 表达", refs, reason="初次推测", request_id="first")
        again = self.growth.propose(self.scope, "response_style", "温柔表达", list(reversed(refs)), reason="另一理由", request_id="another")
        self.assertEqual(first["proposal_id"], again["proposal_id"])
        self.growth.reject(self.scope, first["proposal_id"], user_event_id=self.confirmation.event_id,
                           reviewer="host", reason="用户明确拒绝", request_id="reject")
        duplicate = self.growth.propose(self.scope, "response_style", "温柔表达", refs, reason="不能换理由继续推", request_id="after-reject")
        self.assertEqual(duplicate["state"], "rejected")
        self.assertEqual(self.count_rows("growth_proposals"), 1)
        inspected = self.growth.inspect(self.scope, first["proposal_id"])
        self.assertNotIn("value", inspected)
        self.assertNotIn("reason", inspected)
        new_user = self.event("我改变想法，现在同意使用温柔表达。")
        changed = self.growth.propose(self.scope, "response_style", "温柔表达", [new_user.event_id], reason="新用户依据", request_id="new-evidence")
        self.assertEqual(changed["state"], "pending")
        self.assertNotEqual(changed["proposal_id"], first["proposal_id"])

    def test_pending_proposals_are_bounded_and_expire_after_a_day(self):
        for index in range(64):
            self.propose(value=f"表达方式{index}", request_id=f"pending-{index}")
        with self.assertRaises(ValueError):
            self.propose(value="超额候选", request_id="overflow")
        self.clock.advance(hours=25)
        expired = self.propose(value="表达方式0", request_id="retry-expired")
        self.assertEqual(expired["state"], "invalidated")
        proposal = self.propose(value="过期后新候选", request_id="new")
        self.assertEqual(proposal["state"], "pending")
        self.assertEqual(self.count_rows("growth_values"), 0)

    def test_approval_refuses_expired_candidate_and_hides_its_payload(self):
        proposal = self.propose()
        self.clock.advance(days=1)
        result = self.approve(proposal)
        self.assertEqual(result["state"], "invalidated")
        self.assertEqual(self.growth.profile(self.scope), [])
        self.assertNotIn("value", self.growth.inspect(self.scope, proposal["proposal_id"]))

    def test_profile_and_history_source_checks_run_in_one_database_snapshot(self):
        self.approve(self.propose())
        original = self.growth.evidence.valid
        seen = []
        def check(*args, **kwargs):
            self.assertTrue(self.runtime.db.connection.in_transaction)
            seen.append(True)
            return original(*args, **kwargs)
        with patch.object(self.growth.evidence, "valid", side_effect=check):
            self.assertTrue(self.growth.profile(self.scope))
            self.assertTrue(self.growth.history(self.scope, "storytelling"))
        self.assertGreaterEqual(len(seen), 4)
        self.assertFalse(self.runtime.db.connection.in_transaction)
