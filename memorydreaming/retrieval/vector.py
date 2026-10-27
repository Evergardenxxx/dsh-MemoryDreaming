"""作用域内的向量派生索引；配置隔离、损坏回退与原子重建。"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Sequence

from ..clock import utcnow
from ..errors import SourceRevisionChanged
from ..models import LinkRelation, MemoryItem, MemoryStatus, MemoryType, ReadFilter, Scope
from ..storage.database import Database
from ..storage.repository import scope_sql
from .embedding import Embedder, EmbeddingError, HashingEmbedder, normalized_vector


@dataclass
class VectorHit:
    memory_id: str
    memory_type: MemoryType
    score: float


@dataclass
class VectorSearchResult:
    hits: list[VectorHit] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedVectors:
    """单次操作的内存计算结果；禁止跨正文或跨模型复用，不持久保存。"""
    namespace: str
    entries: Mapping[str, tuple[str, tuple[float, ...]]]


class VectorIndex:
    """单投影、精确扫描；更换模型配置后显式重建，不混用向量空间。"""

    def __init__(self, db: Database, embedder: Embedder | None = None) -> None:
        self._db = db
        self._repos = db.repositories
        self._embedder = embedder if embedder is not None else HashingEmbedder()
        self._dim = self._embedder.dim
        if isinstance(self._dim, bool) or not isinstance(self._dim, int) or not 1 <= self._dim <= 65536:
            raise ValueError("嵌入维度超出范围")
        signature = {"model": self._embedder.name, "dim": self._dim,
                     "config": getattr(self._embedder, "cache_signature", None),
                     "projection": "memory-render-400-l2-float32-v1"}
        self._signature = json.loads(json.dumps(signature, ensure_ascii=False, sort_keys=True, allow_nan=False))
        digest = hashlib.sha256(json.dumps(self._signature, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        self._model_name = f"{self._embedder.name}:{digest}"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def cache_signature(self) -> dict:
        return {"namespace": self._model_name}

    def _encode_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            encode = getattr(self._embedder, "embed_documents", None)
            rows = encode(texts) if encode is not None else [self._embedder.embed(text) for text in texts]
            if len(rows) != len(texts):
                raise EmbeddingError("INVALID_EMBEDDING")
            vectors = [normalized_vector(row, self._dim) for row in rows]
            if not isinstance(self._embedder, HashingEmbedder) and any(not any(row) for row in vectors):
                raise EmbeddingError("INVALID_EMBEDDING")
            return vectors
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError() from exc

    def _encode_query(self, text: str) -> list[float]:
        try:
            encode = getattr(self._embedder, "embed_query", self._embedder.embed)
            vector = normalized_vector(encode(text), self._dim)
            if not any(vector) and not isinstance(self._embedder, HashingEmbedder):
                raise EmbeddingError("INVALID_EMBEDDING")
            return vector
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError() from exc

    def _eligible(self, memory: MemoryItem) -> bool:
        return memory.status in (MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED) and not (
            self._repos.forgetting.is_suppressed(memory) or (
                memory.status is MemoryStatus.SUPERSEDED
                and self._repos.links.has_relation(memory.memory_id, LinkRelation.PART_OF)
            )
        )

    def _store(self, memory_id: str, vector: Sequence[float], now: datetime) -> bool:
        if not any(vector):
            self.delete(memory_id)
            return False
        self._repos.vectors.upsert(memory_id, self.model_name, self._dim, array("f", vector).tobytes(), now)
        return True

    def prepare_memories(self, memories: list[MemoryItem]) -> PreparedVectors:
        """耗时编码必须在调用者写事务之外完成。"""
        if self._db.connection.in_transaction:
            raise ValueError("向量预计算必须在外部事务之外调用")
        eligible = [m for m in memories if self._eligible(m) and m.render(400).strip()]
        texts = [m.render(400).strip() for m in eligible]
        vectors = self._encode_documents(texts) if texts else []
        entries = {m.memory_id: (text, tuple(vector)) for m, text, vector in zip(eligible, texts, vectors)}
        return PreparedVectors(self.model_name, MappingProxyType(entries))

    def index_memory(self, memory: MemoryItem, now: datetime | None = None,
                     *, prepared: PreparedVectors | None = None) -> bool:
        if not self._eligible(memory):
            self.delete(memory.memory_id)
            return False
        text = memory.render(400).strip()
        if not text:
            self.delete(memory.memory_id)
            return False
        if prepared is None:
            vector = self._encode_documents([text])[0]
        else:
            entry = prepared.entries.get(memory.memory_id)
            if prepared.namespace != self.model_name or entry is None or entry[0] != text:
                raise SourceRevisionChanged("预计算向量与待写正文或模型配置不一致")
            vector = entry[1]
        return self._store(memory.memory_id, vector, now or utcnow())

    def index_memories(self, memories: list[MemoryItem], now: datetime | None = None) -> int:
        return sum(1 for memory in memories if self.index_memory(memory, now))

    def delete(self, memory_id: str) -> None:
        self._repos.vectors.delete(memory_id)

    def search(self, scope: Scope, query_text: str, memory_types: list[MemoryType] | None = None,
               limit: int = 20, min_score: float = 0.3, *, read_filter: ReadFilter | None = None) -> list[VectorHit]:
        """兼容的底层接口；宿主使用 search_with_status 保留降级状态。"""
        return self.search_with_status(scope, query_text, memory_types, limit, min_score,
                                       read_filter=read_filter).hits

    def search_with_status(self, scope: Scope, query_text: str, memory_types: list[MemoryType] | None = None,
                           limit: int = 20, min_score: float = 0.3,
                           *, read_filter: ReadFilter | None = None) -> VectorSearchResult:
        result = VectorSearchResult()
        query_vector = self._encode_query(query_text)
        if not any(query_vector):
            return result
        rows = self._repos.vectors.load_scope(scope, self.model_name, memory_types, read_filter=read_filter)
        for memory_id, memory_type, blob in rows:
            memory = self._repos.memories.get(memory_id)
            if memory is None or not self._eligible(memory):
                continue
            try:
                stored = array("f")
                stored.frombytes(blob)
                vector = normalized_vector(stored, self._dim)
                if not any(vector):
                    raise EmbeddingError("INVALID_EMBEDDING")
            except (EmbeddingError, ValueError, TypeError):
                if "VECTOR_INDEX_CORRUPT" not in result.degraded_reasons:
                    result.degraded_reasons.append("VECTOR_INDEX_CORRUPT")
                continue
            score = max(-1.0, min(1.0, sum(q * s for q, s in zip(query_vector, vector))))
            if score >= min_score:
                result.hits.append(VectorHit(memory_id, memory_type, score))

        # 旧版没有配置指纹；错配投影要求重建。真实模型还检查完全缺失的投影。
        missing = self._repos.vectors.unindexed_scope(
            scope, self.model_name, memory_types, read_filter=read_filter,
            include_missing=not isinstance(self._embedder, HashingEmbedder),
        )
        for memory_id in missing:
            memory = self._repos.memories.get(memory_id)
            if memory is not None and self._eligible(memory) and memory.render(400).strip():
                result.degraded_reasons.append("VECTOR_INDEX_INCOMPLETE")
                break
        result.hits.sort(key=lambda hit: (-hit.score, hit.memory_id))
        result.hits = result.hits[:limit]
        return result

    def rebuild(self, scope: Scope, now: datetime | None = None) -> int:
        """短事务快照 → 事务外批量编码 → 代次复核后原子发布。

        任一连接在编码期间修改来源/遗忘/索引，都会保守拒绝此次发布。
        模型失败、代次冲突和提交失败均不会先删掉原有完整索引。
        """
        if self._db.connection.in_transaction:
            raise ValueError("向量重建必须在外部事务之外调用")
        moment = now or utcnow()
        with self._db.read_snapshot():
            generation = self._repos.retrieval_cache.generation()
            memories = [m for m in self._repos.memories.list_by_scope(
                scope, limit=-1, statuses=(MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED)
            ) if self._eligible(m) and m.render(400).strip()]
        vectors = self._encode_documents([m.render(400).strip() for m in memories]) if memories else []
        with self._db.transaction():
            if self._repos.retrieval_cache.generation() != generation:
                raise SourceRevisionChanged("向量计算期间来源或派生索引已变化，请重新重建")
            visible, params = scope_sql(scope, "m")
            self._db.connection.execute(
                f"DELETE FROM memory_vectors WHERE memory_id IN (SELECT m.memory_id FROM memories m WHERE {visible})",
                params,
            )
            return sum(self._store(m.memory_id, vector, moment) for m, vector in zip(memories, vectors))

    def count(self) -> int:
        return self._repos.vectors.count(self.model_name)
