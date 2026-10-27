"""纠错计算期间的独立连接写入、审批竞态和动作/回执原子性。"""

from dataclasses import replace
import json
from pathlib import Path
import re
from unittest.mock import patch

from memorydreaming import CoreProposalPolicy, HostAdapter, SessionBinding
from memorydreaming.errors import (InvalidCorrection, MemorySuppressed, SourceRevisionChanged,
                                   StorageError)
from memorydreaming.llm import AdapterError
from memorydreaming.models import CoreKind, Correction, ForgetSelector, MemoryStatus, MemoryType, Scope
from memorydreaming.portability import export_snapshot, validate_snapshot
from memorydreaming.proposals import ChangeProposals
from memorydreaming.references import ViewReader
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database, SCHEMA_VERSION
from tests.helpers import NOW, RuntimeTestCase


class CallbackEmbedder:
    name, dim = "correction-callback", 2

    def __init__(self):
        self.on_documents = None
        self.document_calls = 0

    def embed(self, text):
        return [1.0, 0.0]

    def embed_documents(self, texts):
        self.document_calls += 1
        if self.on_documents:
            self.on_documents()
        return [self.embed(text) for text in texts]


class CorrectionPlanTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.model = CallbackEmbedder()
        self.engine = MemoryRuntime(self.runtime.settings, clock=self.clock, embedder=self.model)
        self.addCleanup(self.engine.close)
        self.other = Database.open(self.db_path, initialize=False)
        self.other.connection.execute("PRAGMA busy_timeout=10")
        self.addCleanup(self.other.close)
        self.memory = self.add_memory(MemoryType.SEMANTIC,
                                      {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"})
        self.change = Correction(kind="change", corrected_body={
            "subject": "用户", "predicate": "喜欢", "obj": "森林故事"})

    def assert_original(self):
        old = self.repos.memories.get(self.memory.memory_id)
        self.assertEqual(old.status, MemoryStatus.ACTIVE)
        self.assertIsNone(old.valid_to)
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.count_rows("memory_links"), 0)
        self.assertEqual(self.count_rows("memory_vectors"), 0)

    def test_encoding_releases_write_lock_and_no_half_replacement_is_visible(self):
        def during_encoding():
            self.assertFalse(self.engine.db.connection.in_transaction)
            with self.other.transaction():
                self.other.meta_set("concurrent-correction", "ok")
            self.assert_original()
        self.model.on_documents = during_encoding
        result = self.engine.correct(self.memory.memory_id, self.change)
        self.assertEqual(result.new_memory.body["obj"], "森林故事")
        self.assertEqual(self.model.document_calls, 1)
        self.assertEqual(self.count_rows("memory_vectors"), 1)

    def test_revision_during_encoding_cannot_publish_old_plan(self):
        def revise():
            with self.other.transaction():
                self.other.repositories.memories.update_content(
                    self.memory.memory_id, {"subject": "用户", "predicate": "喜欢", "obj": "新事实"}, NOW)
        self.model.on_documents = revise
        with self.assertRaises(SourceRevisionChanged):
            self.engine.correct(self.memory.memory_id, self.change)
        self.assert_original()
        self.assertEqual(self.repos.memories.get(self.memory.memory_id).body["obj"], "新事实")

    def test_forgetting_fact_during_encoding_cannot_revive_it(self):
        self.model.on_documents = lambda: self.runtime.forget_fact(
            ForgetSelector(memory_ids=[self.memory.memory_id]))
        with self.assertRaises(SourceRevisionChanged):
            self.engine.correct(self.memory.memory_id, self.change)
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.repos.memories.get(self.memory.memory_id).status, MemoryStatus.DELETED)
        self.assertEqual(self.count_rows("memory_vectors"), 0)

    def test_purge_during_encoding_cannot_restore_any_data(self):
        self.model.on_documents = lambda: self.runtime.forget_scope(self.scope)
        with self.assertRaises(SourceRevisionChanged):
            self.engine.correct(self.memory.memory_id, self.change)
        for table in ("memories", "memory_vectors", "memory_links", "entity_mentions"):
            self.assertEqual(self.count_rows(table), 0)

    def test_input_is_copied_before_encoding(self):
        self.model.on_documents = lambda: self.change.corrected_body.update(obj="不应写入")
        result = self.engine.correct(self.memory.memory_id, self.change)
        self.assertEqual(result.new_memory.body["obj"], "森林故事")

    def test_system_recording_time_starts_at_publication_after_slow_encoding(self):
        self.model.on_documents = lambda: self.clock.advance(seconds=10)
        result = self.engine.correct(self.memory.memory_id, self.change)
        self.assertEqual(result.new_memory.valid_from, NOW)
        self.assertEqual(result.new_memory.recorded_at, self.clock.now())
        stored = self.repos.memories.get(result.new_memory.memory_id)
        self.assertEqual(stored.recorded_at, self.clock.now())

    def test_plan_cannot_cross_runtime_be_modified_or_be_replayed(self):
        plan = self.engine._prepare_correction(self.memory.memory_id, self.change)
        with self.assertRaises(SourceRevisionChanged):
            self.runtime._apply_prepared_correction(plan)
        plan.replacement.body["obj"] = "篡改"
        with self.assertRaises(SourceRevisionChanged):
            self.engine._apply_prepared_correction(plan)
        self.assert_original()
        plan = self.engine._prepare_correction(self.memory.memory_id, self.change)
        self.engine._apply_prepared_correction(plan)
        with self.assertRaises(SourceRevisionChanged):
            self.engine._apply_prepared_correction(plan)
        self.assertEqual(self.count_rows("memories"), 2)

    def test_preparation_cannot_inherit_outer_write_transaction(self):
        with self.engine.db.transaction():
            with self.assertRaises(StorageError):
                self.engine.correct(self.memory.memory_id, self.change)
        self.assertEqual(self.model.document_calls, 0)
        self.assert_original()

    def test_index_or_link_failures_roll_back_old_and_new_records(self):
        for target in (self.engine._vectors, self.engine.db.repositories.links,
                       self.engine.db.repositories.entities):
            attribute = "index_memory" if target is not self.engine.db.repositories.links else "add"
            with self.subTest(attribute=attribute, target=type(target).__name__):
                with patch.object(target, attribute, side_effect=RuntimeError("injected")):
                    with self.assertRaises(RuntimeError):
                        self.engine.correct(self.memory.memory_id, self.change)
                self.assert_original()

    def test_forgotten_or_superseded_target_cannot_be_reused(self):
        self.engine.correct(self.memory.memory_id, self.change)
        with self.assertRaises(InvalidCorrection):
            self.engine.correct(self.memory.memory_id, self.change)
        self.runtime.forget_fact(ForgetSelector(memory_ids=[self.memory.memory_id]))
        with self.assertRaises(MemorySuppressed):
            self.engine.correct(self.memory.memory_id, Correction(kind="fix"))

    def test_missing_forgotten_and_cross_scope_sources_are_rejected_before_model(self):
        event = self.runtime.observe(self.make_turn("别的用户的依据"), Scope("u2", "c1"), "other-source").event
        forgotten = self.runtime.observe(self.make_turn("已遗忘依据"), self.scope, "forgotten-source").event
        self.runtime.forget_event(forgotten.event_id)
        for source_id in ("missing", event.event_id, forgotten.event_id):
            with self.subTest(source_id=source_id), self.assertRaises(InvalidCorrection):
                self.engine.correct(self.memory.memory_id, replace(self.change, evidence_event_ids=[source_id]))
        self.assertEqual(self.model.document_calls, 0)
        self.assert_original()


class ApprovalComputationTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.memory = self.add_memory(MemoryType.SEMANTIC,
                                      {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"})
        self.model = CallbackEmbedder()
        self.engine = MemoryRuntime(self.runtime.settings, clock=self.clock, embedder=self.model)
        self.addCleanup(self.engine.close)
        self.binding = SessionBinding("u1", "c1", "r1", "window")
        self.policy = CoreProposalPolicy(change_proposals=True, checkpoint_proposals=True)
        self.session = HostAdapter(self.engine, core_policy=self.policy).open_session(self.binding)
        self.other_session = HostAdapter(self.runtime, core_policy=self.policy).open_session(self.binding)
        self.turn = self.session.prepare_turn("request", "请把我的偏好改成森林故事。")
        search = self.session.call_tool("memory_search", {
            "query": "海边故事", "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 4, "max_tokens": 1200})
        self.assertIsNotNone(search["data"], search)
        self.target = search["data"]["evidence"][0]["ref"]
        self.args = {"operation": "correct_fact", "target_refs": [self.target],
                     "replacement_text": "现在喜欢森林故事", "replacement_body": {
                         "subject": "用户", "predicate": "喜欢", "obj": "森林故事"},
                     "reason": "已核对真实请求", "evidence_refs": []}
        self.proposal_id = self.propose()
        self.decision = dict(user_event_id=self.turn.user_event_id, reviewer="host:test", reason="核对通过")

    def propose(self, **overrides):
        response = self.session.call_tool("memory_request_change", self.args | overrides)
        self.assertEqual(response["status"], "proposed", response)
        return response["data"]["proposal_id"]

    def approve(self):
        return self.session.approve_change_proposal(self.proposal_id, **self.decision)

    def check_contract(self, receipt):
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/contracts/response.schema.json").read_text(encoding="utf-8"))
        definition = schema["$defs"]["change_receipt_data"]
        self.assertEqual(set(receipt), set(definition["required"]))
        self.assertEqual(set(receipt["affected"]), {"events", "memories", "core_fields", "activities"})
        self.assertTrue(all(type(n) is int and n >= 0 for n in receipt["affected"].values()))
        self.assertIn(receipt["state"], definition["properties"]["state"]["enum"])
        self.assertIn(receipt["erasure_level"], definition["properties"]["erasure_level"]["enum"])
        self.assertRegex(receipt["proposal_id"], r"^(proposal|change)_[A-Za-z0-9_-]+$")
        self.assertTrue(re.fullmatch(r"operation_[A-Za-z0-9_-]+", receipt["operation_id"]))

    def test_approval_encoding_is_unlocked_and_success_retries_do_not_encode_again(self):
        def check():
            self.assertFalse(self.engine.db.connection.in_transaction)
            with self.runtime.db.transaction():
                self.runtime.db.meta_set("approval-unlocked", "yes")
            self.assertEqual(self.count_rows("memories"), 1)
            self.assertEqual(self.other_session.get_change_proposal(self.proposal_id)["state"], "pending_validation")
        self.model.on_documents = check
        first = self.approve()
        self.check_contract(first)
        self.assertEqual(first["affected"]["memories"], 2)
        self.assertEqual(first["erasure_level"], "not_applicable")
        self.assertEqual(self.approve(), first)
        self.assertEqual(self.model.document_calls, 1)

    def test_rejection_by_another_host_during_encoding_wins(self):
        self.model.on_documents = lambda: self.other_session.reject_change_proposal(self.proposal_id, **self.decision)
        with self.assertRaises(AdapterError) as caught:
            self.approve()
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.session.get_change_proposal(self.proposal_id)["state"], "rejected")

    def test_approval_by_another_host_returns_its_exact_receipt(self):
        receipts = []
        self.model.on_documents = lambda: receipts.append(
            self.other_session.approve_change_proposal(self.proposal_id, **self.decision))
        self.assertEqual(self.approve(), receipts[0])
        self.assertEqual(self.count_rows("memories"), 2)

    def test_target_revision_and_request_forgetting_invalidate_during_encoding(self):
        for mutation in ("target", "request"):
            with self.subTest(mutation=mutation):
                if mutation == "request":
                    # 上个提议已终结，重新签发目标引用创建另一提议。
                    search = self.session.call_tool("memory_search", {
                        "query": "海边故事", "activity_ref": None, "time_range": None,
                        "detail": "evidence", "max_items": 4, "max_tokens": 1200})
                    self.target = search["data"]["evidence"][0]["ref"]
                    self.proposal_id = self.propose(target_refs=[self.target], reason="第二次请求")
                def mutate():
                    if mutation == "request":
                        self.runtime.forget_event(self.turn.user_event_id)
                    else:
                        with self.runtime.db.transaction():
                            self.repos.memories.update_content(self.memory.memory_id,
                                {"subject": "用户", "predicate": "喜欢", "obj": "海边故事修订"}, NOW)
                self.model.on_documents = mutate
                receipt = self.approve()
                self.check_contract(receipt)
                self.assertFalse(receipt["applied"])
                self.assertEqual(receipt["remaining_work"], ["STALE_REFERENCE"])
                self.assertEqual(self.count_rows("memories"), 1)

    def test_expiry_during_encoding_invalidates_without_applying(self):
        self.model.on_documents = lambda: self.clock.advance(days=2)
        receipt = self.approve()
        self.assertEqual(receipt["remaining_work"], ["EXPIRED"])
        self.assertFalse(receipt["applied"])
        self.assertEqual(self.count_rows("memories"), 1)

    def test_policy_revoked_during_encoding_invalidates(self):
        self.model.on_documents = lambda: setattr(self.session.adapter, "core_policy", CoreProposalPolicy())
        receipt = self.approve()
        self.assertEqual(receipt["remaining_work"], ["POLICY_CHANGED"])
        self.assertFalse(receipt["applied"])

    def test_unrelated_generation_change_is_retryable_and_keeps_proposal_pending(self):
        self.model.on_documents = lambda: self.runtime.observe(
            self.make_turn("另一作用域的消息"), Scope("other", "other"), "unrelated")
        with self.assertRaises(AdapterError) as caught:
            self.approve()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.session.get_change_proposal(self.proposal_id)["state"], "pending_validation")
        self.model.on_documents = None
        self.assertTrue(self.approve()["applied"])

    def test_model_failure_is_retryable_without_invalidating_proposal(self):
        def fail():
            raise RuntimeError("local model temporarily unavailable")
        self.model.on_documents = fail
        with self.assertRaises(AdapterError) as caught:
            self.approve()
        self.assertEqual(caught.exception.code, "UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.session.get_change_proposal(self.proposal_id)["state"], "pending_validation")
        self.assertEqual(self.count_rows("memories"), 1)
        self.model.on_documents = None
        self.assertTrue(self.approve()["applied"])

    def test_action_or_receipt_failure_rolls_back_and_allows_original_approval_retry(self):
        for target, attribute in ((self.engine._vectors, "index_memory"), (ChangeProposals, "_finish")):
            with self.subTest(attribute=attribute):
                with patch.object(target, attribute, side_effect=RuntimeError("persist failed")):
                    with self.assertRaises(RuntimeError):
                        self.approve()
                self.assertEqual(self.count_rows("memories"), 1)
                self.assertEqual(self.count_rows("memory_links"), 0)
                self.assertEqual(self.count_rows("memory_vectors"), 0)
                self.assertEqual(self.session.get_change_proposal(self.proposal_id)["state"], "pending_validation")
        self.assertTrue(self.approve()["applied"])

    def test_free_text_retraction_is_partial_and_does_not_claim_erasure(self):
        self.proposal_id = self.propose(replacement_body=None)
        receipt = self.approve()
        self.check_contract(receipt)
        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(receipt["erasure_level"], "not_applicable")
        self.assertEqual(receipt["affected"]["memories"], 1)
        self.assertEqual(self.model.document_calls, 0)

    def test_legacy_receipt_is_normalized_without_inventing_affected_counts(self):
        with self.engine.db.transaction():
            self.engine.db.connection.execute(
                "UPDATE change_proposals SET state='applied',decided_at=?,reviewer=?,decision_reason=?,receipt_json=? WHERE proposal_id=?",
                (NOW.isoformat(), self.decision["reviewer"], self.decision["reason"], json.dumps({
                    "kind": "change_receipt", "operation": "correct_fact", "applied": True,
                    "erasure_level": "logical_suppressed", "affected": 1, "remaining_work": []}), self.proposal_id))
        receipt = self.approve()
        self.check_contract(receipt)
        self.assertEqual(receipt["erasure_level"], "not_applicable")
        self.assertEqual(receipt["affected"]["memories"], 0)
        self.assertIn("LEGACY_AFFECTED_COUNTS_UNAVAILABLE", receipt["remaining_work"])
        self.assertEqual(self.model.document_calls, 0)

    def test_scope_purge_has_durable_minimal_receipt_after_proposal_and_event_are_gone(self):
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "爸爸"}, "建立关系")
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "老爸"}, "更新关系")
        activity = self.runtime.register_activity(self.scope, "待删除活动", goal="待删除目标")
        self.proposal_id = self.propose(operation="forget_scope", target_refs=[], replacement_text=None,
                                        replacement_body=None)
        receipt = self.approve()
        self.check_contract(receipt)
        self.assertEqual(receipt["erasure_level"], "active_store_purged")
        self.assertEqual(receipt["affected"]["events"], 1)
        self.assertEqual(receipt["affected"]["memories"], 1)
        self.assertEqual(receipt["affected"]["activities"], 1)
        self.assertEqual(receipt["affected"]["core_fields"], 1)
        self.assertIsNone(self.repos.events.get(self.turn.user_event_id))
        self.assertIsNone(self.repos.activities.get(activity.activity_id))
        self.assertEqual(self.count_rows("change_proposals"), 0)
        self.assertEqual(self.count_rows("core_versions"), 0)
        self.assertEqual(self.count_rows("change_purge_receipts"), 1)
        self.assertEqual(self.approve(), receipt)
        reopened = HostAdapter(self.runtime, core_policy=self.policy).open_session(self.binding)
        self.assertEqual(reopened.approve_change_proposal(self.proposal_id, **self.decision), receipt)
        detail = reopened.get_change_proposal(self.proposal_id)
        self.assertEqual(detail["receipt"], receipt)
        self.assertNotIn("森林故事", json.dumps(detail, ensure_ascii=False))
        with self.assertRaises(AdapterError):
            reopened.approve_change_proposal(self.proposal_id, **(self.decision | {"reason": "新审批"}))

    def test_scope_purge_receipt_failure_rolls_back_entire_purge(self):
        self.proposal_id = self.propose(operation="forget_scope", target_refs=[], replacement_text=None,
                                        replacement_body=None)
        with self.engine.db.transaction():
            self.engine.db.connection.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON change_purge_receipts "
                                              "BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):
            self.approve()
        self.assertIsNotNone(self.repos.events.get(self.turn.user_event_id))
        self.assertEqual(self.count_rows("memories"), 1)
        self.assertEqual(self.count_rows("scope_purges"), 0)
        self.assertEqual(self.session.get_change_proposal(self.proposal_id)["state"], "pending_validation")
        with self.engine.db.transaction():
            self.engine.db.connection.execute("DROP TRIGGER fail_receipt")
        self.assertTrue(self.approve()["applied"])

    def test_schema19_upgrade_and_old_snapshot_remain_compatible(self):
        snapshot = export_snapshot(self.runtime.db)
        snapshot["manifest"]["schema_version"] = "19"
        snapshot["tables"].pop("change_purge_receipts")
        snapshot["manifest"]["tables"].pop("change_purge_receipts")
        self.assertTrue(validate_snapshot(self.runtime.db, snapshot)["ready"])
        with self.runtime.db.transaction():
            self.runtime.db.connection.execute("DROP TABLE change_purge_receipts")
            self.runtime.db.meta_set("schema_version", "19")
        migrated = Database.open(self.db_path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.meta_get("schema_version"), SCHEMA_VERSION)
        self.assertEqual(migrated.connection.execute("SELECT COUNT(*) FROM change_purge_receipts").fetchone()[0], 0)

    def test_scope_purge_during_approval_encoding_cannot_restore_proposal_or_fact(self):
        self.model.on_documents = lambda: self.runtime.forget_scope(self.scope)
        with self.assertRaises(AdapterError):
            self.approve()
        for table in ("memories", "events", "change_proposals", "change_purge_receipts", "memory_vectors"):
            self.assertEqual(self.count_rows(table), 0)

    def test_wrong_user_request_never_reaches_encoding(self):
        with self.assertRaises(AdapterError):
            self.session.approve_change_proposal(self.proposal_id, **(self.decision | {"user_event_id": "other"}))
        self.assertEqual(self.model.document_calls, 0)
        self.assertEqual(self.count_rows("memories"), 1)

    def test_retried_pending_proposal_and_multi_target_validation(self):
        self.assertEqual(self.propose(), self.proposal_id)
        self.assertEqual(self.count_rows("change_proposals"), 1)
        response = self.session.call_tool("memory_request_change", self.args | {"target_refs": [self.target, self.target]})
        self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")

    def test_fact_and_event_forgetting_receipts_match_contract(self):
        self.proposal_id = self.propose(operation="forget_fact", replacement_body=None, replacement_text=None)
        receipt = self.approve()
        self.check_contract(receipt)
        self.assertEqual(receipt["erasure_level"], "logical_suppressed")
        self.assertEqual(receipt["affected"]["memories"], 1)
        event_ref = self.session.registry.issue(ViewReader(self.engine, self.binding, self.clock).load(
            "event", self.turn.user_event_id))
        self.proposal_id = self.propose(operation="forget_event", target_refs=[event_ref],
                                       replacement_body=None, replacement_text=None)
        receipt = self.approve()
        self.check_contract(receipt)
        self.assertEqual(receipt["erasure_level"], "event_redacted")
        self.assertEqual(receipt["affected"]["events"], 1)

    def test_minimal_purge_receipt_survives_database_reopen_without_plaintext_decision(self):
        self.proposal_id = self.propose(operation="forget_scope", target_refs=[], replacement_text=None,
                                        replacement_body=None)
        receipt = self.approve()
        restarted = MemoryRuntime(self.runtime.settings, clock=self.clock)
        self.addCleanup(restarted.close)
        session = HostAdapter(restarted, core_policy=self.policy).open_session(self.binding)
        self.assertEqual(session.approve_change_proposal(self.proposal_id, **self.decision), receipt)
        row = restarted.db.connection.execute("SELECT * FROM change_purge_receipts").fetchone()
        payload = json.dumps(dict(row), ensure_ascii=False)
        for value in ("森林故事", self.decision["reason"], self.decision["reviewer"], self.turn.user_event_id):
            self.assertNotIn(value, payload)

    def test_pending_and_rejected_receipts_match_contract(self):
        pending = self.session.get_change_proposal(self.proposal_id)["receipt"]
        self.check_contract(pending)
        self.assertEqual(pending["state"], "not_applied")
        self.assertEqual(pending["remaining_work"], ["PENDING_APPROVAL"])
        receipt = self.session.reject_change_proposal(self.proposal_id, **self.decision)
        self.check_contract(receipt)
        self.assertEqual(receipt["remaining_work"], ["REJECTED"])
        self.assertEqual(self.model.document_calls, 0)

    def test_checkpoint_receipt_matches_contract(self):
        activity = self.runtime.register_activity(self.scope, "写作", goal="开头")
        binding = replace(self.binding, allowed_activity_ids=(activity.activity_id,))
        session = HostAdapter(self.engine, core_policy=self.policy).open_session(binding)
        session.current_event_id = self.turn.user_event_id
        activity_ref = session.registry.issue(ViewReader(self.engine, binding, self.clock).load(
            "activity", activity.activity_id))
        event_ref = session.registry.issue(ViewReader(self.engine, binding, self.clock).load(
            "event", self.turn.user_event_id))
        proposal = session.call_tool("memory_checkpoint_propose", {
            "activity_ref": activity_ref, "expected_version": 1, "goal": "结尾", "open_loops": ["灯塔"],
            "checkpoint_note": "推进到结尾", "evidence_refs": [event_ref]})
        self.assertEqual(proposal["status"], "proposed", proposal)
        receipt = session.approve_change_proposal(proposal["data"]["proposal_id"], **self.decision)
        self.check_contract(receipt)
        self.assertEqual(receipt["affected"]["activities"], 1)

    def test_relationship_purge_removes_its_core_but_keeps_other_relationship_and_domain(self):
        other = replace(self.scope, relationship_id="r2")
        fantasy = replace(self.scope, domain="fictional")
        for scope in (self.scope, other, fantasy):
            self.runtime.apply_core_update(scope, "relationship", {"称呼": "家人"}, "建立关系")
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦"}, "建立身份")
        self.runtime.forget_scope(self.scope)
        self.assertIsNone(self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP))
        self.assertIsNotNone(self.repos.cores.latest(other, CoreKind.RELATIONSHIP))
        self.assertIsNotNone(self.repos.cores.latest(fantasy, CoreKind.RELATIONSHIP))
        self.assertIsNotNone(self.repos.cores.latest(self.scope, CoreKind.IDENTITY))

    def test_event_forgetting_counts_memories_that_keep_an_independent_source(self):
        independent = self.runtime.observe(self.make_turn("独立来源"), self.scope, "independent").event
        memory = self.add_memory(MemoryType.EPISODIC, {"what_happened": "有两个来源的经历"},
                                 source_event_ids=[self.turn.user_event_id, independent.event_id])
        event_ref = self.session.registry.issue(ViewReader(self.engine, self.binding, self.clock).load(
            "event", self.turn.user_event_id))
        self.proposal_id = self.propose(operation="forget_event", target_refs=[event_ref],
                                       replacement_body=None, replacement_text=None)
        receipt = self.approve()
        self.check_contract(receipt)
        self.assertEqual(receipt["affected"]["memories"], 1)
        kept = self.repos.memories.get(memory.memory_id)
        self.assertEqual(kept.status, MemoryStatus.ACTIVE)
        self.assertEqual(kept.source_event_ids, [independent.event_id])
