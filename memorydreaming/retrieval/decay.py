"""可用性衰减：让「很久没被想起」的记忆自然下沉。

为什么需要它？
检索分（RRF）只回答「这条记不记得住相关」，不回答「它还活跃吗」。
两条同样相关的记忆，一条昨天刚用过、一条三个月没碰过，
默认应该先看前者——这就是架构文档第 5 节说的「可用性衰减」。

公式借自 cogmem-agent 的简化版：

    半衰期 = 基础半衰期 × (1 + importance)
    有效强度 = max(下限, 强度 × 0.5 ** (距上次使用 / 半衰期))

两个设计点值得说明：

1. **importance 参与半衰期**。越重要的事忘得越慢——这比「统一半衰期 + 加权」
   更接近人：一次搬家（重要）十年后还记得，今天午饭吃了什么（不重要）下周就忘。
2. **有下限**。没有下限的话，一条五年前的错误记忆会衰减到 0，
   但它仍然是错的、仍然可能被关键词精确命中。衰减只影响排序，
   不该变成「删除」的同义词。

注意：这里的衰减**只用于排序和默认粒度**，不改变事实的真实性。
"""

from __future__ import annotations

from datetime import datetime

from ..models import MemoryItem

# 基础半衰期：60 天。取自 cogmem-agent 的 base_half_life，也是个直觉上合理的默认值——
# 两个月没提起的事，默认该往后放，但还查得到。
DEFAULT_HALF_LIFE_HOURS = 24 * 60

# 衰减下限：再久没用的记忆也保留三成「容易想起」的权重，
# 免得精确回查时排到无关的新记忆后面。
DEFAULT_DECAY_FLOOR = 0.3


def effective_strength(
    memory: MemoryItem,
    now: datetime,
    *,
    base_half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    floor: float = DEFAULT_DECAY_FLOOR,
) -> float:
    """这条记忆在 `now` 时刻的「容易想起」程度，取值 0~1。"""
    last_touched = memory.last_used_at or memory.recorded_at
    age_hours = max(0.0, (now - last_touched).total_seconds() / 3600)

    # 重要的事半衰期更长：importance=1 时翻倍，importance=0 时不变
    half_life = base_half_life_hours * (1.0 + max(0.0, min(1.0, memory.importance)))
    decayed = memory.retrieval_strength * 0.5 ** (age_hours / half_life)

    return max(floor, min(1.0, decayed))


def heat_tier(strength: float) -> str:
    """把有效强度分成三档，方便日志和调试时一眼看出冷热。

    分档只是给人看的标签，检索排序用的是连续值，不是这三档。
    """
    if strength >= 0.6:
        return "hot"
    if strength >= 0.35:
        return "warm"
    return "cold"
