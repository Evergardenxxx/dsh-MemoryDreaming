"""质量验收门槛：合成/重放不能冒充真实、阈值不能偷看验收标签。"""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memorydreaming.embedding_evaluation import DEFAULT_DATASET
from memorydreaming.extraction_evaluation import dataset_fingerprint
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.quality_acceptance import (QualityPolicy, calibrate_embedding, disjoint_datasets,
    evaluate_embedding_acceptance, evaluate_extraction_acceptance)
from memorydreaming.retrieval.embedding import HashingEmbedder


class QualityAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calibration = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
        self.evaluation = copy.deepcopy(self.calibration)
        self.calibration["dataset_id"] = "calibration-only"
        self.evaluation["dataset_id"] = "evaluation-only"
        self.calibration["queries"] = [self.calibration["queries"][i] for i in (0, 2, 4, 6, 8, 10, 12, 14)]
        self.evaluation["queries"] = [self.evaluation["queries"][i] for i in (1, 3, 5, 7, 9, 11, 13, 15)]
        self.cal_path, self.eval_path = self.root/"cal.json", self.root/"eval.json"
        self.save()

    def save(self):
        self.cal_path.write_text(json.dumps(self.calibration, ensure_ascii=False), encoding="utf-8")
        self.eval_path.write_text(json.dumps(self.evaluation, ensure_ascii=False), encoding="utf-8")

    def artifact(self, embedder=None):
        return calibrate_embedding(embedder or HashingEmbedder(), calibration_path=self.cal_path,
                                   evaluation_path=self.eval_path, thresholds=[.1, .4, .9])

    def evaluate(self, artifact, **kwargs):
        return evaluate_embedding_acceptance(HashingEmbedder(), calibration_path=self.cal_path,
            evaluation_path=self.eval_path, calibration_artifact=artifact, **kwargs)

    def test_calibration_never_encodes_evaluation_queries(self):
        class Observed(HashingEmbedder):
            def embed_query(inner, text):
                self.assertNotIn(text, [q["text"] for q in self.evaluation["queries"]])
                return inner.embed(text)
        artifact = self.artifact(Observed())
        self.assertEqual(artifact["status"], "calibrated")
        self.assertIn(artifact["min_score"], [.1, .4, .9])

    def test_overlap_ids_or_normalized_text_is_rejected(self):
        for mode in ("id", "text", "dataset"):
            evaluation = copy.deepcopy(self.evaluation)
            if mode == "id":
                evaluation["queries"][0]["id"] = self.calibration["queries"][0]["id"]
            elif mode == "text":
                evaluation["queries"][0]["text"] = " "+self.calibration["queries"][0]["text"]+" "
            else:
                evaluation["dataset_id"] = self.calibration["dataset_id"]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                disjoint_datasets(self.calibration, evaluation, "embedding")

    def test_frozen_artifact_model_dataset_and_digest_cannot_be_changed(self):
        artifact = self.artifact()
        changed = copy.deepcopy(artifact)
        changed["min_score"] = .99
        with self.assertRaises(ValueError):
            self.evaluate(changed)
        with self.assertRaises(ValueError):
            evaluate_embedding_acceptance(HashingEmbedder(dim=256), calibration_path=self.cal_path,
                evaluation_path=self.eval_path, calibration_artifact=artifact)
        self.evaluation["queries"][0]["text"] += "（新的内容）"
        self.save()
        with self.assertRaises(ValueError):
            self.evaluate(artifact)

    def test_synthetic_hash_report_can_complete_execution_but_never_pass_acceptance(self):
        report = self.evaluate(self.artifact())
        self.assertTrue(report["quality_complete"])
        self.assertFalse(report["acceptance"]["accepted"])
        self.assertTrue(report["acceptance"]["threshold_frozen"])
        self.assertIn("REAL_MODEL_EVIDENCE_REQUIRED", report["acceptance"]["reasons"])
        self.assertIn("INDEPENDENT_HUMAN_REVIEW_REQUIRED", report["acceptance"]["reasons"])
        self.assertIn("INSUFFICIENT_EVALUATION_SAMPLES", report["acceptance"]["reasons"])
        self.assertNotIn(self.evaluation["queries"][0]["text"], str(report))

    def test_review_receipt_requires_exact_dataset_binding_and_timezone(self):
        artifact = self.artifact()
        review = {"independent_human_review": True, "reviewer": "external-reviewer",
                  "reviewed_at": datetime.now(timezone.utc).isoformat(),
                  "calibration_dataset_sha256": dataset_fingerprint(self.calibration),
                  "evaluation_dataset_sha256": dataset_fingerprint(self.evaluation)}
        self.assertTrue(self.evaluate(artifact, review=review)["acceptance"]["independent_human_review"])
        review["evaluation_dataset_sha256"] = "wrong"
        self.assertFalse(self.evaluate(artifact, review=review)["acceptance"]["independent_human_review"])
        review["evaluation_dataset_sha256"] = dataset_fingerprint(self.evaluation)
        review["reviewed_at"] = "2026-10-27T12:00:00"
        self.assertFalse(self.evaluate(artifact, review=review)["acceptance"]["independent_human_review"])

    def test_extraction_rule_baseline_is_not_live_deepseek_evidence(self):
        cal = {"dataset_id": "cal", "cases": [{"id": "cal-1", "content": "我喜欢猫。", "labels": [
            {"memory_type": "semantic", "body": {"obj": "猫"}, "modality": "occurred", "evidence": "我喜欢猫"}]}]}
        evaluation = {"dataset_id": "evaluation", "cases": [
            {"id": "eval-1", "content": "我喜欢海边。", "labels": [
                {"memory_type": "semantic", "body": {"obj": "海边"}, "modality": "occurred", "evidence": "我喜欢海边"}]},
            {"id": "eval-2", "content": "假如我会飞。", "labels": [], "forbid_occurred": True}]}
        self.cal_path.write_text(json.dumps(cal), encoding="utf-8")
        self.eval_path.write_text(json.dumps(evaluation), encoding="utf-8")
        report = evaluate_extraction_acceptance(RuleBasedExtractor(), calibration_path=self.cal_path, evaluation_path=self.eval_path)
        self.assertFalse(report["acceptance"]["accepted"])
        self.assertIn("REAL_MODEL_EVIDENCE_REQUIRED", report["acceptance"]["reasons"])
        self.assertEqual(report["errors"], 0)

    def test_missing_api_key_reports_blocker_without_calling_provider(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("acceptance_runner", Path(__file__).resolve().parents[1]/"scripts/quality_acceptance.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        output = self.root/"report.json"
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), patch("builtins.print"):
            code = script.main(["extraction-deepseek", "--calibration", str(self.cal_path), "--evaluation", str(self.eval_path), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["provider_called"])

    def test_harness_export_does_not_leak_labels_or_fake_successful_predictions(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("acceptance_export", Path(__file__).resolve().parents[1]/"scripts/quality_acceptance.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        data = {"dataset_id": "export", "cases": [{"id": "case-1", "content": "我喜欢海边", "labels": [
            {"memory_type": "semantic", "body": {"obj": "海边"}, "modality": "occurred", "evidence": "我喜欢海边"}]}]}
        self.eval_path.write_text(json.dumps(data), encoding="utf-8")
        out = self.root/"harness"
        report = script.export_harness(self.eval_path, out)
        self.assertFalse(report["provider_called"])
        self.assertNotIn('"labels"', (out/"case-0001.txt").read_text(encoding="utf-8"))
        predictions = json.loads((out/"predictions.json").read_text(encoding="utf-8"))
        self.assertEqual(predictions["predictions"]["case-1"]["error_code"], "NOT_RUN")
        with self.assertRaises(ValueError):
            script.export_harness(self.eval_path, out)

    def test_policy_rejects_nonfinite_or_boolean_thresholds(self):
        for kwargs in ({"min_recall": float("nan")}, {"min_cases": True}, {"max_false_memory_rate": -1}):
            with self.assertRaises(ValueError):
                QualityPolicy(**kwargs)
