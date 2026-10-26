"""向量通道测试：嵌入的性质、索引维护、检索隔离。

这里刻意不追求「语义相似」的断言——哈希嵌入做不到。
测试验的是**管道正确性**：
向量有没有归一化、索引会不会跟着增删改、越权数据会不会漏出来。
换成真实模型后，这些用例仍然应该全部通过。
"""

from __future__ import annotations

import unittest

from memorydreaming.models import MemoryType
from memorydreaming.retrieval.embedding import HashingEmbedder

from tests.helpers import RuntimeTestCase


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class HashingEmbedderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.embedder = HashingEmbedder(dim=128)

    def test_vectors_are_unit_length(self) -> None:
        """归一化之后，点积就是余弦相似度——这是检索侧省掉一次开方的前提。"""
        vector = self.embedder.embed("周末去海边，我要带相机")
        norm = sum(value * value for value in vector) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_same_text_gives_same_vector(self) -> None:
        """必须可复现：同一段文本两次编码结果要完全一致。"""
        first = self.embedder.embed("睡前听故事")
        second = self.embedder.embed("睡前听故事")
        self.assertEqual(first, second)

    def test_literal_overlap_scores_higher_than_unrelated(self) -> None:
        """字面重合度高的句子，相似度应当更高——这是哈希嵌入唯一能保证的事。"""
        base = self.embedder.embed("周末去海边")
        close = self.embedder.embed("周末去海边玩")
        far = self.embedder.embed("今天在公司加班")

        self.assertGreater(_cosine(base, close), _cosine(base, far))

    def test_empty_text_gives_zero_vector(self) -> None:
        self.assertFalse(any(self.embedder.embed("")))


class VectorIndexTest(RuntimeTestCase):
    def test_rebuild_indexes_existing_memories(self) -> None:
        self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "周末去海边，我要带相机"}, hours_ago=2
        )

        count = self.runtime.rebuild_vectors(self.scope)

        self.assertEqual(count, 1)
        self.assertEqual(self.runtime.vectors.count(), 1)

    def test_search_finds_similar_memory(self) -> None:
        memory = self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "周末去海边，我要带相机"}, hours_ago=2
        )
        self.runtime.rebuild_vectors(self.scope)

        hits = self.runtime.vectors.search(self.scope, "去海边", min_score=0.1)

        self.assertEqual([hit.memory_id for hit in hits], [memory.memory_id])

    def test_unrelated_query_is_filtered_by_min_score(self) -> None:
        self.add_memory(
            MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "睡前故事"}
        )
        self.runtime.rebuild_vectors(self.scope)

        hits = self.runtime.vectors.search(self.scope, "量子纠缠实验", min_score=0.1)

        self.assertEqual(hits, [])

    def test_scope_isolation(self) -> None:
        self.add_memory(MemoryType.EPISODIC, {"what_happened": "周末去海边"}, hours_ago=2)
        self.runtime.rebuild_vectors(self.scope)

        other = self.scope.__class__(user_id="u1", character_id="c2")
        hits = self.runtime.vectors.search(other, "去海边", min_score=0.1)

        self.assertEqual(hits, [])

    def test_forget_removes_vector(self) -> None:
        memory = self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "周末去海边，我要带相机"}, hours_ago=2
        )
        self.runtime.rebuild_vectors(self.scope)

        from memorydreaming.models import ForgetSelector

        self.runtime.forget(ForgetSelector(memory_ids=[memory.memory_id]))

        self.assertEqual(self.runtime.vectors.count(), 0)
        self.assertEqual(self.runtime.vectors.search(self.scope, "去海边", min_score=0.1), [])

    def test_retrieval_trace_includes_vector_channel(self) -> None:
        """通道接好了没有——看检索轨迹里有没有 vector。

        注意：当前的哈希嵌入只看字面，所以这个通道和关键词通道高度重合，
        而且稍微绕一点的说法（如「周末有什么安排」）分数就会掉到阈值以下。
        这是占位模型的能力边界，不是管道的 bug——换成真实模型后，
        同样的断言仍然成立，召回质量才会真正提升。
        """
        self.add_memory(
            MemoryType.EPISODIC, {"what_happened": "周末去海边，我要带相机"}, hours_ago=2
        )
        self.runtime.rebuild_vectors(self.scope)

        result = self.runtime.retrieve("周末去海边要带什么", self.scope)

        self.assertIn("vector", result.trace.channel_hits)


if __name__ == "__main__":
    unittest.main()
