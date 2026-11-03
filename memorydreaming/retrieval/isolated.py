"""可终止的持久本地模型进程；超时包含加载、编码和 IPC。"""

import json
import queue
import subprocess
import sys
import threading
import time

from .embedding import EmbeddingError, SentenceTransformerEmbedder


class IsolatedSentenceTransformerEmbedder(SentenceTransformerEmbedder):
    _MAX_REQUEST_BYTES = 8 * 1024 * 1024
    _MAX_RESPONSE_CHARS = 16 * 1024 * 1024

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

    def _request(self, texts, prefix):
        config = {"model_path": str(self._path), "revision": self._revision, "dim": self.dim,
            "query_prefix": self._query_prefix, "document_prefix": self._document_prefix,
            "max_seq_length": self._max_seq_length, "batch_size": self._batch_size}
        if self._matryoshka:
            config["matryoshka"] = True
        if self._expected_model_dim is not None:
            config["expected_model_dim"] = self._expected_model_dim
        if self._model_profile != "generic":
            config["model_profile"] = self._model_profile
        return json.dumps({"config": config,
            "texts": texts, "prefix": prefix}, ensure_ascii=False)+"\n"

    def embed_documents(self, texts):
        """有界 IPC 批次；每批单独受加载、编码和传输的硬截止时间约束。

        模型内部 batch_size 不限制整条 JSONL 响应大小，因此必须在进程
        边界再次分批。这里只返回全部计算结果，不分批写数据库；后续批次
        失败时，VectorIndex 仍能保留原投影并拒绝发布半成品。
        """
        if not texts:
            return []
        # 归一化后的有限 float JSON 表示预留每维 32 字节，另留协议开销。
        # 固定最多 32 行避免小维度时单条请求仍包含过多长文档。
        row_budget = self.dim * 32 + 4
        limit = min(self._batch_size, 32, max(1, (self._MAX_RESPONSE_CHARS - 1024) // row_budget))
        envelope_bytes = len(self._request([], self._document_prefix).encode("utf-8"))
        vectors, batch = [], []
        batch_bytes = envelope_bytes
        for text in texts:
            # 精确计算 UTF-8 和 JSON 转义后的输入大小，包括查询/正文前缀、
            # 配置及分隔符；不能只按字符数估算中文或包含控制字符的正文。
            text_bytes = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
            if envelope_bytes + text_bytes > self._MAX_REQUEST_BYTES:
                raise EmbeddingError("EMBEDDING_INPUT_TOO_LARGE")
            addition = text_bytes + (2 if batch else 0)
            if batch and (len(batch) >= limit or batch_bytes + addition > self._MAX_REQUEST_BYTES):
                vectors.extend(self._encode(batch, self._document_prefix))
                batch, batch_bytes = [], envelope_bytes
                addition = text_bytes
            batch.append(text)
            batch_bytes += addition
        if batch:
            vectors.extend(self._encode(batch, self._document_prefix))
        return vectors

    def _encode(self, texts, prefix):
        if not texts:
            return []
        request = self._request(texts, prefix)
        if len(request.encode("utf-8")) > self._MAX_REQUEST_BYTES:
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
                    line = process.stdout.readline(self._MAX_RESPONSE_CHARS+1)
                    if len(line) > self._MAX_RESPONSE_CHARS or not line.endswith("\n"):
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
                code = "INVALID_EMBEDDING" if isinstance(response, dict) and response.get("code") == "INVALID_EMBEDDING" else "EMBEDDING_UNAVAILABLE"
                raise EmbeddingError(code)
            rows = response.get("vectors")
            if not isinstance(rows, list) or len(rows) != len(texts):
                self._stop(process)
                raise EmbeddingError("INVALID_EMBEDDING")
            from .embedding import normalized_vector
            try:
                vectors = [normalized_vector(row, self.dim) for row in rows]
                if self._matryoshka and any(not any(vector) for vector in vectors):
                    raise EmbeddingError("INVALID_EMBEDDING")
                return vectors
            except EmbeddingError:
                self._stop(process)
                raise
        except EmbeddingError:
            raise
        except Exception:
            self._stop(self._process)
            raise EmbeddingError() from None
        finally:
            self._lock.release()
