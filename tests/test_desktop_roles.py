"""Role forms exercise real domain persistence and the trusted page boundary."""

from dataclasses import replace
from unittest.mock import patch

from memorydreaming import HostAdapter, SessionBinding
from memorydreaming.desktop import DesktopService
from memorydreaming.llm import AdapterError
from memorydreaming.llm import dumps
from memorydreaming.models import Scope, new_id
from tests.helpers import RuntimeTestCase


class DesktopRolesTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.binding = SessionBinding("u1", "c1", "r1", "desktop-test")
        self.adapter = HostAdapter(self.runtime)
        self.desktop = DesktopService(self.adapter, self.binding)

    def args(self, kind, **changes):
        payloads = {
            "narrative": {"title": "海边的故事", "text": "我们一起虚构过海边故事。", "perspective": "shared", "modality": "shared_fiction"},
            "intention": {"text": "下次继续讲海边故事", "owner": "character", "category": "continuation"},
            "companionship": {"value": "睡前说话轻柔一些"},
            "growth": {"value": "用短句讲睡前故事"},
        }
        args = {"role_type": kind, "expected_version": 0, "payload": payloads[kind],
                "reason": "用户在管理页面明确确认", "request_id": new_id("desktop")}
        if kind == "companionship":
            args.update(id="bedtime", context="sleep")
        elif kind == "growth":
            args["id"] = "storytelling"
        args.update(changes)
        return args

    def save(self, kind, **changes):
        return self.desktop.handle("role_save", self.args(kind, **changes))

    def catalog(self, kind, **changes):
        return self.desktop.handle("role_catalog", {"role_type": kind, **changes})

    def transition(self, saved, state, **changes):
        receipt = saved["receipt"]
        args = {key: receipt[key] for key in ("role_type", "id")}
        if "context" in receipt:
            args["context"] = receipt["context"]
        args.update(expected_version=receipt["version"], state=state, reason="用户明确变更状态", request_id=new_id("transition"))
        args.update(changes)
        return self.desktop.handle("role_transition", args)

    def history(self, receipt, **changes):
        args = {key: receipt[key] for key in ("role_type", "id")}
        if "context" in receipt:
            args["context"] = receipt["context"]
        return self.desktop.handle("role_history", {**args, **changes})

    def assert_error(self, code, action, args):
        with self.assertRaises(AdapterError) as caught:
            self.desktop.handle(action, args)
        self.assertEqual(caught.exception.code, code)

    def test_four_forms_create_current_heads_and_terminal_history(self):
        for kind, state in (("narrative", "withdrawn"), ("intention", "completed"),
                            ("companionship", "revoked"), ("growth", "revoked")):
            with self.subTest(kind=kind):
                saved = self.save(kind)
                item = self.catalog(kind)["items"][0]
                self.assertEqual(item["id"], saved["receipt"]["id"])
                self.assertFalse(item["redacted"])
                self.assertEqual(item["version"], 2 if kind == "intention" else 1)
                result = self.transition(saved, state)
                terminal = self.catalog(kind)["items"][0]
                self.assertEqual(terminal["state"], state)
                self.assertEqual(terminal["version"], result["receipt"]["version"])
                self.assertGreaterEqual(len(self.history(result["receipt"])["items"]), 2)
                if kind == "narrative":
                    self.assertEqual(terminal["payload"]["modality"], "shared_fiction")
                if kind in ("companionship", "growth"):
                    self.assertIsNone(terminal["payload"]["value"])

    def test_edits_compare_versions_and_intention_is_reconfirmed(self):
        for kind in ("narrative", "intention", "companionship", "growth"):
            with self.subTest(kind=kind):
                saved = self.save(kind)
                version = saved["receipt"]["version"]
                args = self.args(kind, id=saved["receipt"]["id"], expected_version=version)
                if kind == "growth":
                    args["allow_recent"] = True
                result = self.desktop.handle("role_save", args)
                increment = 2 if kind == "intention" else 1
                self.assertEqual(result["receipt"]["version"], version+increment)
                if kind == "intention":
                    item = self.runtime.intentions.get(self.scope, result["receipt"]["id"])
                    self.assertEqual(item["state"], "open")
                    self.assertEqual(item["confirmation_refs"][0]["target"], result["confirmation_event_id"])
                self.assert_error("STALE_REFERENCE", "role_save", {**args, "request_id": new_id("stale")})

    def test_duplicate_write_is_stable_after_subsequent_changes(self):
        for kind, terminal in (("narrative", "withdrawn"), ("intention", "cancelled"),
                               ("companionship", "revoked"), ("growth", "revoked")):
            with self.subTest(kind=kind):
                args = self.args(kind)
                first = self.desktop.handle("role_save", args)
                changed = self.transition(first, terminal)
                events = self.count_rows("events")
                self.assertEqual(self.desktop.handle("role_save", args), first)
                self.assertEqual(events, self.count_rows("events"))
                self.assertGreater(changed["receipt"]["version"], first["receipt"]["version"])
                self.assert_error("IDEMPOTENCY_CONFLICT", "role_save", {**args, "reason": "不同理由"})

    def test_duplicate_transition_retains_original_receipt_after_later_save(self):
        first = self.save("companionship")
        args = {"role_type": "companionship", "id": "bedtime", "context": "sleep", "expected_version": 1,
                "state": "revoked", "reason": "撤销", "request_id": "stable-revoke"}
        revoked = self.desktop.handle("role_transition", args)
        self.save("companionship", expected_version=2)
        self.assertEqual(self.desktop.handle("role_transition", args), revoked)

    def test_growth_cooldown_requires_explicit_boolean_override(self):
        first = self.save("growth")
        args = self.args("growth", expected_version=first["receipt"]["version"])
        events = self.count_rows("events")
        self.assert_error("INVALID_ARGUMENT", "role_save", args)
        self.assertEqual(self.count_rows("events"), events)
        for value in (1, "true", None):
            self.assert_error("INVALID_ARGUMENT", "role_save", {**args, "allow_recent": value})
        self.assertEqual(self.desktop.handle("role_save", {**args, "allow_recent": True})["receipt"]["version"], 2)
        self.assert_error("INVALID_ARGUMENT", "role_save", self.args("narrative", allow_recent=True))

    def test_owner_category_and_terminal_narrative_intention_are_immutable(self):
        first = self.save("intention")
        args = self.args("intention", id=first["receipt"]["id"], expected_version=2)
        args["payload"]["owner"] = "user"
        self.assert_error("INVALID_ARGUMENT", "role_save", args)
        closed = self.transition(first, "cancelled")
        args = self.args("intention", id=first["receipt"]["id"], expected_version=closed["receipt"]["version"])
        self.assert_error("INVALID_ARGUMENT", "role_save", args)
        narrative = self.save("narrative")
        closed = self.transition(narrative, "withdrawn")
        self.assert_error("NOT_FOUND_OR_FORBIDDEN", "role_save", self.args("narrative", id=narrative["receipt"]["id"], expected_version=closed["receipt"]["version"]))

    def test_revoked_preference_and_growth_accept_explicit_current_version(self):
        for kind in ("companionship", "growth"):
            saved = self.save(kind)
            revoked = self.transition(saved, "revoked")
            self.assert_error("STALE_REFERENCE", "role_save", self.args(kind))
            restored = self.save(kind, expected_version=revoked["receipt"]["version"])
            self.assertEqual(restored["receipt"]["version"], 3)

    def test_forgetting_hides_body_preserves_head_and_prevents_resurrection(self):
        for kind in ("narrative", "intention", "companionship", "growth"):
            with self.subTest(kind=kind):
                args = self.args(kind)
                first = self.desktop.handle("role_save", args)
                self.runtime.forget_event(first["confirmation_event_id"], scope=self.scope)
                item = self.catalog(kind)["items"][0]
                self.assertTrue(item["redacted"])
                self.assertEqual(item["payload"], {})
                self.assertEqual(item["id"], first["receipt"]["id"])
                fresh = self.args(kind, id=item["id"], expected_version=item["version"])
                self.assert_error("NOT_FOUND_OR_FORBIDDEN", "role_save", fresh)
                self.assert_error("STALE_REFERENCE", "role_save", {**fresh, "expected_version": 0})
                with self.assertRaises(AdapterError):
                    self.desktop.handle("role_save", args)
                history = self.history(first["receipt"])
                self.assertTrue(all(row["redacted"] for row in history["items"]))

    def test_readonly_scope_and_unexpected_keys_rejected(self):
        saved = self.save("narrative")
        foreign = DesktopService(self.adapter, replace(self.binding, relationship_id="other"))
        self.assertEqual(foreign.handle("role_catalog", {"role_type": "narrative"})["items"], [])
        with self.assertRaises(AdapterError):
            foreign.handle("role_history", {"role_type": "narrative", "id": saved["receipt"]["id"]})
        read_only = DesktopService(self.adapter, self.binding, read_only=True)
        for action in ("role_save", "role_transition"):
            with self.assertRaises(AdapterError) as caught:
                read_only.handle(action, {})
            self.assertEqual(caught.exception.code, "READ_ONLY")
        for additions in ({"scope": {}}, {"sources": []}, {"context": "other"}):
            self.assert_error("INVALID_ARGUMENT", "role_save", self.args("narrative", **additions))

    def test_failed_approval_rolls_back_confirmation_and_proposal(self):
        before = {table: self.count_rows(table) for table in ("events", "narrative_proposals", "narrative_entries", "journal_entries")}
        with patch.object(self.runtime.narrative, "approve", side_effect=ValueError("模拟事务中失败")):
            self.assert_error("INVALID_ARGUMENT", "role_save", self.args("narrative"))
        self.assertEqual(before, {table: self.count_rows(table) for table in before})

    def test_receipt_failure_rolls_back_every_role_change(self):
        journal = self.runtime.db.repositories.journal
        append = journal.append

        def fail_receipt(event_type, **kwargs):
            if event_type == "desktop.role_operation":
                raise ValueError("模拟最终回执写入失败")
            return append(event_type, **kwargs)

        for kind, table in (("narrative", "narrative_entries"), ("intention", "intentions"),
                            ("companionship", "companionship_preferences"), ("growth", "growth_values")):
            with self.subTest(kind=kind):
                tables = ("events", table, "journal_entries")
                before = {name: self.count_rows(name) for name in tables}
                with patch.object(journal, "append", side_effect=fail_receipt):
                    self.assert_error("INVALID_ARGUMENT", "role_save", self.args(kind))
                self.assertEqual(before, {name: self.count_rows(name) for name in tables})

    def test_activity_sources_are_redacted_by_page_and_cannot_be_edited(self):
        saved = self.save("narrative")
        # A page's authorization may change independently of still-valid domain
        # evidence. Simulate the ViewReader denying its previously visible root.
        with patch.object(self.desktop, "_sources_visible", return_value=False):
            item = self.catalog("narrative")["items"][0]
            self.assertTrue(item["redacted"])
            self.assertEqual(item["payload"], {})
            self.assertEqual(item["version"], saved["receipt"]["version"])
            history = self.history(saved["receipt"])
            self.assertTrue(all(row["snapshot"] is None for row in history["items"]))
            self.assert_error("NOT_FOUND_OR_FORBIDDEN", "role_save", self.args("narrative", id=item["id"], expected_version=item["version"]))

    def test_forgetting_a_revocation_does_not_allow_tombstone_overwrite(self):
        for kind in ("companionship", "growth"):
            saved = self.save(kind)
            revoked = self.transition(saved, "revoked")
            self.runtime.forget_event(revoked["confirmation_event_id"], scope=self.scope)
            item = self.catalog(kind)["items"][0]
            self.assertTrue(item["redacted"])
            self.assert_error("NOT_FOUND_OR_FORBIDDEN", "role_save", self.args(kind, expected_version=revoked["receipt"]["version"]))

    def test_cross_scope_save_cannot_overwrite_foreign_identifier(self):
        saved = self.save("intention")
        other = DesktopService(self.adapter, replace(self.binding, relationship_id="other"))
        for expected in (0, saved["receipt"]["version"]):
            with self.assertRaises(AdapterError):
                other.handle("role_save", self.args("intention", id=saved["receipt"]["id"], expected_version=expected))
        self.assertEqual(self.catalog("intention")["items"][0]["version"], saved["receipt"]["version"])

    def test_catalog_pagination_and_history_limits_are_bounded(self):
        saved = [self.save("narrative") for _ in range(3)]
        first = self.catalog("narrative", limit=2)
        second = self.catalog("narrative", limit=2, offset=first["next_offset"])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({row["id"] for row in first["items"]+second["items"]}), 3)
        for _ in range(3):
            current = saved[0]["receipt"]
            saved[0] = self.save("narrative", id=current["id"], expected_version=current["version"])
        history = self.history(saved[0]["receipt"], limit=2)
        self.assertEqual(len(history["items"]), 2)
        self.assertTrue(history["has_more"])
        self.assertTrue(history["truncated"])
        for value in (True, 0, 51):
            self.assert_error("INVALID_ARGUMENT", "role_history", {"role_type": "narrative", "id": saved[0]["receipt"]["id"], "limit": value})

    def test_timezone_and_payload_fields_are_validated(self):
        args = self.args("intention")
        args["payload"]["due_at"] = "2026-11-01T21:00:00+08:00"
        saved = self.desktop.handle("role_save", args)
        self.assertIn("13:00", self.catalog("intention")["items"][0]["payload"]["due_at"])
        for bad in ("2026-11-01T21:00:00", "not-a-time"):
            args = self.args("intention")
            args["payload"]["due_at"] = bad
            self.assert_error("INVALID_ARGUMENT", "role_save", args)
        args = self.args("growth")
        args["payload"]["sources"] = [saved["confirmation_event_id"]]
        self.assert_error("INVALID_ARGUMENT", "role_save", args)

    def test_large_unicode_catalog_and_history_shrink_without_skipping_rows(self):
        payload = {"title": "长篇自传", "text": "梦"*2000, "perspective": "shared", "modality": "shared_fiction"}
        saved = [self.save("narrative", payload=payload) for _ in range(50)]
        first = self.catalog("narrative", limit=50)
        self.assertTrue(first["truncated"])
        self.assertTrue(first["has_more"])
        self.assertLess(len(dumps(first).encode("utf-8")), 262144)
        self.assertEqual(first["next_offset"], len(first["items"]))
        second = self.catalog("narrative", limit=50, offset=first["next_offset"])
        self.assertFalse(second["has_more"])
        self.assertEqual({item["id"] for item in first["items"]+second["items"]}, {item["receipt"]["id"] for item in saved})
        current = saved[0]
        for _ in range(49):
            current = self.save("narrative", id=current["receipt"]["id"], expected_version=current["receipt"]["version"], payload=payload)
        history = self.history(current["receipt"], limit=50)
        self.assertTrue(history["truncated"])
        self.assertTrue(history["has_more"])
        self.assertLess(len(dumps(history).encode("utf-8")), 262144)
        self.assertLess(len(history["items"]), 50)
        self.assertTrue(all(len(item["snapshot"]["text"]) == 2000 for item in history["items"]))
