"""面向原生聊天宿主插件的轻量 JSONL 桥接协议。

这不是 MCP：桥接进程不暴露工具目录，也不实现 JSON-RPC。原生 Harness
插件只把生命周期事件转成有界的内部操作。普通读写由 HostAdapter 负责
预算与绑定；sleep 领域服务负责逐批审阅、梦境及来源撤销。模型没有这些
内部操作的调用权限，桥接也不暴露网络监听或工具目录。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from .host import HostAdapter, MemorySession, SessionBinding
from .llm import AdapterError, text, timestamp
from .mcp import _reject_constant, _unique_object


class BridgeServer:
    """为一个固定身份绑定维护多个 conversation session。"""

    def __init__(self, runtime, *, user_id: str, character_id: str, relationship_id: str,
                 domain: str = "real", max_tokens: int = 1200, auto_prefetch=False):
        self.runtime = runtime
        self.adapter = HostAdapter(runtime, background_prefetch=auto_prefetch)
        self.user_id = text(user_id, "user_id", 200)
        self.character_id = text(character_id, "character_id", 200)
        self.relationship_id = text(relationship_id, "relationship_id", 200)
        self.domain = text(domain, "domain", 200)
        self.max_tokens = int(max_tokens)
        self.sessions: dict[str, object] = {}

    def _session(self, conversation_id: str):
        conversation_id = text(conversation_id, "conversation_id", 200)
        session = self.sessions.get(conversation_id)
        if session is None:
            binding = SessionBinding(self.user_id, self.character_id, self.relationship_id,
                                     conversation_id, domain=self.domain)
            # Lazy recovery: even an oversized core must not prevent saving the
            # real user event or a zero-budget turn. Explicit bootstrap reads fit
            # their own budget instead of running before event persistence.
            session = MemorySession(self.adapter, binding)
            self.sessions[conversation_id] = session
        return session

    @staticmethod
    def _ok(request_id, data):
        return {"id": request_id, "ok": True, "data": data}

    @staticmethod
    def _error(request_id, code, message):
        return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}

    def handle(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            return self._error(None, "INVALID_REQUEST", "op 必须为字符串")
        request_id = request.get("id")
        op = request["op"]
        try:
            if op == "prepare_turn":
                session = self._session(request["conversation_id"])
                turn = session.prepare_turn(request["message_id"], request["content"],
                                            max_tokens=request.get("max_tokens", self.max_tokens))
                if request.get("max_tokens") == 0 and self.adapter.prefetch_scheduler and len(request["content"]) <= 4000:
                    session.schedule_prefetch(request["content"], max_tokens=self.max_tokens)
                return self._ok(request_id, asdict(turn))
            if op == "after_delivery":
                session = self._session(request["conversation_id"])
                result = session.after_delivery(
                    request["message_id"], request["content"],
                    user_event_id=request["user_event_id"],
                    delivery_state=request.get("delivery_state", "final"),
                    used_references=request.get("used_references") or [],
                    expected_guard=request.get("expected_guard"),
                    occurred_at=timestamp(request["occurred_at"]) if request.get("occurred_at") else None,
                )
                return self._ok(request_id, result)
            if op == "bootstrap":
                session = self._session(request["conversation_id"])
                return self._ok(request_id, session.bootstrap(max_tokens=self.max_tokens))
            if op == "pre_compress":
                session = self._session(request["conversation_id"])
                result = session.pre_compress(
                    state=request.get("state"),
                    expected_version=request.get("expected_version"),
                    request_id=request_id,
                    idempotency_key=request.get("idempotency_key"),
                    max_tokens=request.get("max_tokens") or self.max_tokens,
                )
                return self._ok(request_id, result)
            if op == "prefetch":
                session = self._session(request["conversation_id"])
                result = session.prefetch(
                    request["query"], detail=request.get("detail", "auto"),
                    max_items=request.get("max_items", 12),
                    max_tokens=request.get("max_tokens") or self.max_tokens,
                )
                return self._ok(request_id, result)
            if op == "schedule_prefetch":
                session = self._session(request["conversation_id"])
                return self._ok(request_id, session.schedule_prefetch(request["query"],
                    max_tokens=request.get("max_tokens", self.max_tokens)))
            if op == "diagnostics":
                scope = self._session(request["conversation_id"]).binding.scope
                return self._ok(request_id, {"health": self.runtime.memory_health(scope),
                    "metrics": self.runtime.operational_trend(scope), "alerts": self.runtime.check_alerts(scope)})
            # 睡眠是可信生命周期能力，不注册为模型工具；每次请求仍使用
            # bridge 启动时的固定身份绑定，模型不能自行选择其他人的 scope。
            if op in ("sleep_prepare", "sleep_batch", "sleep_review", "sleep_complete", "wake"):
                session = self._session(request["conversation_id"])
                scope = session.binding.scope
                service = self.runtime.sleep
                if op == "sleep_prepare":
                    result = service.prepare(scope, session.binding.conversation_id, request["request_id"])
                elif op == "sleep_batch":
                    result = service.batch(scope, request["sleep_id"], request.get("cursor", 0),
                                           request_id=f"{request['sleep_id']}:{request.get('cursor', 0)}")
                elif op == "sleep_review":
                    result = service.record_review(scope, request["sleep_id"], request["cursor"],
                        notes=request["notes"], source_event_ids=request["source_event_ids"], claim_token=request["claim_token"])
                elif op == "sleep_complete":
                    result = service.complete(scope, request["sleep_id"])
                else:
                    result = service.wake(scope, session.binding.conversation_id, request["request_id"], max_tokens=self.max_tokens)
                    recovered = session.bootstrap(max_tokens=self.max_tokens)
                    data = recovered["data"]
                    result["bootstrap"] = {key: data.get(key) for key in
                        ("identity_fields", "relationship_fields", "recovery_status", "core_versions")}
                return self._ok(request_id, result)
            if op == "close_session":
                conversation_id = text(request["conversation_id"], "conversation_id", 200)
                session = self.sessions.pop(conversation_id, None)
                if session is not None:
                    session.close()
                return self._ok(request_id, {"closed": session is not None})
            if op == "shutdown":
                self.close()
                return self._ok(request_id, {"stopped": True})
            return self._error(request_id, "UNSUPPORTED", "不支持的桥接操作")
        except KeyError as exc:
            return self._error(request_id, "INVALID_ARGUMENT", f"缺少参数: {exc.args[0]}")
        except (AdapterError, ValueError, TypeError) as exc:
            message = getattr(exc, "message", str(exc))
            code = getattr(exc, "code", "INVALID_ARGUMENT")
            return self._error(request_id, code, message)
        except Exception:
            print("memorydreaming bridge failure", file=sys.stderr)
            return self._error(request_id, "UNAVAILABLE", "记忆服务暂时不可用")

    def close(self):
        for session in self.sessions.values():
            session.close()
        self.sessions.clear()
        self.adapter.close()

    def serve(self, input_stream=None, output_stream=None):
        source, target = input_stream or sys.stdin, output_stream or sys.stdout
        for line in source:
            if len(line) > 1024 * 1024:
                target.write(json.dumps(self._error(None, "REQUEST_TOO_LARGE", "请求超过大小限制"),
                                        ensure_ascii=False, separators=(",", ":")) + "\n")
                target.flush()
                return 2
            if not line.strip():
                continue
            request = None
            try:
                request = json.loads(line, parse_constant=_reject_constant,
                                     object_pairs_hook=_unique_object)
            except (ValueError, RecursionError):
                response = self._error(None, "PARSE_ERROR", "请求不是有效 JSON")
            else:
                response = self.handle(request)
            target.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            target.flush()
            if isinstance(request, dict) and request.get("op") == "shutdown":
                return 0
        self.close()
        return 0
