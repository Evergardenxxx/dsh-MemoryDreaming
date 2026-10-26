"""持久事实抑制：不同抽取器、来源片段、核心投影、并发任务与迁移。"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from unittest.mock import patch

from memorydreaming.cli import main
from memorydreaming.errors import MemorySuppressed, VersionConflict
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.models import (
    CoreKind, ForgetSelector, MemoryDraft, MemoryStatus, MemoryType, new_id,
)
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database, SCHEMA_VERSION

from tests.helpers import RuntimeTestCase


class FactSuppressionTest(RuntimeTestCase):
    def observe_and_extract(self, text="我喜欢猫。我喜欢狗。"):
        event = self.runtime.observe(self.make_turn(text), self.scope, new_id("turn")).event
        self.runtime.run_consolidation(RuleBasedExtractor())
        records = self.repos.memories.list_by_scope(self.scope)
        return event, {item.body.get("obj"): item for item in records}

    def reextract(self, event, extractor=None):
        class UpgradedExtractor(RuleBasedExtractor):
            version = "upgraded-after-forget"
        self.repos.jobs.enqueue("extract", event.scope, {"event_id": event.event_id}, self.clock.now())
        return self.runtime.run_consolidation(extractor or UpgradedExtractor())

    def test_new_extractor_version_cannot_revive_forgotten_fact(self):
        event, facts = self.observe_and_extract()
        receipt = self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        report = self.reextract(event)
        self.assertEqual(report.suppressed, 1)
        self.assertEqual(report.failed, 0)
        active = self.repos.memories.list_by_scope(self.scope)
        self.assertTrue(active)
        self.assertTrue(all(item.body.get("obj") == "狗" for item in active))
        self.assertEqual(self.repos.events.get(event.event_id).content, event.content)
        self.assertEqual(receipt.erasure_level, "logical_suppressed")
        self.assertTrue(receipt.tombstone_ids)
        self.assertEqual(receipt.conservative_event_ids, [])

    def test_suppression_survives_restart_and_rebuild(self):
        event, facts = self.observe_and_extract()
        self.runtime.forget(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as reopened:
            reopened.db.repositories.jobs.enqueue("extract", self.scope, {"event_id": event.event_id}, self.clock.now())
            class Upgrade(RuleBasedExtractor):
                version = "restart"
            self.assertEqual(reopened.run_consolidation(Upgrade()).suppressed, 1)
            reopened.rebuild_vectors(self.scope)
            self.assertTrue(all(item.memory.body.get("obj") != "猫"
                                for item in reopened.retrieve("猫", self.scope).items))

    def test_identical_assertions_from_independent_events_are_all_suppressed(self):
        _, first = self.observe_and_extract("我喜欢猫。")
        self.observe_and_extract("我喜欢猫。")
        result = self.runtime.forget_fact(ForgetSelector(memory_ids=[first["猫"].memory_id]))
        self.assertGreaterEqual(len(result.deleted_ids), 1)
        self.assertEqual(self.repos.memories.list_by_scope(self.scope), [])
        self.assertEqual(self.runtime.retrieve("猫", self.scope).items, [])

    def test_repeated_forget_does_not_duplicate_tombstones(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        selector = ForgetSelector(memory_ids=[facts["猫"].memory_id])
        first = self.runtime.forget_fact(selector)
        second = self.runtime.forget_fact(selector)
        self.assertEqual(first.tombstone_ids, second.tombstone_ids)
        self.assertEqual(second.deleted_ids, [])
        self.assertEqual(self.count_rows("memory_tombstones"), 1)
        self.assertEqual(self.count_rows("tombstone_sources"), 1)

    def test_duplicate_assertion_sources_also_block_retyped_reextraction(self):
        _, first = self.observe_and_extract("我喜欢猫。")
        event, _ = self.observe_and_extract("我喜欢猫。")
        alternate = self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "用户偏爱小猫"},
            source_event_ids=[event.event_id], source_spans={event.event_id: [[0, 4]]},
        )
        self.runtime.forget_fact(ForgetSelector(memory_ids=[first["猫"].memory_id]))
        self.assertEqual(self.repos.memories.get(alternate.memory_id).status, MemoryStatus.DELETED)
        class RetypedExtractor:
            version = "duplicate-retyped"
            def extract(self, event):
                return [MemoryDraft(MemoryType.EPISODIC, {"what_happened": "用户偏爱猫咪"},
                                    "cat", source_span=(0, 4))]
        report = self.reextract(event, RetypedExtractor())
        self.assertEqual((report.created, report.suppressed), (0, 1))

    def test_suppressed_new_statement_registers_its_source_before_later_reextraction(self):
        _, first = self.observe_and_extract("我喜欢猫。")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[first["猫"].memory_id]))
        event = self.runtime.observe(self.make_turn("我喜欢猫。我喜欢狗。"), self.scope, "repeat").event
        self.assertEqual(self.runtime.run_consolidation(RuleBasedExtractor()).suppressed, 1)
        class RetypedExtractor:
            version = "new-source-retyped"
            def extract(self, event):
                return [MemoryDraft(MemoryType.EPISODIC, {"what_happened": "用户偏爱小猫"},
                                    "cat", source_span=(0, 4))]
        report = self.reextract(event, RetypedExtractor())
        self.assertEqual((report.created, report.suppressed), (0, 1))
        self.assertTrue(any(item.body.get("obj") == "狗" for item in self.repos.memories.list_by_scope(self.scope)))

    def test_same_assertion_in_new_message_remains_suppressed_until_explicit_restore_exists(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        event = self.runtime.observe(self.make_turn("我喜欢猫。"), self.scope, "new-source").event
        report = self.runtime.run_consolidation(RuleBasedExtractor())
        self.assertEqual(report.suppressed, 1)
        self.assertEqual(self.repos.events.get(event.event_id).content, "我喜欢猫。")

    def test_source_overlap_catches_paraphrase_and_changed_memory_type(self):
        event, facts = self.observe_and_extract()
        self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        class ParaphraseExtractor:
            version = "different-shape"
            def extract(self, event):
                return [MemoryDraft(MemoryType.EPISODIC, {"what_happened": "用户偏爱小猫"},
                                    "unrelated-key", source_span=(0, 4))]
        report = self.reextract(event, ParaphraseExtractor())
        self.assertEqual((report.suppressed, report.created), (1, 0))

    def test_spanless_candidates_are_quarantined_without_removing_known_other_fact(self):
        event, facts = self.observe_and_extract()
        self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        class SpanlessExtractor:
            version = "no-offsets"
            def extract(self, event):
                return [MemoryDraft(MemoryType.EPISODIC, {"what_happened": "用户喜欢动物"}, "guess")]
        report = self.reextract(event, SpanlessExtractor())
        self.assertEqual(report.suppressed, 1)
        self.assertEqual(self.repos.memories.get(facts["狗"].memory_id).status, MemoryStatus.ACTIVE)

    def test_invalid_source_span_rolls_back_entire_extraction(self):
        event, _ = self.observe_and_extract("我喜欢猫。")
        class BadExtractor:
            version = "bad-offset"
            def extract(self, event):
                return [MemoryDraft(MemoryType.EPISODIC, {"what_happened": "猫"}, "bad",
                                    source_span=(0, len(event.content) + 1))]
        report = self.reextract(event, BadExtractor())
        self.assertEqual((report.failed, report.created), (1, 0))

    def test_source_offsets_use_unicode_codepoints_and_original_whitespace(self):
        event = self.runtime.observe(self.make_turn("😀。  我叫林鸥， 我喜欢猫。\n  我不喜欢下雨。"), self.scope, "offsets").event
        drafts = RuleBasedExtractor().extract(event)
        self.assertEqual([event.content[slice(*draft.source_span)] for draft in drafts],
                         ["我叫林鸥", "我喜欢猫", "我不喜欢下雨"])

    def test_repository_insert_and_update_cannot_reintroduce_assertion(self):
        _, facts = self.observe_and_extract()
        self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        candidate = replace(facts["猫"], memory_id=new_id("mem"), status=MemoryStatus.ACTIVE,
                            source_event_ids=[], source_spans={})
        with self.assertRaises(MemorySuppressed), self.runtime.db.transaction():
            self.repos.memories.insert(candidate)
        with self.assertRaises(MemorySuppressed), self.runtime.db.transaction():
            self.repos.memories.update_content(facts["狗"].memory_id, facts["猫"].body, self.clock.now())

    def test_read_and_index_guards_reject_accidentally_reactivated_row(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        item = facts["猫"]
        self.runtime.forget_fact(ForgetSelector(memory_ids=[item.memory_id]))
        # 模拟旧工具或人工 SQL 错误恢复状态；不能绕过最后一道读取校验。
        self.runtime.db.connection.execute("UPDATE memories SET status='active' WHERE memory_id=?", (item.memory_id,))
        self.assertEqual(self.runtime.retrieve("猫", self.scope).items, [])
        self.assertEqual(self.runtime.bootstrap("u1", "c1").highlights, [])
        self.assertFalse(self.runtime.vectors.index_memory(self.repos.memories.get(item.memory_id)))
        self.assertEqual(self.runtime.rebuild_vectors(self.scope), 0)

    def test_tombstone_ledger_contains_fingerprints_and_offsets_without_fact_text(self):
        _, facts = self.observe_and_extract("我喜欢保密项目名称。")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["保密项目名称"].memory_id]))
        rows = [dict(row) for row in self.runtime.db.connection.execute("SELECT * FROM memory_tombstones")]
        self.assertNotIn("保密项目名称", json.dumps(rows, ensure_ascii=False))
        self.assertEqual(len(rows[0]["assertion_key"]), 64)

    def test_changing_extractor_speaker_metadata_cannot_bypass_assertion_suppression(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        replay = replace(facts["猫"], memory_id=new_id("mem"), source_speaker="character",
                         source_event_ids=[], source_spans={}, status=MemoryStatus.ACTIVE)
        with self.assertRaises(MemorySuppressed):
            self.repos.memories.insert(replay)

    def test_unknown_legacy_spans_are_reported_conservatively(self):
        event, facts = self.observe_and_extract()
        self.runtime.db.connection.execute("UPDATE memories SET source_spans_json='{}' WHERE memory_id=?",
                                           (facts["猫"].memory_id,))
        receipt = self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        self.assertEqual(receipt.conservative_event_ids, [event.event_id])

    def test_tombstones_are_isolated_by_exact_activity_relationship_and_domain(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        item = facts["猫"]
        copies = []
        for scope in (replace(self.scope, activity_id="activity-b"),
                      replace(self.scope, relationship_id="r2"), replace(self.scope, domain="fiction")):
            copy = replace(item, memory_id=new_id("mem"), scope=scope, source_event_ids=[], source_spans={})
            self.repos.memories.insert(copy)
            copies.append(copy)
        self.runtime.forget_fact(ForgetSelector(memory_ids=[item.memory_id]))
        self.assertTrue(all(not self.repos.forgetting.is_suppressed(copy) for copy in copies))
        self.assertTrue(all(self.repos.memories.get(copy.memory_id).status is MemoryStatus.ACTIVE for copy in copies))

    def test_summary_guard_hides_a_stale_projection_even_if_status_is_restored(self):
        items = [self.add_memory(MemoryType.EPISODIC, {"what_happened": text}) for text in
                 ("海边散步捡贝壳", "海边日落拍照片", "海边露营看星星")]
        self.runtime.build_summaries(self.scope)
        summary = self.repos.summaries.list_active(self.scope)[0]
        self.runtime.forget_fact(ForgetSelector(memory_ids=[items[0].memory_id]))
        self.runtime.db.connection.execute("UPDATE memories SET status='active' WHERE memory_id=?", (items[0].memory_id,))
        self.runtime.db.connection.execute("UPDATE summaries SET status='active' WHERE summary_id=?", (summary.summary_id,))
        self.assertEqual(self.repos.summaries.list_active(self.scope), [])
        self.assertNotIn(items[0].memory_id, [item.memory_id for item in self.runtime.expand_summary(summary.summary_id)])

    def test_failure_rolls_back_tombstones_epoch_and_memory_deletion(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        with patch.object(self.repos.forgetting, "redact_cores", side_effect=RuntimeError("projection offline")):
            with self.assertRaises(RuntimeError):
                self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        self.assertEqual(self.count_rows("memory_tombstones"), 0)
        self.assertEqual(self.repos.forgetting.epoch(self.scope), 0)
        self.assertEqual(self.repos.memories.get(facts["猫"].memory_id).status, MemoryStatus.ACTIVE)

    def test_cli_forget_reports_logical_suppression(self):
        _, facts = self.observe_and_extract("我喜欢猫。")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--db", str(self.db_path), "forget", facts["猫"].memory_id])
        receipt = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(receipt["erasure_level"], "logical_suppressed")
        self.assertTrue(receipt["tombstone_ids"])


class CoreSourceTest(RuntimeTestCase):
    def test_field_sources_preserve_name_when_forgetting_preference(self):
        self.runtime.observe(self.make_turn("我叫林鸥。我喜欢猫。"), self.scope, "both")
        self.runtime.run_consolidation(RuleBasedExtractor())
        facts = {item.body["obj"]: item for item in self.repos.memories.list_by_scope(self.scope)}
        first = self.runtime.propose_core_update(
            self.scope, CoreKind.IDENTITY, {"名字": "林鸥", "偏好": "猫"}, "explicit",
            field_evidence_ids={"名字": [facts["林鸥"].memory_id], "偏好": [facts["猫"].memory_id]},
        )
        receipt = self.runtime.forget_fact(ForgetSelector(memory_ids=[facts["猫"].memory_id]))
        core = self.runtime.bootstrap("u1", "c1").identity
        self.assertEqual(core.payload, {"名字": "林鸥"})
        self.assertEqual(core.core_version, first.core_version + 1)
        self.assertIn("identity:偏好", receipt.core_fields_removed)
        self.assertTrue(all("偏好" not in item.payload for item in self.repos.cores.history(self.scope, CoreKind.IDENTITY)))
        with self.assertRaises(MemorySuppressed):
            self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"偏好": "猫"}, "replay")
        with self.assertRaises(VersionConflict):
            self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"名字": "新名字"}, "stale", expected_version=1)

    def test_patching_one_field_preserves_other_field_provenance(self):
        self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"name": "小梦"}, "initial", evidence_ids=["event-a"])
        updated = self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"style": "简短"}, "style", evidence_ids=["event-b"])
        self.assertEqual(updated.field_evidence_ids, {"name": ["event-a"], "style": ["event-b"]})
        self.assertEqual(set(updated.evidence_ids), {"event-a", "event-b"})

    def test_core_domains_and_relationship_versions_are_independent(self):
        scopes = [self.scope, replace(self.scope, domain="fiction"), replace(self.scope, relationship_id="other")]
        for i, scope in enumerate(scopes):
            core = self.runtime.propose_core_update(scope, CoreKind.RELATIONSHIP, {"称呼": f"关系{i}"}, "setting")
            self.assertEqual(core.core_version, 1)
        for i, scope in enumerate(scopes):
            result = self.runtime.bootstrap(scope.user_id, scope.character_id,
                                            relationship_id=scope.relationship_id, domain=scope.domain)
            self.assertEqual(result.relationship.payload, {"称呼": f"关系{i}"})

    def test_identity_is_shared_between_relationships_in_same_domain(self):
        self.runtime.propose_core_update(self.scope, CoreKind.IDENTITY, {"name": "小梦"}, "setting")
        self.assertEqual(self.repos.cores.latest(replace(self.scope, relationship_id="other"), CoreKind.IDENTITY).payload,
                         {"name": "小梦"})
        self.assertIsNone(self.repos.cores.latest(replace(self.scope, domain="fiction"), CoreKind.IDENTITY))

    def test_forgetting_one_relationship_does_not_modify_another(self):
        first = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "猫"})
        other_scope = replace(self.scope, relationship_id="other")
        other = replace(first, memory_id=new_id("mem"), scope=other_scope)
        self.repos.memories.insert(other)
        for scope, item in ((self.scope, first), (other_scope, other)):
            self.runtime.propose_core_update(scope, CoreKind.RELATIONSHIP, {"偏好": "猫"}, "setting",
                                             field_evidence_ids={"偏好": [item.memory_id]})
        self.runtime.forget_fact(ForgetSelector(memory_ids=[first.memory_id]))
        self.assertEqual(self.repos.cores.latest(other_scope, CoreKind.RELATIONSHIP).payload, {"偏好": "猫"})
        self.assertEqual(self.repos.memories.get(other.memory_id).status, MemoryStatus.ACTIVE)


class ForgetDuringExtractionTest(RuntimeTestCase):
    def test_inflight_result_is_discarded_then_recomputed_against_tombstone(self):
        event = self.runtime.observe(self.make_turn("我喜欢猫。我喜欢狗。"), self.scope, "source").event
        self.runtime.run_consolidation(RuleBasedExtractor())
        self.runtime.run_consolidation()  # 清空之前的 dedupe
        cat = next(item for item in self.repos.memories.list_by_scope(self.scope) if item.body["obj"] == "猫")
        self.repos.jobs.enqueue("extract", self.scope, {"event_id": event.event_id}, self.clock.now())
        settings, clock = self.runtime.settings, self.clock
        class RacingExtractor(RuleBasedExtractor):
            version = "racing"
            def extract(self, event):
                drafts = super().extract(event)
                with MemoryRuntime(settings, clock=clock) as other:
                    other.forget_fact(ForgetSelector(memory_ids=[cat.memory_id]))
                return drafts
        report = self.runtime.run_consolidation(RacingExtractor())
        self.assertEqual((report.stale_results, report.created, report.failed), (1, 0, 0))
        self.assertEqual(self.runtime.job_stats()["pending"], 1)
        class Retry(RuleBasedExtractor):
            version = "racing"
        report = self.runtime.run_consolidation(Retry())
        self.assertEqual((report.suppressed, report.created), (1, 1))
        self.assertEqual(self.repos.memories.get(cat.memory_id).status, MemoryStatus.DELETED)


class ForgetMigrationTest(RuntimeTestCase):
    def test_v5_migration_preserves_core_ids_and_masks_deleted_evidence(self):
        path = self.db_path.with_name("v5.db")
        # 从当前库创建事实，再还原上一版的核心表和缺少片段的记忆列。
        legacy = MemoryRuntime(self.runtime.settings.with_db(path), clock=self.clock)
        legacy.observe(self.make_turn("我喜欢猫。"), self.scope, "legacy")
        legacy.run_consolidation(RuleBasedExtractor())
        old = legacy.db.repositories.memories.list_by_scope(self.scope)[0]
        legacy.db.connection.execute("UPDATE memories SET status='deleted',source_spans_json='{}'")
        stamp = self.clock.now().isoformat()
        legacy.db.connection.executescript("""
DROP TABLE core_versions;
CREATE TABLE core_versions (
 user_id TEXT NOT NULL, character_id TEXT NOT NULL, kind TEXT NOT NULL, core_version INTEGER NOT NULL,
 relationship_id TEXT, payload_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL DEFAULT '[]',
 change_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', effective_from TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(user_id,character_id,kind,core_version));
ALTER TABLE memories DROP COLUMN source_spans_json;
DROP TABLE tombstone_sources;
DROP TABLE memory_tombstones;
DROP TABLE scope_mutations;
DROP TABLE core_field_tombstones;
DELETE FROM schema_meta WHERE key='forgetting_hmac_key';
UPDATE schema_meta SET value='5' WHERE key='schema_version';
""")
        legacy.db.connection.execute(
            "INSERT INTO core_versions VALUES ('u1','c1','identity',7,NULL,?,?,?,?,?,?)",
            (json.dumps({"偏好": "猫"}), json.dumps([old.memory_id]), "legacy", "active", stamp, stamp),
        )
        legacy.close()
        for _ in range(2):
            db = Database.open(path)
            try:
                self.assertEqual(db.meta_get("schema_version"), SCHEMA_VERSION)
                self.assertEqual(db.repositories.memories.get(old.memory_id).status, MemoryStatus.DELETED)
                core = db.repositories.cores.latest(self.scope, CoreKind.IDENTITY)
                self.assertEqual(core.core_version, 8)
                self.assertEqual(core.payload, {})
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM memory_tombstones").fetchone()[0], 1)
                with self.assertRaises(MemorySuppressed):
                    db.repositories.cores.append(self.scope, CoreKind.IDENTITY, {"偏好": "猫"}, "replay",
                                                 self.clock.now(), self.clock.now())
            finally:
                db.close()
