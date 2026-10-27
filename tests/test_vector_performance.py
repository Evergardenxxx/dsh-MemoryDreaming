"""Exact scan optimization: numeric equivalence, bounded main-row reads and live-source checks."""

from array import array
from dataclasses import replace
import random
from unittest.mock import patch

from memorydreaming.models import ForgetSelector, MemoryStatus, MemoryType, ReadFilter, Scope, new_id
from memorydreaming.retrieval.embedding import normalized_vector
from memorydreaming.retrieval.vector import VectorIndex
from tests.helpers import RuntimeTestCase


class QueryEmbedder:
    name = "vector-equivalence"
    dim = 8

    def __init__(self, query):
        self.query = query

    def embed(self, text):
        return list(self.query)


class VectorPerformanceTest(RuntimeTestCase):
    def item(self, index, indexer, vector):
        item = self.add_memory(MemoryType.EPISODIC, {"what_happened": f"source-{index}"})
        self.repos.vectors.upsert(item.memory_id, indexer.model_name, indexer._dim,
                                 array("f", vector).tobytes(), self.clock.now())
        return item

    def test_sparse_and_dense_dot_products_match_full_normalization_reference(self):
        rng = random.Random(15497)
        for query in ([0, 0, 1, 0, 0, 0, -2, 0], [rng.uniform(-2, 2) for _ in range(8)]):
            with self.subTest(query=query):
                index = VectorIndex(self.runtime.db, QueryEmbedder(query))
                for n in range(200):
                    self.item(n, index, [rng.uniform(-5, 5) for _ in range(8)])
                normalized_query = normalized_vector(query, 8)
                expected = []
                for key, kind, blob in self.repos.vectors.load_scope(self.scope, index.model_name):
                    values = array("f")
                    values.frombytes(blob)
                    score = max(-1., min(1., sum(q * v for q, v in zip(normalized_query, normalized_vector(values, 8)))))
                    if score >= -.3:
                        expected.append((key, score))
                expected.sort(key=lambda pair: (-pair[1], pair[0]))
                actual = index.search_with_status(self.scope, "query", limit=17, min_score=-.3)
                self.assertEqual([hit.memory_id for hit in actual.hits], [key for key, score in expected[:17]])
                for hit, (_, score) in zip(actual.hits, expected):
                    self.assertEqual(hit.score, score)
                self.assertEqual(actual.degraded_reasons, [])

    def test_high_scoring_suppressed_records_do_not_hide_lower_live_results_across_pages(self):
        index = VectorIndex(self.runtime.db, QueryEmbedder([1, 0, 0, 0, 0, 0, 0, 0]))
        blocked = [self.item(n, index, [1, 0, 0, 0, 0, 0, 0, 0]) for n in range(260)]
        valid = self.item("live", index, [1, 1, 0, 0, 0, 0, 0, 0])
        self.runtime.forget_fact(ForgetSelector(memory_ids=[item.memory_id for item in blocked]))
        # Simulate an obsolete tool improperly restoring deleted rows/vectors.
        # Final ledger checks must still skip all 260 before reaching the live row.
        with self.runtime.db.transaction():
            for item in blocked:
                self.runtime.db.connection.execute("UPDATE memories SET status='active' WHERE memory_id=?", (item.memory_id,))
                self.repos.vectors.upsert(item.memory_id, index.model_name, 8,
                    array("f", [1, 0, 0, 0, 0, 0, 0, 0]).tobytes(), self.clock.now())
        hits = index.search(self.scope, "query", limit=1, min_score=0)
        self.assertEqual([hit.memory_id for hit in hits], [valid.memory_id])

    def test_top_result_main_row_fetches_are_batched_instead_of_n_plus_one(self):
        index = VectorIndex(self.runtime.db, QueryEmbedder([1, 0, 0, 0, 0, 0, 0, 0]))
        for n in range(300):
            self.item(n, index, [1, n/1000, 0, 0, 0, 0, 0, 0])
        with patch.object(self.repos.memories, "get", wraps=self.repos.memories.get) as singles, patch.object(
                self.repos.memories, "fetch_many", wraps=self.repos.memories.fetch_many) as batches:
            result = index.search_with_status(self.scope, "query", limit=5, min_score=0)
        self.assertEqual(len(result.hits), 5)
        self.assertEqual(singles.call_count, 0)
        self.assertEqual(batches.call_count, 1)

    def test_corrupt_hidden_rows_do_not_mark_live_channel_corrupt(self):
        index = VectorIndex(self.runtime.db, QueryEmbedder([1, 0, 0, 0, 0, 0, 0, 0]))
        hidden = self.item("hidden", index, [1] * 8)
        live = self.item("live", index, [1] * 8)
        self.runtime.forget_fact(ForgetSelector(memory_ids=[hidden.memory_id]))
        self.runtime.db.connection.execute("UPDATE memories SET status='active' WHERE memory_id=?", (hidden.memory_id,))
        self.repos.vectors.upsert(hidden.memory_id, index.model_name, 8, b"invalid", self.clock.now())
        result = index.search_with_status(self.scope, "query", min_score=0)
        self.assertEqual([hit.memory_id for hit in result.hits], [live.memory_id])
        self.assertEqual(result.degraded_reasons, [])
        self.repos.vectors.upsert(live.memory_id, index.model_name, 8, array("f", [float("inf")] * 8).tobytes(), self.clock.now())
        self.assertEqual(index.search_with_status(self.scope, "query").degraded_reasons, ["VECTOR_INDEX_CORRUPT"])

    def test_version_changed_evidence_is_not_skipped_by_score_first_scan(self):
        index = VectorIndex(self.runtime.db, QueryEmbedder([1] * 8))
        parent = self.item("parent", index, [1] * 8)
        derived = self.item("derived", index, [1] * 8)
        evidence = self.runtime.evidence_ref("memory", parent.memory_id, self.scope)
        self.runtime.set_evidence_groups(derived.memory_id, self.scope, [[evidence]], expected_version=0, reason="source")
        with self.runtime.db.transaction():
            self.repos.memories.update_content(parent.memory_id, {"what_happened": "corrected"}, self.clock.now())
        result = index.search_with_status(self.scope, "query", min_score=-1)
        self.assertNotIn(derived.memory_id, [hit.memory_id for hit in result.hits])

    def test_lower_level_scan_uses_one_read_snapshot_and_keeps_scope_filters(self):
        index = VectorIndex(self.runtime.db, QueryEmbedder([1] * 8))
        live = self.item("live", index, [1] * 8)
        foreign = replace(live, memory_id=new_id("mem"), scope=Scope("other", "c1"))
        with self.runtime.db.transaction():
            self.repos.memories.insert(foreign)
            self.repos.vectors.upsert(foreign.memory_id, index.model_name, 8, array("f", [1] * 8).tobytes(), self.clock.now())
        original = self.repos.vectors.load_scope
        def check(*args, **kwargs):
            self.assertTrue(self.runtime.db.connection.in_transaction)
            self.assertEqual(self.runtime.db.connection.execute("PRAGMA query_only").fetchone()[0], 1)
            return original(*args, **kwargs)
        with patch.object(self.repos.vectors, "load_scope", side_effect=check):
            result = index.search_with_status(self.scope, "query", read_filter=ReadFilter(activity_ids=(None,)))
        self.assertEqual([hit.memory_id for hit in result.hits], [live.memory_id])
        self.assertFalse(self.runtime.db.connection.in_transaction)
        self.assertEqual(self.runtime.db.connection.execute("PRAGMA query_only").fetchone()[0], 0)
