"""结果融合：把多个检索通道的候选合成一个排序。

为什么不能直接把关键词分数和最近时间排序加在一起？
因为它们的量纲根本不同：bm25 是负数（-0.5 这种），时间排序是「越新越靠前」，
硬相加只会得到一个说不清含义的数字。

RRF（Reciprocal Rank Fusion，倒数排名融合）只用「名次」不用「原始分数」：
    得分 = Σ  1 / (k + 名次)
k 取 60 是信息检索里的常见默认值，作用是压平头部差距——
第 1 名和第 2 名的分差不会大得离谱，避免一个通道偶然给出高分就垄断结果。

这个做法在架构文档第 4 节被点名推荐（Engram 的混合检索也用它），
好处是实现简单、不需要调权重、对量纲差异免疫。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelHit:
    """一个通道返回的一条候选。

    weight 由调用方按「这类记忆对本次问题有多相关」给出，
    相当于在融合时给不同通道加权，但不用改融合算法本身。
    """

    memory_id: str
    channel: str
    rank: int          # 该通道内的名次，从 1 开始
    weight: float = 1.0


@dataclass
class FusedCandidate:
    """融合后的候选。"""

    memory_id: str
    score: float
    channels: list[str] = field(default_factory=list)
    best_rank: int = 999

    @property
    def channel_count(self) -> int:
        """有多少个通道同时命中了它。多通道一致通常意味着更可信。"""
        return len(self.channels)


def fuse(hits: list[ChannelHit], k: int = 60) -> list[FusedCandidate]:
    """按 RRF 融合候选，返回按得分降序的列表。

    同一条记忆被多个通道命中时，分数会累加——这正是我们要的：
    既被关键词匹配、又落在时间窗口里的记忆，应该排在只被一个通道命中的前面。
    """
    scores: dict[str, float] = {}
    channels: dict[str, list[str]] = {}
    best_rank: dict[str, int] = {}

    for hit in hits:
        scores[hit.memory_id] = scores.get(hit.memory_id, 0.0) + hit.weight / (k + hit.rank)
        channels.setdefault(hit.memory_id, []).append(hit.channel)
        best_rank[hit.memory_id] = min(best_rank.get(hit.memory_id, 999), hit.rank)

    ordered = sorted(
        scores,
        key=lambda memory_id: (-scores[memory_id], best_rank[memory_id], memory_id),
    )

    return [
        FusedCandidate(
            memory_id=memory_id,
            score=scores[memory_id],
            channels=channels[memory_id],
            best_rank=best_rank[memory_id],
        )
        for memory_id in ordered
    ]
