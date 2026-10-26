from __future__ import annotations

from dataclasses import replace

from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.llm import AdapterError

from tests.helpers import RuntimeTestCase


class HandoffTests(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activity_scope = replace(self.scope, relationship_id="r1")
        self.activity = self.runtime.register_activity(self.activity_scope, "海边故事", goal="讲第二章")
        self.binding = SessionBinding(
            "u1", "c1", "r1", "window-1",
            allowed_activity_ids=(self.activity.activity_id,),
            selected_activity_id=self.activity.activity_id,
        )
        self.adapter = HostAdapter(self.runtime, clock=self.clock)

    def test_pre_compress_updates_checkpoint_and_bootstrap_recovers_short_handoff(self):
        session = self.adapter.open_session(self.binding)
        result = session.pre_compress(
            {
                "goal": "讲第三章",
                "open_loops": ["补上灯塔结尾"],
                "active_entities": ["灯塔", "小寄居蟹"],
                "checkpoint": {"scene": "礁石旁"},
                "handoff_note": "下一窗口从第三章继续",
                "next_action": "先复述上一段",
            },
            expected_version=1,
            idempotency_key="compress-1",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["activity_version"], 2)
        self.assertEqual(result["data"]["briefing"]["goal"], "讲第三章")

        reopened = self.adapter.open_session(replace(self.binding, conversation_id="window-2"))
        handoff = reopened.opened["data"]["handoff"]
        self.assertEqual(handoff["activity_id"], self.activity.activity_id)
        self.assertEqual(handoff["briefing"]["open_loops"], ["补上灯塔结尾"])
        self.assertNotIn("我喜欢", str(handoff))

    def test_pre_compress_is_idempotent_and_stale_versions_fail_closed(self):
        session = self.adapter.open_session(self.binding)
        first = session.pre_compress({"goal": "第一版"}, expected_version=1, idempotency_key="same")
        second = session.pre_compress({"goal": "第一版"}, expected_version=2, idempotency_key="same")
        self.assertTrue(second["data"]["duplicate"])
        self.assertEqual(first["data"]["handoff_id"], second["data"]["handoff_id"])
        with self.assertRaises(AdapterError) as raised:
            session.pre_compress({"goal": "冲突版本"}, expected_version=1, idempotency_key="other")
        self.assertEqual(raised.exception.code, "VERSION_CONFLICT")

    def test_handoff_without_activity_can_be_restored_without_copying_full_history(self):
        binding = SessionBinding("u1", "c1", "r1", "window-1")
        session = self.adapter.open_session(binding)
        session.pre_compress({"handoff_note": "等待用户选择活动", "next_action": "询问继续哪件事"})
        reopened = self.adapter.open_session(replace(binding, conversation_id="window-2"))
        self.assertEqual(
            reopened.opened["data"]["handoff"]["briefing"]["next_action"],
            "询问继续哪件事",
        )

