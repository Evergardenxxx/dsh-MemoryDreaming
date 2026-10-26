from __future__ import annotations

from memorydreaming import CoreProposalPolicy, HostAdapter, SessionBinding
from memorydreaming.models import MemoryType, Scope
from tests.helpers import RuntimeTestCase


class ChangeProposalTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.memory = self.add_memory(MemoryType.SEMANTIC,
                                      {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"})
        self.binding = SessionBinding("u1", "c1", "r1", "window-1")
        self.policy = CoreProposalPolicy(change_proposals=True, checkpoint_proposals=True)
        self.session = HostAdapter(self.runtime, core_policy=self.policy).open_session(self.binding)
        self.turn = self.session.prepare_turn("user-1", "请忘记我喜欢海边故事。")

    def test_change_proposal_is_pending_until_host_approval(self):
        search = self.session.call_tool("memory_search", {
            "query": "海边故事", "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 4, "max_tokens": 1200,
        })
        target = search["data"]["evidence"][0]["ref"]
        proposed = self.session.call_tool("memory_request_change", {
            "operation": "forget_fact", "target_refs": [target],
            "replacement_text": None, "reason": "用户明确要求忘记该偏好", "evidence_refs": [],
        })
        self.assertEqual(proposed["status"], "proposed")
        proposal_id = proposed["data"]["proposal_id"]
        self.assertIsNotNone(self.repos.memories.get(self.memory.memory_id))
        receipt = self.session.approve_change_proposal(
            proposal_id, user_event_id=self.turn.user_event_id,
            reviewer="host:test", reason="已核对用户原话",
        )
        self.assertTrue(receipt["applied"])
        self.assertIsNone(self.runtime.retrieve("海边故事", self.scope).items[0].memory
                          if self.runtime.retrieve("海边故事", self.scope).items else None)

    def test_change_proposals_are_disabled_by_default(self):
        closed = HostAdapter(self.runtime).open_session(self.binding)
        response = closed.call_tool("memory_request_change", {
            "operation": "forget_scope", "target_refs": [],
            "replacement_text": None, "reason": "清除", "evidence_refs": [],
        })
        self.assertEqual(response["error"]["code"], "UNSUPPORTED")

    def test_checkpoint_proposal_uses_activity_version_and_approval(self):
        activity = self.runtime.register_activity(self.scope, "海边故事", goal="完成第二章")
        binding = SessionBinding("u1", "c1", "r1", "window-activity",
                                 allowed_activity_ids=(activity.activity_id,),
                                 selected_activity_id=activity.activity_id)
        session = HostAdapter(self.runtime, core_policy=self.policy).open_session(binding)
        turn = session.prepare_turn("user-activity", "继续写灯塔结尾。")
        search = session.call_tool("memory_search", {
            "query": "灯塔", "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 4, "max_tokens": 1200,
        })
        evidence = search["data"]["evidence"]
        refs = [evidence[0]["ref"]] if evidence else []
        # 当前轮事件本身不是历史候选时，使用既有记忆作为最小可验证依据。
        if not refs:
            refs = [session.call_tool("memory_search", {
                "query": "海边故事", "activity_ref": None, "time_range": None,
                "detail": "evidence", "max_items": 4, "max_tokens": 1200,
            })["data"]["evidence"][0]["ref"]]
        proposed = session.call_tool("memory_checkpoint_propose", {
            "activity_ref": session.bootstrap()["data"]["activities"][0]["ref"],
            "expected_version": 1, "goal": "完成第二章", "open_loops": ["灯塔结尾"],
            "checkpoint_note": "继续写作", "evidence_refs": refs,
        })
        self.assertEqual(proposed["status"], "proposed")
        receipt = session.approve_change_proposal(
            proposed["data"]["proposal_id"], user_event_id=turn.user_event_id,
            reviewer="host:test", reason="已核对活动进度",
        )
        self.assertTrue(receipt["applied"])
        self.assertEqual(self.runtime.db.repositories.activities.get(activity.activity_id).checkpoint_version, 2)

    def test_structured_fact_replacement_supersedes_old_value(self):
        search = self.session.call_tool("memory_search", {
            "query": "海边故事", "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 4, "max_tokens": 1200,
        })
        proposal = self.session.call_tool("memory_request_change", {
            "operation": "correct_fact", "target_refs": [search["data"]["evidence"][0]["ref"]],
            "replacement_text": "用户更正了偏好", "replacement_body": {
                "subject": "用户", "predicate": "喜欢", "obj": "森林故事",
            },
            "reason": "用户明确更正偏好", "evidence_refs": [],
        })
        receipt = self.session.approve_change_proposal(
            proposal["data"]["proposal_id"], user_event_id=self.turn.user_event_id,
            reviewer="host:test", reason="已核对结构化更正",
        )
        self.assertTrue(receipt["applied"])
        current = self.runtime.db.repositories.memories.list_by_scope(self.scope, limit=20)
        self.assertIn("森林故事", [item.body.get("obj") for item in current])

