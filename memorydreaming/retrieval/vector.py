"""向量索引：增量维护记忆向量，并提供余弦相似度检索。

第一版是**暴力检索**（全量载入、逐条点乘），理由和取舍：

- 本地场景记忆量在几百到几千条，一次全量点乘是毫秒级，没有性能问题；
- 没有近似误差，评估检索效果时不会被 ANN 的召回率干扰；
- 代码短、可读，新手能完整看懂「向量检索」到底发生了什么。

规模真的上去（十万条以上）再换 sqlite-vec、faiss 或 pgvector，
那时只需要替换这个类的 search 实现，pipeline 不用动。

和 FTS 索引一样，向量是**派生数据**：随时可以用 rebuild() 从 memories 重建。
换嵌入模型时请调 rebuild()，否则新旧向量混在一起，相似度没有意义。
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime

from ..clock import utcnow
from ..models import LinkRelation, MemoryItem, MemoryStatus, MemoryType, ReadFilter, Scope
from ..storage.database import Database
from ..storage.repository import scope_sql
from .embedding import Embedder, HashingEmbedder


@dataclass
class VectorHit:
    """一条向量命中。score 是余弦相似度，范围大致在 -1~1，越大越像。"""

    memory_id: str
    memory_type: MemoryType
    score: float


class VectorIndex:
    """记忆向量的增量维护与检索。"""

    def __init__(self, db: Database, embedder: Embedder | None = None) -> None:
        self._db = db
        self._repos = db.repositories
        self._embedder: Embedder = embedder or HashingEmbedder()

    @property
    def model_name(self) -> str:
        """当前嵌入模型的名字，会写进 memory_vectors.model。"""
        return self._embedder.name

    @property
    def cache_signature(self) -> dict:
        """模型名须标识版本/前缀/归一化策略；自定义模型可补充配置指纹。"""
        return {"model": self._embedder.name, "dim": self._embedder.dim,
                "config": getattr(self._embedder, "cache_signature", None)}

    def index_memory(self, memory: MemoryItem, now: datetime | None = None) -> bool:
        """为一条记忆建立/更新向量。返回 False 表示正文为空，跳过。

        空正文不索引是有意为之：零向量和任何东西的相似度都是 0，
        留在库里只会拖慢扫描。
        """
        # 用 render() 而不是 body_text 建向量：
        # body_text 是给检索用的扁平串，会把「用户提到的约定，尚未完成」这类
        # 模板话术也算进向量，稀释真正的内容。render() 是按类型组织过的正文，
        # 更接近人读到的句子，向量更集中。
        if memory.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED) or self._repos.forgetting.is_suppressed(memory):
            self.delete(memory.memory_id)
            return False
        text = memory.render(400).strip()
        if not text:
            return False

        vector = self._embedder.embed(text)
        if not any(vector):
            return False

        payload = array("f", vector).tobytes()  # float32 比 JSON 省四分之三空间
        self._repos.vectors.upsert(
            memory_id=memory.memory_id,
            model=self._embedder.name,
            dim=self._embedder.dim,
            vector=payload,
            now=now or utcnow(),
        )
        return True

    def index_memories(self, memories: list[MemoryItem], now: datetime | None = None) -> int:
        """批量建索引，返回成功条数。"""
        return sum(1 for memory in memories if self.index_memory(memory, now))

    def delete(self, memory_id: str) -> None:
        """删除记忆时同步清掉向量，避免它换个形式又被检索到。"""
        self._repos.vectors.delete(memory_id)

    def search(
        self,
        scope: Scope,
        query_text: str,
        memory_types: list[MemoryType] | None = None,
        limit: int = 20,
        min_score: float = 0.3,
        *, read_filter: ReadFilter | None = None,
    ) -> list[VectorHit]:
        """按余弦相似度检索。

        参数:
            min_score: 低于这个分数直接丢弃。没有阈值的话，
                       任何两句中文都会因为共享「我们」「什么」而有一点分，
                       结果就是噪声淹没真信号。
        """
        query_vector = self._embedder.embed(query_text)
        if not any(query_vector):
            return []

        rows = self._repos.vectors.load_scope(scope, self._embedder.name, memory_types, read_filter=read_filter)
        hits: list[VectorHit] = []

        for memory_id, memory_type, blob in rows:
            memory = self._repos.memories.get(memory_id)
            if memory is None or self._repos.forgetting.is_suppressed(memory):
                continue
            stored = array("f")
            stored.frombytes(blob)
            # 维度不一致说明索引是旧模型留下的（换 dim 却没重建），跳过而不是报错
            if len(stored) != len(query_vector):
                continue

            score = sum(q * s for q, s in zip(query_vector, stored))
            if score >= min_score:
                hits.append(VectorHit(memory_id, memory_type, score))

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    def rebuild(self, scope: Scope, now: datetime | None = None) -> int:
        """重建某个角色的全部向量，返回重建条数。

        什么时候需要它：
          - 换嵌入模型之后；
          - 从备份恢复、或者早期版本没有向量时；
          - 发现检索结果和正文对不上时（可能是索引漂移）。
        """
        moment = now or utcnow()
        # 重建不能使用检索候选上限，否则第 10001 条以后的有效向量会被误删。
        memories = self._repos.memories.list_by_scope(
            scope, limit=-1, statuses=(MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED)
        )
        memories = [
            memory for memory in memories
            if not self._repos.forgetting.is_suppressed(memory) and not (
                memory.status is MemoryStatus.SUPERSEDED
                and self._repos.links.has_relation(memory.memory_id, LinkRelation.PART_OF)
            )
        ]
        alive = {memory.memory_id for memory in memories}

        # 清理范围必须与读取范围一致。重建真实世界或活动 A 时，
        # 不能把同角色的虚构世界/活动 B 向量当成孤立数据清掉。
        visible, params = scope_sql(scope, "m")
        rows = self._db.connection.execute(
            f"""
            SELECT v.memory_id
            FROM memory_vectors v
            JOIN memories m ON m.memory_id = v.memory_id
            WHERE {visible}
            """,
            params,
        ).fetchall()
        for row in rows:
            if row["memory_id"] not in alive:
                self._repos.vectors.delete(row["memory_id"])

        return self.index_memories(memories, moment)

    def count(self) -> int:
        """当前模型下已建索引的条数。"""
        return self._repos.vectors.count(self._embedder.name)
