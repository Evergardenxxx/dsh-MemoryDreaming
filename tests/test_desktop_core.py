"""桌面核心编辑使用真实SQLite、字段历史、来源和原子用户确认。"""

from dataclasses import replace
import json
from unittest.mock import patch

from memorydreaming import HostAdapter, MemoryRuntime, SessionBinding
from memorydreaming.desktop import DesktopService
from memorydreaming.errors import MemorySuppressed
from memorydreaming.llm import AdapterError
from memorydreaming.models import CoreKind, ForgetSelector, MemoryType, Scope, new_id
from tests.helpers import NOW, RuntimeTestCase


class DesktopCoreTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.binding = SessionBinding("u1", "c1", "r1", "desktop-test")
        self.desktop = DesktopService(HostAdapter(self.runtime), self.binding)
        self.initial = self.desktop.handle("initialize_role", {
            "identity_fields": {"名字": "小梦", "身份": "长期陪伴角色"},
            "relationship_fields": {"称呼": "爸爸", "相处设定": "父女角色称呼"},
            "expected_version": 0, "request_id": "initial-core"})

    def editor(self, kind="identity", **args):
        return self.desktop.handle("core_editor", {"core_kind": kind, **args})

    def arguments(self, *, kind="identity", patch=None, removed=None, **changes):
        state = self.editor(kind)
        args = {"core_kind": kind, "patch": {"名字": "小梦新名"} if patch is None else patch,
                "remove_fields": [] if removed is None else removed,
                "expected_version": state["version"], "expected_revision": state["revision"],
                "reason": "用户明确修改核心", "request_id": new_id("core-save")}
        return {**args, **changes}

    def save(self, **kwargs):
        return self.desktop.handle("core_save", self.arguments(**kwargs))

    def assert_error(self, code, action, args):
        with self.assertRaises(AdapterError) as caught:
            self.desktop.handle(action, args)
        self.assertEqual(caught.exception.code, code)

    def event(self, content, *, scope=None):
        return self.runtime.observe(self.make_turn(content), scope or self.scope, new_id("source")).event

    def test_editor_reports_exact_fields_source_preview_and_version_history(self):
        state = self.editor()
        self.assertTrue(state["editable"])
        self.assertFalse(state["redacted_fields"])
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["fields"]["名字"], "小梦")
        source = state["field_sources"]["名字"][0]
        self.assertEqual(source["id"], self.initial["confirmation_event_id"])
        self.assertIn("桌面用户明确操作", source["text"])
        self.assertEqual(state["history"]["items"][0]["fields"], state["fields"])

    def test_patch_adds_json_values_preserves_untouched_field_sources_and_skips_extraction(self):
        first = self.repos.cores.latest(self.scope, CoreKind.IDENTITY)
        result = self.save(patch={"名字": "小梦新名", "配置": {"昵称": ["梦", None], "主动": False, "次数": 3, "温度": .75}})
        core = self.repos.cores.latest(self.scope, CoreKind.IDENTITY)
        self.assertEqual(core.core_version, 2)
        self.assertEqual(core.payload["配置"], {"昵称": ["梦", None], "主动": False, "次数": 3, "温度": .75})
        self.assertEqual(core.field_evidence_ids["身份"], first.field_evidence_ids["身份"])
        self.assertEqual(core.field_evidence_ids["名字"], [result["confirmation_event_id"]])
        self.assertEqual(self.count_rows("consolidation_jobs"), 0)
        payload = self.runtime.db.connection.execute("SELECT payload_json FROM journal_entries WHERE event_type='desktop.core_operation'").fetchone()[0]
        self.assertNotIn("小梦新名", payload)
        self.assertNotIn("用户明确修改", payload)

    def test_removal_has_authorizing_revision_history_and_no_temporal_fallback(self):
        self.clock.advance(seconds=1)
        result = self.save(patch={}, removed=["名字"])
        current = self.editor()
        self.assertNotIn("名字", current["fields"])
        deleted = self.runtime.db.connection.execute("SELECT * FROM core_field_revisions WHERE field_name='名字' AND core_version=2").fetchone()
        self.assertTrue(deleted["deleted"])
        self.assertEqual(json.loads(deleted["evidence_json"]), [result["confirmation_event_id"]])
        historical = self.runtime.core_at(self.scope, CoreKind.IDENTITY, knowledge_at=NOW, fact_at=NOW)
        self.assertEqual(historical.payload["名字"], "小梦")
        self.runtime.forget_event(result["confirmation_event_id"], scope=self.scope)
        self.assertNotIn("名字", self.runtime.core_at(self.scope, CoreKind.IDENTITY,
            knowledge_at=self.clock.now(), fact_at=self.clock.now()).payload)
        state = self.editor()
        self.assertTrue(state["editable"])
        self.assertIsNone(state["history"]["items"][0]["reason"])
        self.save(patch={"身份": "新的陪伴设定"})
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "core_save", self.arguments(patch={"名字": "猜名字改新值"}))

    def test_cas_rejects_changed_version_and_exact_source_revision_without_saving_event(self):
        stale = self.arguments()
        self.save(patch={"名字": "先保存的名字"})
        before = self.count_rows("events")
        self.assert_error("STALE_REFERENCE", "core_save", stale)
        self.assertEqual(self.count_rows("events"), before)
        fresh = self.arguments()
        with self.runtime.db.transaction():
            self.runtime.db.connection.execute("UPDATE events SET content=content||'修订' WHERE event_id=?", (self.initial["confirmation_event_id"],))
        self.assert_error("STALE_REFERENCE", "core_save", fresh)
        self.assertEqual(self.count_rows("events"), before)

    def test_stable_retry_survives_later_edit_and_new_runtime(self):
        args = self.arguments()
        first = self.desktop.handle("core_save", args)
        self.save(patch={"名字": "后来确认的名字"})
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as reopened:
            another = DesktopService(HostAdapter(reopened), self.binding)
            self.assertEqual(another.handle("core_save", args), first)
        self.assertEqual(self.editor()["version"], 3)
        self.assert_error("IDEMPOTENCY_CONFLICT", "core_save", {**args, "reason": "同编号不同内容"})
        self.runtime.forget_event(first["confirmation_event_id"], scope=self.scope)
        # 正文已被擦除，旧幂等键不能再恢复成功回执或重新写入。
        with self.assertRaises(AdapterError):
            self.desktop.handle("core_save", args)

    def test_partial_suppression_of_confirmation_prevents_retry(self):
        args = self.arguments()
        result = self.desktop.handle("core_save", args)
        event = self.repos.events.get(result["confirmation_event_id"])
        memory = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "确认", "obj": "小梦新名"},
            source_event_ids=[event.event_id], source_spans={event.event_id: [[0, 3]]})
        self.runtime.forget_fact(ForgetSelector(memory_ids=[memory.memory_id], scope=self.scope))
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "core_save", args)

    def test_forgotten_source_fields_do_not_leak_or_reappear_through_guessed_new_values(self):
        self.runtime.forget_event(self.initial["confirmation_event_id"], scope=self.scope)
        state = self.editor()
        self.assertNotIn("小梦", json.dumps(state, ensure_ascii=False))
        self.assertFalse(state["fields"])
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "core_save", self.arguments(patch={"名字": "全新但猜中的字段"}))

    def test_invisible_activity_field_disables_editor_without_exposing_name(self):
        activity = self.runtime.register_activity(self.scope, "私密活动")
        scoped = replace(self.scope, activity_id=activity.activity_id)
        event = self.event("秘密新设定", scope=scoped)
        self.runtime.apply_core_update(self.scope, "identity", {"秘密字段": "隐私值"}, "私密理由",
            expected_version=1, evidence_ids=[event.event_id])
        state = self.editor()
        self.assertTrue(state["redacted_fields"])
        self.assertFalse(state["editable"])
        self.assertNotIn("秘密字段", json.dumps(state, ensure_ascii=False))
        self.assertNotIn("隐私值", json.dumps(state, ensure_ascii=False))
        self.assertNotIn("私密理由", json.dumps(state, ensure_ascii=False))
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "core_save", self.arguments(patch={"身份": "试图绕过"}))
        allowed = DesktopService(HostAdapter(self.runtime), replace(self.binding, allowed_activity_ids=(activity.activity_id,)))
        self.assertTrue(allowed.handle("core_editor", {"core_kind": "identity"})["editable"])

    def test_identity_version_shared_relationship_version_isolated(self):
        other = DesktopService(HostAdapter(self.runtime), replace(self.binding, relationship_id="r2"))
        shared = other.handle("core_editor", {"core_kind": "identity"})
        self.assertTrue(shared["exists"])
        self.assertEqual(shared["version"], 1)
        self.assertFalse(shared["editable"])
        self.assertEqual(shared["fields"], {})
        self.assertFalse(other.handle("core_editor", {"core_kind": "relationship"})["exists"])
        self.save(kind="relationship", patch={"称呼": "老爸"})
        self.assertFalse(other.handle("core_editor", {"core_kind": "relationship"})["exists"])
        self.assertEqual(self.editor()["version"], 1)

    def test_read_only_and_unknown_identity_arguments_are_rejected(self):
        readonly = DesktopService(HostAdapter(self.runtime), self.binding, read_only=True)
        self.assertFalse(readonly.handle("core_editor", {"core_kind": "identity"})["editable"])
        with self.assertRaises(AdapterError) as caught:
            readonly.handle("core_save", self.arguments())
        self.assertEqual(caught.exception.code, "READ_ONLY")
        for action, args in (("core_editor", {"core_kind": "identity", "scope": {}}),
                             ("core_save", {**self.arguments(), "user_id": "elsewhere"})):
            self.assert_error("INVALID_ARGUMENT", action, args)

    def test_invalid_patch_numbers_depth_overlap_and_types_never_write(self):
        cases = [{"patch": {}}, {"patch": {"次数": 2**53}}, {"patch": {"次数": 10**10000}},
                 {"patch": {"次数": float("nan")}}, {"patch": {"次数": float("inf")}},
                 {"patch": {"名字": "新值"}, "remove_fields": ["名字"]},
                 {"remove_fields": ["名字", "名字"]}, {"patch": {"列表": list(range(33))}},
                 {"patch": {"对象": {str(i): i for i in range(33)}}},
                 {"patch": {"文本": "长"*4001}}, {"patch": {"不支持": {1, 2}}},
                 {"expected_version": True}]
        for index, changes in enumerate(cases):
            args = self.arguments()
            args.update(changes)
            before = self.count_rows("events")
            with self.subTest(case=index):
                self.assert_error("INVALID_ARGUMENT", "core_save", args)
                self.assertEqual(self.count_rows("events"), before)

    def test_bool_to_integer_change_is_not_mistaken_for_noop(self):
        self.save(patch={"标志": True})
        self.save(patch={"标志": 1})
        self.assertIs(type(self.editor()["fields"]["标志"]), int)

    def test_receipt_or_domain_failure_rolls_back_confirmation_core_and_history(self):
        args = self.arguments()
        before = {table: self.count_rows(table) for table in ("events", "core_versions", "core_field_revisions", "journal_entries")}
        append = self.repos.journal.append
        def fail_receipt(event_type, **kwargs):
            if event_type == "desktop.core_operation":
                raise RuntimeError("simulated receipt disk error")
            return append(event_type, **kwargs)
        with patch.object(self.repos.journal, "append", side_effect=fail_receipt), self.assertRaises(RuntimeError):
            self.desktop.handle("core_save", args)
        self.assertEqual({table: self.count_rows(table) for table in before}, before)
        with patch.object(self.runtime, "apply_core_update", side_effect=RuntimeError("domain write")), self.assertRaises(RuntimeError):
            self.desktop.handle("core_save", args)
        self.assertEqual({table: self.count_rows(table) for table in before}, before)

    def test_oversized_existing_values_are_not_partially_rendered_or_editable(self):
        self.runtime.apply_core_update(self.scope, "identity", {"过长": "私密大文本"*4000}, "明确设定",
            expected_version=1, evidence_ids=[self.initial["confirmation_event_id"]])
        state = self.editor()
        self.assertTrue(state["truncated"])
        self.assertFalse(state["editable"])
        self.assertEqual(state["fields"], {})
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "core_save", self.arguments(patch={"身份": "不能覆盖"}))

    def test_unsafe_existing_integer_disables_lossy_browser_roundtrip(self):
        self.runtime.apply_core_update(self.scope, "identity", {"大整数": 2**60}, "旧版可信写入",
            expected_version=1, evidence_ids=[self.initial["confirmation_event_id"]])
        self.assertFalse(self.editor()["editable"])

    def test_oversized_source_preview_disables_both_rendered_and_forged_save(self):
        sources = [self.event("源"*3000+str(index)).event_id for index in range(8)]
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "多个来源的名字", "身份": "多个来源的身份"},
            "可信来源写入", expected_version=1, evidence_ids=sources)
        state = self.editor()
        self.assertTrue(state["truncated"])
        self.assertFalse(state["editable"])
        self.assertEqual(state["fields"], {})
        args = self.arguments(patch={"名字": "不能绕过预览上限"})
        before = self.count_rows("events")
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "core_save", args)
        self.assertEqual(self.count_rows("events"), before)

    def test_multiple_history_rows_respect_limit(self):
        self.save()
        state = self.editor(limit=1)
        self.assertEqual(len(state["history"]["items"]), 1)
        self.assertTrue(state["history"]["has_more"])

    def test_removing_all_fields_keeps_versioned_empty_core_and_deletion_evidence(self):
        result = self.save(patch={}, removed=["名字", "身份"])
        state = self.editor()
        self.assertEqual(state["fields"], {})
        self.assertTrue(state["exists"])
        self.assertTrue(state["editable"])
        rows = self.runtime.db.connection.execute("SELECT evidence_json FROM core_field_revisions WHERE kind='identity' AND core_version=2").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(json.loads(row[0]) == [result["confirmation_event_id"]] for row in rows))
        # 明确移除与遗忘不同，来源仍有效时可重新填写不同值。
        self.save(patch={"名字": "明确重新填写的名字", "空值": None})
        self.assertIsNone(self.editor()["fields"]["空值"])

    def test_empty_role_cannot_be_created_by_save(self):
        page = DesktopService(HostAdapter(self.runtime), replace(self.binding, relationship_id="r-new"))
        state = page.handle("core_editor", {"core_kind": "relationship"})
        self.assertFalse(state["exists"])
        with self.assertRaises(AdapterError):
            page.handle("core_save", {**self.arguments(), "core_kind": "relationship", "expected_version": 1,
                                      "expected_revision": state["revision"]})

    def test_removed_field_domain_validation_and_past_revision_preserve_later_values(self):
        self.clock.advance(seconds=20)
        event = self.event("名字改成后来的名字")
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "后来的名字"}, "先记录当前名字",
            expected_version=1, evidence_ids=[event.event_id])
        before = self.count_rows("core_versions")
        for removed in (["未知字段"], ["名字", "名字"], ["名字"]):
            values = {"名字": "冲突值"} if removed == ["名字"] else {}
            with self.subTest(removed=removed), self.assertRaises(ValueError):
                self.runtime.apply_core_update(self.scope, "identity", values, "非法删除",
                    expected_version=2, evidence_ids=[event.event_id], remove_fields=removed)
        self.assertEqual(self.count_rows("core_versions"), before)
        # 晚到删除的生效时间早于已经生效的名字变更，不覆盖更晚值。
        self.runtime.apply_core_update(self.scope, "identity", {}, "补记早先的移除",
            expected_version=2, evidence_ids=[event.event_id], remove_fields=["名字"], effective_from=NOW)
        self.assertEqual(self.editor()["fields"]["名字"], "后来的名字")

    def test_nested_json_remains_complete_in_current_fields_and_wrapped_history(self):
        value = "末层值"
        for _ in range(8):
            value = [value]
        self.save(patch={"深层": value})
        state = self.editor()
        self.assertEqual(state["fields"]["深层"], value)
        self.assertEqual(state["history"]["items"][0]["fields"]["深层"], value)
        self.assertTrue(state["editable"])
        self.assertFalse(state["truncated"])
        self.assertFalse(state["history"]["items"][0]["truncated"])
