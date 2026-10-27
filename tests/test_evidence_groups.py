"""类型化 AND/OR 来源、版本锁定、外部根、宿主读取与便携性。"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memorydreaming import EvidenceRef, HostAdapter, SessionBinding
from memorydreaming.evidence import EvidenceEngine
from memorydreaming.errors import MemorySuppressed, ScopeNotFound, VersionConflict
from memorydreaming.models import Correction, ForgetSelector, LinkRelation, MemoryStatus, MemoryType, Scope, new_id
from memorydreaming.references import ViewReader
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import SCHEMA_VERSION
from tests.helpers import RuntimeTestCase


class EvidenceGroupsTest(RuntimeTestCase):
    def node(self, text, **kwargs):
        return self.add_memory(MemoryType.EPISODIC, {"what_happened": text}, **kwargs)

    def ref(self, item):
        return self.runtime.evidence_ref("memory", item.memory_id, item.scope)

    def bind(self, child, groups, version=0):
        return self.runtime.set_evidence_groups(child.memory_id, child.scope, groups,
                                                expected_version=version, reason="可信宿主确认推导依据")

    def test_and_missing_one_premise_retracts_downstream_and_its_summary(self):
        a, b, child, next_child = [self.node(text) for text in ("海边有空", "海边开放", "海边出行建议", "海边下一步")]
        self.bind(child, [[self.ref(a), self.ref(b)]])
        self.bind(next_child, [[self.ref(child)]])
        self.runtime.build_summaries(self.scope)
        receipt = self.runtime.forget_fact(ForgetSelector(memory_ids=[a.memory_id]))
        self.assertIn(child.memory_id, receipt.invalidated_derived)
        self.assertIn(next_child.memory_id, receipt.invalidated_derived)
        self.assertEqual(self.repos.memories.get(b.memory_id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.RETRACTED)
        self.assertNotIn(child.memory_id, [x.memory_id for x in self.runtime.retrieve("海边", self.scope).items])
        self.assertFalse(self.repos.summaries.list_active(self.scope))

    def test_or_keeps_independent_group_until_all_groups_fail(self):
        a, b, child = [self.node(text) for text in ("来源甲", "来源乙", "独立支持结论")]
        self.bind(child, [[self.ref(a)], [self.ref(b)]])
        self.runtime.forget_fact(ForgetSelector(memory_ids=[a.memory_id]))
        self.assertTrue(self.runtime.inspect_evidence(child.memory_id, self.scope)["supported"])
        self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.ACTIVE)
        self.runtime.forget_fact(ForgetSelector(memory_ids=[b.memory_id]))
        self.assertFalse(self.runtime.inspect_evidence(child.memory_id, self.scope)["supported"])
        self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.RETRACTED)

    def test_event_premise_loss_works_without_flat_source_event_ids(self):
        event = self.runtime.observe(self.make_turn("地点已开放"), self.scope, "event-root").event
        child = self.node("可以出发")
        ref = self.runtime.evidence_ref("event", event.event_id, self.scope)
        self.bind(child, [[ref]])
        self.assertEqual(self.repos.memories.get(child.memory_id).source_event_ids, [])
        result = self.runtime.forget_event(event.event_id)
        self.assertIn(child.memory_id, result.invalidated_derived)
        self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.RETRACTED)

    def test_core_version_is_pinned_and_change_retracts_dependents(self):
        self.runtime.apply_core_update(self.scope, "identity", {"身份": "同行者"}, "用户设定")
        child = self.node("由当前身份制定的安排")
        ref = self.runtime.evidence_ref("core_version", "identity", self.scope)
        self.assertIsInstance(ref, EvidenceRef)
        self.bind(child, [[ref]])
        self.runtime.apply_core_update(self.scope, "identity", {"身份": "讲述者"}, "用户修改", expected_version=1)
        self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.RETRACTED)

    def test_core_cannot_turn_its_dependent_memory_into_its_own_root(self):
        child = self.node("未经独立支持的身份推断")
        self.runtime.apply_core_update(self.scope, "identity", {"身份": "推断角色"}, "错误回路",
                                      field_evidence_ids={"身份": [child.memory_id]})
        core = self.runtime.evidence_ref("core_version", "identity", self.scope)
        with self.assertRaises(MemorySuppressed):
            self.bind(child, [[core]])
        self.assertEqual(self.count_rows("evidence_sets"), 0)

    def test_unsafe_or_self_references_are_rejected_without_partial_storage(self):
        a, child = self.node("依据"), self.node("结论")
        ref = self.ref(a)
        for groups in ([], [[]], [[ref] * 21], [[EvidenceRef("summary", "x", "0" * 64)]], [[self.ref(child)]]):
            with self.subTest(groups=groups), self.assertRaises((ValueError, MemorySuppressed)):
                self.bind(child, groups)
        self.assertEqual(self.count_rows("evidence_sets"), 0)

    def test_memory_content_change_invalidates_pinned_child_but_reads_do_not(self):
        parent, child = self.node("旧依据"), self.node("推导结论")
        self.bind(child, [[self.ref(parent)]])
        self.runtime.mark_used([parent.memory_id])
        self.assertTrue(self.runtime.inspect_evidence(child.memory_id, self.scope)["supported"])
        with self.runtime.db.transaction():
            self.repos.memories.update_content(parent.memory_id, {"what_happened": "新依据"}, self.clock.now())
        self.assertTrue(self.repos.forgetting.is_suppressed(self.repos.memories.get(child.memory_id)))
        self.assertIn(child.memory_id, self.runtime.refresh_evidence())

    def test_correction_change_and_fix_propagate(self):
        for kind in ("change", "fix"):
            with self.subTest(kind=kind):
                parent, child = self.node(f"原始依据{kind}"), self.node(f"结论{kind}")
                self.bind(child, [[self.ref(parent)]])
                result = self.runtime.correct(parent.memory_id, Correction(kind, corrected_body={"what_happened": f"新依据{kind}"}))
                self.assertIn(child.memory_id, result.invalidated_derived)
                self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.RETRACTED)

    def test_rebinding_parent_invalidates_pinned_downstream(self):
        a, b, parent, child = [self.node(text) for text in ("依据甲", "依据乙", "中间归纳", "下游结论")]
        first = self.bind(parent, [[self.ref(a)]])
        self.bind(child, [[self.ref(parent)]])
        result = self.bind(parent, [[self.ref(b)]], first["version"])
        self.assertIn(child.memory_id, result["invalidated_derived"])
        self.assertEqual(self.repos.memories.get(parent.memory_id).status, MemoryStatus.ACTIVE)

    def test_stale_expected_version_and_source_revision_do_not_overwrite_groups(self):
        a, b, child = self.node("甲"), self.node("乙"), self.node("结论")
        stale_ref = self.ref(b)
        self.bind(child, [[self.ref(a)]])
        with self.assertRaises(VersionConflict):
            self.bind(child, [[stale_ref]])
        with self.runtime.db.transaction():
            self.repos.memories.update_content(b.memory_id, {"what_happened": "乙已变更"}, self.clock.now())
        with self.assertRaises(MemorySuppressed):
            self.bind(child, [[stale_ref]], 1)
        self.assertEqual(self.runtime.inspect_evidence(child.memory_id, self.scope)["version"], 1)

    def test_foreign_relationship_activity_and_domain_cannot_become_evidence(self):
        child = self.node("受限结论")
        for scope in (replace(self.scope, user_id="other"), replace(self.scope, relationship_id="other"),
                      replace(self.scope, activity_id="private"), replace(self.scope, domain="fiction")):
            item = replace(self.node("外部来源"), memory_id=new_id("mem"), scope=scope)
            with self.runtime.db.transaction():
                self.repos.memories.insert(item)
            with self.subTest(scope=scope), self.assertRaises(ScopeNotFound):
                self.runtime.evidence_ref("memory", item.memory_id, self.scope)
            foreign = self.runtime.evidence_ref("memory", item.memory_id, scope)
            with self.assertRaises(MemorySuppressed):
                self.bind(child, [[foreign]])

    def test_cycle_without_external_root_is_hidden_even_after_incorrect_sql_reactivation(self):
        root, a, b = self.node("外部根"), self.node("循环甲"), self.node("循环乙")
        with self.runtime.db.transaction():
            for child, parent in ((a, root), (a, b), (b, a)):
                self.repos.links.add(child.memory_id, parent.memory_id, LinkRelation.DERIVED_FROM, self.clock.now())
        self.runtime.forget_fact(ForgetSelector(memory_ids=[root.memory_id]))
        self.runtime.db.connection.execute("UPDATE memories SET status='active' WHERE memory_id IN (?,?)", (a.memory_id, b.memory_id))
        self.assertTrue(self.repos.forgetting.is_suppressed(self.repos.memories.get(a.memory_id)))
        self.assertEqual(self.runtime.retrieve("循环", self.scope).items, [])

    def test_graph_budget_fails_closed_without_mass_retraction(self):
        a, child = self.node("根"), self.node("结论")
        self.bind(child, [[self.ref(a)]])
        engine = EvidenceEngine(self.runtime.db.connection, self.scope, max_nodes=1).solve([("memory", child.memory_id)])
        self.assertFalse(engine.complete)
        self.assertEqual(engine.live, set())
        self.assertEqual(self.repos.memories.get(child.memory_id).status, MemoryStatus.ACTIVE)

    def test_host_expands_complete_and_witness_after_other_or_branch_is_deleted(self):
        scope = Scope("u1", "c1", "r1")
        def scoped(text):
            return replace(self.node(text), scope=scope)
        nodes = [scoped(text) for text in ("海边甲", "海边乙", "海边丙", "海边推导")]
        for item in nodes:
            self.runtime.db.connection.execute("UPDATE memories SET relationship_id='r1' WHERE memory_id=?", (item.memory_id,))
        a, b, c, child = nodes
        self.bind(child, [[self.ref(a), self.ref(b)], [self.ref(c)]])
        self.runtime.forget_fact(ForgetSelector(memory_ids=[a.memory_id]))
        session = HostAdapter(self.runtime).open_session(SessionBinding("u1", "c1", "r1", "window"))
        result = session.call_tool("memory_search", {"query": "海边推导", "detail": "evidence", "activity_ref": None,
                                   "time_range": None, "max_items": 8, "max_tokens": 4096})
        hit = next(item for item in result["data"]["evidence"] if "推导" in item["text"])
        self.assertEqual(hit["support"]["mode"], "all")
        self.assertEqual(hit["support"]["source_refs"], hit["source_refs"])
        expanded = session.call_tool("memory_read", {"references": hit["source_refs"], "max_tokens": 4096})
        self.assertEqual(len(expanded["data"]["records"]), 1)
        self.assertIn("海边丙", expanded["data"]["records"][0]["text"])
        view = ViewReader(self.runtime, session.binding, self.clock).load("memory", child.memory_id)
        self.assertEqual([source.target for source in view.sources], [c.memory_id])

    def test_pinned_core_is_readable_through_llm_source_refs(self):
        scope = Scope("u1", "c1", "r1")
        self.runtime.apply_core_update(scope, "identity", {"名字": "小梦"}, "用户设定")
        child = self.node("海边角色安排")
        self.runtime.db.connection.execute("UPDATE memories SET relationship_id='r1' WHERE memory_id=?", (child.memory_id,))
        child = replace(child, scope=scope)
        self.bind(child, [[self.runtime.evidence_ref("core_version", "identity", scope)]])
        reader = ViewReader(self.runtime, SessionBinding("u1", "c1", "r1", "core-window"), self.clock)
        view = reader.load("memory", child.memory_id)
        self.assertEqual(view.sources[0].kind, "core_version")
        self.assertIn("小梦", view.sources[0].data["text"])

    def test_binding_failure_rolls_back_all_tables_and_version(self):
        parent, child = self.node("根"), self.node("结论")
        ref = self.ref(parent)
        with patch.object(self.repos.journal, "append", side_effect=RuntimeError("故障")):
            with self.assertRaises(RuntimeError):
                self.bind(child, [[ref]])
        self.assertEqual(self.count_rows("evidence_sets"), 0)
        self.assertEqual(self.count_rows("evidence_groups"), 0)
        self.assertEqual(self.count_rows("evidence_members"), 0)
        self.assertEqual(self.bind(child, [[ref]])["version"], 1)

    def test_scope_snapshot_roundtrip_and_purge_preserve_no_orphan_support(self):
        a, b, child = self.node("依据甲"), self.node("依据乙"), self.node("结论")
        self.bind(child, [[self.ref(a), self.ref(b)]])
        path = Path(self._tmpdir.name) / "evidence.json"
        self.runtime.export_snapshot(str(path), scope=self.scope)
        settings = replace(self.runtime.settings, db_path=Path(self._tmpdir.name) / "copy.db")
        with MemoryRuntime(settings, clock=self.clock) as target:
            target.db.meta_set("forgetting_hmac_key", self.runtime.db.meta_get("forgetting_hmac_key"))
            # 沿用作用域快照的已有契约：目标须先同步相同的遗忘代次。
            with target.db.transaction():
                for _ in range(self.repos.forgetting.epoch(self.scope)):
                    target.db.repositories.forgetting.bump(self.scope)
            target.import_snapshot(str(path))
            self.assertTrue(target.inspect_evidence(child.memory_id, self.scope)["supported"])
            target.forget_fact(ForgetSelector(memory_ids=[a.memory_id]))
            self.assertEqual(target.db.repositories.memories.get(child.memory_id).status, MemoryStatus.RETRACTED)
        self.runtime.forget_scope(self.scope)
        for table in ("evidence_sets", "evidence_groups", "evidence_members"):
            self.assertEqual(self.count_rows(table), 0)
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_v22_upgrade_keeps_legacy_or_links(self):
        a, b, child = self.node("甲"), self.node("乙"), self.node("结论")
        for parent in (a, b):
            self.repos.links.add(child.memory_id, parent.memory_id, LinkRelation.DERIVED_FROM, self.clock.now())
        for table in ("evidence_members", "evidence_groups", "evidence_sets"):
            self.runtime.db.connection.execute(f"DROP TABLE {table}")
        self.runtime.db.meta_set("schema_version", "22")
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as migrated:
            self.assertEqual(migrated.db.meta_get("schema_version"), SCHEMA_VERSION)
            migrated.forget_fact(ForgetSelector(memory_ids=[a.memory_id]))
            self.assertEqual(migrated.db.repositories.memories.get(child.memory_id).status, MemoryStatus.ACTIVE)
