"""Native supervisor: real subprocess termination, recovery and deadline tests."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BridgeSupervisorTest(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 不在 PATH 中")
        with tempfile.TemporaryDirectory() as directory:
            config = {"repoPath": str(ROOT), "dbPath": str(Path(directory) / "supervisor.db"),
                      "autoConsolidation": False, "bridgeTimeoutMs": 1000, "bridgeShutdownMs": 200}
            source = '''
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { BridgeClient } from '%ENTRY%';
const config = %CONFIG%;
const worker = fileURLToPath('%WORKER%');
const commitWorker = fileURLToPath('%COMMIT_WORKER%');
const clients = [];
function fakeClient(mode, extra = {}) {
  let spawns = 0;
  const client = new BridgeClient({ ...config, ...extra }, {}, { spawnProcess() {
    return spawn(process.execPath, [worker, spawns++ === 0 ? mode : 'echo'],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  } });
  clients.push(client);
  return client;
}
try { %SCRIPT% } finally { await Promise.all(clients.map(client => client.close())); }
'''.replace("%ENTRY%", (ROOT / "integrations/deepseek-harness/lib/bridge-client.js").as_uri()).replace(
                "%WORKER%", (ROOT / "tests/fixtures/bridge-worker.mjs").as_uri()).replace(
                "%COMMIT_WORKER%", (ROOT / "tests/fixtures/bridge-commit-crash.py").as_uri()).replace(
                "%CONFIG%", json.dumps(config, ensure_ascii=False)).replace("%SCRIPT%", script)
            result = subprocess.run([node, "--input-type=module", "-e", source], cwd=ROOT,
                                    text=True, encoding="utf-8", capture_output=True, timeout=20,
                                    env={**os.environ, "PYTHONUTF8": "1", "MEMORYDREAMING_EMBEDDING_PROVIDER": "hash"})
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_already_aborted_request_never_starts_worker(self):
        self.run_js('''
const client = fakeClient('echo');
const controller = new AbortController(); controller.abort();
await assert.rejects(client.request('bootstrap', {}, controller.signal), { code: 'ABORTED' });
assert.equal(client.child, null);
assert.equal(client.pending.size, 0);
''')

    def test_cancel_kills_shared_hung_process_and_next_request_restarts(self):
        self.run_js('''
const client = fakeClient('hang');
const controller = new AbortController();
const a = client.request('prepare_turn', {}, controller.signal);
const b = client.request('bootstrap', {});
const results = Promise.allSettled([a, b]);
await delay(100);
const record = client.active;
controller.abort();
const [one, two] = await results;
assert.equal(one.reason.code, 'ABORTED');
assert.equal(two.reason.code, 'BRIDGE_RESTARTED');
assert.equal(client.pending.size, 0);
assert.equal((await client.request('bootstrap', {})).op, 'bootstrap');
await record.exited;
assert.equal(record.child.exitCode !== null || record.child.signalCode !== null, true);
''')

    def test_request_timeout_terminates_worker_and_cleans_pending(self):
        self.run_js('''
const client = fakeClient('hang', { bridgeTimeoutMs: 150 });
await assert.rejects(client.request('prepare_turn', {}), { code: 'BRIDGE_TIMEOUT' });
assert.equal(client.pending.size, 0);
assert.equal((await client.request('bootstrap', {})).op, 'bootstrap');
''')

    def test_normal_exit_without_response_rejects_instead_of_hanging(self):
        self.run_js('''
const client = fakeClient('exit-zero');
await assert.rejects(client.request('prepare_turn', {}), { code: 'BRIDGE_EXITED' });
assert.equal((await client.request('bootstrap', {})).op, 'bootstrap');
''')

    def test_partial_line_from_dead_worker_cannot_poison_replacement(self):
        self.run_js('''
const client = fakeClient('partial-exit');
await assert.rejects(client.request('prepare_turn', {}), { code: 'BRIDGE_EXITED' });
assert.equal((await client.request('bootstrap', {})).op, 'bootstrap');
assert.equal(client.active.buffer, '');
''')

    def test_invalid_and_oversized_output_terminate_worker(self):
        self.run_js('''
for (const mode of ['invalid', 'invalid-shape', 'oversized']) {
  const client = fakeClient(mode);
  await assert.rejects(client.request('bootstrap', {}), { code: 'BRIDGE_PROTOCOL_ERROR' });
  assert.equal(client.pending.size, 0);
  assert.equal((await client.request('bootstrap', {})).op, 'bootstrap');
}
''')

    def test_disposal_has_deadline_closes_even_unused_client_and_removes_exit_hook(self):
        self.run_js('''
const baseline = process.listenerCount('exit');
const client = fakeClient('hang');
const pending = client.request('prepare_turn', {});
const settled = Promise.allSettled([pending]);
await delay(50);
await client.close();
assert.equal((await settled)[0].status, 'rejected');
assert.equal(client.pending.size, 0);
await assert.rejects(client.request('bootstrap', {}), { code: 'BRIDGE_CLOSED' });
const unused = fakeClient('echo');
await unused.close();
await assert.rejects(unused.request('bootstrap', {}), { code: 'BRIDGE_CLOSED' });
assert.equal(process.listenerCount('exit'), baseline);
await client.close();
''')

    def test_stale_stdout_and_old_exit_cannot_resolve_new_request(self):
        self.run_js('''
const client = fakeClient('hang');
const old = client.request('bootstrap', {});
const result = Promise.allSettled([old]);
await delay(50);
const record = client.active;
client.retire(record, Object.assign(new Error('test reset'), { code: 'BRIDGE_RESTARTED' }));
await result;
const fresh = client.request('bootstrap', {});
while (!client.active) await delay(1);
const entry = [...client.pending.values()][0];
client.onStdout(record, JSON.stringify({ id: entry.id, ok: true, data: { op: 'poison' } }) + '\\n');
record.child.emit('exit', 0, null);
assert.equal((await fresh).op, 'bootstrap');
''')

    def test_pending_and_input_limits_do_not_terminate_valid_worker(self):
        self.run_js('''
const client = fakeClient('hang', { bridgeMaxPending: 1 });
const controller = new AbortController();
const pending = client.request('bootstrap', {}, controller.signal);
const result = Promise.allSettled([pending]);
await assert.rejects(client.request('bootstrap', {}), { code: 'BRIDGE_BUSY' });
controller.abort(); await result;
await assert.rejects(client.request('bootstrap', { content: '字'.repeat(400000) }), { code: 'REQUEST_TOO_LARGE' });
''')

    def test_actual_bridge_crash_then_replay_keeps_durable_event_id(self):
        self.run_js('''
const client = new BridgeClient(config, {}); clients.push(client);
const request = { conversation_id: 'restart', message_id: 'u1', content: '保存的用户消息', max_tokens: 0 };
const first = await client.request('prepare_turn', request);
const record = client.active;
record.child.kill('SIGKILL');
await record.exited;
const second = await client.request('prepare_turn', request);
assert.equal(first.user_event_id, second.user_event_id);
await client.request('after_delivery', { conversation_id: 'restart', message_id: 'a1',
  content: '实际中断回复', user_event_id: first.user_event_id, delivery_state: 'interrupted' });
''')

    def test_commit_without_receipt_is_retried_with_same_event_and_delivery_ids(self):
        self.run_js('''
let count = 0;
const client = new BridgeClient(config, {}, { spawnProcess(python, args, options) {
  count++;
  if (count === 1 || count === 3) return spawn(python, ['-X', 'utf8', commitWorker, config.dbPath],
    { ...options, env: { ...options.env, PYTHONPATH: config.repoPath } });
  return spawn(python, args, options);
} });
clients.push(client);
const request = { conversation_id: 'uncertain', message_id: 'u1', content: '提交成功但回执丢失', max_tokens: 0 };
await assert.rejects(client.request('prepare_turn', request), { code: 'BRIDGE_EXITED' });
const stored = await client.request('prepare_turn', request);
const record = client.active;
record.child.kill('SIGKILL'); await record.exited;
const delivery = { conversation_id: 'uncertain', message_id: 'a1', content: '已投递的回答',
  user_event_id: stored.user_event_id, delivery_state: 'interrupted' };
await assert.rejects(client.request('after_delivery', delivery), { code: 'BRIDGE_EXITED' });
const retry = await client.request('after_delivery', delivery);
assert.equal(retry.duplicate, true);
assert.equal((await client.request('prepare_turn', request)).user_event_id, stored.user_event_id);
''')

    def test_missing_python_executable_rejects_without_unhandled_pipe_error(self):
        self.run_js('''
const client = new BridgeClient({ ...config, pythonCommand: 'missing-memorydreaming-executable-12345' }, {});
clients.push(client);
await assert.rejects(client.request('bootstrap', { conversation_id: 'missing' }), { code: 'BRIDGE_START_FAILED' });
assert.equal(client.pending.size, 0);
''')
