"""基于规则的示例抽取器。

它**不是**生产级实现，只用来演示「抽取器该长什么样」：
输入一条原始事件，输出若干条记忆候选。

三条规则，刚好覆盖三类不同的认识论状态：

    我叫 X        → 语义记忆（用户明确设定，explicit_setting）
    我们约好 X     → 工作记忆（未完成事项，模态度 intention）
    我喜欢 X       → 语义记忆（用户自述，user_report）

注意「我喜欢」这条记的是**用户报告**，不是客观事实；
「我们约好」记的是**意愿**，不是已经发生的事。
这正是架构文档反复强调的分寸：不能把愿望、假设、引用提升成已发生的事实。

换成模型抽取时，只要新类同样实现 `version` 和 `extract(event)`，
其余代码一行都不用改。
"""

from __future__ import annotations

import re

from .models import (
    EpistemicStatus,
    EventRecord,
    MemoryDraft,
    MemoryType,
    Modality,
)

# 规则表：(正则, 记忆类型, 候选键, 构造函数)
# 候选键用于幂等：同一事件重复抽取时不会写重复记录。


def _name_draft(match: re.Match[str]) -> MemoryDraft:
    name = match.group("value")
    return MemoryDraft(
        memory_type=MemoryType.SEMANTIC,
        body={"subject": "用户", "predicate": "称呼", "obj": name, "scope_note": "用户明确要求"},
        candidate_key="rule:name",
        epistemic_status=EpistemicStatus.EXPLICIT_SETTING,
        modality=Modality.OCCURRED,
        importance=0.9,  # 称呼属于「稳定身份」，比一般偏好重要
    )


def _address_draft(match: re.Match[str]) -> MemoryDraft:
    """「以后叫我阿鸥就行」——用户对「怎么称呼自己」的明确要求。

    它和「我叫林鸥」是两件事：原名是事实，偏好称呼是关系设定。
    两者都值得记住，但只有后者会影响角色怎么开口。
    """
    name = match.group("value").strip("，。！ ")
    return MemoryDraft(
        memory_type=MemoryType.SEMANTIC,
        body={
            "subject": "用户",
            "predicate": "希望被称呼为",
            "obj": name,
            "scope_note": "用户明确要求",
        },
        candidate_key="rule:preferred_address",
        epistemic_status=EpistemicStatus.EXPLICIT_SETTING,
        modality=Modality.OCCURRED,
        importance=0.9,
    )


def _commitment_draft(match: re.Match[str]) -> MemoryDraft:
    promise = match.group("value").strip("，。！ ")
    return MemoryDraft(
        memory_type=MemoryType.WORKING,
        body={
            "goal": "",
            "open_loops": [promise],
            "active_entities": [],
            "checkpoint_note": "用户提到的约定，尚未完成",
        },
        candidate_key="rule:commitment",
        epistemic_status=EpistemicStatus.USER_REPORT,
        modality=Modality.INTENTION,  # 约定是「打算做」，还没发生
        importance=0.8,
    )


def _preference_draft(match: re.Match[str]) -> MemoryDraft:
    thing = match.group("value").strip("，。！ ")
    # 否定必须显式保住：「我不喜欢猫」不能记成「喜欢猫」。
    # 这是规则抽取最典型的坑，也是「抽取结果需要校验」这条要求的由来。
    negated = bool(match.groupdict().get("neg"))
    return MemoryDraft(
        memory_type=MemoryType.SEMANTIC,
        body={
            "subject": "用户",
            "predicate": "不喜欢" if negated else "喜欢",
            "obj": thing,
            "scope_note": "",
        },
        candidate_key="rule:preference",
        epistemic_status=EpistemicStatus.USER_REPORT,
        modality=Modality.OCCURRED,
        importance=0.6,
    )


_RULES: list[tuple[re.Pattern[str], object]] = [
    # 顺序有意义：更具体的「以后叫我 X」必须排在通用的「叫我 X」前面
    (
        re.compile(r"以后叫我\s*(?P<value>[\u4e00-\u9fffA-Za-z]{1,8}?)(?:就行|就好|吧|啦|，|。|！|$)"),
        _address_draft,
    ),
    # 名字也要截断：「叫我林小鸥就好」不能记成「林小鸥就好」
    (
        re.compile(
            r"(?:我叫|叫我|我的名字是)\s*(?P<value>[\u4e00-\u9fffA-Za-z]{1,8}?)"
            r"(?:就行|就好|吧|啦|，|。|！|$)"
        ),
        _name_draft,
    ),
    (re.compile(r"(?:我们约好|说好|答应你)\s*(?P<value>[^。！？]{1,30})"), _commitment_draft),
    (re.compile(r"(?P<neg>不)?(?:我)?喜欢\s*(?P<value>[^。！？]{1,30})"), _preference_draft),
]


class RuleBasedExtractor:
    """示例抽取器：只用正则，不依赖模型。"""

    version = "rule-v1"

    def extract(self, event: EventRecord) -> list[MemoryDraft]:
        """只处理用户说的话；角色自己的话不作为关于用户的事实来源。"""
        if event.speaker != "user":
            return []

        drafts: list[MemoryDraft] = []
        for pattern, builder in _RULES:
            match = pattern.search(event.content)
            if match:
                drafts.append(builder(match))  # type: ignore[operator]
        return drafts
