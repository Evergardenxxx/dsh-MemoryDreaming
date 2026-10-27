"""原生 Harness 插件桥接协议：无 MCP 工具目录的生命周期往返。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from memorydreaming.bridge import BridgeServer
from memorydreaming.models import MemoryType, Scope
from tests.helpers import RuntimeTestCase


class BridgeTest(RuntimeTestCase):
    def test_zero_budget_remains_zero_even_when_minimum_core_exceeds_bootstrap_limit(self):
        scope = Scope("u1", "c1", "r1")
        self.runtime.propose_core_update(scope, "identity", {"说明": "核心设定" * 2000}, "用户设定")
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1", max_tokens=128)
        request = {"id": 1, "op": "prepare_turn", "conversation_id": "full-window",
                   "message_id": "u1", "content": "保存这句话", "max_tokens": 0}
        prepared = server.handle(request)
        self.assertTrue(prepared["ok"], prepared)
        self.assertEqual(prepared["data"]["memory_status"], "skipped_budget")
        self.assertEqual(prepared["data"]["counted_tokens"], 0)
        self.assertEqual(len(prepared["data"]["messages"]), 1)
        self.assertEqual(server.handle(request)["data"]["user_event_id"], prepared["data"]["user_event_id"])
        for invalid in (False, -1):
            response = server.handle({**request, "max_tokens": invalid})
            self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")

    def test_harness_bundle_mounts_native_cordis_entry_without_mcp(self):
        root = Path(__file__).resolve().parents[1] / "integrations" / "deepseek-harness"
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        patch = (root / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertEqual(package["main"], "lib/index.js")
        self.assertTrue((root / "lib" / "index.js").is_file())
        self.assertIn("@local/memorydreaming", patch)
        self.assertNotIn("@deepseek-ai/dsh-mcp-client", patch)

    def test_prepare_and_after_delivery_roundtrip_without_tool_catalog(self):
        self.add_memory(MemoryType.SEMANTIC, {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"})
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        output = io.StringIO()
        requests = [
            {"id": 1, "op": "prepare_turn", "conversation_id": "window-1",
             "message_id": "user-1", "content": "你还记得我喜欢什么吗？"},
        ]
        self.assertEqual(server.serve(io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n"), output), 0)
        prepared = json.loads(output.getvalue())
        self.assertTrue(prepared["ok"])
        data = prepared["data"]
        self.assertTrue(data["user_event_id"])
        self.assertEqual(len(data["messages"]), 3)
        self.assertIn("memory-context", data["messages"][1]["content"])

        delivered = server.handle({
            "id": 2,
            "op": "after_delivery",
            "conversation_id": "window-1",
            "message_id": "assistant-1",
            "user_event_id": data["user_event_id"],
            "content": "我记得，你喜欢睡前听海边故事。",
            "delivery_state": "final",
        })
        self.assertTrue(delivered["ok"])
        self.assertFalse(delivered["data"]["duplicate"])

        row = self.runtime.db.connection.execute("SELECT COUNT(*) AS n FROM host_deliveries").fetchone()
        self.assertEqual(row["n"], 1)

    def test_pre_compress_roundtrip_persists_handoff_for_next_window(self):
        activity = self.runtime.register_activity(Scope("u1", "c1", "r1"), "故事", goal="第二章")
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        session = server._session("window-1")
        # BridgeServer 默认没有选择活动；显式绑定一个活动来模拟宿主的压缩回调。
        session.binding = session.binding.__class__(
            "u1", "c1", "r1", "window-1",
            allowed_activity_ids=(activity.activity_id,), selected_activity_id=activity.activity_id,
        )
        response = server.handle({
            "id": "compress-1", "op": "pre_compress", "conversation_id": "window-1",
            "state": {"goal": "第三章", "open_loops": ["灯塔结尾"]},
            "expected_version": 1, "idempotency_key": "compress-1",
        })
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["data"]["briefing"]["goal"], "第三章")
        next_window = server._session("window-2")
        next_window.binding = next_window.binding.__class__(
            "u1", "c1", "r1", "window-2",
            allowed_activity_ids=(activity.activity_id,), selected_activity_id=activity.activity_id,
        )
        next_window.opened = next_window.bootstrap()
        self.assertEqual(next_window.opened["data"]["handoff"]["briefing"]["open_loops"], ["灯塔结尾"])

    def test_bridge_rejects_unknown_operation_and_duplicate_json(self):
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        self.assertEqual(server.handle({"id": "x", "op": "unknown"})["error"]["code"], "UNSUPPORTED")
        output = io.StringIO()
        server.serve(io.StringIO('{"id":1,"op":"unknown","op":"other"}\n'), output)
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"]["code"], "PARSE_ERROR")

    def test_prefetch_bridge_op_warms_without_exposing_memory_body(self):
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        response = server.handle({
            "id": "prefetch-1", "op": "prefetch", "conversation_id": "window-1",
            "query": "海边", "detail": "auto", "max_items": 12, "max_tokens": 1200,
        })
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["cached"], True)
        self.assertNotIn("evidence", response["data"])

    def test_actual_cli_bridge_subprocess_keeps_stdout_as_jsonl(self):
        requests = [
            {"id": 1, "op": "bootstrap", "conversation_id": "cli-window"},
            {"id": 2, "op": "shutdown"},
        ]
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "memorydreaming", "--db", str(self.db_path),
             "bridge", "--user", "u1", "--character", "c1", "--relationship", "r1"],
            input="\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n",
            text=True, encoding="utf-8", capture_output=True, timeout=15,
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, "MEMORYDREAMING_JOB_MAX_ATTEMPTS": "invalid"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertTrue(replies[0]["ok"])
        self.assertTrue(replies[1]["data"]["stopped"])
        self.assertIn("不是整数", result.stderr)
