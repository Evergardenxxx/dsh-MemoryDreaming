"""模型部署约定、生产/评测一致性和配置命名空间；不冒充模型质量评估。"""

from contextlib import closing, redirect_stdout
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from memorydreaming import MemoryRuntime, Settings
from memorydreaming.cli import main
from memorydreaming.retrieval.embedding import embedder_from_settings
from memorydreaming.retrieval.isolated import IsolatedSentenceTransformerEmbedder
from memorydreaming.retrieval.profiles import QWEN3_MEMORY_QUERY_PREFIX, QWEN3_NATIVE_DIMS
from memorydreaming.retrieval.vector import VectorIndex
from memorydreaming.storage.database import Database


ROOT = Path(__file__).resolve().parents[1]


class EmbeddingProfilesTest(unittest.TestCase):
    def settings(self, **changes):
        options = dict(embedding_provider="sentence-transformers", embedding_profile="qwen3-embedding-0.6b",
                       embedding_model_path="missing-local-only", embedding_revision="fixed-test-revision")
        return Settings(**(options | changes))

    def test_profiles_map_native_dimensions_without_loading_or_importing_models(self):
        for profile, native in QWEN3_NATIVE_DIMS.items():
            for dimension in (32, 512, native):
                with self.subTest(profile=profile, dimension=dimension), patch.dict(
                    sys.modules, {"sentence_transformers": None}
                ):
                    embedder = embedder_from_settings(self.settings(embedding_profile=profile, embedding_dim=dimension))
                    self.assertIsNone(embedder._model)
                    self.assertEqual(embedder.dim, dimension)
                    self.assertEqual(embedder.cache_signature["expected_model_dim"], native)
                    self.assertEqual(embedder.cache_signature["query_prefix"], QWEN3_MEMORY_QUERY_PREFIX)
                    self.assertEqual(embedder.cache_signature["document_prefix"], "")
                    self.assertEqual(embedder.cache_signature["model_profile"], "qwen3")

    def test_profile_rejects_wrong_backend_dimension_or_query_document_protocol(self):
        invalid = ({"embedding_profile": "unknown"}, {"embedding_profile": None},
                   {"embedding_profile": ["qwen3"]},
                   {"embedding_provider": "hash"}, {"embedding_dim": 31}, {"embedding_dim": 1025},
                   {"embedding_dim": True}, {"embedding_document_prefix": "资料："},
                   {"embedding_query_prefix": "检索："}, {"embedding_query_prefix": "Instruct: \nQuery:"},
                   {"embedding_query_prefix": "Instruct: find memories\nQuery: "})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings(**changes)

    def test_runtime_and_inprocess_evaluation_share_same_numerical_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(db_path=Path(directory)/"test.db")
            direct = embedder_from_settings(settings)
            with MemoryRuntime(settings) as runtime:
                actual = runtime.vectors._embedder
                self.assertIsInstance(actual, IsolatedSentenceTransformerEmbedder)
                self.assertIsNone(actual._process)
                self.assertEqual(actual.cache_signature, direct.cache_signature)
                self.assertEqual(runtime.vectors.model_name, VectorIndex(runtime.db, direct).model_name)

    def test_profile_dimension_instruction_and_revision_changes_isolate_indexes(self):
        settings = self.settings()
        configs = (settings, replace(settings, embedding_dim=256),
                   replace(settings, embedding_profile="qwen3-embedding-4b"),
                   replace(settings, embedding_revision="another-revision"),
                   replace(settings, embedding_query_prefix="Instruct: Find related past experiences\nQuery:"),
                   replace(settings, embedding_profile="generic"))
        with closing(Database.open(":memory:")) as database:
            namespaces = [VectorIndex(database, embedder_from_settings(config)).model_name for config in configs]
        self.assertEqual(len(set(namespaces)), len(configs))

    def test_generic_keeps_existing_prefix_dimension_and_v1_projection(self):
        embedder = embedder_from_settings(self.settings(embedding_profile="generic", embedding_dim=384,
            embedding_query_prefix="为这个句子生成表示以用于检索相关文章："))
        self.assertEqual(embedder.cache_signature["adapter"], "sentence-transformers-local-v1")
        self.assertNotIn("matryoshka", embedder.cache_signature)
        self.assertEqual(embedder.dim, 384)
        self.assertEqual(embedder_from_settings(Settings()).name, "hash-ngram-1024")

    def test_environment_loads_explicit_qwen_profile_and_output_dimension(self):
        env = {"MEMORYDREAMING_EMBEDDING_PROVIDER": "sentence-transformers",
               "MEMORYDREAMING_EMBEDDING_PROFILE": "qwen3-embedding-8b",
               "MEMORYDREAMING_EMBEDDING_DIM": "256",
               "MEMORYDREAMING_EMBEDDING_MODEL_PATH": "pre-downloaded",
               "MEMORYDREAMING_EMBEDDING_REVISION": "test-fixed",
               "MEMORYDREAMING_EMBEDDING_QUERY_PREFIX": "",
               "MEMORYDREAMING_EMBEDDING_DOCUMENT_PREFIX": ""}
        with patch.dict(os.environ, env):
            settings = Settings.from_env()
        with closing(embedder_from_settings(settings, isolated=True)) as embedder:
            self.assertEqual(embedder.dim, 256)
            self.assertEqual(embedder.cache_signature["expected_model_dim"], 4096)
            self.assertIsNone(embedder._process)

    def test_default_memory_instruction_is_applied_once_and_documents_are_unprompted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(embedding_model_path=directory, embedding_dim=32)
            model = MagicMock(max_seq_length=32768)
            transformer = SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace(model_type="qwen3")))
            pooling = SimpleNamespace(pooling_mode_lasttoken=True, include_prompt=True)
            model.__getitem__.side_effect = lambda index: (transformer, pooling)[index]
            model.get_sentence_embedding_dimension.return_value = 1024
            model.encode.side_effect = lambda texts, **kwargs: [[3, 4] + [0] * 1022 for _ in texts]
            with patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=lambda *a, **kw: model)}):
                embedder = embedder_from_settings(settings)
                self.assertEqual(embedder.embed_query("沙滩")[:2], [.6, .8])
                self.assertEqual(embedder.embed_documents(["海边"])[0][:2], [.6, .8])
            self.assertEqual(model.encode.call_args_list[0].args[0], [QWEN3_MEMORY_QUERY_PREFIX+"沙滩"])
            self.assertEqual(model.encode.call_args_list[1].args[0], ["海边"])
            self.assertTrue(all(call.kwargs["prompt"] == "" for call in model.encode.call_args_list))

    def test_cli_missing_qwen_weights_reports_unavailable_without_touching_user_database(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(embedding_model_path=str(Path(directory)/"absent"))
            path = Path(directory)/"never-open.db"
            stdout = io.StringIO()
            with patch("memorydreaming.cli.Settings.from_env", return_value=settings), redirect_stdout(stdout):
                status = main(["--db", str(path), "eval-embedding"])
            self.assertEqual(status, 2)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["error_code"], "EMBEDDING_UNAVAILABLE")
            self.assertFalse(report["quality_complete"])
            self.assertFalse(path.exists())

    def test_quality_cli_uses_same_profile_options_and_closes_model_on_failure(self):
        spec = importlib.util.spec_from_file_location("quality_acceptance_script", ROOT/"scripts/quality_acceptance.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        expected = self.settings(embedding_dim=256, embedding_batch_size=2,
                                 embedding_max_seq_length=128, embedding_timeout_seconds=5)
        captured = []
        close = MagicMock()
        real_factory = embedder_from_settings

        def factory(settings, **kwargs):
            captured.append((settings, kwargs))
            adapter = real_factory(settings, **kwargs)
            adapter.close = close
            return adapter

        with tempfile.TemporaryDirectory() as directory, patch(
            "memorydreaming.retrieval.embedding.embedder_from_settings", side_effect=factory
        ), patch.object(script, "calibrate_embedding", return_value={"status": "unavailable"}), redirect_stdout(io.StringIO()):
            status = script.main(["embedding", "--model-path", "missing-local-only", "--revision", "fixed-test-revision",
                "--profile", "qwen3-embedding-0.6b", "--dim", "256", "--batch-size", "2", "--max-seq-length", "128",
                "--timeout-seconds", "5", "--calibration", "unused", "--evaluation", "unused",
                "--calibration-artifact", str(Path(directory)/"nested"/"frozen.json"),
                "--output", str(Path(directory)/"report.json")])
        self.assertEqual(status, 2)
        self.assertEqual(captured, [(expected, {"isolated": True})])
        close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
