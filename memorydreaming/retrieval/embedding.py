"""嵌入模型：把一段文本变成向量，供语义通道检索。

为什么先做一个「哈希嵌入」而不是直接接模型？

1. 零依赖。整个项目到现在没有第三方包，这一点对新手练习很重要：
   `python -m memorydreaming demo` 不需要 key、不需要下载模型。
2. 先把管道修通。存储、增量更新、融合、评估、重建这些环节，
   和「用的是哪个模型」无关。管道通了，换模型只是替换一个类。
3. 它的能力边界很清楚——只捕捉字面重叠，不懂语义。
   所以「海边」不会靠近「沙滩」。这不是 bug，是这个占位实现的已知极限，
   写在这里免得你误以为检索效果就该这样。

换成真实模型时，实现同样的 `Embedder` 协议即可：

    class OpenAIEmbedder:
        name = "text-embedding-3-small"
        dim = 1536
        def embed(self, text: str) -> list[float]:
            ...

然后把实例传给 `MemoryRuntime(embedder=...)`，其余代码一行都不用改。
"""

from __future__ import annotations

import math
import hashlib
from pathlib import Path
from threading import RLock
import zlib
from collections import Counter
from typing import Protocol

from .keyword import text_shingles


class EmbeddingError(Exception):
    """可报告的向量通道失败；消息不得包含模型路径或待编码正文。"""

    def __init__(self, code: str = "EMBEDDING_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


def normalized_vector(values, dim: int) -> list[float]:
    """统一验证和归一化，避免 NaN、错维度或巨大数破坏排序。"""
    try:
        values = list(values)
        if len(values) != dim or any(isinstance(v, (bool, str, bytes)) for v in values):
            raise ValueError
        vector = [float(v) for v in values]
        if not all(math.isfinite(v) for v in vector):
            raise ValueError
        norm = math.hypot(*vector)
        if not math.isfinite(norm):
            raise ValueError
        return [v / norm for v in vector] if norm else vector
    except (ValueError, TypeError, OverflowError) as exc:
        raise EmbeddingError("INVALID_EMBEDDING") from exc


class Embedder(Protocol):
    """嵌入模型协议。只有三样东西：名字、维度、编码方法。

    name 会写进 memory_vectors.model 列：换模型后旧向量不会被误用，
    重建索引时也可以按模型过滤。
    """

    name: str
    dim: int

    def embed(self, text: str) -> list[float]:
        """把文本编码成定长向量。返回的向量必须已归一化（模长为 1），
        这样检索时点积就等于余弦相似度，省一次开方。"""
        ...


class HashingEmbedder:
    """字符 n-gram 哈希嵌入（零依赖占位实现）。

    做法：
      1. 用 text_shingles 取出所有二字词、三字词；
      2. 对每个词算 crc32 哈希，映射到 [0, dim) 的某一维；
      3. 用哈希的另一位决定加还是减（signed hashing）。
         这一步能显著降低「不同词撞到同一维」造成的偏差；
      4. 按词频做次线性加权（1 + log(tf)），最后整体归一化。

    举个能直观感受的例子：
      「周末去海边」和「海边去周末」字面几乎一样 → 相似度很高。
      「周末去海边」和「周末去登山」只共享「周末」→ 相似度中等。
      「周末去海边」和「今天加班」几乎不共享 → 相似度接近 0。

    这正是「字形相似」，不是「语义相似」。真要做语义，换模型。
    """

    def __init__(self, dim: int = 1024, ngram_sizes: tuple[int, ...] = (2, 3)) -> None:
        # 维度不是越大越好，但太小会很糟：实测 256 维时，
        # 「今天天气怎么样」也能和新记忆撞出 0.1 的分（纯碰撞噪声），
        # 1024 维下这类无关文本直接是 0.0。代价是每条记忆 4KB。
        if dim < 32:
            raise ValueError("dim 太小会让哈希碰撞严重，建议至少 128")
        self.dim = dim
        self._sizes = ngram_sizes
        self.name = f"hash-ngram-{dim}"

    @property
    def cache_signature(self) -> dict:
        return {"ngram_sizes": self._sizes, "algorithm": "crc32-signed-sublinear-v1"}

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim

        # 用 Counter 而不是 set：重复出现的词应该权重更高
        counts = Counter(text_shingles(text, self._sizes))
        if not counts:
            return vector

        for gram, frequency in counts.items():
            hashed = zlib.crc32(gram.encode("utf-8"))
            index = hashed % self.dim
            sign = 1.0 if (hashed >> 16) & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(frequency))

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:  # 理论上不会发生，除非所有词都恰好互相抵消
            return vector
        return [value / norm for value in vector]


class SentenceTransformerEmbedder:
    """显式本地权重、CPU、懒加载的可选语义模型，不在会话启动时下载。

    revision 是部署者提供的不可变权重版本；替换本地权重必须更新它。
    同一实例可由宿主和后台整理器共享，模型加载与 encode 由锁串行保护。
    """

    name = "sentence-transformers-local"

    def __init__(self, model_path: str | Path, *, revision: str, dim: int,
                 query_prefix: str = "", document_prefix: str = "",
                 max_seq_length: int = 512, batch_size: int = 16) -> None:
        if not str(model_path).strip() or not isinstance(revision, str) or not revision.strip():
            raise ValueError("本地模型路径和不可变 revision 必须显式提供")
        for name, value, maximum in (("dim", dim, 65536), ("max_seq_length", max_seq_length, 32768),
                                     ("batch_size", batch_size, 1024)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} 超出有效范围")
        if any(not isinstance(prefix, str) or len(prefix) > 2000 for prefix in (query_prefix, document_prefix)):
            raise ValueError("嵌入前缀必须为不超过 2000 字符的字符串")
        self.dim = dim
        self._path = Path(model_path).expanduser().resolve()
        self._revision = revision
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._max_seq_length = max_seq_length
        self._batch_size = batch_size
        self._model = None
        self._lock = RLock()

    @property
    def cache_signature(self) -> dict:
        return {"adapter": "sentence-transformers-local-v1", "revision": self._revision,
                "location_sha256": hashlib.sha256(str(self._path).encode("utf-8")).hexdigest(),
                "query_prefix": self._query_prefix, "document_prefix": self._document_prefix,
                "max_seq_length": self._max_seq_length, "normalization": "l2-v1", "device": "cpu"}

    def _load(self):
        if self._model is None:
            if not self._path.is_dir():
                raise EmbeddingError()
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(str(self._path), device="cpu", local_files_only=True,
                                        trust_remote_code=False)
            if model.get_sentence_embedding_dimension() != self.dim:
                raise EmbeddingError("INVALID_EMBEDDING")
            if model.max_seq_length < self._max_seq_length:
                # 不隐式扩展模型的位置编码或截断配置。
                raise EmbeddingError("INVALID_EMBEDDING")
            model.max_seq_length = self._max_seq_length
            self._model = model
        return self._model

    def _encode(self, texts: list[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        try:
            with self._lock:
                model = self._load()
                rows = model.encode([prefix + text for text in texts], batch_size=self._batch_size,
                                    normalize_embeddings=True, convert_to_numpy=True,
                                    show_progress_bar=False, prompt="")
                if len(rows) != len(texts):
                    raise EmbeddingError("INVALID_EMBEDDING")
                return [normalized_vector(row, self.dim) for row in rows]
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError() from exc

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text], self._query_prefix)[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, self._document_prefix)

    def embed(self, text: str) -> list[float]:
        """兼容旧接口，单条正文编码；查询应使用 embed_query。"""
        return self.embed_documents([text])[0]


def embedder_from_settings(settings) -> Embedder:
    if settings.embedding_provider == "hash":
        return HashingEmbedder()
    return SentenceTransformerEmbedder(
        settings.embedding_model_path, revision=settings.embedding_revision, dim=settings.embedding_dim,
        query_prefix=settings.embedding_query_prefix, document_prefix=settings.embedding_document_prefix,
        max_seq_length=settings.embedding_max_seq_length, batch_size=settings.embedding_batch_size,
    )
