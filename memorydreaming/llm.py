"""协议中立的查询/建议工具、参数校验与可替换 token 计量。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .retrieval.context import estimate_tokens


class AdapterError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, next_action: str = "none"):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.next_action = retryable, next_action

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "next_action": self.next_action}


class TokenCounter(Protocol):
    mode: str

    def count(self, text: str) -> int: ...


class EstimatedTokenCounter:
    mode = "estimated"

    def count(self, text: str) -> int:
        return estimate_tokens(text)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def tool_catalog(*, include_core_proposals=False, include_change_proposals=False) -> list[dict]:
    """每次返回新对象，调用方可转换供应商格式而不修改全局定义。"""
    tools = json.loads(Path(__file__).with_name("llm-tools.json").read_text(encoding="utf-8"))["tools"]
    selected = tools if include_core_proposals else tools[:2]
    if include_change_proposals:
        names = {tool["name"] for tool in selected}
        selected = [tool for tool in tools if tool["name"] in names
                    or tool["name"] in {"memory_checkpoint_propose", "memory_request_change"}]
    else:
        selected = [tool for tool in selected
                    if tool["name"] not in {"memory_checkpoint_propose", "memory_request_change"}]
    return selected


def invalid(message: str) -> None:
    raise AdapterError("INVALID_ARGUMENT", message)


def text(value: Any, name: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        invalid(f"{name} 必须为 1..{maximum} 字符的非空字符串")
    return value


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        invalid(f"{name} 必须为 {minimum}..{maximum} 的整数")
    return value


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        invalid("时间必须为带时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        invalid("时间不是有效的 ISO 8601 字符串")
    if result.tzinfo is None or result.utcoffset() is None:
        invalid("时间必须包含时区")
    return result


def validate_tool(name: str, arguments: Any, *, include_core_proposals=False,
                  include_change_proposals=False) -> dict:
    schemas = {tool["name"]: tool["input_schema"] for tool in tool_catalog(
        include_core_proposals=include_core_proposals,
        include_change_proposals=include_change_proposals,
    )}
    if name not in schemas:
        raise AdapterError("UNSUPPORTED", "该工具尚未开放")
    schema = schemas[name]
    if not isinstance(arguments, dict):
        invalid("参数必须为对象")
    required = set(schema["required"])
    allowed = set(schema.get("properties", {}))
    if not required.issubset(arguments) or set(arguments) - allowed:
        invalid("参数必须包含工具定义中的必需字段，且不能包含其他字段")
    args = dict(arguments)
    if name == "memory_search":
        args.setdefault("knowledge_at", None)
        args.setdefault("fact_at", None)
        if args["knowledge_at"] is not None:
            timestamp(args["knowledge_at"])
        if args["fact_at"] is not None:
            timestamp(args["fact_at"])
    if "max_tokens" in args and args["max_tokens"] is not None:
        integer(args["max_tokens"], "max_tokens", 128, 4096)
    if name == "memory_core_propose":
        ref = args["target_ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"mref_[A-Za-z0-9_-]{1,128}", ref):
            invalid("target_ref 必须为当前核心的 mref 引用")
        text(args["reason"], "reason", 1000)
        fields = args["fields"]
        if not isinstance(fields, list) or not 1 <= len(fields) <= 12:
            invalid("fields 必须包含 1..12 个字段")
        names = set()
        for field in fields:
            if not isinstance(field, dict) or set(field) != {"name", "value", "evidence_refs"}:
                invalid("字段必须只包含 name / value / evidence_refs")
            text(field["name"], "name", 100)
            text(field["value"], "value", 1000)
            if field["name"] in names:
                invalid("字段名不可重复")
            names.add(field["name"])
            refs = field["evidence_refs"]
            if (not isinstance(refs, list) or len(refs) > 10
                    or any(not isinstance(ref, str) or not re.fullmatch(r"mref_[A-Za-z0-9_-]{1,128}", ref) for ref in refs)):
                invalid("evidence_refs 必须为最多 10 个 mref 引用")
        return args
    if name == "memory_read":
        refs = args["references"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 10:
            invalid("references 必须包含 1..10 项")
        if any(not isinstance(ref, str) or not re.fullmatch(r"mref_[A-Za-z0-9_-]{1,128}", ref) for ref in refs):
            invalid("references 必须为先前结果提供的 mref 引用")
        return args
    if name == "memory_checkpoint_propose":
        text(args["activity_ref"], "activity_ref", 150)
        integer(args["expected_version"], "expected_version", 1, 2**31 - 1)
        if not isinstance(args["goal"], str) or len(args["goal"]) > 1000:
            invalid("goal 必须为不超过 1000 字符的字符串")
        if (not isinstance(args["open_loops"], list) or len(args["open_loops"]) > 20
                or any(not isinstance(value, str) or len(value) > 1000 for value in args["open_loops"])):
            invalid("open_loops 必须为最多 20 项的短字符串列表")
        if not isinstance(args["checkpoint_note"], str) or len(args["checkpoint_note"]) > 2000:
            invalid("checkpoint_note 必须为不超过 2000 字符的字符串")
        if (not isinstance(args["evidence_refs"], list) or not args["evidence_refs"]
                or len(args["evidence_refs"]) > 20):
            invalid("evidence_refs 必须为 1..20 项")
        if any(not isinstance(ref, str) or not re.fullmatch(r"mref_[A-Za-z0-9_-]{1,128}", ref)
               for ref in args["evidence_refs"]):
            invalid("evidence_refs 必须为当前资料引用")
        return args
    if name == "memory_request_change":
        if args["operation"] not in ("correct_fact", "forget_fact", "forget_event", "forget_scope"):
            invalid("不支持的变更操作")
        if (not isinstance(args["target_refs"], list) or len(args["target_refs"]) > 20
                or any(not isinstance(ref, str) or not re.fullmatch(r"mref_[A-Za-z0-9_-]{1,128}", ref)
                       for ref in args["target_refs"])):
            invalid("target_refs 必须为最多 20 项当前资料引用")
        if args["replacement_text"] is not None and (
                not isinstance(args["replacement_text"], str) or len(args["replacement_text"]) > 2000):
            invalid("replacement_text 必须为不超过 2000 字符的字符串或 null")
        text(args["reason"], "reason", 1000)
        if (not isinstance(args["evidence_refs"], list) or len(args["evidence_refs"]) > 20
                or any(not isinstance(ref, str) or not re.fullmatch(r"mref_[A-Za-z0-9_-]{1,128}", ref)
                       for ref in args["evidence_refs"])):
            invalid("evidence_refs 必须为最多 20 项当前资料引用")
        return args
    text(args["query"], "query")
    ref = args["activity_ref"]
    if ref is not None and (not isinstance(ref, str) or not re.fullmatch(r"aref_[A-Za-z0-9_-]{1,128}", ref)):
        invalid("activity_ref 必须为目录引用或 null")
    if args["detail"] not in ("auto", "overview", "evidence"):
        invalid("detail 必须为 auto / overview / evidence")
    if args["max_items"] is not None:
        integer(args["max_items"], "max_items", 1, 20)
    interval = args["time_range"]
    if interval is not None:
        if not isinstance(interval, dict) or set(interval) != {"axis", "start", "end"}:
            invalid("time_range 必须包含 axis / start / end")
        if interval["axis"] not in ("occurred", "recorded", "valid"):
            invalid("未知的时间轴")
        start, end = timestamp(interval["start"]), timestamp(interval["end"])
        if start >= end:
            invalid("time_range 必须满足 start < end")
        if interval["axis"] != "occurred":
            raise AdapterError("UNSUPPORTED", "当前只支持 occurred 时间轴")
    return args


def envelope(request_id: str, data: dict | None, *, status: str = "ok", gaps=None, warnings=None, error=None) -> dict:
    return {"protocol_version": "1.0", "request_id": request_id, "status": status, "data": data,
            "gaps": list(gaps or []), "warnings": list(warnings or []), "error": error,
            "budget": {"limit_tokens": 0, "counted_tokens": 0, "count_mode": "estimated", "truncated": False}}


def measure(response: dict, counter: TokenCounter, limit: int) -> int:
    """计量最终 JSON；token 数字本身也计入，迭代到稳定或取保守固定值。"""
    budget = response["budget"]
    budget.update(limit_tokens=limit, count_mode=counter.mode, counted_tokens=0)
    for _ in range(12):
        count = counter.count(dumps(response))
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("TokenCounter.count 必须返回非负整数")
        if count == budget["counted_tokens"]:
            return count
        budget["counted_tokens"] = count
    # 某些 tokenizer 在数字边界来回振荡；保守计量，但不冒充 exact。
    budget["count_mode"] = "estimated"
    budget["counted_tokens"] = max(budget["counted_tokens"], counter.count(dumps(response)))
    return max(budget["counted_tokens"], counter.count(dumps(response)))
