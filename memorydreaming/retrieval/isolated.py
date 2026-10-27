"""可终止的持久本地模型进程；超时包含加载、编码和 IPC。"""

import json
import queue
import subprocess
import sys
import threading
import time

from .embedding import EmbeddingError, SentenceTransformerEmbedder


class IsolatedSentenceTransformerEmbedder(SentenceTransformerEmbedder):
    def __init__(self, *args, timeout_seconds=30, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout_seconds
        self._process = None

    def _command(self):
        return [sys.executable, "-X", "utf8", "-m", "memorydreaming.embedding_worker"]

    def _start(self):
        self._process = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self._process

    def _stop(self, process):
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        if self._process is process:
            self._process = None

    def close(self):
        # 可从宿主卸载线程终止编码，不等待模型自己的锁。
        self._stop(self._process)

    def _encode(self, texts, prefix):
        if not texts:
            return []
        request = json.dumps({"config": {"model_path": str(self._path), "revision": self._revision, "dim": self.dim,
            "query_prefix": self._query_prefix, "document_prefix": self._document_prefix,
            "max_seq_length": self._max_seq_length, "batch_size": self._batch_size},
            "texts": texts, "prefix": prefix}, ensure_ascii=False)+"\n"
        if len(request.encode("utf-8")) > 8*1024*1024:
            raise EmbeddingError("EMBEDDING_INPUT_TOO_LARGE")
        deadline = time.monotonic()+self._timeout
        if not self._lock.acquire(timeout=self._timeout):
            raise EmbeddingError("EMBEDDING_TIMEOUT")
        try:
            process = self._process
            if process is None or process.poll() is not None:
                process = self._start()
            result = queue.Queue(maxsize=1)
            def exchange():
                try:
                    process.stdin.write(request)
                    process.stdin.flush()
                    line = process.stdout.readline(16*1024*1024+1)
                    if len(line) > 16*1024*1024 or not line.endswith("\n"):
                        raise ValueError()
                    result.put(json.loads(line))
                except Exception:
                    result.put(None)
            worker = threading.Thread(target=exchange, name="memory-embedding-ipc", daemon=True)
            worker.start()
            try:
                response = result.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                self._stop(process)
                worker.join(timeout=2)
                raise EmbeddingError("EMBEDDING_TIMEOUT") from None
            if not isinstance(response, dict) or not response.get("ok"):
                self._stop(process)
                raise EmbeddingError()
            rows = response.get("vectors")
            if not isinstance(rows, list) or len(rows) != len(texts):
                self._stop(process)
                raise EmbeddingError("INVALID_EMBEDDING")
            from .embedding import normalized_vector
            return [normalized_vector(row, self.dim) for row in rows]
        except EmbeddingError:
            raise
        except Exception:
            self._stop(self._process)
            raise EmbeddingError() from None
        finally:
            self._lock.release()
