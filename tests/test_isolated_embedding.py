"""真正子进程挂起时可强制结束并在下一次编码恢复。"""

import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch

from memorydreaming.retrieval.embedding import EmbeddingError
from memorydreaming.retrieval.isolated import IsolatedSentenceTransformerEmbedder
from memorydreaming.retrieval.vector import VectorIndex
from memorydreaming.models import MemoryType
from tests.helpers import NOW, RuntimeTestCase


class IsolatedEmbeddingTest(RuntimeTestCase):
    def adapter(self, timeout_seconds=3, **changes):
        adapter = IsolatedSentenceTransformerEmbedder(self._tmpdir.name, revision="local", dim=2,
                                                     timeout_seconds=timeout_seconds, **changes)
        self.addCleanup(adapter.close)
        return adapter

    def test_timeout_kills_process_and_next_call_recovers(self):
        adapter = self.adapter(timeout_seconds=.3)
        hang = [sys.executable, "-c", "import time; time.sleep(60)"]
        echo = [sys.executable, "-u", "-c", "import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'ok':True,'vectors':[[1,0] for x in r['texts']]}),flush=True)"]
        with patch.object(adapter, "_command", side_effect=[hang, echo]):
            started = time.monotonic()
            with self.assertRaises(EmbeddingError) as caught:
                adapter.embed_query("不能挂死")
            self.assertEqual(caught.exception.code, "EMBEDDING_TIMEOUT")
            self.assertLess(time.monotonic()-started, 3)
            self.assertIsNone(adapter._process)
            # The hang deadline above is deliberately tight. Recovery checks
            # a new process, whose startup can take longer on a busy CI host.
            adapter._timeout = 3
            self.assertEqual(adapter.embed_query("恢复"), [1, 0])
            process = adapter._process
            adapter.close()
            self.assertIsNotNone(process.poll())

    def test_corrupt_child_reply_is_rejected(self):
        adapter = self.adapter()
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", "import sys;sys.stdin.readline();print('invalid')"]):
            with self.assertRaises(EmbeddingError) as caught:
                adapter.embed_query("坏输出")
        self.assertEqual(caught.exception.code, "EMBEDDING_UNAVAILABLE")
        self.assertIsNone(adapter._process)

    def test_only_allowlisted_child_error_codes_are_returned_to_host(self):
        for code in ("INVALID_EMBEDDING", "EMBEDDING_UNAVAILABLE", "private model path", None):
            adapter = self.adapter()
            payload = json.dumps({"ok": False, "code": code})
            child = "import sys;sys.stdin.readline();print(" + repr(payload) + ",flush=True)"
            with self.subTest(code=code), patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]):
                with self.assertRaises(EmbeddingError) as caught:
                    adapter.embed_query("private text")
                expected = "INVALID_EMBEDDING" if code == "INVALID_EMBEDDING" else "EMBEDDING_UNAVAILABLE"
                self.assertEqual(str(caught.exception), expected)
                self.assertIsNone(adapter._process)

    def test_matryoshka_policy_and_native_dimension_reach_persistent_child(self):
        adapter = self.adapter(matryoshka=True, expected_model_dim=4, model_profile="qwen3")
        # A real JSONL child verifies the parent's configuration instead of
        # mocking _encode; its vectors already represent the projected output.
        child = ("import sys,json\nfor line in sys.stdin:\n"
                 " r=json.loads(line);c=r['config'];assert c['matryoshka'] is True;"
                 "assert c['expected_model_dim']==4;assert c['dim']==2;assert c['model_profile']=='qwen3';"
                 "print(json.dumps({'ok':True,'vectors':[[3,4] for _ in r['texts']]}),flush=True)")
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]):
            self.assertEqual(adapter.embed_query("海边"), [0.6, 0.8])
            process = adapter._process
            self.assertEqual(adapter.embed_documents(["沙滩", "日落"]), [[0.6, 0.8]] * 2)
            self.assertIs(adapter._process, process)

    def test_production_worker_applies_projection_before_sending_reply(self):
        fake_path = Path(self._tmpdir.name) / "fake-local-dependency"
        fake_path.mkdir()
        (fake_path / "sentence_transformers.py").write_text(
            "from types import SimpleNamespace as NS\n"
            "class SentenceTransformer:\n"
            " def __init__(self,*a,**kw):\n"
            "  assert kw=={'device':'cpu','local_files_only':True,'trust_remote_code':False}\n"
            "  self.max_seq_length=512\n"
            " def __getitem__(self,index):\n"
            "  return [NS(auto_model=NS(config=NS(model_type='qwen3'))),"
            "NS(pooling_mode_lasttoken=True,include_prompt=True)][index]\n"
            " def get_sentence_embedding_dimension(self):return 4\n"
            " def encode(self,texts,**kw):\n"
            "  assert kw['prompt']==''\n"
            "  return [[3,4,12,0] for _ in texts]\n", encoding="utf-8")
        adapter = self.adapter(matryoshka=True, expected_model_dim=4, model_profile="qwen3")
        # Keep the production worker and actual subprocess boundary; substitute
        # only the optional model library so no weights or network are needed.
        child = ("import sys;sys.path.insert(0," + repr(str(fake_path)) + ");"
                 "from memorydreaming.embedding_worker import main;raise SystemExit(main())")
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]):
            self.assertEqual(adapter.embed_query("海边"), [0.6, 0.8])
            self.assertEqual(adapter.embed_documents(["沙滩", "灯塔"]), [[0.6, 0.8]] * 2)

    def test_default_child_configuration_stays_compatible(self):
        adapter = self.adapter()
        child = ("import sys,json\nr=json.loads(sys.stdin.readline());c=r['config'];"
                 "assert 'matryoshka' not in c;assert 'expected_model_dim' not in c;assert 'model_profile' not in c;"
                 "print(json.dumps({'ok':True,'vectors':[[1,0]]}),flush=True)")
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]):
            self.assertEqual(adapter.embed_query("海边"), [1, 0])

    def test_invalid_projected_child_output_stops_process(self):
        for row in ([0, 0], [1, float("nan")], [1, 0, 0]):
            adapter = self.adapter(matryoshka=True, expected_model_dim=4)
            payload = json.dumps({"ok": True, "vectors": [row]})
            child = "import sys;sys.stdin.readline();print("+repr(payload)+",flush=True)"
            with self.subTest(row=row), patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]):
                with self.assertRaises(EmbeddingError) as caught:
                    adapter.embed_query("bad output")
                self.assertEqual(caught.exception.code, "INVALID_EMBEDDING")
                self.assertIsNone(adapter._process)

    def test_empty_documents_do_not_start_a_worker(self):
        adapter = self.adapter()
        with patch.object(adapter, "_start") as start:
            self.assertEqual(adapter.embed_documents([]), [])
        start.assert_not_called()

    def test_bulk_documents_keep_order_and_reuse_worker_across_bounded_batches(self):
        adapter = self.adapter(batch_size=1024)
        child = ("import sys,json\nfor line in sys.stdin:\n"
                 " r=json.loads(line);assert len(r['texts'])<=32;"
                 "print(json.dumps({'ok':True,'vectors':[[int(x)+1,1] for x in r['texts']]}),flush=True)")
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]), patch.object(
            adapter, "_start", wraps=adapter._start
        ) as start:
            rows = adapter.embed_documents([str(index) for index in range(70)])
        self.assertEqual(len(rows), 70)
        for index, row in enumerate(rows):
            self.assertAlmostEqual(row[0], (index + 1) / math.hypot(index + 1, 1))
            self.assertAlmostEqual(row[1], 1 / math.hypot(index + 1, 1))
        start.assert_called_once()

    def test_response_dimension_budget_and_model_batch_size_both_limit_ipc(self):
        for dimension, model_batch_size, expected_limit in ((2, 2, 2), (65536, 1024, 7)):
            adapter = IsolatedSentenceTransformerEmbedder(
                self._tmpdir.name, revision="local", dim=dimension, batch_size=model_batch_size)
            self.addCleanup(adapter.close)
            seen = []

            def encode(batch, prefix):
                seen.append(list(batch))
                return [[1, 0]] * len(batch)

            with self.subTest(dimension=dimension), patch.object(adapter, "_encode", side_effect=encode):
                self.assertEqual(len(adapter.embed_documents(["text"] * 19)), 19)
            self.assertTrue(all(len(batch) <= expected_limit for batch in seen))
            self.assertGreater(len(seen), 1)

    def test_input_byte_chunks_include_json_escaping_unicode_and_prefix(self):
        adapter = self.adapter(batch_size=1024, document_prefix="资料：\n")
        texts = ['海"\n边' + str(index) for index in range(5)]
        envelope = len(adapter._request([], adapter._document_prefix).encode("utf-8"))
        one = len(json.dumps(texts[0], ensure_ascii=False).encode("utf-8"))
        seen = []

        def encode(batch, prefix):
            request_bytes = len(adapter._request(batch, prefix).encode("utf-8"))
            self.assertLessEqual(request_bytes, adapter._MAX_REQUEST_BYTES)
            seen.append(list(batch))
            return [[1, 0]] * len(batch)

        with patch.object(adapter, "_MAX_REQUEST_BYTES", envelope + one * 2 + 2), patch.object(
            adapter, "_encode", side_effect=encode
        ):
            self.assertEqual(adapter.embed_documents(texts), [[1, 0]] * 5)
        self.assertEqual(seen, [texts[:2], texts[2:4], texts[4:]])

    def test_oversized_single_document_or_query_fails_without_spawning(self):
        adapter = self.adapter()
        envelope = len(adapter._request([], "").encode("utf-8"))
        with patch.object(adapter, "_MAX_REQUEST_BYTES", envelope + 10), patch.object(adapter, "_start") as start:
            for operation in (adapter.embed_documents, lambda texts: adapter.embed_query(texts[0])):
                with self.subTest(operation=operation), self.assertRaises(EmbeddingError) as caught:
                    operation(["海" * 20])
                self.assertEqual(caught.exception.code, "EMBEDDING_INPUT_TOO_LARGE")
        start.assert_not_called()

    def test_later_ipc_batch_failure_preserves_entire_previous_index(self):
        adapter = self.adapter(batch_size=2)
        index = VectorIndex(self.runtime.db, adapter)
        for number in range(5):
            self.add_memory(MemoryType.EPISODIC, {"what_happened": f"海边散步{number}"})
        child = ("import sys,json\nfor line in sys.stdin:\n"
                 " r=json.loads(line);print(json.dumps({'ok':True,'vectors':[[1,0] for _ in r['texts']]}),flush=True)")
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", child]):
            self.assertEqual(index.rebuild(self.scope, NOW), 5)
        previous = [tuple(row) for row in self.runtime.db.connection.execute("SELECT * FROM memory_vectors ORDER BY memory_id")]
        adapter.close()
        fail_later = ("import sys,json\ncount=0\nfor line in sys.stdin:\n"
                      " r=json.loads(line);count+=1;"
                      "print(json.dumps({'ok':True,'vectors':[[0,1] for _ in r['texts']]} if count==1 "
                      "else {'ok':False,'code':'INVALID_EMBEDDING'}),flush=True)")
        with patch.object(adapter, "_command", return_value=[sys.executable, "-u", "-c", fail_later]):
            with self.assertRaises(EmbeddingError) as caught:
                index.rebuild(self.scope, NOW)
        self.assertEqual(caught.exception.code, "INVALID_EMBEDDING")
        self.assertIsNone(adapter._process)
        self.assertEqual([tuple(row) for row in self.runtime.db.connection.execute(
            "SELECT * FROM memory_vectors ORDER BY memory_id")], previous)
