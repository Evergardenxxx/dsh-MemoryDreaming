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
import zlib
from collections import Counter
from typing import Protocol

from .keyword import text_shingles


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
