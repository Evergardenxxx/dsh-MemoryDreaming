"""真正子进程挂起时可强制结束并在下一次编码恢复。"""

import json
import sys
import time
from unittest.mock import patch

from memorydreaming.retrieval.embedding import EmbeddingError
from memorydreaming.retrieval.isolated import IsolatedSentenceTransformerEmbedder
from tests.helpers import RuntimeTestCase


class IsolatedEmbeddingTest(RuntimeTestCase):
    def adapter(self, timeout_seconds=3):
        adapter = IsolatedSentenceTransformerEmbedder(self._tmpdir.name, revision="local", dim=2, timeout_seconds=timeout_seconds)
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
