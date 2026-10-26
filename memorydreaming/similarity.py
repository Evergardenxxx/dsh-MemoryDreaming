"""记忆相似度：去重和主题聚类共用的一套判断。

这里的「像不像」只有一个现实用途：**去重**（consolidation）。
同类型、阈值高（0.85），只合并几乎一样的表述。

主题聚类**没有**用这套度量——试过，不可行：`第一次去海边` 和
`周末去海边要带相机` 只共享两个词，Jaccard 只有 0.07，
但它们显然同属一个主题。主题分组最终改成了「二字词 + 文档频率」，
见 summarization.py。这个失败尝试写在 docs/open-problems.md 第 6 节里，
因为它是本项目最典型的一次「度量选错，阈值怎么调都不对」。

为什么用 Jaccard 而不是余弦？
因为这里的输入是**集合**（有哪些二字词），没有词频、也没有归一化长度的问题。
Jaccard 的含义很直白：两句话共享的词，占它们所有词的比例。
缺点是抓不到同义改写（「失眠」≠「睡不着」），这和项目的分词现状一致，
属于已知限制，写在 docs/open-problems.md 里。
"""

from __future__ import annotations

from .models import MemoryItem
from .retrieval.keyword import text_shingles


def jaccard(left: set[str], right: set[str]) -> float:
    """两个集合的交并比，取值 0~1。"""
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def cluster(
    memories: list[MemoryItem],
    threshold: float,
    *,
    respect_type: bool = True,
    min_shingles: int = 3,
) -> list[list[MemoryItem]]:
    """把互为相似的记忆聚成簇，单条不成簇的直接丢弃。

    参数:
        threshold:    Jaccard 下限。去重用 0.85，主题聚类用 0.25。
        respect_type: 是否只在同类型内比较。去重要求 True（工作记忆和情景记忆
                      本来就不是一回事），聚类用 False（主题天然跨类型）。
        min_shingles: 特征太少的正文不参与，避免「好」「是的」这种短句误判。

    做法是朴素 O(n²)，但加了两个廉价剪枝，实际很快：
      1. 类型不同直接跳过；
      2. Jaccard 的上界就是 min/max，长度比不达标就不用算交集。
    本地几百条记忆完全够用；几万条时应该先按时间窗或向量近邻粗筛。
    """
    prepared: list[tuple[MemoryItem, set[str]]] = []
    for memory in memories:
        shingles = text_shingles(memory.body_text)
        if len(shingles) >= min_shingles:
            prepared.append((memory, shingles))

    used: set[str] = set()
    clusters: list[list[MemoryItem]] = []

    for index, (memory, shingles) in enumerate(prepared):
        if memory.memory_id in used:
            continue

        group = [memory]
        used.add(memory.memory_id)

        for other, other_shingles in prepared[index + 1 :]:
            if other.memory_id in used:
                continue
            if respect_type and other.memory_type is not memory.memory_type:
                continue

            shorter, longer = sorted((len(shingles), len(other_shingles)))
            if longer == 0 or shorter / longer < threshold:
                continue

            if jaccard(shingles, other_shingles) >= threshold:
                group.append(other)
                used.add(other.memory_id)

        if len(group) > 1:
            clusters.append(group)

    return clusters
