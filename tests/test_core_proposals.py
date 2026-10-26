"""核心候选：来源、宿主审批、并发窗口、遗忘栅栏与原子回执。"""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from memorydreaming import CoreProposalPolicy, HostAdapter, MemoryRuntime, SessionBinding
from memorydreaming.llm import AdapterError, dumps, tool_catalog
from memorydreaming.mcp import McpServer
from memorydreaming.models import CoreKind, ForgetSelector, MemoryType, Scope
from memorydreaming.proposals import CoreProposals
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import RuntimeTestCase
from tests.test_host import CharacterCounter
from tests.test_mcp import INITIALIZE, READY, request


class CoreProposalTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦", "身份": "陪伴角色"}, "初始设定")
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "爸爸", "约定": "睡前聊天"}, "初始设定")
        self.binding = SessionBinding("u1", "c1", "r1", "window-1")
        self.policy = CoreProposalPolicy(identity_fields=("名字",), relationship_fields=("称呼", "约定"))
        self.adapter = HostAdapter(self.runtime, core_policy=self.policy)
        self.session = self.adapter.open_session(self.binding)
        self.turn = self.session.prepare_turn("user-1", "以后叫我老爸吧。")

    def core(self, kind=CoreKind.RELATIONSHIP):
        return self.repos.cores.latest(self.scope, kind)

    def args(self, *, session=None, **changes):
        session = session or self.session
        result = {"target_ref": session.bootstrap()["data"]["relationship_ref"],
                  "fields": [{"name": "称呼", "value": "老爸", "evidence_refs": []}],
                  "reason": "用户明确修改称呼", "max_tokens": 4096}
        result.update(changes)
        return result

    def propose(self, *, session=None, **changes):
        return (session or self.session).call_tool("memory_core_propose", self.args(session=session, **changes))

    def proposal(self, **changes):
        response = self.propose(**changes)
        self.assertEqual(response["status"], "proposed", response)
        self.assertFalse(response["data"]["applied"])
        return response["data"]["proposal_id"]

    def approve(self, proposal_id, *, session=None, **changes):
        args = {"user_event_id": self.turn.user_event_id, "reviewer": "host:explicit-request",
                "reason": "宿主已核对实际用户请求及字段"}
        args.update(changes)
        return (session or self.session).approve_core_proposal(proposal_id, **args)

    def test_default_has_no_proposal_tool_and_policy_requires_explicit_fields(self):
        closed = HostAdapter(self.runtime).open_session(self.binding)
        result = self.propose(session=closed)
        self.assertEqual(result["error"]["code"], "UNSUPPORTED")
        self.assertFalse(HostAdapter(self.runtime).capabilities["core_proposals"])
        self.assertTrue(self.adapter.capabilities["core_proposals"])
        self.assertEqual([tool["name"] for tool in self.adapter.tools], ["memory_search", "memory_read", "memory_core_propose"])
        field_schema = self.adapter.tools[-1]["input_schema"]["properties"]["fields"]["items"]["properties"]["name"]
        self.assertEqual(set(field_schema["enum"]), {"名字", "称呼", "约定"})
        self.assertIn('"applies_immediately":false', self.turn.messages[1]["content"])
        with self.assertRaises(AdapterError):
            CoreProposalPolicy(identity_fields=("",))
        with self.assertRaises(ValueError):
            CoreProposalPolicy(identity_fields="名字")

    def test_candidate_does_not_change_core_and_binds_real_request(self):
        proposal_id = self.proposal()
        self.assertEqual(self.core().core_version, 1)
        self.assertEqual(self.core().payload["称呼"], "爸爸")
        pending = self.session.get_core_proposal(proposal_id)
        self.assertEqual(pending["state"], "pending_validation")
        self.assertEqual(pending["fields"][0]["sources"], [self.turn.user_event_id])
        self.assertEqual(pending["receipt"]["remaining_work"], ["PENDING_APPROVAL"])

    def test_approval_preserves_other_fields_records_evidence_and_refreshes_next_turn(self):
        receipt = self.approve(self.proposal())
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["affected"]["core_fields"], 1)
        self.assertEqual(self.core().payload, {"称呼": "老爸", "约定": "睡前聊天"})
        self.assertEqual(self.core().field_evidence_ids["称呼"], [self.turn.user_event_id])
        prepared = self.session.prepare_turn("user-2", "你现在怎么叫我？")
        self.assertIn("老爸", prepared.messages[1]["content"])
        self.assertEqual(self.count_rows("core_versions"), 3)

    def test_proposal_and_approval_retries_are_idempotent(self):
        args = self.args()
        first = self.session.call_tool("memory_core_propose", args, request_id="transport-1")
        second = self.session.call_tool("memory_core_propose", args, request_id="transport-2")
        self.assertEqual(first["data"], second["data"])
        proposal_id = first["data"]["proposal_id"]
        self.assertEqual(self.approve(proposal_id), self.approve(proposal_id))
        self.assertEqual(self.core().core_version, 2)
        self.assertEqual(self.count_rows("core_proposals"), 1)

    def test_pending_survives_restart_and_new_session_references(self):
        proposal_id = self.proposal()
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as runtime:
            session = HostAdapter(runtime, core_policy=self.policy).open_session(self.binding)
            session.prepare_turn("user-1", "以后叫我老爸吧。")
            replay = self.propose(session=session)
            self.assertEqual(replay["data"]["proposal_id"], proposal_id)
            receipt = self.approve(proposal_id, session=session)
            self.assertTrue(receipt["applied"])
        self.assertEqual(self.core().payload["称呼"], "老爸")

    def test_rejection_is_terminal_and_does_not_apply(self):
        proposal_id = self.proposal()
        args = {"user_event_id": self.turn.user_event_id, "reviewer": "host", "reason": "用户否决"}
        one = self.session.reject_core_proposal(proposal_id, **args)
        self.assertEqual(one, self.session.reject_core_proposal(proposal_id, **args))
        self.assertFalse(one["applied"])
        self.assertEqual(self.core().core_version, 1)
        with self.assertRaises(AdapterError):
            self.approve(proposal_id)

    def test_terminal_audit_cannot_be_rewritten(self):
        proposal_id = self.proposal()
        self.approve(proposal_id)
        with self.assertRaises(AdapterError):
            self.approve(proposal_id, reviewer="other-host")
        with self.assertRaises(AdapterError):
            self.approve(proposal_id, reason="换审批理由")
        self.assertEqual(self.session.get_core_proposal(proposal_id)["reviewer"], "host:explicit-request")

    def test_wrong_authorization_event_and_foreign_binding_are_forbidden(self):
        proposal_id = self.proposal()
        with self.assertRaises(AdapterError) as raised:
            self.approve(proposal_id, user_event_id="another-event")
        self.assertEqual(raised.exception.code, "NOT_FOUND_OR_FORBIDDEN")
        for change in ({"user_id": "u2"}, {"character_id": "c2"}, {"relationship_id": "r2"}, {"domain": "fiction"}):
            with self.subTest(change=change):
                session = self.adapter.open_session(replace(self.binding, **change))
                with self.assertRaises(AdapterError) as raised:
                    session.get_core_proposal(proposal_id)
                self.assertEqual(raised.exception.code, "NOT_FOUND_OR_FORBIDDEN")
                with self.assertRaises(AdapterError):
                    self.approve(proposal_id, session=session)
        self.assertEqual(self.core().core_version, 1)

    def test_policy_whitelist_unknown_nested_fields_and_duplicate_names_are_rejected(self):
        for fields in (
            [{"name": "身份", "value": "新身份", "evidence_refs": []}],
            [{"name": "称呼", "value": {"name": "老爸"}, "evidence_refs": []}],
            [{"name": "称呼", "value": "老爸", "evidence_refs": [], "approve": True}],
            [{"name": "称呼", "value": "老爸", "evidence_refs": []}] * 2,
        ):
            with self.subTest(fields=fields):
                self.session.prepare_turn("user-1", "以后叫我老爸吧。")
                self.assertEqual(self.propose(fields=fields)["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.count_rows("core_proposals"), 0)

    def test_no_saved_user_turn_cannot_propose(self):
        session = self.adapter.open_session(self.binding)
        self.assertEqual(self.propose(session=session)["error"]["code"], "UNAVAILABLE")
        self.assertEqual(self.count_rows("core_proposals"), 0)

    def test_non_core_target_and_forged_evidence_cannot_propose(self):
        item = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "称呼", "obj": "老爸"})
        search = self.session.call_tool("memory_search", {"query": "老爸", "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 4, "max_tokens": 4096})
        ref = search["data"]["evidence"][0]["ref"]
        self.assertEqual(self.propose(target_ref=ref)["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.propose(fields=[{"name": "称呼", "value": "老爸", "evidence_refs": ["mref_forged"]}])
                         ["error"]["code"], "NOT_FOUND_OR_FORBIDDEN")
        self.assertIsNotNone(item)

    def test_core_cannot_be_its_own_evidence(self):
        args = self.args()
        args["fields"][0]["evidence_refs"] = [args["target_ref"]]
        result = self.session.call_tool("memory_core_propose", args)
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_other_window_change_invalidates_instead_of_overwriting(self):
        proposal_id = self.proposal()
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as runtime:
            runtime.apply_core_update(self.scope, "relationship", {"称呼": "父亲"}, "另一个窗口的设置", expected_version=1)
        receipt = self.approve(proposal_id)
        self.assertFalse(receipt["applied"])
        self.assertEqual(receipt["remaining_work"], ["VERSION_CONFLICT"])
        self.assertEqual(self.core().payload["称呼"], "父亲")
        self.assertEqual(self.session.get_core_proposal(proposal_id)["state"], "invalidated")

    def test_expired_proposal_does_not_refresh_on_retry(self):
        proposal_id = self.proposal()
        self.clock.advance(hours=23)
        self.assertEqual(self.proposal(), proposal_id)
        self.clock.advance(hours=1)
        receipt = self.approve(proposal_id)
        self.assertEqual(receipt["remaining_work"], ["EXPIRED"])
        self.assertEqual(self.core().core_version, 1)

    def test_policy_narrowing_blocks_old_pending_proposal(self):
        proposal_id = self.proposal()
        session = HostAdapter(self.runtime).open_session(self.binding)
        receipt = self.approve(proposal_id, session=session)
        self.assertEqual(receipt["remaining_work"], ["POLICY_CHANGED"])

    def test_identity_proposal_changes_only_identity_and_keeps_relationship_version(self):
        target = self.session.bootstrap()["data"]["identity_ref"]
        proposal_id = self.proposal(target_ref=target, fields=[{"name": "名字", "value": "小满", "evidence_refs": []}])
        self.approve(proposal_id)
        self.assertEqual(self.core(CoreKind.IDENTITY).payload, {"名字": "小满", "身份": "陪伴角色"})
        self.assertEqual(self.core().core_version, 1)

    def test_character_statement_is_not_accepted_as_field_evidence(self):
        event = self.runtime.observe(self.make_turn("模型自称用户要求改称呼", speaker="character"), self.scope, "character-source").event
        ref = self.session.registry.issue(CoreProposals(self.session).reader().load("event", event.event_id))
        result = self.propose(fields=[{"name": "称呼", "value": "老爸", "evidence_refs": [ref]}])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_additional_user_evidence_is_saved_per_field(self):
        event = self.runtime.observe(self.make_turn("我更习惯老爸这个称呼"), self.scope, "user-history").event
        ref = self.session.registry.issue(CoreProposals(self.session).reader().load("event", event.event_id))
        proposal_id = self.proposal(fields=[{"name": "称呼", "value": "老爸", "evidence_refs": [ref]}])
        self.approve(proposal_id)
        self.assertEqual(set(self.core().field_evidence_ids["称呼"]), {self.turn.user_event_id, event.event_id})

    def test_two_connections_approving_same_proposal_return_same_receipt(self):
        proposal_id = self.proposal()
        first = self.approve(proposal_id)
        with MemoryRuntime(self.runtime.settings, clock=self.clock) as runtime:
            session = HostAdapter(runtime, core_policy=self.policy).open_session(replace(self.binding, conversation_id="approval-window"))
            self.assertEqual(self.approve(proposal_id, session=session), first)
        self.assertEqual(self.core().core_version, 2)

    def test_pending_limit_rejects_new_candidates_but_allows_exact_retry(self):
        for index in range(64):
            self.session.prepare_turn(f"limit-{index}", "以后叫我老爸吧。")
            proposal_id = self.proposal()
        self.assertEqual(self.proposal(), proposal_id)
        self.session.prepare_turn("limit-64", "以后叫我老爸吧。")
        self.assertEqual(self.propose()["error"]["code"], "UNAVAILABLE")
        self.assertEqual(self.count_rows("core_proposals"), 64)

    def test_forgotten_source_invalidates_and_hides_patch(self):
        proposal_id = self.proposal()
        memory = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "称呼", "obj": "老爸"},
                                 source_event_ids=[self.turn.user_event_id],
                                 source_spans={self.turn.user_event_id: [[0, 8]]})
        self.runtime.forget_fact(ForgetSelector(memory_ids=[memory.memory_id]))
        detail = self.session.get_core_proposal(proposal_id)
        self.assertEqual(detail["state"], "invalidated")
        self.assertNotIn("老爸", dumps(detail))
        self.assertNotIn("fields", detail)
        self.assertFalse(self.approve(proposal_id)["applied"])

    def test_request_content_revision_is_checked_at_approval(self):
        proposal_id = self.proposal()
        self.runtime.db.connection.execute("UPDATE events SET content=? WHERE event_id=?", ("我没有要求改称呼", self.turn.user_event_id))
        self.assertEqual(self.approve(proposal_id)["remaining_work"], ["STALE_REFERENCE"])
        self.assertEqual(self.core().core_version, 1)

    def test_applied_fields_follow_forgetting_sources(self):
        proposal_id = self.proposal()
        self.approve(proposal_id)
        memory = self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "称呼", "obj": "老爸"},
                                 source_event_ids=[self.turn.user_event_id], source_spans={self.turn.user_event_id: [[0, 8]]})
        self.runtime.forget_fact(ForgetSelector(memory_ids=[memory.memory_id]))
        self.assertNotIn("称呼", self.core().payload)
        detail = self.session.get_core_proposal(proposal_id)
        self.assertNotIn("老爸", dumps(detail))
        self.assertTrue(detail["receipt"]["applied"])  # 历史生效回执，不承诺字段至今仍有效。

    def test_receipt_failure_rolls_back_core_and_leaves_candidate_pending(self):
        proposal_id = self.proposal()
        with patch.object(CoreProposals, "finish", side_effect=RuntimeError("模拟审批持久化失败")):
            with self.assertRaises(RuntimeError):
                self.approve(proposal_id)
        self.assertEqual(self.core().core_version, 1)
        self.assertEqual(self.session.get_core_proposal(proposal_id)["state"], "pending_validation")
        self.assertTrue(self.approve(proposal_id)["applied"])

    def test_too_small_budget_rolls_back_candidate_write(self):
        session = HostAdapter(self.runtime, core_policy=self.policy, token_counter=CharacterCounter()).open_session(self.binding)
        session.prepare_turn("user-1", "以后叫我老爸吧。")
        # 错误结果也放不下时以 AdapterError 带外返回，数据库仍不产生候选。
        with self.assertRaises(AdapterError):
            session.call_tool("memory_core_propose", self.args(session=session), max_tokens=1)
        self.assertEqual(self.count_rows("core_proposals"), 0)

    def test_deadline_after_write_rolls_back_candidate(self):
        original = CoreProposals.create
        def slow_create(service, args):
            data = original(service, args)
            self.clock.advance(seconds=2)
            return data
        with patch.object(CoreProposals, "create", slow_create):
            response = self.session.call_tool("memory_core_propose", self.args(), deadline=self.clock.now() + timedelta(seconds=1))
        self.assertEqual(response["error"]["code"], "TIMEOUT")
        self.assertEqual(self.count_rows("core_proposals"), 0)

    def test_mcp_does_not_expose_proposal_even_for_enabled_python_session(self):
        server = McpServer(self.session)
        server.handle(INITIALIZE)
        server.handle(READY)
        tools = server.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["memory_search", "memory_read"])
        result = server.handle(request("tools/call", {"name": "memory_core_propose", "arguments": self.args()}))
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(self.count_rows("core_proposals"), 0)

    def test_runtime_and_design_proposal_tool_schemas_match(self):
        path = Path(__file__).resolve().parents[1] / "docs/contracts/llm-tools.json"
        documented = json.loads(path.read_text(encoding="utf-8"))
        expected = next(tool for tool in documented["tools"] if tool["name"] == "memory_core_propose")
        self.assertEqual(tool_catalog(include_core_proposals=True)[-1], expected)

    def test_schema_v7_upgrade_preserves_cores_and_deliveries(self):
        delivery = self.session.after_delivery("assistant-1", "好的。", user_event_id=self.turn.user_event_id)
        self.runtime.db.connection.execute("DROP TABLE core_proposals")
        self.runtime.db.meta_set("schema_version", "7")
        reopened = Database.open(self.db_path)
        try:
            self.assertEqual(reopened.meta_get("schema_version"), SCHEMA_VERSION)
            self.assertEqual(reopened.repositories.cores.latest(self.scope, CoreKind.RELATIONSHIP).payload["称呼"], "爸爸")
            self.assertEqual(reopened.connection.execute("SELECT event_id FROM host_deliveries").fetchone()[0], delivery["event_id"])
            self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM core_proposals").fetchone()[0], 0)
        finally:
            reopened.close()
