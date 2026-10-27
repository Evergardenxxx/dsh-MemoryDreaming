"""长期角色资料的宿主编排，不在普通请求里塞入完整自传或全部承诺。"""

from .life_evidence import dumps
from .llm import EstimatedTokenCounter


def role_context(runtime, scope, *, context="default", max_tokens=600):
    """只读取已确认、当前有效的角色投影；普通输入不推断或自动批准偏好。"""
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 128 <= max_tokens <= 4096:
        raise ValueError("max_tokens 必须为 128..4096")
    with runtime.db.transaction(write=False):
        story = runtime.narrative.timeline(scope, limit=3, max_tokens=400)
        data = {"kind": "role_continuity", "autobiography": story.get("entries", story.get("items", [])),
                "intentions": [{"id": row["intention_id"], "version": row["version"], "text": row["text"],
                    "due_at": row["due_at"], "event_trigger": row["event_trigger"], "state": "open"}
                    for row in runtime.continuity._life_state(scope)["intentions"][:4]],
                "companionship": runtime.companionship.read_for_context(scope, context=context, limit=4),
                "growth": runtime.growth.profile(scope, limit=4),
                "instructions": "这些是有来源的已确认角色资料。自传是叙事投影，承诺尚未完成，表达成长不改变事实或审批权。缺失内容不得补造。",
                "truncated": bool(story.get("truncated", False))}
        counter = EstimatedTokenCounter()
        while counter.count(dumps(data)) > max_tokens:
            removed = False
            for key in ("autobiography", "intentions", "companionship", "growth"):
                if isinstance(data[key], list) and data[key]:
                    data[key].pop(); removed = True; break
            if not removed:
                data.pop("instructions", None)
                break
            data["truncated"] = True
        return data


def has_role_context(data):
    return any(data.get(key) for key in ("autobiography", "intentions", "companionship", "growth"))
