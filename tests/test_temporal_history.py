"""字段级核心双时间和摘要正文历史回滚，不允许恢复已遗忘来源。"""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from memorydreaming import HostAdapter, SessionBinding
from memorydreaming.errors import MemorySuppressed, ScopeNotFound, VersionConflict
from memorydreaming.models import CoreKind, ForgetSelector, MemoryType, Scope
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import RuntimeTestCase


class CoreTemporalTest(RuntimeTestCase):
    def test_knowledge_and_effective_axes_differ_and_partial_patches_keep_field_times(self):
        start = self.clock.now()
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦", "身份": "陪伴角色"}, "初始")
        self.clock.advance(days=2)
        effective = start + timedelta(days=1)
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "梦梦"}, "追记昨天改名", effective_from=effective)
        historical = self.runtime.core_at(self.scope, "identity", knowledge_at=start + timedelta(hours=12), fact_at=effective)
        now_knows_old_fact = self.runtime.core_at(self.scope, "identity", knowledge_at=self.clock.now(), fact_at=effective)
        before = self.runtime.core_at(self.scope, "identity", knowledge_at=self.clock.now(), fact_at=start)
        self.assertEqual(historical.payload["名字"], "小梦")
        self.assertEqual(now_knows_old_fact.payload, {"名字": "梦梦", "身份": "陪伴角色"})
        self.assertEqual(before.payload["名字"], "小梦")
        self.assertIsNone(self.runtime.core_at(self.scope, "identity", knowledge_at=start - timedelta(seconds=1), fact_at=start))

    def test_late_older_change_does_not_replace_newer_effective_value(self):
        start = self.clock.now()
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "初始"}, "最初")
        self.clock.advance(days=3)
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "较晚生效"}, "第三天改名")
        self.clock.advance(days=1)
        result = self.runtime.apply_core_update(self.scope, "identity", {"名字": "较早生效"}, "追记第二天",
                                               effective_from=start + timedelta(days=1))
        self.assertEqual(result.payload["名字"], "较晚生效")
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload["名字"], "较晚生效")
        old = self.runtime.core_at(self.scope, "identity", knowledge_at=self.clock.now(), fact_at=start + timedelta(days=2))
        self.assertEqual(old.payload["名字"], "较早生效")

    def test_temporal_read_still_masks_forgotten_core_fields(self):
        source = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "猫"})
        moment = self.clock.now()
        self.runtime.apply_core_update(self.scope, "identity", {"偏好": "猫", "名字": "小梦"}, "来源明确",
                                       field_evidence_ids={"偏好": [source.memory_id]})
        self.clock.advance(days=1)
        self.runtime.forget_fact(ForgetSelector(memory_ids=[source.memory_id]))
        core = self.runtime.core_at(self.scope, "identity", knowledge_at=moment, fact_at=moment)
        self.assertNotIn("偏好", core.payload)
        self.assertEqual(core.payload["名字"], "小梦")

    def test_relationship_domain_and_user_are_isolated(self):
        scopes = [Scope("u1", "c1", "r1"), Scope("u1", "c1", "r2"), Scope("u1", "c1", "r1", domain="fiction")]
        for index, scope in enumerate(scopes):
            self.runtime.apply_core_update(scope, "relationship", {"称呼": str(index)}, "分域设置")
        for index, scope in enumerate(scopes):
            core = self.runtime.core_at(scope, "relationship", knowledge_at=self.clock.now(), fact_at=self.clock.now())
            self.assertEqual(core.payload["称呼"], str(index))
        self.assertIsNone(self.runtime.core_at(Scope("other", "c1", "r1"), "relationship", knowledge_at=self.clock.now(), fact_at=self.clock.now()))

    def test_llm_can_expand_historical_core_without_replacing_live_bootstrap(self):
        scope = Scope("u1", "c1", "r1")
        moment = self.clock.now()
        self.runtime.apply_core_update(scope, "identity", {"名字": "小梦"}, "初始")
        self.clock.advance(days=1)
        self.runtime.apply_core_update(scope, "identity", {"名字": "梦梦"}, "更新")
        session = HostAdapter(self.runtime).open_session(SessionBinding("u1", "c1", "r1", "history-window"))
        result = session.historical_core("identity", knowledge_at=moment, fact_at=moment)
        record = result["data"]["records"][0]
        self.assertTrue(record["temporal"]["historical"])
        self.assertIn("小梦", record["text"])
        expanded = session.call_tool("memory_read", {"references": [record["ref"]], "max_tokens": 2048})
        self.assertIn("小梦", expanded["data"]["records"][0]["text"])
        self.assertIn("梦梦", str(session.bootstrap()))

    def test_future_effective_date_rejected_and_failed_write_has_no_field_history(self):
        with self.assertRaises(ValueError):
            self.runtime.apply_core_update(self.scope, "identity", {"名字": "未来"}, "非法", effective_from=self.clock.now() + timedelta(days=1))
        with patch.object(self.runtime, "refresh_evidence", side_effect=RuntimeError("故障")):
            with self.assertRaises(RuntimeError):
                self.runtime.apply_core_update(self.scope, "identity", {"名字": "事务失败"}, "测试")
        self.assertEqual(self.count_rows("core_field_revisions"), 0)
        self.assertEqual(self.count_rows("core_versions"), 0)

    def test_same_effective_time_later_knowledge_wins_without_changing_earlier_view(self):
        start = self.clock.now()
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "最初"}, "初始")
        self.clock.advance(hours=1)
        first_known = self.clock.now()
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "第一修订"}, "修订", effective_from=start)
        self.clock.advance(hours=1)
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "第二修订"}, "再修订", effective_from=start)
        self.assertEqual(self.runtime.core_at(self.scope, "identity", knowledge_at=first_known, fact_at=start).payload["名字"], "第一修订")
        self.assertEqual(self.runtime.core_at(self.scope, "identity", knowledge_at=self.clock.now(), fact_at=start).payload["名字"], "第二修订")


class SummaryHistoryTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.items = [self.add_memory(MemoryType.EPISODIC, {"what_happened": text}) for text in
                      ("海边捡贝壳", "海边看日落", "海边听浪声")]
        self.runtime.build_summaries(self.scope)
        self.card = self.repos.summaries.list_active(self.scope)[0]

    def versions(self):
        return self.runtime.summary_history(self.card.summary_id, self.scope)

    def restore(self, revision=1, **changes):
        return self.runtime.restore_summary(self.card.summary_id, revision, self.scope,
            **{"expected_version": self.versions()[0]["revision"], "reason": "恢复已复核版本", "idempotency_key": "restore-1", **changes})

    def test_restore_recovers_old_content_as_new_revision_with_receipt(self):
        old = self.card.digest
        self.clock.advance(seconds=1)
        with self.runtime.db.transaction():
            self.repos.summaries.upsert(replace(self.card, digest="新版概述"), self.clock.now())
        self.assertEqual(self.versions()[0]["revision"], 2)
        receipt = self.restore()
        self.assertEqual(receipt["version"], 3)
        self.assertEqual(self.repos.summaries.get(self.card.summary_id).digest, old)
        self.assertEqual(self.versions()[0]["operation"], "restore:1")
        self.assertTrue(self.restore(expected_version=2)["duplicate"])
        with self.assertRaises(ValueError):
            self.restore(expected_version=2, reason="改写原审批")
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_restore_cannot_publish_changed_or_forgotten_source(self):
        self.runtime.withdraw_summaries([self.card.summary_id], self.scope)
        self.repos.memories.update_content(self.items[0].memory_id, {"what_happened": "已纠正的来源"}, self.clock.now())
        self.assertIsNone(self.versions()[-1]["snapshot"])
        with self.assertRaises(MemorySuppressed):
            self.restore()
        self.runtime.forget_fact(ForgetSelector(memory_ids=[self.items[1].memory_id]))
        self.assertTrue(all(row["snapshot"] is None for row in self.versions()))

    def test_stale_expected_version_and_scope_rejected(self):
        self.runtime.withdraw_summaries([self.card.summary_id], self.scope)
        with self.assertRaises(VersionConflict):
            self.restore(expected_version=1)
        with self.assertRaises(ScopeNotFound):
            self.runtime.summary_history(self.card.summary_id, replace(self.scope, user_id="other"))

    def test_noop_rebuild_does_not_invent_new_content_revision(self):
        count = self.count_rows("summary_revisions")
        self.clock.advance(seconds=1)
        self.runtime.build_summaries(self.scope)
        self.assertEqual(self.count_rows("summary_revisions"), count)

    def test_restore_failure_rolls_back_content_history_and_receipt(self):
        self.runtime.withdraw_summaries([self.card.summary_id], self.scope)
        count = self.count_rows("summary_revisions")
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("故障")):
            with self.assertRaises(RuntimeError):
                self.restore()
        self.assertEqual(self.repos.summaries.get(self.card.summary_id).status, "withdrawn")
        self.assertEqual(self.count_rows("summary_revisions"), count)
        self.assertEqual(self.count_rows("summary_restore_receipts"), 0)

    def test_snapshot_and_scope_purge_include_history(self):
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦"}, "初始")
        self.runtime.withdraw_summaries([self.card.summary_id], self.scope)
        self.restore()
        path = Path(self._tmpdir.name) / "history.json"
        self.runtime.export_snapshot(str(path))
        settings = replace(self.runtime.settings, db_path=Path(self._tmpdir.name) / "history-copy.db")
        with MemoryRuntime(settings, clock=self.clock) as target:
            target.db.meta_set("forgetting_hmac_key", self.runtime.db.meta_get("forgetting_hmac_key"))
            target.import_snapshot(str(path))
            self.assertEqual(len(target.summary_history(self.card.summary_id, self.scope)), 3)
            self.assertEqual(target.core_at(self.scope, "identity", knowledge_at=self.clock.now(), fact_at=self.clock.now()).payload["名字"], "小梦")
        self.runtime.forget_scope(self.scope)
        for table in ("summary_revisions", "summary_restore_receipts", "core_field_revisions"):
            self.assertEqual(self.count_rows(table), 0)

    def test_v23_upgrade_seeds_existing_core_and_summary_once(self):
        start = self.clock.now()
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "初始"}, "已有核心")
        self.clock.advance(seconds=1)
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "新名"}, "已有核心变化")
        for table in ("proposal_batches", "summary_restore_receipts", "summary_revisions", "core_field_revisions"):
            self.runtime.db.connection.execute(f"DROP TABLE {table}")
        self.runtime.db.meta_set("schema_version", "23")
        for _ in range(2):
            with MemoryRuntime(self.runtime.settings, clock=self.clock) as migrated:
                self.assertEqual(migrated.db.meta_get("schema_version"), SCHEMA_VERSION)
                self.assertEqual(migrated.core_at(self.scope, "identity", knowledge_at=start, fact_at=start).payload["名字"], "初始")
                history = migrated.summary_history(self.card.summary_id, self.scope)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["operation"], "migration_baseline")
