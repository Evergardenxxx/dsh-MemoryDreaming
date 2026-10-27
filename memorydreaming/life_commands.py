"""显式用户 JSON 指令的可信宿主入口，模型输出不能调用此入口。

每次操作绑定已保存的当前用户事件。通用SDK仍允许模型提候选、宿主另行审批；
这里仅用于用户直接给出完整结构化命令的可审查快捷路径。
"""

from datetime import datetime
import json

from .llm import AdapterError, text
from .life_evidence import dumps
from .storage.repository import _scope_key


COMMANDS = {
    "记忆自传": "autobiography", "/memory autobiography": "autobiography",
    "记忆承诺": "promise", "/memory promise": "promise", "记忆完成": "complete", "/memory complete": "complete",
    "记忆取消": "cancel", "/memory cancel": "cancel", "记忆触发": "trigger", "/memory trigger": "trigger",
    "记忆偏好": "preference", "/memory preference": "preference", "记忆成长": "growth", "/memory growth": "growth",
    "记忆提醒": "proactive", "/memory proactive": "proactive", "记忆自检": "check", "/memory check": "check",
}


def _actual_command(content):
    content = content.strip()
    for prefix, operation in COMMANDS.items():
        if content == prefix:
            return operation, {}
        if content.startswith(prefix+" "):
            try:
                value = json.loads(content[len(prefix):].strip())
                if isinstance(value, dict):
                    return operation, value
            except ValueError:
                break
    raise AdapterError("NOT_FOUND_OR_FORBIDDEN", "用户原话不是完整的角色管理命令")


def _time(value):
    if not isinstance(value, str) or len(value) > 80:
        raise AdapterError("INVALID_ARGUMENT", "时间须为带时区的 ISO8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except ValueError:
        raise AdapterError("INVALID_ARGUMENT", "时间必须带时区，例如 2026-10-28T20:00:00+08:00") from None


def execute_life_command(session, operation, payload, message_id):
    if not isinstance(payload, dict) or len(payload) > 16:
        raise AdapterError("INVALID_ARGUMENT", "命令须为短 JSON 对象")
    text(message_id, "message_id", 200)
    event_id = session.current_event_id
    event = session.runtime.db.repositories.events.get(event_id) if event_id else None
    if (event is None or event.speaker != "user" or event.conversation_id != session.binding.conversation_id
            or event.scope.user_id != session.binding.user_id or event.scope.character_id != session.binding.character_id
            or event.scope.relationship_id != session.binding.relationship_id or event.scope.domain != session.binding.domain):
        raise AdapterError("NOT_FOUND_OR_FORBIDDEN", "命令必须关联当前真实用户消息")
    actual_operation, actual_payload = _actual_command(event.content)
    if (actual_operation != operation or dumps(actual_payload) != dumps(payload)
            or event.idempotency_key != dumps([session.binding.conversation_id, "user", message_id])
            or session.runtime.db.repositories.forgetting.is_event_forgotten(event_id)):
        raise AdapterError("NOT_FOUND_OR_FORBIDDEN", "命令参数或消息ID与当前用户原话不匹配")
    runtime, scope = session.runtime, event.scope
    reviewer, reason = "native-explicit-user-command", "用户直接发送完整结构化命令"
    request = "life-command:"+message_id
    with runtime.db.transaction():
        if operation == "autobiography":
            if not payload:
                return runtime.narrative.timeline(scope)
            _allowed(payload, {"title", "text", "perspective", "modality", "occurred_at", "entry_id", "version"})
            proposal = runtime.narrative.propose(scope, payload["title"], payload["text"], [event_id], request,
                perspective=payload.get("perspective", "shared"), modality=payload.get("modality", "occurred"),
                occurred_at=_time(payload["occurred_at"]) if payload.get("occurred_at") else None,
                entry_id=payload.get("entry_id"), expected_version=payload.get("version", 0))
            return runtime.narrative.approve(scope, proposal["proposal_id"], reviewer, reason, user_event_id=event_id)
        if operation == "promise":
            if not payload:
                return {"kind": "intentions", "items": runtime.intentions.list_open(scope)}
            _allowed(payload, {"text", "owner", "category", "due_at", "event_trigger"})
            proposal = runtime.intentions.propose(scope, payload["text"], [event_id], request_id=request,
                owner=payload.get("owner", "character"), category=payload.get("category", "promise"),
                due_at=_time(payload["due_at"]) if payload.get("due_at") else None, event_trigger=payload.get("event_trigger"))
            # 这条命令只新建候选，不执行 revise。首次确认总是针对 version1；
            # 重试时 propose 返回的可能已经是 open/version2，不能重算审批参数。
            return runtime.intentions.confirm(scope, proposal["intention_id"], expected_version=1,
                confirmation_refs=[event_id], reviewer=reviewer, reason=reason, request_id=request+":confirm")
        if operation in ("complete", "cancel"):
            _allowed(payload, {"id", "version"})
            return runtime.intentions.resolve(scope, payload["id"], state="completed" if operation == "complete" else "cancelled",
                expected_version=payload["version"], evidence=[event_id], reviewer=reviewer, reason=reason, request_id=request)
        if operation == "trigger":
            _allowed(payload, {"name"})
            return runtime.intentions.record_trigger(scope, payload["name"], event_id, request_id=request)
        if operation == "preference":
            if not payload:
                return {"kind": "companionship", "items": runtime.companionship.list_confirmed(scope)}
            _allowed(payload, {"key", "value", "context", "version"})
            proposal = runtime.companionship.propose(scope, payload["key"], payload["value"], context=payload.get("context", "default"),
                evidence=[event_id], reason=reason, idempotency_key=request)
            return runtime.companionship.confirm(scope, proposal["proposal_id"], user_event_id=event_id, reviewer=reviewer,
                reason=reason, expected_version=payload.get("version", proposal["expected_version"]))
        if operation == "growth":
            if not payload:
                return {"kind": "growth", "items": runtime.growth.profile(scope)}
            _allowed(payload, {"field", "value", "version", "allow_recent"})
            proposal = runtime.growth.propose(scope, payload["field"], payload["value"], [event_id], reason=reason, request_id=request)
            return runtime.growth.approve(scope, proposal["proposal_id"], user_event_id=event_id, reviewer=reviewer,
                reason=reason, expected_version=payload.get("version", 0), request_id=request+":confirm", allow_recent=payload.get("allow_recent", False))
        if operation == "proactive":
            if not payload:
                return runtime.intentions.policy(scope)
            _allowed(payload, {"enabled", "version", "timezone", "quiet_start", "quiet_end", "min_interval_seconds", "max_per_day"})
            policy = runtime.intentions.policy(scope)
            expected = payload.get("version")
            if expected is None:
                previous = runtime.db.connection.execute(
                    "SELECT receipt_json FROM intention_operations WHERE scope_key=? AND request_id=?",
                    (_scope_key(scope), request),
                ).fetchone()
                # 沿用首次成功决定的期望版本，再交给领域层重新计算请求指纹。
                # 原命令重试只读取旧回执，不会把后来更新的策略写回旧状态。
                expected = json.loads(previous["receipt_json"])["version"]-1 if previous else policy["version"]
            return runtime.intentions.configure_policy(scope, enabled=payload["enabled"], confirmation_refs=[event_id],
                request_id=request, expected_version=expected,
                **{name: payload[name] for name in ("timezone", "quiet_start", "quiet_end", "min_interval_seconds", "max_per_day") if name in payload})
        if operation == "check":
            _allowed(payload, set())
            return runtime.continuity.check(scope)
        raise AdapterError("UNSUPPORTED", "未支持的长期角色命令")


def _allowed(payload, names):
    if set(payload)-names:
        raise AdapterError("INVALID_ARGUMENT", "命令含未知字段")
