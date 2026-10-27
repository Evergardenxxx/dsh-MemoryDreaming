"""批量审批复用已有提议，所有变更及单项/整批回执原子生效。"""

from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from memorydreaming import CoreProposalPolicy, HostAdapter, SessionBinding
from memorydreaming.errors import VersionConflict
from memorydreaming.llm import AdapterError
from memorydreaming.models import CoreKind, MemoryStatus, MemoryType, Scope
from memorydreaming.runtime import MemoryRuntime
from tests.helpers import RuntimeTestCase


class ProposalBatchTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.runtime.apply_core_update(self.scope, "identity", {"名字": "小梦"}, "初始")
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "爸爸"}, "初始")
        self.memories = [self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": word})
                         for word in ("海边故事", "森林故事")]
        self.policy = CoreProposalPolicy(identity_fields=("名字",), relationship_fields=("称呼",), change_proposals=True)
        self.binding = SessionBinding("u1", "c1", "r1", "batch-window")
        self.session = HostAdapter(self.runtime, core_policy=self.policy, max_calls_per_turn=32).open_session(self.binding)
        self.turn = self.session.prepare_turn("request", "改名为梦梦，改称呼为老爸，并把偏好改成山间故事。", max_tokens=0)

    def core_proposal(self, kind):
        target = self.session.bootstrap()["data"][kind + "_ref"]
        field, value = ("名字", "梦梦") if kind == "identity" else ("称呼", "老爸")
        result = self.session.call_tool("memory_core_propose", {"target_ref": target,
            "fields": [{"name": field, "value": value, "evidence_refs": []}], "reason": "用户请求", "max_tokens": 2048})
        self.assertEqual(result["status"], "proposed", result)
        return {"kind": "core", "proposal_id": result["data"]["proposal_id"]}

    def change_proposal(self, index=0, *, replacement=False, operation=None):
        result = self.session.call_tool("memory_search", {"query": self.memories[index].body["obj"],
            "activity_ref": None, "time_range": None, "detail": "evidence", "max_items": 10, "max_tokens": 4096})
        target = next(item["ref"] for item in result["data"]["evidence"] if self.memories[index].body["obj"] in item["text"])
        result = self.session.call_tool("memory_request_change", {"operation": operation or ("correct_fact" if replacement else "forget_fact"),
            "target_refs": [target], "replacement_text": None,
            "replacement_body": {"subject": "用户", "predicate": "喜欢", "obj": "山间故事"} if replacement else None,
            "reason": "用户明确变更", "evidence_refs": []})
        self.assertEqual(result["status"], "proposed", result)
        return {"kind": "change", "proposal_id": result["data"]["proposal_id"]}

    def batch(self, items):
        return self.session.create_proposal_batch(items, reason="同一次用户请求", idempotency_key="batch-1")

    def approve(self, batch_id, **changes):
        return self.session.approve_proposal_batch(batch_id, **{"user_event_id": self.turn.user_event_id,
            "reviewer": "trusted-host", "reason": "已核对整批变更", **changes})

    def test_mixed_core_and_fact_batch_applies_once_after_host_approval(self):
        items = [self.core_proposal("identity"), self.core_proposal("relationship"), self.change_proposal()]
        batch = self.batch(items)
        self.assertEqual(batch["state"], "pending")
        self.assertEqual(self.batch(items)["batch_id"], batch["batch_id"])
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload["名字"], "小梦")
        receipt = self.approve(batch["batch_id"])
        self.assertTrue(receipt["applied"])
        self.assertEqual(len(receipt["receipts"]), 3)
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload["名字"], "梦梦")
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.RELATIONSHIP).payload["称呼"], "老爸")
        self.assertEqual(self.repos.memories.get(self.memories[0].memory_id).status, MemoryStatus.DELETED)
        self.assertEqual(receipt, self.approve(batch["batch_id"]))
        self.assertTrue(self.runtime.verify_journal()["valid"])

    def test_failure_after_first_core_write_rolls_back_entire_batch(self):
        items = [self.core_proposal("identity"), self.core_proposal("relationship")]
        batch = self.batch(items)
        original = self.runtime.apply_core_update
        def fail_second(scope, kind, *args, **kwargs):
            if kind == CoreKind.RELATIONSHIP:
                raise RuntimeError("第二项故障")
            return original(scope, kind, *args, **kwargs)
        with patch.object(self.runtime, "apply_core_update", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.approve(batch["batch_id"])
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).core_version, 1)
        self.assertEqual(self.session.get_proposal_batch(batch["batch_id"])["state"], "pending")
        for item in items:
            self.assertEqual(self.session.get_core_proposal(item["proposal_id"])["state"], "pending_validation")
        self.assertTrue(self.approve(batch["batch_id"])["applied"])

    def test_expected_conflict_rolls_back_and_invalidates_batch_without_changing_single_proposals(self):
        items = [self.core_proposal("identity"), self.core_proposal("relationship")]
        batch = self.batch(items)
        original = self.runtime.apply_core_update
        def conflict(scope, kind, *args, **kwargs):
            if kind == CoreKind.RELATIONSHIP:
                raise VersionConflict(1, 2)
            return original(scope, kind, *args, **kwargs)
        with patch.object(self.runtime, "apply_core_update", side_effect=conflict):
            result = self.approve(batch["batch_id"])
        self.assertEqual(result["state"], "invalidated")
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).core_version, 1)
        self.assertEqual(self.session.get_core_proposal(items[0]["proposal_id"])["state"], "pending_validation")

    def test_stale_member_rejects_whole_batch_before_writes(self):
        items = [self.core_proposal("identity"), self.core_proposal("relationship")]
        batch = self.batch(items)
        self.clock.advance(seconds=1)
        self.runtime.apply_core_update(self.scope, "relationship", {"称呼": "新称呼"}, "另一个窗口更新")
        receipt = self.approve(batch["batch_id"])
        self.assertEqual(receipt["state"], "invalidated")
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).payload["名字"], "小梦")

    def test_structured_changes_encode_outside_write_transaction_and_publish_together(self):
        items = [self.core_proposal("identity"), self.change_proposal(0, replacement=True), self.change_proposal(1, replacement=True)]
        batch = self.batch(items)
        encode = self.runtime.vectors._encode_documents
        calls = []
        def check(texts):
            self.assertFalse(self.runtime.db.connection.in_transaction)
            calls.append(texts)
            return encode(texts)
        with patch.object(self.runtime.vectors, "_encode_documents", side_effect=check):
            receipt = self.approve(batch["batch_id"])
        self.assertTrue(receipt["applied"], receipt)
        self.assertEqual(len(calls), 2)
        self.assertEqual([self.repos.memories.get(m.memory_id).status for m in self.memories], [MemoryStatus.SUPERSEDED] * 2)

    def test_reject_is_idempotent_and_cannot_later_be_approved(self):
        batch = self.batch([self.core_proposal("identity")])
        args = {"user_event_id": self.turn.user_event_id, "reviewer": "trusted-host", "reason": "拒绝该批次"}
        result = self.session.reject_proposal_batch(batch["batch_id"], **args)
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result, self.session.reject_proposal_batch(batch["batch_id"], **args))
        with self.assertRaises(AdapterError):
            self.approve(batch["batch_id"])

    def test_wrong_user_event_or_foreign_conversation_cannot_approve(self):
        batch = self.batch([self.core_proposal("identity")])
        with self.assertRaises(AdapterError):
            self.approve(batch["batch_id"], user_event_id="foreign")
        other = HostAdapter(self.runtime, core_policy=self.policy).open_session(
            SessionBinding("u1", "c1", "r1", "other-window"))
        with self.assertRaises(AdapterError):
            other.get_proposal_batch(batch["batch_id"])

    def test_duplicate_target_and_scope_forget_batch_are_rejected(self):
        item = self.core_proposal("identity")
        with self.assertRaises(AdapterError):
            self.batch([item, item])
        proposal = self.session.call_tool("memory_request_change", {"operation": "forget_scope", "target_refs": [],
            "replacement_text": None, "reason": "整库删除", "evidence_refs": []})
        with self.assertRaises(AdapterError):
            self.batch([{"kind": "change", "proposal_id": proposal["data"]["proposal_id"]}])

    def test_batch_purged_with_request_scope(self):
        self.batch([self.core_proposal("identity")])
        self.assertEqual(self.count_rows("proposal_batches"), 1)
        self.runtime.forget_scope(self.scope)
        self.assertEqual(self.count_rows("proposal_batches"), 0)

    def test_snapshot_preserves_pending_batch_and_approval_receipts(self):
        batch = self.batch([self.core_proposal("identity"), self.core_proposal("relationship")])
        path = Path(self._tmpdir.name) / "batch.json"
        self.runtime.export_snapshot(str(path))
        with MemoryRuntime(replace(self.runtime.settings, db_path=Path(self._tmpdir.name) / "batch-copy.db"), clock=self.clock) as target:
            target.db.meta_set("forgetting_hmac_key", self.runtime.db.meta_get("forgetting_hmac_key"))
            target.import_snapshot(str(path))
            session = HostAdapter(target, core_policy=self.policy).open_session(self.binding)
            receipt = session.approve_proposal_batch(batch["batch_id"], user_event_id=self.turn.user_event_id,
                                                     reviewer="host", reason="快照恢复后审批")
            self.assertTrue(receipt["applied"], receipt)
            self.assertEqual(receipt, session.approve_proposal_batch(batch["batch_id"], user_event_id=self.turn.user_event_id,
                                                     reviewer="host", reason="快照恢复后审批"))

    def test_earlier_deletion_of_later_evidence_invalidates_whole_batch(self):
        target = self.memories[0]
        search = self.session.call_tool("memory_search", {"query": target.body["obj"], "activity_ref": None,
            "time_range": None, "detail": "evidence", "max_items": 10, "max_tokens": 4096})
        source = next(i["ref"] for i in search["data"]["evidence"] if target.body["obj"] in i["text"])
        proposal = self.session.call_tool("memory_core_propose", {"target_ref": self.session.bootstrap()["data"]["identity_ref"],
            "fields": [{"name": "名字", "value": "梦梦", "evidence_refs": [source]}], "reason": "带依据修改", "max_tokens": 2048})
        batch = self.batch([self.change_proposal(), {"kind": "core", "proposal_id": proposal["data"]["proposal_id"]}])
        receipt = self.approve(batch["batch_id"])
        self.assertEqual(receipt["state"], "invalidated")
        self.assertEqual(self.repos.memories.get(target.memory_id).status, MemoryStatus.ACTIVE)
        self.assertEqual(self.repos.cores.latest(self.scope, CoreKind.IDENTITY).core_version, 1)
