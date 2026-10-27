"""DeepSeek Harness 原生插件的真实 JSONL bridge 生命周期验收。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeepSeekHarnessPluginTest(unittest.TestCase):
    def test_native_embedding_config_reaches_bridge_and_reports_fallback(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 不在 PATH 中")
        root = Path(__file__).resolve().parents[1]
        entry = (root / "integrations" / "deepseek-harness" / "lib" / "index.js").as_uri()
        with tempfile.TemporaryDirectory() as temp:
            config = {"repoPath": str(root), "dbPath": str(Path(temp) / "native.db"), "maxTokens": 4096,
                      "autoConsolidation": False, "embeddingProvider": "sentence-transformers",
                      "embeddingModelPath": str(Path(temp) / "not-downloaded"), "embeddingRevision": "fixed-v1",
                      "embeddingDim": 512, "embeddingQueryPrefix": "检索：", "vectorMinScore": 0.65}
            script = r'''
import { apply } from "%ENTRY%";
const handlers = new Map();
const ctx = { logger: { debug() {}, warn() {} }, on(event, handler) { handlers.set(event, handler); } };
apply(ctx, %CONFIG%);
try {
  const session = { id: 'semantic-window' };
  await handlers.get('session/created')(session);
  await handlers.get('session/event')(session, { type: 'turn/start', data: { turn: 't1' } });
  await handlers.get('session/event')(session, { type: 'user/message', seq: 1,
    data: { id: 'u1', source: { kind: 'user' }, content: '以前海边散步的故事' } });
  const result = await handlers.get('agent/pre-step')(
    { agent: { session }, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [{ role: 'user', content: '以前海边散步的故事' }] }),
  );
  const content = result.messages.find(m => typeof m.content === 'string' && m.content.startsWith('<memory-context')).content;
  const payload = JSON.parse(content.split('\n')[1]);
  if (payload.search.retrieval_complete !== false) throw new Error('missing model must report partial');
  if (!payload.search.degraded_reasons.includes('EMBEDDING_UNAVAILABLE')) throw new Error('embedding config not forwarded');
  console.log(JSON.stringify({ ok: true }));
} finally {
  await handlers.get('dispose')();
}
'''.replace("%ENTRY%", entry).replace("%CONFIG%", json.dumps(config, ensure_ascii=False))
            result = subprocess.run([node, "--input-type=module", "-e", script], cwd=root, text=True,
                                    encoding="utf-8", capture_output=True, timeout=30,
                                    env={**os.environ, "MEMORYDREAMING_EMBEDDING_PROVIDER": "hash", "PYTHONUTF8": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])

    def test_native_lifecycle_starts_bridge_and_closes_session(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 不在 PATH 中")
        root = Path(__file__).resolve().parents[1]
        entry = (root / "integrations" / "deepseek-harness" / "lib" / "index.js").as_uri()
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "harness.db").replace("\\", "/")
            repo_path = str(root).replace("\\", "/")
            script = r"""
import { apply, inject, name } from "%ENTRY%";
const handlers = new Map();
const ctx = {
  logger: { debug() {}, warn() {} },
  on(event, handler) { handlers.set(event, handler); },
};
apply(ctx, { repoPath: "%REPO%", dbPath: "%DB%", maxTokens: 99999, autoConsolidation: false });
if (name !== 'memorydreaming' || inject.length !== 0) throw new Error('plugin exposes tools');
const session = { id: 'native-window' };
await handlers.get('session/created')(session);
await handlers.get('session/event')(session, { type: 'turn/start', data: { turn: 'turn-1' } });
await handlers.get('session/event')(session, { type: 'user/message', seq: 1,
  data: { id: 'user-1', source: { kind: 'user' }, content: '我喜欢海边故事。' } });
const decision = await handlers.get('agent/pre-step')(
  { agent: { session }, signal: new AbortController().signal },
  async () => ({ kind: 'enter', messages: [{ role: 'user', content: '我喜欢海边故事。' }] }),
);
if (!Array.isArray(decision.messages)) throw new Error('prepare_turn did not return messages');
await handlers.get('session/event')(session, { type: 'assistant/message',
  data: { id: 'assistant-1', content: '我记住了。' } });
handlers.get('agent/turn-stopping')({ agent: { session }, turn: 'turn-1', signal: new AbortController().signal });
await new Promise((resolve) => setTimeout(resolve, 250));
await handlers.get('agent/context-compressing')({ agent: { session }, state: { goal: '继续' }, signal: new AbortController().signal });
await handlers.get('session/disposed')(session);
await handlers.get('dispose')();
console.log(JSON.stringify({ ok: true, messageCount: decision.messages.length }));
""".replace('%ENTRY%', entry).replace('%REPO%', repo_path).replace('%DB%', db_path)
            result = subprocess.run(
                [node, "--input-type=module", "-e", script],
                cwd=root, text=True, encoding="utf-8", capture_output=True,
                env={**os.environ, "PYTHONUTF8": "1"}, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            self.assertTrue(lines, result.stdout)
            self.assertEqual(json.loads(lines[-1])["ok"], True)
            self.assertTrue((Path(temp) / "harness.db").exists())


if __name__ == "__main__":
    unittest.main()
