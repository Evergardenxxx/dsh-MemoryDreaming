"""记忆形成策略：在抽取器和持久化之间做统一的安全校验。

抽取器负责提出候选，形成策略负责决定候选是否符合四类记忆的最小格式。
这样规则抽取器、LLM 抽取器和未来的人工导入不会各自实现一套隐含约束。
这里不判断事实真假；事实真假仍由来源、认识论状态、纠正和遗忘链路处理。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .models import EventRecord, MemoryDraft, MemoryType


class MemoryFormationPolicy:
    """四类记忆候选的结构与权重边界。

    失败采用 fail-closed：单个抽取任务出现不合法候选时整批失败并重试，
    防止把半条 LLM JSON 静默写入长期记忆。抽取器可以先过滤“不值得记”的
    事件，但不能绕过这里的格式校验。
    """

    REQUIRED_FIELDS: Mapping[MemoryType, tuple[str, ...]] = {
        MemoryType.WORKING: ("goal", "open_loops", "active_entities", "checkpoint_note"),
        MemoryType.EPISODIC: ("what_happened",),
        MemoryType.SEMANTIC: ("subject", "predicate", "obj"),
        MemoryType.PROCEDURAL: ("trigger", "steps"),
    }

    @classmethod
    def validate_body(cls, memory_type: MemoryType, body: dict) -> dict:
        """校验已经形成的正文，供人工/宿主纠错替换复用。"""
        if not isinstance(memory_type, MemoryType):
            raise ValueError("memory_type 必须是 working/episodic/semantic/procedural")
        if not isinstance(body, dict):
            raise ValueError("记忆正文必须为对象")
        required = cls.REQUIRED_FIELDS[memory_type]
        missing = [name for name in required if name not in body]
        if missing:
            raise ValueError(f"{memory_type.value} 正文缺少字段：{', '.join(missing)}")
        if memory_type is MemoryType.SEMANTIC:
            if any(not isinstance(body[name], str) or not body[name].strip()
                   for name in ("subject", "predicate", "obj")):
                raise ValueError("semantic 正文的 subject/predicate/obj 必须为非空字符串")
        elif memory_type is MemoryType.EPISODIC:
            if not isinstance(body["what_happened"], str) or not body["what_happened"].strip():
                raise ValueError("episodic 正文的 what_happened 必须为非空字符串")
        elif memory_type is MemoryType.WORKING:
            if any(not isinstance(body[name], str) for name in ("goal", "checkpoint_note")):
                raise ValueError("working 正文的 goal/checkpoint_note 必须为字符串")
            if any(not isinstance(body[name], list) or
                   any(not isinstance(item, str) or not item.strip() for item in body[name])
                   for name in ("open_loops", "active_entities")):
                raise ValueError("working 正文的 open_loops/active_entities 必须为非空字符串数组")
            if not body["goal"].strip() and not body["open_loops"]:
                raise ValueError("working 正文至少需要 goal 或 open_loops")
        else:
            if not isinstance(body["trigger"], str) or not body["trigger"].strip():
                raise ValueError("procedural 正文的 trigger 必须为非空字符串")
            if (not isinstance(body["steps"], list) or not body["steps"]
                    or not all(isinstance(step, str) and step.strip() for step in body["steps"])):
                raise ValueError("procedural 正文的 steps 必须为非空数组")
        return body

    def validate(self, event: EventRecord, draft: MemoryDraft) -> MemoryDraft:
        if not isinstance(draft.memory_type, MemoryType):
            raise ValueError("候选 memory_type 必须是 working/episodic/semantic/procedural")
        if not isinstance(draft.body, dict):
            raise ValueError("候选 body 必须是对象")
        if not isinstance(draft.candidate_key, str) or not draft.candidate_key.strip():
            raise ValueError("候选 candidate_key 不能为空")
        if isinstance(draft.importance, bool) or not isinstance(draft.importance, (int, float)):
            raise ValueError("候选 importance 必须是数字")
        if not 0 <= float(draft.importance) <= 1:
            raise ValueError("候选 importance 必须在 0 到 1 之间")

        self.validate_body(draft.memory_type, draft.body)

        # source_speaker 只能细化来源，不能伪造与事件不一致的说话者。
        if draft.source_speaker is not None and draft.source_speaker != event.speaker:
            raise ValueError("候选 source_speaker 与来源事件不一致")
        # 不允许通过浮点 nan/inf 绕过边界检查或破坏排序。
        if float(draft.importance) != float(draft.importance) or abs(float(draft.importance)) == float("inf"):
            raise ValueError("候选 importance 必须为有限数字")
        return replace(draft, importance=float(draft.importance))

