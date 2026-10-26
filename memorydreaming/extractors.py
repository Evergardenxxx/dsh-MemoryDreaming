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
from collections.abc import Iterator
from typing import Callable

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


def _episodic_draft(match: re.Match[str]) -> MemoryDraft:
    """把明确的用户经历保存为情景记忆；不把普通聊天全部变成长期事件。"""
    happened = match.group(0).strip("，。！ ")
    return MemoryDraft(
        memory_type=MemoryType.EPISODIC,
        body={"what_happened": happened, "participants": ["用户"], "context": ""},
        candidate_key="rule:episode",
        epistemic_status=EpistemicStatus.USER_REPORT,
        modality=Modality.OCCURRED,
        importance=0.5,
    )


def _procedural_draft(match: re.Match[str]) -> MemoryDraft:
    trigger = match.group("trigger").strip()
    steps = [part.strip() for part in re.split(r"(?:然后|并且|、)", match.group("step")) if part.strip()]
    return MemoryDraft(
        memory_type=MemoryType.PROCEDURAL,
        body={
            "trigger": trigger,
            "preconditions": [],
            "steps": steps,
            "exceptions": [],
            "expected_outcome": "",
        },
        candidate_key="rule:procedure",
        epistemic_status=EpistemicStatus.EXPLICIT_SETTING,
        modality=Modality.OCCURRED,
        importance=0.8,
    )


_RULES: list[tuple[re.Pattern[str], Callable[[re.Match[str]], MemoryDraft]]] = [
    # 称呼请求与姓名陈述使用互斥规则，避免同一短语同时改写姓名和偏好。
    (
        re.compile(r"^(?:你)?(?:以后)?叫我\s*(?P<value>[\u4e00-\u9fffA-Za-z]{1,8}?)(?:就行|就好|吧|啦)?$"),
        _address_draft,
    ),
    # 名字也要截断：「叫我林小鸥就好」不能记成「林小鸥就好」
    (
        re.compile(
            r"^(?:我叫|我的名字是)\s*(?P<value>[\u4e00-\u9fffA-Za-z]{1,8}?)"
            r"(?:就行|就好|吧|啦)?$"
        ),
        _name_draft,
    ),
    (re.compile(r"^(?:我们约好|说好|答应你)\s*(?P<value>[^。！？]{1,30})$"), _commitment_draft),
    # 稳定相处规则：必须是显式的「以后/每次/当我 + 条件 + 动作」结构，
    # 避免把普通愿望或假设误记成程序记忆。
    (
        re.compile(r"^(?:以后|每次|当我)\s*(?P<trigger>[^，,：:]{1,20})(?:，|,|：|:)\s*"
                   r"(?:请|就|都)?\s*(?P<step>[^。！？]{1,40})$"),
        _procedural_draft,
    ),
    # 有明确行动动词的用户自述才进入情景记忆；「我喜欢/我想」由其他规则处理。
    (
        re.compile(r"^(?:(?:今天|昨天|前天|上次|刚刚)\s*)?我(?:去|去了|遇到|看见|看到|参加|完成|做了|买了|读了|搬到|开始)[^。！？]{1,40}$"),
        _episodic_draft,
    ),
    (
        re.compile(r"^我们(?:一起)?(?:去|去了|聊了|完成了|做了|看了)[^。！？]{1,40}$"),
        _episodic_draft,
    ),
    (re.compile(r"^我(?P<neg>不)?喜欢\s*(?P<value>[^。！？]{1,30})$"), _preference_draft),
]

# 逗号是程序记忆格式的一部分，而普通事实分句会在逗号处分割；
# 因此在分句前单独识别完整的条件→动作句。
_PROCEDURE_PATTERN = re.compile(
    r"^(?:以后|每次|当我)\s*(?P<trigger>[^，,：:]{1,20})(?:，|,|：|:)\s*"
    r"(?:请|就|都)?\s*(?P<step>[^。！？]{1,40})$"
)


# 示例抽取器只接受可以直接识别的自述。复杂语境留给更完整的抽取器，
# 原始事件始终保留；漏抽可以重建，错误的个人事实却会污染后续交流。
_UNSAFE_CONTEXT = re.compile(
    r"[\"'“”‘’「」『』]|如果|假如|假设|倘若|要是|假装|扮演|"
    r"例如|比如|举例|台词|引文|原话|例句|不是不|是否|会不会|什么|哪里|怎样|"
    r"(?:^|[，,：:])(?:他|她|它|别人|有人|朋友|同事|妈妈|爸爸|你|作者|角色)"
    r"[^，,：:]{0,8}(?:说|问|写|表示)"
)


def _declarative_clauses(content: str) -> Iterator[tuple[str, int]]:
    """拒绝引述、假设和疑问句，然后分出简单陈述，避免跨说话者归因。"""
    for sentence in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;]?", content):
        text = sentence.group().strip()
        sentence_start = sentence.start() + len(sentence.group()) - len(sentence.group().lstrip())
        if _UNSAFE_CONTEXT.search(text):
            continue
        if text.endswith(("?", "？")) or re.search(r"(?:吗|么|呢)[。！!]?$", text):
            continue
        for clause in re.finditer(r"[^，,]+", text.rstrip("。！？!?；;")):
            normalized = clause.group().strip()
            if normalized:
                start = sentence_start + clause.start() + len(clause.group()) - len(clause.group().lstrip())
                yield normalized, start


class RuleBasedExtractor:
    """示例抽取器：只用正则，不依赖模型。"""

    version = "rule-v2"

    def extract(self, event: EventRecord) -> list[MemoryDraft]:
        """只处理用户说的话；角色自己的话不作为关于用户的事实来源。"""
        if event.speaker != "user":
            return []

        drafts: list[MemoryDraft] = []
        for match in _PROCEDURE_PATTERN.finditer(event.content.strip()):
            draft = _procedural_draft(match)
            draft.candidate_key += f":procedure:{match.start()}"
            draft.source_span = (match.start(), match.end())
            drafts.append(draft)
        for clause_index, (clause, clause_start) in enumerate(_declarative_clauses(event.content)):
            for pattern, builder in _RULES:
                for match in pattern.finditer(clause):
                    draft = builder(match)
                    # 一条消息可包含多个同类型陈述；键由原文中的稳定位置区分。
                    draft.candidate_key += f":{clause_index}:{match.start()}"
                    draft.source_span = (clause_start + match.start(), clause_start + match.end())
                    drafts.append(draft)
        return drafts
