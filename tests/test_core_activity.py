"""核心档案与活动检查点测试：版本号是并发安全的唯一防线。"""

from __future__ import annotations

import unittest

from memorydreaming.errors import VersionConflict
from memorydreaming.models import ActivityStatus, CoreKind, Scope

from tests.helpers import RuntimeTestCase


class BootstrapTest(RuntimeTestCase):
    def test_missing_core_returns_warning_instead_of_raising(self) -> None:
        """没有身份核心时不能崩，也不能编一个假身份。"""
        result = self.runtime.bootstrap("u1", "c1")

        self.assertIsNone(result.identity)
        self.assertIsNone(result.relationship)
        self.assertIsNotNone(result.recovery_warning)

    def test_restores_identity_and_selected_activity(self) -> None:
        self.runtime.propose_core_update(
            self.scope, CoreKind.IDENTITY, {"名字": "小满"}, "初次设定"
        )
        self.runtime.propose_core_update(
            self.scope, CoreKind.RELATIONSHIP, {"关系": "朋友"}, "初次设定"
        )
        story = self.runtime.register_activity(self.scope, "《海底邮局》", goal="讲完第三章")
        self.runtime.register_activity(self.scope, "英语学习", goal="背十个单词")

        # 取默认活动：最近更新的那段
        default = self.runtime.bootstrap("u1", "c1")
        self.assertIsNotNone(default.identity)
        self.assertEqual(default.identity.payload["名字"], "小满")
        self.assertEqual(len(default.activities), 2)

        # 显式指定继续哪一段
        explicit = self.runtime.bootstrap("u1", "c1", activity_id=story.activity_id)
        self.assertEqual(explicit.selected_activity.activity_id, story.activity_id)

    def test_other_characters_activity_is_not_exposed(self) -> None:
        other_scope = Scope(user_id="u1", character_id="c2")
        foreign = self.runtime.register_activity(other_scope, "别人的故事")

        result = self.runtime.bootstrap("u1", "c1", activity_id=foreign.activity_id)
        self.assertIsNone(result.selected_activity)


class CoreVersionTest(RuntimeTestCase):
    def test_new_version_merges_patch_and_keeps_history(self) -> None:
        first = self.runtime.propose_core_update(
            self.scope, CoreKind.IDENTITY, {"名字": "小满", "风格": "温和"}, "初次设定"
        )
        second = self.runtime.propose_core_update(
            self.scope,
            CoreKind.IDENTITY,
            {"名字": "小满（阿满）"},
            "用户要求改称呼",
            expected_version=first.core_version,
        )

        self.assertEqual(second.core_version, first.core_version + 1)
        # 补丁合并：没提到的键保持不变
        self.assertEqual(second.payload["风格"], "温和")
        self.assertEqual(second.payload["名字"], "小满（阿满）")

        history = self.repos.cores.history(self.scope, CoreKind.IDENTITY)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].status, "active")
        self.assertEqual(history[1].status, "superseded")

    def test_stale_version_is_rejected(self) -> None:
        first = self.runtime.propose_core_update(
            self.scope, CoreKind.IDENTITY, {"名字": "小满"}, "初次设定"
        )
        self.runtime.propose_core_update(
            self.scope, CoreKind.IDENTITY, {"名字": "阿满"}, "改称呼", expected_version=1
        )

        with self.assertRaises(VersionConflict):
            self.runtime.propose_core_update(
                self.scope,
                CoreKind.IDENTITY,
                {"名字": "小满"},
                "用旧版本覆盖",
                expected_version=first.core_version,
            )


class CheckpointTest(RuntimeTestCase):
    def test_checkpoint_increments_version(self) -> None:
        activity = self.runtime.register_activity(self.scope, "故事", goal="讲完")

        version = self.runtime.checkpoint(
            activity.activity_id,
            {"open_loops": ["第三章"], "checkpoint": {"chapter": 2}},
            expected_version=1,
        )

        self.assertEqual(version, 2)
        reloaded = self.repos.activities.get(activity.activity_id)
        self.assertEqual(reloaded.open_loops, ["第三章"])
        self.assertEqual(reloaded.checkpoint["chapter"], 2)

    def test_stale_checkpoint_is_rejected(self) -> None:
        activity = self.runtime.register_activity(self.scope, "故事")
        self.runtime.checkpoint(activity.activity_id, {"goal": "a"}, expected_version=1)

        with self.assertRaises(VersionConflict):
            self.runtime.checkpoint(activity.activity_id, {"goal": "b"}, expected_version=1)

    def test_unknown_field_is_rejected_early(self) -> None:
        """字段名写错要当场报错，而不是静默丢掉。"""
        activity = self.runtime.register_activity(self.scope, "故事")

        with self.assertRaises(ValueError):
            self.runtime.checkpoint(activity.activity_id, {"open_loop": ["拼错了"]}, 1)

    def test_status_can_be_updated_with_checkpoint(self) -> None:
        activity = self.runtime.register_activity(self.scope, "故事")
        self.runtime.checkpoint(
            activity.activity_id,
            {"status": ActivityStatus.PAUSED.value},
            expected_version=1,
        )
        self.assertEqual(
            self.repos.activities.get(activity.activity_id).status, ActivityStatus.PAUSED
        )


if __name__ == "__main__":
    unittest.main()
