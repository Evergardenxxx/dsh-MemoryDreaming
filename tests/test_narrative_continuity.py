"""自传确认/修订与生存档案；当前来源撤销始终优先于角色连续性。"""

from dataclasses import replace
from pathlib import Path
import json
from unittest.mock import patch

from memorydreaming import MemoryRuntime, Scope
from memorydreaming.errors import MemorySuppressed, ScopeNotFound, VersionConflict
from memorydreaming.life_evidence import dumps
from memorydreaming.llm import EstimatedTokenCounter
from memorydreaming.models import ForgetSelector, MemoryType, new_id
from tests.helpers import RuntimeTestCase


class NarrativeContinuityTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")

    def event(self, text="从今天起你叫小梦。", **kwargs):
        return self.runtime.observe(self.make_turn(text, **kwargs), self.scope, new_id("user")).event

    def proposal(self, event=None, **kwargs):
        event = event or self.event()
        return self.runtime.narrative.propose(self.scope, "第一次命名", "用户为角色取名为小梦。",
                                            [event.event_id], "story-1", **kwargs)

    def accepted(self, event=None):
        proposal = self.proposal(event)
        return self.runtime.narrative.approve(self.scope, proposal["proposal_id"], "trusted-host", "已核对原话")

    def seed_core(self):
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦", "身份": "陪伴角色"}, "用户设定")
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "爸爸"}, "用户设定")

    def target(self, name):
        target = MemoryRuntime(replace(self.runtime.settings, db_path=Path(self._tmpdir.name)/name), clock=self.clock)
        self.addCleanup(target.close)
        return target

    def test_proposed_story_is_hidden_until_trusted_approval_and_never_becomes_fact(self):
        proposal = self.proposal()
        self.assertFalse(proposal["applied"])
        self.assertEqual(self.runtime.narrative.timeline(self.scope)["entries"], [])
        receipt = self.runtime.narrative.approve(self.scope, proposal["proposal_id"], "host", "用户确认")
        entry = self.runtime.narrative.timeline(self.scope)["entries"][0]
        self.assertEqual(entry["entry_id"], receipt["entry_id"])
        self.assertEqual(entry["record_kind"], "autobiography_projection")
        self.assertEqual(entry["perspective"], "shared")
        self.assertEqual(entry["modality"], "occurred")
        self.assertEqual(len(entry["sources"]), 1)
        self.assertEqual(self.count_rows("memories"), 0)
        self.assertEqual(self.count_rows("core_versions"), 0)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_conversation_suggestions_remain_unaccepted_and_idempotent(self):
        self.event("从今天开始我叫你小梦。")
        self.event("好的。")
        suggested = self.runtime.narrative.suggest_from_conversation(self.scope, "conv-test")
        self.assertEqual(len(suggested["proposals"]), 1)
        self.assertFalse(suggested["applied"])
        self.assertEqual(suggested["automatic_facts_created"], 0)
        self.assertEqual(self.runtime.narrative.timeline(self.scope)["entries"], [])
        again = self.runtime.narrative.suggest_from_conversation(self.scope, "conv-test")
        self.assertEqual(again["proposals"][0]["proposal_id"], suggested["proposals"][0]["proposal_id"])

    def test_idempotent_proposal_time_does_not_change_and_approval_cannot_be_rewritten(self):
        event = self.event()
        proposal = self.proposal(event)
        self.clock.advance(hours=1)
        self.assertEqual(self.proposal(event)["proposal_id"], proposal["proposal_id"])
        receipt = self.runtime.narrative.approve(self.scope, proposal["proposal_id"], "host", "确认")
        repeated = self.runtime.narrative.approve(self.scope, proposal["proposal_id"], "host", "确认")
        self.assertEqual(repeated["entry_id"], receipt["entry_id"])
        self.assertTrue(repeated["duplicate"])
        with self.assertRaises(ValueError):
            self.runtime.narrative.approve(self.scope, proposal["proposal_id"], "host", "不同审批")
        with self.assertRaises(ValueError):
            self.runtime.narrative.propose(self.scope, "不同标题", "不同内容", [event.event_id], "story-1")

    def test_revision_keeps_approved_story_until_confirmed_then_records_history(self):
        event = self.event()
        receipt = self.accepted(event)
        revision = self.runtime.narrative.propose(self.scope, "取名时刻", "用户明确为角色取名为小梦。", [event.event_id],
            "story-2", entry_id=receipt["entry_id"], expected_version=1)
        self.assertEqual(self.runtime.narrative.timeline(self.scope)["entries"][0]["title"], "第一次命名")
        applied = self.runtime.narrative.approve(self.scope, revision["proposal_id"], "host", "修订确认")
        self.assertEqual(applied["version"], 2)
        self.assertEqual(len(self.runtime.narrative.history(self.scope, receipt["entry_id"])), 2)
        with self.assertRaises(VersionConflict):
            self.runtime.narrative.withdraw(self.scope, receipt["entry_id"], expected_version=1, reason="旧版本")
        self.runtime.narrative.withdraw(self.scope, receipt["entry_id"], expected_version=2, reason="撤回叙事")
        self.assertEqual(self.runtime.narrative.timeline(self.scope)["entries"], [])

    def test_user_role_and_shared_fiction_labels_survive_timeline(self):
        event = self.event("我们在故事里给你起名小梦。")
        for perspective in ("user", "character", "shared"):
            item = self.runtime.narrative.propose(self.scope, perspective, "故事中的取名。", [event.event_id],
                "fiction-"+perspective, perspective=perspective, modality="shared_fiction")
            self.runtime.narrative.approve(self.scope, item["proposal_id"], "host", "明确共同虚构")
        timeline = self.runtime.narrative.timeline(self.scope, max_tokens=4096)["entries"]
        self.assertEqual({row["perspective"] for row in timeline}, {"user", "character", "shared"})
        self.assertTrue(all(row["modality"] == "shared_fiction" for row in timeline))

    def test_source_revocation_hides_current_history_and_unconfirmed_payload(self):
        event = self.event()
        accepted = self.accepted(event)
        pending = self.runtime.narrative.propose(self.scope, "新候选", "仍引用同一原话", [event.event_id], "pending")
        self.runtime.forget_event(event.event_id)
        self.runtime.narrative.invalidate_stale()
        self.assertEqual(self.runtime.narrative.timeline(self.scope)["entries"], [])
        self.assertIsNone(self.runtime.narrative.history(self.scope, accepted["entry_id"])[0]["snapshot"])
        self.assertEqual(self.runtime.narrative.inspect(self.scope, pending["proposal_id"])["state"], "invalidated")
        row = self.runtime.db.connection.execute("SELECT title,text FROM narrative_entries").fetchone()
        self.assertEqual(tuple(row), ("", ""))
        self.assertTrue(all(row[0] is None for row in self.runtime.db.connection.execute("SELECT payload_json FROM narrative_proposals")))

    def test_foreign_sources_and_generated_ids_are_rejected(self):
        event = self.runtime.observe(self.make_turn("其他用户的命名"), replace(self.scope, user_id="other"), "foreign").event
        for ref in (event.event_id, "dream_fake", "missing_event"):
            with self.subTest(ref=ref), self.assertRaises((MemorySuppressed, ScopeNotFound)):
                self.runtime.narrative.propose(self.scope, "不能引用", "不能引用", [ref], "bad-"+ref)
        proposal = self.proposal()
        with self.assertRaises(ScopeNotFound):
            self.runtime.narrative.approve(replace(self.scope, relationship_id="other"), proposal["proposal_id"], "host", "不能越权")

    def test_acceptance_failure_rolls_back_projection_version_and_receipt(self):
        proposal = self.proposal()
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("故障注入")):
            with self.assertRaises(RuntimeError):
                self.runtime.narrative.approve(self.scope, proposal["proposal_id"], "host", "确认")
        self.assertEqual(self.count_rows("narrative_entries"), 0)
        self.assertEqual(self.count_rows("narrative_versions"), 0)
        self.assertEqual(self.runtime.narrative.inspect(self.scope, proposal["proposal_id"])["state"], "proposed")

    def test_missing_core_is_reported_and_recovery_card_is_bounded(self):
        card = self.runtime.continuity.check(self.scope, max_tokens=128)
        self.assertEqual(card["recovery_status"], "incomplete")
        self.assertLessEqual(EstimatedTokenCounter().count(dumps(card)), 128)
        self.assertEqual(self.count_rows("core_versions"), 0)
        self.seed_core()
        self.accepted()
        card = self.runtime.continuity.check(self.scope, max_tokens=4096)
        self.assertEqual(card["recovery_status"], "complete")
        self.assertEqual(card["identity"]["fields"]["名字"], "小梦")
        self.assertEqual(card["relationship"]["fields"]["称呼"], "爸爸")
        self.assertEqual(len(card["autobiography"]), 1)

    def test_private_archive_restores_role_on_a_fresh_device_without_leaking_key(self):
        self.seed_core()
        self.accepted()
        # 非零遗忘代次也是恢复档案的一部分，不能静默丢弃屏障。
        self.repos.forgetting.bump(self.scope)
        archive = Path(self._tmpdir.name)/"survival.json"
        written = self.runtime.continuity.prepare_archive(self.scope, archive)
        key = self.runtime.db.meta_get("forgetting_hmac_key")
        self.assertNotIn(key, dumps(written))
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8"))["recovery_material"]["forgetting_hmac_key"], key)
        target = self.target("fresh-device.db")
        before = target.db.meta_get("forgetting_hmac_key")
        self.assertNotEqual(before, key)
        preflight = target.continuity.verify_archive(archive, self.scope)
        self.assertTrue(preflight["ready"], preflight)
        self.assertTrue(preflight["fresh_target_key_recovery_required"])
        self.assertEqual(target.db.meta_get("forgetting_hmac_key"), before)  # 只读预检不改目标。
        receipt = target.continuity.restore_archive(archive, self.scope)
        self.assertTrue(receipt["continuity_verified"])
        self.assertTrue(receipt["fresh_target_key_recovered"])
        self.assertNotIn(key, dumps(receipt))
        self.assertEqual(target.continuity.check(self.scope)["identity"]["fields"]["名字"], "小梦")

    def test_continuity_fingerprint_and_archive_cover_promises_preferences_and_growth(self):
        self.seed_core(); self.accepted()
        event = self.event("答应下次接着讲故事，难过时先听我说，讲故事慢一点。")
        before = self.runtime.continuity.fingerprint(self.scope)
        intention = self.runtime.intentions.propose(self.scope, "下次继续灯塔故事", [event.event_id], request_id="promise")
        self.runtime.intentions.confirm(self.scope, intention["intention_id"], expected_version=1,
            confirmation_refs=[event.event_id], reviewer="host", reason="用户同意", request_id="promise-confirm")
        preference = self.runtime.companionship.propose(self.scope, "comfort", "先听用户说完", evidence=[event.event_id],
            reason="用户明确请求", idempotency_key="preference")
        self.runtime.companionship.confirm(self.scope, preference["proposal_id"], user_event_id=event.event_id,
            reviewer="host", reason="已确认", expected_version=0)
        growth = self.runtime.growth.propose(self.scope, "storytelling", "叙述节奏更慢", [event.event_id],
                                             reason="用户反馈", request_id="growth")
        self.runtime.growth.approve(self.scope, growth["proposal_id"], user_event_id=event.event_id,
            reviewer="host", reason="确认成长", expected_version=0, request_id="growth-confirm")
        after = self.runtime.continuity.fingerprint(self.scope)
        self.assertNotEqual(before, after)
        archive = Path(self._tmpdir.name)/"complete-life.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        target = self.target("complete-life-copy.db")
        result = target.continuity.restore_archive(archive, self.scope)
        self.assertTrue(result["continuity_verified"])
        self.assertEqual(target.continuity.fingerprint(self.scope), after)
        card = target.continuity.check(self.scope, max_tokens=4096)
        self.assertEqual(len(card["intentions"]), 1)
        self.assertEqual(len(card["companionship"]), 1)
        self.assertEqual(len(target.growth.profile(self.scope)), 1)

    def test_nonempty_device_or_existing_purge_marker_never_changes_key(self):
        self.seed_core(); self.accepted()
        archive = Path(self._tmpdir.name)/"private.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        for mode in ("existing-data", "purged-data"):
            target = self.target(mode+".db")
            oldkey = target.db.meta_get("forgetting_hmac_key")
            target.observe(self.make_turn("这台设备已有数据"), self.scope, mode)
            if mode == "purged-data":
                target.forget_scope(self.scope)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                target.continuity.restore_archive(archive, self.scope)
            self.assertEqual(target.db.meta_get("forgetting_hmac_key"), oldkey)

    def test_old_archive_cannot_resurrect_current_database_after_forgetting(self):
        self.seed_core()
        event = self.event()
        self.accepted(event)
        archive = Path(self._tmpdir.name)/"before-forget.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        self.runtime.forget_event(event.event_id)
        report = self.runtime.continuity.verify_archive(archive, self.scope)
        self.assertFalse(report["ready"])
        with self.assertRaises(ValueError):
            self.runtime.continuity.restore_archive(archive, self.scope)

    def test_archive_refuses_tampering_or_overwrite(self):
        self.seed_core(); self.accepted()
        archive = Path(self._tmpdir.name)/"saved.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        with self.assertRaises(ValueError):
            self.runtime.continuity.prepare_archive(self.scope, archive)
        raw = json.loads(archive.read_text(encoding="utf-8"))
        raw["snapshot"]["manifest"]["exported_at"] = "tampered"
        archive.write_text(dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runtime.continuity.verify_archive(archive, self.scope)

    def test_restore_verification_failure_rolls_back_fresh_key_and_imported_data(self):
        self.seed_core(); self.accepted()
        archive = Path(self._tmpdir.name)/"rollback.json"
        self.runtime.continuity.prepare_archive(self.scope, archive)
        raw = json.loads(archive.read_text(encoding="utf-8"))
        raw["manifest"]["continuity_fingerprint"] = "0"*64
        archive.write_text(dumps(raw), encoding="utf-8")
        target = self.target("rollback-device.db")
        before = target.db.meta_get("forgetting_hmac_key")
        with self.assertRaises(ValueError):
            target.continuity.restore_archive(archive, self.scope)
        self.assertEqual(target.db.meta_get("forgetting_hmac_key"), before)
        self.assertEqual(target.db.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_scope_purge_cleans_narrative_payloads_and_versions(self):
        self.accepted()
        self.runtime.forget_scope(self.scope)
        for table in ("narrative_entries", "narrative_proposals", "narrative_versions"):
            self.assertEqual(self.count_rows(table), 0)
