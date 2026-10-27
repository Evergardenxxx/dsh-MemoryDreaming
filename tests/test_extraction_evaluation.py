"""评分器回归；伪造候选只验证计分逻辑，不作为模型质量结果。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorydreaming.cli import main
from memorydreaming.extraction_evaluation import ReplayExtractor, load_dataset, run_extraction_evaluation
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.llm_extractor import LLMExtractor


class ExtractionEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "cases.json"
        self.dataset = {"dataset_id": "unit-test-only", "cases": [
            {"id": "positive", "content": "我喜欢海边。我讨厌香菜。", "labels": [
                {"memory_type": "semantic", "body": {"obj": "海边"}, "modality": "occurred", "evidence": "我喜欢海边"},
                {"memory_type": "semantic", "body": {"obj": "香菜"}, "modality": "occurred", "evidence": "我讨厌香菜"}]},
            {"id": "negative", "content": "假如我喜欢月球旅行。", "labels": [], "forbid_occurred": True},
        ]}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.dataset, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def candidate(obj="海边", key="one", span=None):
        return {"memory_type": "semantic", "body": {"subject": "用户", "predicate": "喜欢", "obj": obj},
                "candidate_key": key, "source_span": span or [0, 5]}

    @staticmethod
    def extractor(candidates):
        return LLMExtractor(lambda prompt: {"schema_version": "1.0", "candidates": candidates})

    def test_false_positives_false_negatives_and_negative_fact_cases_are_counted(self):
        report = run_extraction_evaluation(self.extractor([self.candidate()]), dataset_path=self.path)
        self.assertEqual((report["expected"], report["predicted"], report["matched"]), (2, 2, 1))
        self.assertEqual(report["false_positives"], 1)
        self.assertEqual(report["false_negatives"], 1)
        self.assertEqual(report["precision"], .5)
        self.assertEqual(report["recall"], .5)
        self.assertEqual(report["false_memory_case_rate"], 1)

    def test_one_candidate_cannot_count_as_two_facts_and_incorrect_spans_do_not_match(self):
        self.dataset["cases"][0]["labels"] = [self.dataset["cases"][0]["labels"][0]] * 2
        self.save()
        report = run_extraction_evaluation(self.extractor([self.candidate()]), dataset_path=self.path)
        self.assertEqual(report["matched"], 1)
        report = run_extraction_evaluation(self.extractor([self.candidate(span=[1, 5])]), dataset_path=self.path)
        self.assertEqual(report["matched"], 0)

    def test_errors_remain_in_denominator_and_report_does_not_copy_content_or_exception(self):
        def broken(prompt):
            raise RuntimeError("private output or sk-secret")
        report = run_extraction_evaluation(LLMExtractor(broken), dataset_path=self.path)
        self.assertEqual(report["errors"], 2)
        self.assertEqual(report["recall"], 0)
        self.assertEqual(report["case_accuracy"], 0)
        self.assertEqual(report["false_negatives"], 2)
        self.assertEqual(report["negative_case_errors"], 1)
        for text in ("private output", "sk-secret", "我喜欢海边"):
            self.assertNotIn(text, str(report))

    def test_predictions_can_be_replayed_without_provider_or_runtime(self):
        predictions = self.root / "predictions.json"
        report = run_extraction_evaluation(self.extractor([self.candidate()]), dataset_path=self.path,
                                           predictions_output=predictions)
        replay = ReplayExtractor(predictions, self.dataset["dataset_id"])
        with patch("memorydreaming.runtime.MemoryRuntime", side_effect=AssertionError("no database")):
            second = run_extraction_evaluation(replay, dataset_path=self.path)
        for key in ("case_accuracy", "precision", "recall", "false_memory_case_rate", "results"):
            self.assertEqual(report[key], second[key])

    def test_missing_prediction_is_an_error_and_dataset_mismatch_is_rejected(self):
        predictions = self.root / "missing.json"
        predictions.write_text(json.dumps({"dataset_id": "unit-test-only", "predictions": {}}), encoding="utf-8")
        replay = ReplayExtractor(predictions, self.dataset["dataset_id"])
        report = run_extraction_evaluation(replay, dataset_path=self.path)
        self.assertEqual(report["errors"], 2)
        self.assertEqual(report["passed"], 0)
        with self.assertRaises(ValueError):
            ReplayExtractor(predictions, "other-dataset")
        with self.assertRaises(ValueError):
            ReplayExtractor(predictions, "unit-test-only", dataset_sha256="changed-dataset-hash")

    def test_rule_baseline_saved_predictions_replay_with_default_time_resolution(self):
        predictions = self.root / "rule-predictions.json"
        report = run_extraction_evaluation(RuleBasedExtractor(), dataset_path=self.path,
                                           predictions_output=predictions)
        second = run_extraction_evaluation(ReplayExtractor(predictions, self.dataset["dataset_id"]),
                                           dataset_path=self.path)
        self.assertEqual(report["results"], second["results"])

    def test_bundled_dataset_covers_four_types_and_negative_contexts(self):
        dataset = load_dataset()
        self.assertEqual(len(dataset["cases"]), 24)
        self.assertEqual({label["memory_type"] for case in dataset["cases"] for label in case["labels"]},
                         {"semantic", "episodic", "working", "procedural"})
        self.assertGreaterEqual(sum(case.get("forbid_occurred", False) for case in dataset["cases"]), 10)

    def test_cli_runs_offline_before_opening_runtime_and_writes_report(self):
        report_path = self.root / "report.json"
        with patch("memorydreaming.cli._open_runtime", side_effect=AssertionError("must not open memory")), \
                patch("builtins.print"):
            result = main(["eval-extraction", "--output", str(report_path)])
        self.assertEqual(result, 1)  # 基线存在差距时保持非零退出码。
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["case_count"], 24)
        self.assertLess(report["recall"], 1)

    def test_labels_must_reference_actual_source_text(self):
        self.dataset["cases"][0]["labels"][0]["evidence"] = "不存在的原话"
        self.save()
        with self.assertRaises(ValueError):
            load_dataset(self.path)
