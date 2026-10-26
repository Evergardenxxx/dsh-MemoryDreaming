"""零依赖 MCP stdio 服务：一行一个 UTF-8 JSON-RPC 消息，只开放只读工具。"""

from __future__ import annotations

import json
import sys

from . import __version__
from .llm import AdapterError, dumps, tool_catalog


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
BOOTSTRAP_URI = "memorydreaming://bootstrap"


class McpServer:
    def __init__(self, session):
        self.session = session
        self.initialized = False
        self.ready = False

    @staticmethod
    def error(request_id, code, message, data=None):
        body = {"code": code, "message": message}
        if data is not None:
            body["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": body}

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if "id" in request and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            return self.error(None, -32600, "Invalid request id")
        method, params = request["method"], request.get("params", {})
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "params must be an object")
        try:
            if method == "initialize":
                if self.initialized:
                    return self.error(request_id, -32600, "Already initialized")
                if (not isinstance(params.get("protocolVersion"), str)
                        or not isinstance(params.get("capabilities"), dict)
                        or not isinstance(params.get("clientInfo"), dict)):
                    return self.error(request_id, -32602, "Missing initialization parameters")
                version = params["protocolVersion"]
                self.initialized = True
                result = {"protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False},
                                           "resources": {"subscribe": False, "listChanged": False}},
                          "serverInfo": {"name": "memorydreaming", "version": __version__},
                          "instructions": "使用受控引用查询记忆；可读取 bootstrap 资源恢复身份资料。自动首轮及逐轮恢复需要宿主调用 HostAdapter 生命周期接口。"}
            elif method == "ping":
                result = {}
            elif not self.ready:
                return self.error(request_id, -32000, "Initialize and send notifications/initialized first")
            elif method == "tools/list":
                if params.get("cursor") is not None:
                    return self.error(request_id, -32602, "This server has no additional pages")
                result = {"tools": [{"name": tool["name"], "description": tool["description"],
                                     "inputSchema": tool["input_schema"],
                                     "annotations": {"readOnlyHint": True, "destructiveHint": False}}
                                    for tool in tool_catalog()]}
            elif method == "tools/call":
                if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments"), dict):
                    return self.error(request_id, -32602, "name and arguments are required")
                response = self.session.call_tool(params["name"], params["arguments"])
                result = {"content": [{"type": "text", "text": dumps(response)}],
                          "isError": response["status"] == "error"}
            elif method == "resources/list":
                if params.get("cursor") is not None:
                    return self.error(request_id, -32602, "This server has no additional pages")
                result = {"resources": [{"uri": BOOTSTRAP_URI, "name": "角色与关系恢复资料",
                                         "mimeType": "application/json",
                                         "description": "当前绑定的核心和允许活动目录；不是自动会话钩子"}]}
            elif method == "resources/read":
                if params.get("uri") != BOOTSTRAP_URI:
                    return self.error(request_id, -32602, "Unknown resource")
                response = self.session.bootstrap()
                result = {"contents": [{"uri": BOOTSTRAP_URI, "mimeType": "application/json", "text": dumps(response)}]}
            else:
                return self.error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except AdapterError as exc:
            return self.error(request_id, -32000, exc.message, {"code": exc.code})
        except Exception as exc:
            # 不将 SQL、文件路径、绑定身份或正文错误回显到模型。
            print(f"memorydreaming MCP failure: {type(exc).__name__}", file=sys.stderr)
            return self.error(request_id, -32603, "Memory service unavailable")

    def serve(self, input_stream=None, output_stream=None):
        source, target = input_stream or sys.stdin, output_stream or sys.stdout
        maximum = 1024 * 1024
        while True:
            line = source.readline(maximum + 1)
            if not line:
                return 0
            if len(line) > maximum:
                target.write(dumps(self.error(None, -32600, "Request exceeds size limit")) + "\n")
                target.flush()
                return 2
            if not line.strip():
                continue
            try:
                request = json.loads(line, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            except (ValueError, RecursionError):
                response = self.error(None, -32700, "Parse error")
            else:
                response = self.handle(request)
            if response is not None:
                target.write(dumps(response) + "\n")
                target.flush()


def _reject_constant(value):
    raise ValueError("Non-finite JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result
