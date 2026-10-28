"""Desktop management uses real storage/proposal paths, never a mock database."""

from dataclasses import replace
from unittest.mock import patch

from memorydreaming import HostAdapter, SessionBinding
from memorydreaming.desktop import DesktopService
from memorydreaming.bridge import BridgeServer
from memorydreaming.errors import MemorySuppressed
from memorydreaming.host import MemorySession
from memorydreaming.llm import AdapterError
from memorydreaming.models import CoreKind, ForgetSelector, MemoryType, Scope, new_id
from memorydreaming.proposals import ChangeProposals, CoreProposalPolicy
from tests.helpers import RuntimeTestCase


class DesktopTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.binding = SessionBinding("u1", "c1", "r1", "window-1")
        self.policy = CoreProposalPolicy(identity_fields=("名字", "身份"), relationship_fields=("称呼",))
        self.adapter = HostAdapter(self.runtime, core_policy=self.policy)
        self.desktop = DesktopService(self.adapter, self.binding)

    def event(self, content="我喜欢海边故事", *, scope=None, conversation="window-1"):
        return self.runtime.observe(self.make_turn(content, conversation_id=conversation), scope or self.scope, new_id("input")).event

    def memory(self, value="海边故事", *, scope=None):
        scope = scope or self.scope
        event = self.event("我喜欢"+value, scope=scope)
        old_scope, self.scope = self.scope, scope
        try:
            item = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": value},
                source_event_ids=[event.event_id], source_spans={event.event_id: [[0, len(event.content)]]})
        finally:
            self.scope = old_scope
        return event, item

    def initialize(self, **changes):
        args = {"identity_fields": {"名字": "小梦", "身份": "陪伴角色"},
                "relationship_fields": {"称呼": "爸爸"}, "expected_version": 0, "request_id": "first-role"}
        args.update(changes)
        return self.desktop.handle("initialize_role", args)

    def core_candidate(self):
        self.initialize()
        session = MemorySession(self.adapter, self.binding)
        session.prepare_turn("nickname", "以后叫我老爸")
        target = session.bootstrap()["data"]["relationship_ref"]
        result = session.call_tool("memory_core_propose", {"target_ref": target,
            "fields": [{"name": "称呼", "value": "老爸", "evidence_refs": []}],
            "reason": "用户更改称呼", "max_tokens": 4096})
        self.assertEqual(result["status"], "proposed", result)
        return result["data"]["proposal_id"]

    def decide(self, candidate, **changes):
        args = {"proposal_type": candidate["proposal_type"], "id": candidate["id"],
                "decision": "approve", "expected_revision": candidate["revision"],
                "request_id": "review:"+candidate["id"], "reason": "桌面用户确认"}
        if "expected_version" in candidate:
            args["expected_version"] = candidate["expected_version"]
        args.update(changes)
        return self.desktop.handle("proposal_decide", args)

    def test_fixed_binding_unknown_parameters_and_read_only(self):
        overview = self.desktop.handle("overview")
        self.assertEqual(overview["binding"]["relationship_id"], "r1")
        self.assertTrue(overview["needs_initialization"])
        for args in ({"user_id": "foreign"}, {"scope": {}}, {"limit": True}, {"query": []}):
            with self.subTest(args=args), self.assertRaises(AdapterError):
                self.desktop.handle("memories", args)
        readonly = DesktopService(self.adapter, self.binding, read_only=True)
        with self.assertRaises(AdapterError) as caught:
            readonly.handle("initialize_role", {})
        self.assertEqual(caught.exception.code, "READ_ONLY")
        self.assertNotIn("initialize_role", readonly.handle("overview")["capabilities"]["actions"])
        self.assertEqual(self.count_rows("events"), 0)

    def test_memory_browse_filters_before_limit_and_excludes_forgotten(self):
        _, own = self.memory()
        _, forgotten = self.memory("山间故事")
        self.runtime.forget_fact(ForgetSelector(memory_ids=[forgotten.memory_id], scope=self.scope))
        self.memory("另一个关系", scope=replace(self.scope, relationship_id="r2"))
        activity = self.runtime.register_activity(self.scope, "未授权活动")
        self.memory("不授权活动", scope=replace(self.scope, activity_id=activity.activity_id))
        result = self.desktop.handle("memories", {"limit": 1, "memory_type": "semantic", "query": "海边"})
        self.assertEqual([row["id"] for row in result["items"]], [own.memory_id])
        self.assertFalse(result["has_more"])
        self.assertNotIn("source_event_ids_json", result["items"][0])

    def test_browse_pagination_is_stable_and_source_detail_is_current(self):
        created = [self.memory(str(index))[1].memory_id for index in range(4)]
        first = self.desktop.handle("memories", {"limit": 2})
        second = self.desktop.handle("memories", {"limit": 2, "offset": first["next_offset"]})
        self.assertEqual(set(created), {row["id"] for row in first["items"]+second["items"]})
        self.assertFalse(second["has_more"])
        detail = self.desktop.handle("record", {"id": created[0]})
        self.assertEqual(detail["body"]["obj"], "0")
        self.assertEqual(detail["sources"][0]["text"], "我喜欢0")
        foreign = DesktopService(self.adapter, replace(self.binding, relationship_id="foreign"))
        with self.assertRaises(AdapterError):
            foreign.handle("record", {"id": created[0]})

    def test_forgetting_uses_existing_receipt_and_retry_does_not_resurrect(self):
        event, item = self.memory()
        detail = self.desktop.handle("record", {"id": item.memory_id})
        args = {"id": item.memory_id, "expected_revision": detail["revision"], "request_id": "forget-1", "reason": "用户点击遗忘"}
        first = self.desktop.handle("memory_forget", args)
        again = DesktopService(self.adapter, self.binding).handle("memory_forget", args)
        self.assertTrue(first["receipt"]["applied"])
        self.assertEqual(first, again)
        self.assertEqual(self.desktop.handle("memories")["items"], [])
        redacted = self.desktop.handle("record", {"kind": "event", "id": event.event_id})
        self.assertIn("[已抑制片段]", redacted["text"])
        self.assertNotIn("海边故事", redacted["text"])
        with self.assertRaises(AdapterError) as caught:
            self.desktop.handle("memory_forget", {**args, "reason": "更换操作内容"})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_structured_correction_persists_new_fact_with_explicit_source(self):
        _, item = self.memory()
        detail = self.desktop.handle("record", {"id": item.memory_id})
        args = {"id": item.memory_id, "expected_revision": detail["revision"], "request_id": "correction-1", "reason": "现在偏好改变",
                "body": {"subject": "用户", "predicate": "喜欢", "obj": "森林故事", "scope_note": ""}}
        first = self.desktop.handle("memory_correct", args)
        again = self.desktop.handle("memory_correct", args)
        self.assertTrue(first["receipt"]["applied"])
        self.assertEqual(first, again)
        new = next(row for row in self.desktop.handle("memories")["items"] if row["body"]["obj"] == "森林故事")
        source = self.desktop.handle("record", {"id": new["id"]})["sources"][0]
        self.assertEqual(source["id"], first["confirmation_event_id"])
        self.assertIn("桌面用户明确操作", source["text"])
        self.assertEqual(self.count_rows("memories"), 2)

    def test_stale_revision_rolls_back_confirmation_and_mutation(self):
        _, item = self.memory()
        before = self.count_rows("events")
        with self.assertRaises(AdapterError) as caught:
            self.desktop.handle("memory_forget", {"id": item.memory_id, "expected_revision": "old",
                "request_id": "stale", "reason": "用户操作"})
        self.assertEqual(caught.exception.code, "STALE_REFERENCE")
        self.assertEqual(self.count_rows("events"), before)
        self.assertEqual(len(self.desktop.handle("memories")["items"]), 1)

    def test_core_review_uses_original_evidence_and_independent_confirmation(self):
        proposal_id = self.core_candidate()
        candidate = self.desktop.handle("proposals", {"proposal_type": "core"})["items"][0]
        self.assertEqual(candidate["id"], proposal_id)
        self.assertEqual(candidate["fields"][0]["value"], "老爸")
        receipt = self.decide(candidate)
        retry = self.decide(candidate)
        self.assertEqual(receipt, retry)
        self.assertNotEqual(receipt["confirmation_event_id"], candidate["user_event_id"])
        core = self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP)
        self.assertEqual(core.payload["称呼"], "老爸")
        self.assertIn(candidate["user_event_id"], core.field_evidence_ids["称呼"])
        self.assertEqual(self.desktop.handle("proposals")["items"], [])

    def test_stale_core_proposal_cannot_apply_or_leak_old_patch(self):
        self.core_candidate()
        candidate = self.desktop.handle("proposals")["items"][0]
        self.runtime.forget_event(candidate["user_event_id"], scope=self.scope)
        self.assertEqual(self.desktop.handle("proposals")["items"], [])
        with self.assertRaises(AdapterError):
            self.decide(candidate)
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP).payload["称呼"], "爸爸")

    def test_human_can_reject_core_candidate_without_mutating_core(self):
        self.core_candidate()
        candidate = self.desktop.handle("proposals")["items"][0]
        result = self.decide(candidate, decision="reject")
        self.assertFalse(result["receipt"]["applied"])
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP).payload["称呼"], "爸爸")

    def test_role_proposals_are_approved_through_domain_services(self):
        event = self.event("我希望慢慢讲故事，也记下第一次海边约定")
        self.runtime.narrative.propose(self.scope, "海边约定", "第一次约好海边故事", [event.event_id], "story")
        self.runtime.growth.propose(self.scope, "storytelling", "慢慢讲故事", [event.event_id], reason="用户明确偏好", request_id="style")
        self.runtime.companionship.propose(self.scope, "讲故事", "慢一点", evidence=[event.event_id], reason="用户偏好", idempotency_key="preference")
        candidates = self.desktop.handle("proposals")["items"]
        self.assertEqual({row["proposal_type"] for row in candidates}, {"narrative", "growth", "companionship"})
        for candidate in candidates:
            result = self.decide(candidate)
            self.assertTrue(result["receipt"]["applied"])
            self.assertEqual(self.decide(candidate)["confirmation_event_id"], result["confirmation_event_id"])
        roles = self.desktop.handle("role")
        self.assertEqual(len(roles["autobiography"]["entries"]), 1)
        self.assertEqual(roles["growth"][0]["field"], "storytelling")
        self.assertEqual(roles["companionship"][0]["key"], "讲故事")

    def test_initialization_is_atomic_and_refuses_existing_or_hidden_role(self):
        first = self.initialize()
        self.assertEqual(first, self.initialize())
        self.assertFalse(self.desktop.handle("overview")["needs_initialization"])
        with self.assertRaises(AdapterError) as caught:
            self.initialize(request_id="overwrite")
        self.assertEqual(caught.exception.code, "ROLE_EXISTS")
        self.runtime.forget_event(first["confirmation_event_id"], scope=self.scope)
        self.assertFalse(self.desktop.handle("overview")["needs_initialization"])
        with self.assertRaises(AdapterError):
            self.initialize(request_id="after-forgetting")

    def test_initialization_failure_rolls_back_both_cores_and_user_event(self):
        actual = self.runtime.apply_core_update
        def fail_second(scope, kind, *args, **kwargs):
            if kind == "relationship":
                raise RuntimeError("simulated second write failure")
            return actual(scope, kind, *args, **kwargs)
        with patch.object(self.runtime, "apply_core_update", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.initialize()
        self.assertEqual(self.count_rows("core_versions"), 0)
        self.assertEqual(self.count_rows("events"), 0)

    def test_dreams_are_fiction_and_disappear_when_source_is_forgotten(self):
        event, _ = self.memory()
        prepared = self.runtime.sleep.prepare(self.scope, "window-1", "sleep")
        cursor = prepared["review"]["next_cursor"]
        while cursor is not None:
            batch = self.runtime.sleep.batch(self.scope, prepared["sleep_id"], cursor)
            self.runtime.sleep.record_review(self.scope, prepared["sleep_id"], cursor, [], batch["source_event_ids"], claim_token=batch["claim_token"])
            cursor = batch["next_cursor"]
        self.runtime.sleep.complete(self.scope, prepared["sleep_id"])
        state = self.desktop.handle("sleep")
        self.assertEqual(len(state["dreams"]), 1)
        self.assertEqual(state["dreams"][0]["epistemic_status"], "generated_fiction")
        self.assertTrue(state["runs"][0]["review_complete"])
        self.runtime.forget_event(event.event_id, scope=self.scope)
        state = self.desktop.handle("sleep")
        self.assertEqual(state["dreams"], [])
        self.assertEqual(state["runs"][0]["status"], "invalidated")

    def test_detail_preserves_editable_body_and_marks_actual_truncation(self):
        _, readable = self.memory("海"*6000)
        detail = self.desktop.handle("record", {"id": readable.memory_id})
        self.assertEqual(detail["body"]["obj"], "海"*6000)
        self.assertFalse(detail["truncated"])
        _, oversized = self.memory("浪"*18000)
        shortened = self.desktop.handle("record", {"id": oversized.memory_id})
        self.assertTrue(shortened["truncated"])
        self.assertNotEqual(shortened["body"]["obj"], "浪"*18000)

    def test_request_id_cannot_replace_initialization_parameters(self):
        self.initialize()
        with self.assertRaises(AdapterError) as caught:
            self.initialize(identity_fields={"名字": "另一个名字"})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload["名字"], "小梦")

    def test_foreign_or_stale_candidate_cannot_create_confirmation(self):
        self.core_candidate()
        candidate = self.desktop.handle("proposals")["items"][0]
        before = self.count_rows("events")
        with self.assertRaises(AdapterError):
            self.decide(candidate, expected_revision="old")
        self.assertEqual(self.count_rows("events"), before)
        foreign = DesktopService(self.adapter, replace(self.binding, relationship_id="other"))
        self.assertEqual(foreign.handle("proposals")["items"], [])
        with self.assertRaises(AdapterError):
            foreign.handle("proposal_decide", {"proposal_type": "core", "id": candidate["id"],
                "decision": "approve", "expected_revision": candidate["revision"],
                "request_id": "foreign-review", "reason": "用户确认"})
        self.assertEqual(self.count_rows("events"), before)

    def test_role_candidates_respect_page_activity_authorization(self):
        activity = self.runtime.register_activity(self.scope, "私密活动")
        private = self.event("私密活动里的约定", scope=replace(self.scope, activity_id=activity.activity_id))
        # Domain projection sources require the exact scope; the desktop must
        # retain that rule rather than accepting a wider operator scope.
        with self.assertRaises(MemorySuppressed):
            self.runtime.narrative.propose(self.scope, "活动约定", "私密活动里的约定", [private.event_id], "private-story")
        self.assertEqual(self.desktop.handle("proposals")["items"], [])
        self.assertEqual(self.desktop.handle("role")["autobiography"]["entries"], [])

    def test_bridge_desktop_dispatch_preserves_fixed_scope_and_reports_errors(self):
        _, item = self.memory()
        bridge = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        self.addCleanup(bridge.close)
        def request(action, arguments=None):
            return bridge.handle({"id": "desktop-request", "op": "desktop", "conversation_id": "window-1",
                                  "action": action, "arguments": arguments or {}})
        result = request("memories")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["items"][0]["id"], item.memory_id)
        invalid = request("memories", {"relationship_id": "r2"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"]["code"], "INVALID_ARGUMENT")
        unsupported = request("arbitrary_sql", {"sql": "SELECT * FROM schema_meta"})
        self.assertFalse(unsupported["ok"])
        self.assertEqual(unsupported["error"]["code"], "UNSUPPORTED")

    def test_default_bridge_reviews_core_without_changing_model_policy(self):
        proposal_id = self.core_candidate()
        bridge = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        self.addCleanup(bridge.close)
        def request(action, args):
            return bridge.handle({"id": "review", "op": "desktop", "conversation_id": "management-page",
                                  "action": action, "arguments": args})
        page = request("proposals", {"proposal_type": "core"})
        self.assertTrue(page["ok"], page)
        candidate = page["data"]["items"][0]
        self.assertEqual(candidate["id"], proposal_id)
        self.assertEqual(candidate["state"], "pending_validation")
        result = request("proposal_decide", {"proposal_type": "core", "id": proposal_id,
            "decision": "approve", "expected_revision": candidate["revision"], "request_id": "bridge-core-review", "reason": "用户核对后确认"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["receipt"]["applied"])
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP).payload["称呼"], "老爸")
        self.assertFalse(bridge.adapter.core_policy.enabled)
        self.assertFalse(bridge.adapter.core_policy.change_proposals)
        self.assertEqual([tool["name"] for tool in bridge.adapter.tools], ["memory_search", "memory_read"])

    def test_default_bridge_reviews_change_and_stale_sources_still_fail(self):
        source, item = self.memory()
        creator = HostAdapter(self.runtime, core_policy=CoreProposalPolicy(change_proposals=True))
        session = MemorySession(creator, self.binding)
        session.prepare_turn("forget-request", "请忘记这条偏好", max_tokens=0)
        ref = session.registry.issue(self.desktop._reader().load("memory", item.memory_id))
        with self.runtime.db.transaction():
            proposal = ChangeProposals(session).create("memory_request_change", {
                "operation": "forget_fact", "target_refs": [ref], "replacement_text": None,
                "replacement_body": None, "reason": "用户要求遗忘", "evidence_refs": []})
        bridge = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        self.addCleanup(bridge.close)
        def request(action, args):
            return bridge.handle({"id": "review", "op": "desktop", "conversation_id": "management-page",
                                  "action": action, "arguments": args})
        candidate = request("proposals", {"proposal_type": "change"})["data"]["items"][0]
        self.assertEqual(candidate["id"], proposal["proposal_id"])
        result = request("proposal_decide", {"proposal_type": "change", "id": candidate["id"],
            "decision": "approve", "expected_revision": candidate["revision"], "request_id": "bridge-change-review", "reason": "用户核对后确认"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["receipt"]["applied"])
        self.assertEqual(self.desktop.handle("memories")["items"], [])
        self.assertFalse(bridge.adapter.core_policy.change_proposals)
        core_id = self.core_candidate()
        core = request("proposals", {"proposal_type": "core"})["data"]["items"][0]
        self.runtime.forget_event(core["user_event_id"], scope=self.scope)
        blocked = request("proposal_decide", {"proposal_type": "core", "id": core_id,
            "decision": "approve", "expected_revision": core["revision"], "request_id": "stale-core-review", "reason": "来源已经撤销"})
        self.assertFalse(blocked["ok"])
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP).payload["称呼"], "爸爸")

    def test_growth_retry_without_explicit_version_uses_persisted_receipt(self):
        def candidate(content, request_id):
            event = self.event(content)
            self.runtime.growth.propose(self.scope, "storytelling", content, [event.event_id],
                                        reason="用户明确表达", request_id=request_id)
            return self.desktop.handle("proposals", {"proposal_type": "growth"})["items"][0]
        def args(item, request_id):
            return {"proposal_type": "growth", "id": item["id"], "decision": "approve",
                "expected_revision": item["revision"], "request_id": request_id, "reason": "用户确认"}
        first = candidate("慢一点", "growth-first")
        self.desktop.handle("proposal_decide", args(first, "approve-first"))
        self.clock.advance(hours=25)
        second = candidate("再慢一点", "growth-second")
        self.assertEqual(second["expected_version"], 1)
        original = args(second, "approve-second")
        receipt = self.desktop.handle("proposal_decide", original)
        self.assertEqual(receipt["receipt"]["version"], 2)
        self.clock.advance(hours=25)
        third = candidate("现在稍快一点", "growth-third")
        self.desktop.handle("proposal_decide", args(third, "approve-third"))
        retried = DesktopService(self.adapter, self.binding).handle("proposal_decide", original)
        self.assertEqual(retried["receipt"]["version"], 2)
        self.assertEqual(retried["confirmation_event_id"], receipt["confirmation_event_id"])
        self.assertEqual(self.runtime.growth.profile(self.scope)[0]["version"], 3)
