"""可选语义模型的部署边界与向量投影的失败/并发回归；不证明真实召回质量。"""

from array import array
from contextlib import closing
from dataclasses import replace
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from memorydreaming.config import Settings
from memorydreaming.errors import SourceRevisionChanged
from memorydreaming.extractors import RuleBasedExtractor
from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.models import ForgetSelector, MemoryType, ReadFilter, Scope
from memorydreaming.retrieval.embedding import (EmbeddingError, HashingEmbedder, SentenceTransformerEmbedder,
                                                embedder_from_settings, normalized_vector)
from memorydreaming.retrieval.vector import VectorIndex
from memorydreaming.runtime import MemoryRuntime
from memorydreaming.storage.database import Database
from tests.helpers import NOW, RuntimeTestCase


class ToyEmbedder:
    """可控故障和查询/正文区分；只用于协议测试。"""
    name = "toy"
    dim = 2

    def __init__(self, revision="v1"):
        self.cache_signature = {"revision": revision}
        self.available = True
        self.query_calls = 0
        self.on_documents = None

    def embed(self, text):
        return [3.0, 4.0]

    def embed_documents(self, texts):
        if self.on_documents:
            self.on_documents()
        return [self.embed(text) for text in texts]

    def embed_query(self, text):
        self.query_calls += 1
        if not self.available:
            raise RuntimeError("secret model path and user text")
        return [6.0, 8.0]


class LocalAdapterTest(RuntimeTestCase):
    def adapter(self, **changes):
        return SentenceTransformerEmbedder(self._tmpdir.name, revision="weights-sha", dim=2, **changes)

    def test_local_model_is_lazy_uses_distinct_prefixes_and_no_remote_code(self):
        model = MagicMock()
        model.get_sentence_embedding_dimension.return_value = 2
        model.max_seq_length = 512
        model.encode.side_effect = lambda texts, **kw: [[3.0, 4.0] for text in texts]
        constructor = MagicMock(return_value=model)
        with patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=constructor)}):
            adapter = self.adapter(query_prefix="查询：", document_prefix="资料：")
            constructor.assert_not_called()
            self.assertEqual(adapter.embed_query("沙滩"), [0.6, 0.8])
            self.assertEqual(adapter.embed_documents(["海边", "灯塔"]), [[0.6, 0.8]] * 2)
        self.assertEqual(constructor.call_count, 1)
        self.assertEqual(constructor.call_args.kwargs,
                         {"device": "cpu", "local_files_only": True, "trust_remote_code": False})
        self.assertEqual(model.encode.call_args_list[0].args[0], ["查询：沙滩"])
        self.assertEqual(model.encode.call_args_list[1].args[0], ["资料：海边", "资料：灯塔"])
        self.assertEqual(model.encode.call_args.kwargs["prompt"], "")

    def test_missing_dependency_and_missing_directory_return_sanitized_errors(self):
        for adapter in (self.adapter(), SentenceTransformerEmbedder(
            self.db_path.parent / "not-downloaded", revision="v1", dim=2
        )):
            with self.subTest(adapter=adapter), patch.dict(sys.modules, {"sentence_transformers": None}):
                with self.assertRaises(EmbeddingError) as caught:
                    adapter.embed_query("private text")
                self.assertEqual(str(caught.exception), "EMBEDDING_UNAVAILABLE")

    def test_model_dimension_and_truncation_mismatch_fail_before_encoding(self):
        for dim, length in ((3, 512), (2, 256)):
            model = MagicMock(max_seq_length=length)
            model.get_sentence_embedding_dimension.return_value = dim
            with patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(
                SentenceTransformer=lambda *a, **kw: model
            )}):
                with self.assertRaises(EmbeddingError) as caught:
                    self.adapter().embed("海边")
            self.assertEqual(caught.exception.code, "INVALID_EMBEDDING")
            model.encode.assert_not_called()

    def test_adapter_retries_failed_lazy_load(self):
        model = MagicMock(max_seq_length=512)
        model.get_sentence_embedding_dimension.return_value = 2
        model.encode.return_value = [[1.0, 0.0]]
        constructor = MagicMock(side_effect=[RuntimeError("temporary"), model])
        with patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=constructor)}):
            adapter = self.adapter()
            with self.assertRaises(EmbeddingError):
                adapter.embed("海边")
            self.assertEqual(adapter.embed("海边"), [1.0, 0.0])
        self.assertEqual(constructor.call_count, 2)

    def test_settings_from_env_selects_adapter_without_importing_model(self):
        values = {"MEMORYDREAMING_EMBEDDING_PROVIDER": "sentence-transformers",
                  "MEMORYDREAMING_EMBEDDING_MODEL_PATH": self._tmpdir.name,
                  "MEMORYDREAMING_EMBEDDING_REVISION": "revision-123", "MEMORYDREAMING_EMBEDDING_DIM": "2",
                  "MEMORYDREAMING_VECTOR_MIN_SCORE": "0.57", "MEMORYDREAMING_EMBEDDING_QUERY_PREFIX": "检索："}
        with patch.dict(os.environ, values), patch.dict(sys.modules, {"sentence_transformers": None}):
            settings = Settings.from_env()
            self.assertEqual(settings.vector_min_score, 0.57)
            self.assertIsInstance(embedder_from_settings(settings), SentenceTransformerEmbedder)
            with MemoryRuntime(replace(settings, db_path=self.db_path), clock=self.clock) as runtime:
                self.assertIsNone(runtime.vectors._embedder._model)

    def test_settings_reject_incomplete_or_invalid_embedding_config(self):
        for changes in ({"embedding_provider": "unknown"}, {"embedding_provider": "sentence-transformers"},
                        {"embedding_dim": True}, {"embedding_batch_size": 0},
                        {"vector_min_score": float("nan")}, {"vector_min_score": 1.1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings(**changes)


class VectorSafetyTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.embedder = ToyEmbedder()
        self.index = VectorIndex(self.runtime.db, self.embedder)

    def add_indexed(self):
        memory = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边散步"})
        self.index.index_memory(memory, NOW)
        return memory

    def test_validation_rejects_nonfinite_wrong_dimension_and_non_numeric_vectors(self):
        for vector in ([float("nan"), 0], [float("inf"), 0], [1], [True, 0], ["1", 0], [None, 1]):
            with self.subTest(vector=vector), self.assertRaises(EmbeddingError):
                normalized_vector(vector, 2)
        self.assertEqual(normalized_vector([3, 4], 2), [0.6, 0.8])

    def test_configuration_changes_isolate_persistent_index_not_only_cache(self):
        self.add_indexed()
        other = VectorIndex(self.runtime.db, ToyEmbedder("v2"))
        self.assertNotEqual(other.model_name, self.index.model_name)
        self.assertEqual(VectorIndex(self.runtime.db, ToyEmbedder()).model_name, self.index.model_name)
        result = other.search_with_status(self.scope, "沙滩")
        self.assertEqual(result.hits, [])
        self.assertEqual(result.degraded_reasons, ["VECTOR_INDEX_INCOMPLETE"])
        self.assertEqual(other.rebuild(self.scope), 1)
        self.assertEqual(len(other.search(self.scope, "沙滩")), 1)
        self.assertEqual(self.index.search(self.scope, "沙滩"), [])

    def test_query_prefix_is_part_of_persistent_namespace(self):
        one = SentenceTransformerEmbedder(self._tmpdir.name, revision="v1", dim=2, query_prefix="查询：")
        two = SentenceTransformerEmbedder(self._tmpdir.name, revision="v1", dim=2)
        self.assertNotEqual(VectorIndex(self.runtime.db, one).model_name, VectorIndex(self.runtime.db, two).model_name)
        self.assertNotEqual(VectorIndex(self.runtime.db, HashingEmbedder(128, (2,))).model_name,
                            VectorIndex(self.runtime.db, HashingEmbedder(128, (3,))).model_name)

    def test_corrupt_vectors_do_not_break_valid_hits(self):
        one = self.add_indexed()
        two = self.add_indexed()
        for blob in (b"x", array("f", [float("nan"), 1]).tobytes(), array("f", [1]).tobytes(),
                     array("f", [0, 0]).tobytes()):
            with self.subTest(blob=blob):
                self.repos.vectors.upsert(one.memory_id, self.index.model_name, 2, blob, NOW)
                result = self.index.search_with_status(self.scope, "沙滩")
                self.assertEqual([hit.memory_id for hit in result.hits], [two.memory_id])
                self.assertEqual(result.degraded_reasons, ["VECTOR_INDEX_CORRUPT"])

    def test_content_revision_removes_stale_vector(self):
        memory = self.add_indexed()
        with self.runtime.db.transaction():
            self.repos.memories.update_content(memory.memory_id, {"what_happened": "高山露营"}, NOW)
        self.assertEqual(self.index.count(), 0)
        result = self.index.search_with_status(self.scope, "海边")
        self.assertIn("VECTOR_INDEX_INCOMPLETE", result.degraded_reasons)
        self.assertEqual(result.hits, [])

    def test_zero_semantic_outputs_report_failure_instead_of_false_no_match(self):
        self.add_indexed()
        with patch.object(self.embedder, "embed_query", return_value=[0, 0]):
            with self.assertRaises(EmbeddingError) as caught:
                self.index.search_with_status(self.scope, "沙滩")
        self.assertEqual(caught.exception.code, "INVALID_EMBEDDING")
        with patch.object(self.embedder, "embed_documents", return_value=[[0, 0]]):
            with self.assertRaises(EmbeddingError):
                self.index.rebuild(self.scope)
        self.assertEqual(self.index.count(), 1)

    def test_rebuild_encoding_failure_preserves_committed_index(self):
        self.add_indexed()
        before = [tuple(row) for row in self.runtime.db.connection.execute("SELECT * FROM memory_vectors")]
        with patch.object(self.embedder, "embed_documents", side_effect=RuntimeError("offline")):
            with self.assertRaises(EmbeddingError):
                self.index.rebuild(self.scope)
        self.assertEqual([tuple(row) for row in self.runtime.db.connection.execute("SELECT * FROM memory_vectors")], before)

    def test_rebuild_publish_failure_rolls_back_deletes_and_partial_writes(self):
        self.add_indexed()
        self.add_indexed()
        before = [tuple(row) for row in self.runtime.db.connection.execute("SELECT * FROM memory_vectors ORDER BY memory_id")]
        upsert = self.repos.vectors.upsert
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("disk full")
            return upsert(*args, **kwargs)

        with patch.object(self.repos.vectors, "upsert", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.index.rebuild(self.scope)
        self.assertEqual([tuple(row) for row in self.runtime.db.connection.execute("SELECT * FROM memory_vectors ORDER BY memory_id")], before)

    def test_encoding_holds_no_write_lock_and_detects_other_connection_revision(self):
        memory = self.add_indexed()

        def concurrent_update():
            self.assertFalse(self.runtime.db.connection.in_transaction)
            with closing(Database.open(self.db_path, initialize=False)) as other:
                other.connection.execute("PRAGMA busy_timeout=10")
                with other.transaction():
                    other.repositories.memories.update_content(memory.memory_id, {"what_happened": "修订后的高山露营"}, NOW)

        self.embedder.on_documents = concurrent_update
        with self.assertRaises(SourceRevisionChanged):
            self.index.rebuild(self.scope)
        self.assertEqual(self.index.count(), 0)
        self.assertIn("高山露营", self.repos.memories.get(memory.memory_id).body_text)

    def test_forgetting_during_rebuild_cannot_restore_vector(self):
        memory = self.add_indexed()
        self.embedder.on_documents = lambda: self.runtime.forget(ForgetSelector(memory_ids=[memory.memory_id]))
        with self.assertRaises(SourceRevisionChanged):
            self.index.rebuild(self.scope)
        self.assertEqual(self.count_rows("memory_vectors"), 0)

    def test_outer_transaction_rebuild_rejected_before_model_computation(self):
        with self.runtime.db.transaction(), patch.object(self.embedder, "embed_documents") as encode:
            with self.assertRaises(ValueError):
                self.index.rebuild(self.scope)
            encode.assert_not_called()

    def test_corrupt_or_missing_other_activity_does_not_leak_degradation(self):
        memory = self.add_indexed()
        self.repos.vectors.upsert(memory.memory_id, self.index.model_name, 2, b"bad", NOW)
        other = Scope("different-user", "c1")
        result = self.index.search_with_status(other, "沙滩")
        self.assertEqual(result.hits, [])
        self.assertEqual(result.degraded_reasons, [])
        result = self.index.search_with_status(self.scope, "沙滩", read_filter=ReadFilter(activity_ids=("not-allowed",)))
        self.assertEqual(result.degraded_reasons, [])

    def test_rebuild_does_not_omit_memories_above_candidate_limit(self):
        for i in range(4):
            self.add_memory(MemoryType.EPISODIC, {"what_happened": f"海边散步{i}"})
        self.assertEqual(self.index.rebuild(self.scope), 4)


class SemanticHostFallbackTest(RuntimeTestCase):
    def test_partial_search_retains_keyword_evidence_and_is_not_cached_then_recovers(self):
        self.scope = Scope("u1", "c1", "r1")
        self.runtime.observe(self.make_turn("我喜欢海边故事。"), self.scope, "seed")
        self.runtime.run_consolidation(RuleBasedExtractor())
        embedder = ToyEmbedder()
        with MemoryRuntime(self.runtime.settings, clock=self.clock, embedder=embedder) as runtime:
            runtime.rebuild_vectors(self.scope)
            session = HostAdapter(runtime, clock=self.clock).open_session(SessionBinding("u1", "c1", "r1", "window"))
            args = {"query": "海边故事", "activity_ref": None, "time_range": None, "detail": "evidence",
                    "max_items": 4, "max_tokens": 2048}
            embedder.available = False
            for _ in range(2):
                response = session.call_tool("memory_search", args)
                self.assertEqual(response["status"], "partial", response)
                self.assertFalse(response["data"]["retrieval_complete"])
                self.assertTrue(response["data"]["evidence"])
                self.assertEqual(response["data"]["degraded_reasons"], ["EMBEDDING_UNAVAILABLE"])
                self.assertNotIn("secret", str(response))
            self.assertEqual(embedder.query_calls, 2)
            self.assertEqual(self.count_rows("retrieval_cache"), 0)
            embedder.available = True
            response = session.call_tool("memory_search", args)
            self.assertEqual(response["status"], "ok", response)
            self.assertTrue(response["data"]["retrieval_complete"])
            session.call_tool("memory_search", args)
            self.assertEqual(embedder.query_calls, 3)


if __name__ == "__main__":
    unittest.main()
