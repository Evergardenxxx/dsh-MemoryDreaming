"""可复现的向量评估及失败报告，不将协议 mock 当作模型质量证据。"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memorydreaming.cli import main
from memorydreaming.embedding_evaluation import DEFAULT_DATASET, load_embedding_dataset, run_embedding_evaluation
from memorydreaming.retrieval.embedding import EmbeddingError, HashingEmbedder


class EmbeddingEvaluationTest(unittest.TestCase):
    def test_builtin_hash_baseline_reports_category_gap_and_stable_fingerprints(self):
        first = run_embedding_evaluation(HashingEmbedder())
        second = run_embedding_evaluation(HashingEmbedder())
        self.assertEqual(first["dataset_sha256"], second["dataset_sha256"])
        self.assertEqual(first["embedding_namespace"], second["embedding_namespace"])
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["query_count"], 16)
        self.assertEqual(first["metrics"]["positive_count"], 12)
        self.assertEqual(first["metrics"]["negative_count"], 4)
        self.assertEqual(first["metrics"]["by_category"]["literal"]["recall_at_k"], 1)
        self.assertLess(first["metrics"]["by_category"]["paraphrase"]["recall_at_k"], 1)
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("傍晚沿着海边散步", serialized)
        self.assertNotIn("黄昏的时候去沙滩", serialized)
        self.assertEqual(first["dataset_review"], "synthetic_not_independently_reviewed")

    def test_failure_after_some_queries_does_not_report_partial_quality_as_complete(self):
        class FailsMidway(HashingEmbedder):
            calls = 0

            def embed_query(self, text):
                self.calls += 1
                if self.calls == 2:
                    raise EmbeddingError()
                return self.embed(text)

        report = run_embedding_evaluation(FailsMidway())
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse(report["quality_complete"])
        self.assertEqual(len(report["results"]), 1)
        self.assertNotIn("metrics", report)

    def test_invalid_dataset_rejects_unknown_relevance_duplicates_and_empty_queries(self):
        original = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            for mutation in (lambda d: d["queries"][0].update(relevant_ids=["unknown"]),
                             lambda d: d["documents"].append(d["documents"][0]),
                             lambda d: d["queries"][0].update(text=""),
                             lambda d: d["queries"][0].update(category="negative")):
                data = json.loads(json.dumps(original))
                mutation(data)
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_embedding_dataset(path)

    def test_cli_ignores_user_database_and_saves_report(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "must-not-be-created.db"
            output = Path(directory) / "report.json"
            with redirect_stdout(io.StringIO()), patch.dict("os.environ", {"MEMORYDREAMING_EMBEDDING_PROVIDER": "hash"}):
                status = main(["--db", str(db_path), "eval-embedding", "--k", "2", "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertFalse(db_path.exists())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["k"], 2)

    def test_cli_returns_unavailable_exit_code_without_model_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"MEMORYDREAMING_EMBEDDING_PROVIDER": "sentence-transformers",
                   "MEMORYDREAMING_EMBEDDING_MODEL_PATH": str(Path(directory) / "absent"),
                   "MEMORYDREAMING_EMBEDDING_REVISION": "v1"}
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch.dict("os.environ", env):
                status = main(["eval-embedding"])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(stdout.getvalue())["error_code"], "EMBEDDING_UNAVAILABLE")

    def test_metrics_use_one_query_vote_and_fractional_multidoc_recall(self):
        data = {"dataset_id": "small", "description": "synthetic",
                "documents": [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}],
                "queries": [{"id": "positive", "text": "one", "relevant_ids": ["a", "b"], "category": "literal"},
                            {"id": "negative", "text": "three", "relevant_ids": [], "category": "negative"}]}
        class Controlled:
            name = "controlled"
            dim = 2

            def embed(self, text):
                return [1, 0] if text == "one" else [0, 1]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            report = run_embedding_evaluation(Controlled(), dataset_path=path, k=1, min_score=0.5)
        self.assertEqual(report["metrics"]["recall_at_k"], 0.5)
        self.assertEqual(report["metrics"]["mrr_at_k"], 1)
        self.assertEqual(report["metrics"]["negative_false_match_rate"], 1)


if __name__ == "__main__":
    unittest.main()
