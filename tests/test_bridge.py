"""原生 Harness 插件桥接协议：无 MCP 工具目录的生命周期往返。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from memorydreaming.bridge import BridgeServer
from memorydreaming.models import MemoryType
from tests.helpers import RuntimeTestCase


class BridgeTest(RuntimeTestCase):
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

    def test_bridge_rejects_unknown_operation_and_duplicate_json(self):
        server = BridgeServer(self.runtime, user_id="u1", character_id="c1", relationship_id="r1")
        self.assertEqual(server.handle({"id": "x", "op": "unknown"})["error"]["code"], "UNSUPPORTED")
        output = io.StringIO()
        server.serve(io.StringIO('{"id":1,"op":"unknown","op":"other"}\n'), output)
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"]["code"], "PARSE_ERROR")

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
