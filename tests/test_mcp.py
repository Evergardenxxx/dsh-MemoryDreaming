"""MCP 进程通信：初始化、工具往返、资源、协议错误与 stdout 洁净性。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.llm import dumps
from memorydreaming.mcp import BOOTSTRAP_URI, McpServer
from memorydreaming.models import MemoryType, Scope
from tests.helpers import RuntimeTestCase


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


INITIALIZE = request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test-client", "version": "1"}})
READY = {"jsonrpc": "2.0", "method": "notifications/initialized"}
SEARCH = {"query": "海边", "activity_ref": None, "time_range": None,
          "detail": "evidence", "max_items": 4, "max_tokens": 4096}


class McpTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope("u1", "c1", "r1")
        self.runtime.propose_core_update(self.scope, "identity", {"名字": "小梦"}, "用户设定")
        self.runtime.propose_core_update(self.scope, "relationship", {"称呼": "爸爸"}, "用户设定")
        session = HostAdapter(self.runtime, clock=self.clock).open_session(SessionBinding("u1", "c1", "r1", "mcp-test"))
        self.server = McpServer(session)

    def start(self):
        self.assertIn("result", self.server.handle(INITIALIZE))
        self.assertIsNone(self.server.handle(READY))

    def test_initialization_and_readonly_tool_catalog(self):
        self.assertIn("error", self.server.handle(request("tools/list")))
        self.start()
        names = [tool["name"] for tool in self.server.handle(request("tools/list"))["result"]["tools"]]
        self.assertEqual(names, ["memory_search", "memory_read"])
        self.assertIn("error", self.server.handle(INITIALIZE))

    def test_tool_search_read_roundtrip_without_exposing_database_ids(self):
        item = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定"})
        self.start()
        found = self.server.handle(request("tools/call", {"name": "memory_search", "arguments": SEARCH}))
        body = json.loads(found["result"]["content"][0]["text"])
        ref = body["data"]["evidence"][0]["ref"]
        read = self.server.handle(request("tools/call", {"name": "memory_read", "arguments": {"references": [ref], "max_tokens": 4096}}))
        self.assertIn("海边约定", dumps(read))
        self.assertNotIn(item.memory_id, dumps(found))
        self.assertFalse(read["result"]["isError"])

    def test_resource_contains_identity_and_declares_lifecycle_requirement(self):
        self.start()
        listing = self.server.handle(request("resources/list"))["result"]["resources"]
        self.assertEqual(listing[0]["uri"], BOOTSTRAP_URI)
        response = self.server.handle(request("resources/read", {"uri": BOOTSTRAP_URI}))
        restored = json.loads(response["result"]["contents"][0]["text"])
        self.assertEqual(restored["data"]["identity_fields"][0]["value"], "小梦")
        self.assertIn("error", self.server.handle(request("resources/read", {"uri": "file:///private"})))

    def test_bad_arguments_and_writing_tool_produce_tool_errors(self):
        self.start()
        denied = self.server.handle(request("tools/call", {"name": "memory_search", "arguments": {**SEARCH, "user_id": "u2"}}))
        self.assertTrue(denied["result"]["isError"])
        body = json.loads(denied["result"]["content"][0]["text"])
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
        unsupported = self.server.handle(request("tools/call", {"name": "memory_request_change", "arguments": {}}))
        self.assertTrue(unsupported["result"]["isError"])

    def test_stream_parse_errors_notifications_and_unknown_methods_do_not_break_later_requests(self):
        lines = ["not json", '{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"ping"}',
                 '{"jsonrpc":"2.0","id":NaN,"method":"ping"}', dumps(INITIALIZE), dumps(READY),
                 dumps(request("unknown", request_id=3)), dumps(request("ping", request_id=4))]
        output = io.StringIO()
        self.assertEqual(self.server.serve(io.StringIO("\n".join(lines) + "\n"), output), 0)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 6)
        self.assertTrue(all(reply["error"]["code"] == -32700 for reply in replies[:3]))
        self.assertEqual(replies[-1]["result"], {})

    def test_oversized_message_is_bounded(self):
        output = io.StringIO()
        self.assertEqual(self.server.serve(io.StringIO("x" * (1024 * 1024 + 1)), output), 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32600)

    def test_actual_cli_subprocess_negotiates_and_restores_persistent_memory(self):
        self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边跨进程约定"})
        messages = [INITIALIZE, READY, request("tools/list", request_id=2),
                    request("resources/read", {"uri": BOOTSTRAP_URI}, 3),
                    request("tools/call", {"name": "memory_search", "arguments": SEARCH}, 4)]
        # 环境变量诊断必须进入 stderr，不能污染 JSON-RPC stdout。
        env = {**os.environ, "MEMORYDREAMING_JOB_MAX_ATTEMPTS": "invalid"}
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "memorydreaming", "--db", str(self.db_path),
             "mcp", "--user", "u1", "--character", "c1", "--relationship", "r1"],
            input="\n".join(dumps(message) for message in messages) + "\n", text=True, encoding="utf-8",
            capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[1], env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        self.assertIn("小梦", replies[2]["result"]["contents"][0]["text"])
        self.assertIn("跨进程约定", replies[3]["result"]["content"][0]["text"])
        self.assertIn("不是整数", result.stderr)

    def test_source_runner_starts_from_an_unrelated_working_directory(self):
        runner = Path(__file__).resolve().parents[1] / "examples/run_mcp.py"
        messages = [INITIALIZE, READY, request("resources/read", {"uri": BOOTSTRAP_URI}, 3)]
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(runner), "--db", str(self.db_path), "mcp",
             "--user", "u1", "--character", "c1", "--relationship", "r1"],
            input="\n".join(dumps(message) for message in messages) + "\n", text=True, encoding="utf-8",
            capture_output=True, timeout=15, cwd=self.db_path.parent,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn("爸爸", replies[-1]["result"]["contents"][0]["text"])
