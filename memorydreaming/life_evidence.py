"""长期角色投影共用的来源契约，不把生成叙事或梦当成事实根。

业务服务只持久保存 kind/target/revision；读取和审批时重新复核当前来源。
本模块不打开连接、启动模型或执行宿主动作。
"""

from dataclasses import asdict
import json

from .evidence import EvidenceEngine
from .errors import MemorySuppressed, ScopeNotFound
from .models import EvidenceRef, Scope


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


class LifeEvidence:
    def __init__(self, runtime):
        self.runtime = runtime

    def capture(self, scope: Scope, refs, *, require_user=False):
        """接受可信宿主签发的 EvidenceRef 或当前来源 ID，返回固定版本列表。

        require_user 用于相处偏好/成长授权：每项依据必须来自真实用户事件
        或用户自述/明确设定的记忆；角色自己说过的话不等于用户确认。
        """
        if not isinstance(scope, Scope) or not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 20:
            raise ValueError("需要绑定 scope 和 1..20 个真实来源")
        engine = EvidenceEngine(self.runtime.db.connection, scope, strict_events=True)
        result, seen = [], set()
        for raw in refs:
            if isinstance(raw, str):
                kind = "memory" if engine.memories.get(raw) is not None else "event"
                target = raw
                revision = engine.revision(kind, target)
                ref = EvidenceRef(kind, target, revision) if revision else None
            elif isinstance(raw, EvidenceRef):
                ref = raw
            elif isinstance(raw, dict) and set(raw) == {"kind", "target", "revision"}:
                ref = EvidenceRef(**raw)
            else:
                ref = None
            if ref is None or ref.kind not in ("event", "memory") or not engine.valid_ref(ref):
                raise MemorySuppressed("角色投影的来源不存在、被遗忘或版本已变化")
            obj = engine._object(ref.kind, ref.target)
            if obj is None:
                raise ScopeNotFound("来源不属于绑定范围")
            if require_user:
                if ref.kind == "event" and obj.speaker != "user":
                    raise ValueError("此操作需要用户原话，不能使用角色自述")
                if ref.kind == "memory" and (obj.source_speaker != "user"
                        or obj.epistemic_status.value not in ("user_report", "explicit_setting")):
                    raise ValueError("此操作需要用户自述或明确设定")
            key = (ref.kind, ref.target)
            if key not in seen:
                result.append(asdict(ref)); seen.add(key)
        engine.solve([(ref["kind"], ref["target"]) for ref in result])
        if any((ref["kind"], ref["target"]) not in engine.live for ref in result):
            raise MemorySuppressed("来源失去独立支持，不能用作角色投影依据")
        # 来源集合的语义不依赖调用顺序；排序防止换排列绕过拒绝/幂等签名。
        return sorted(result, key=lambda ref: (ref["kind"], ref["target"], ref["revision"]))

    def valid(self, scope, refs, *, require_user=False):
        try:
            self.capture(scope, refs, require_user=require_user)
            return True
        except (ValueError, MemorySuppressed, ScopeNotFound):
            return False
