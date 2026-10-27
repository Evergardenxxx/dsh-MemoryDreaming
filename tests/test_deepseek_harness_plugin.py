"""Native admission order with a real Python JSONL bridge; no provider API calls."""

from __future__ import annotations

from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeepSeekHarnessPluginTest(unittest.TestCase):
    def run_plugin(self, script, config=None, inspect_database=None):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 不在 PATH 中")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.db"
            options = {"repoPath": str(ROOT), "dbPath": str(path), "autoConsolidation": False,
                       "activationMode": "always", **(config or {})}
            source = '''
import assert from 'node:assert/strict';
import { apply, name, inject, __testing } from '%ENTRY%';
import { harness } from '%FIXTURE%';
const h = harness();
const config = %CONFIG%;
apply(h.ctx, config);
try { %SCRIPT% } finally { await h.handlers.get('dispose')(); }
'''.replace("%ENTRY%", (ROOT / "integrations/deepseek-harness/lib/index.js").as_uri()).replace(
                "%FIXTURE%", (ROOT / "tests/fixtures/native-harness.mjs").as_uri()).replace(
                "%CONFIG%", json.dumps(options, ensure_ascii=False)).replace("%SCRIPT%", script)
            result = subprocess.run([node, "--input-type=module", "-e", source], cwd=ROOT,
                                    text=True, encoding="utf-8", capture_output=True, timeout=30,
                                    env={**os.environ, "MEMORYDREAMING_EMBEDDING_PROVIDER": "hash", "PYTHONUTF8": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            with closing(sqlite3.connect(path)) as connection:
                initialized = connection.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone() is not None
                events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] if initialized else 0
                deliveries = connection.execute("SELECT COUNT(*) FROM host_deliveries").fetchone()[0] if initialized else 0
                if inspect_database is not None:
                    inspect_database(connection)
            return events, deliveries

    def test_native_embedding_config_reaches_bridge_and_reports_fallback(self):
        self.run_plugin(r'''
const s = h.session('semantic-window');
await h.step(s, '以前海边散步的故事', 'u1');
const m = s.deriveMessages().find(m => m.source.kind === name);
const context = m.content[0].text.split('<memory-context')[1];
const data = JSON.parse(context.split('\n')[1]);
assert.equal(data.search.retrieval_complete, false);
assert.ok(data.search.degraded_reasons.includes('EMBEDDING_UNAVAILABLE'));
''', {"maxTokens": 4096, "embeddingProvider": "sentence-transformers", "embeddingModelPath": "missing-local-model",
      "embeddingRevision": "fixed-v1", "embeddingDim": 512, "embeddingQueryPrefix": "检索：", "vectorMinScore": 0.65})

    def test_native_lifecycle_replaces_memory_and_saves_delivered_reply(self):
        events, deliveries = self.run_plugin(r'''
assert.equal(name, 'memorydreaming');
assert.deepEqual(inject, []);
const s = h.session('native-window');
await h.handlers.get('session/created')(s);
await h.handlers.get('session/event')(s, { type: 'turn/start', data: { turn: 't1' } });
const request = await h.step(s, '我喜欢海边故事。', 'u1');
assert.equal((await h.dispatch(request)).called, true);
assert.equal(request.messages.filter(m => m.source.kind === name).length, 1);
assert.equal(request.messages.filter(m => m.source.kind === 'user').length, 1);
assert.equal(request.messages[0].role, 'system');
assert.equal(request.messages[1].source.kind, name);
await h.step(s, null, null);
await h.step(s, '还记得吗？', 'u2');
assert.equal(s.deriveMessages().filter(m => m.source.kind === name).length, 1);
await h.handlers.get('session/event')(s, { type: 'assistant/message',
  data: { message: { id: 'a1', content: [{ type: 'text', text: '我记住了。' }] } } });
h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 't1', signal: new AbortController().signal });
await h.handlers.get('agent/context-compressing')({ agent: { session: s }, signal: new AbortController().signal });
await h.handlers.get('session/disposed')(s);
''', {"maxTokens": 99999})
        self.assertEqual(events, 3)
        self.assertEqual(deliveries, 1)

    def test_tools_and_output_growth_remove_memory_and_veto_oversized_request(self):
        events, deliveries = self.run_plugin(r'''
const s = h.session('budget-window');
let request = await h.step(s, '我喜欢海边故事。', 'u1');
assert.ok(s.deriveMessages().find(m => m.source.kind === name).content.length);
await h.dispatch(request);
const tools = [{ name: 'large-tool', description: 'x'.repeat(50000), parameters: {} }];
request = await h.step(s, null, null, { tools });
assert.equal(s.deriveMessages().find(m => m.source.kind === name).content.length, 0);
assert.equal(h.budgets.at(-1).memory_status, 'skipped_budget');
let dispatched = false;
await assert.rejects(() => h.dispatch(request, () => { dispatched = true }), { code: 'CONTEXT_WINDOW_EXCEEDED' });
assert.equal(dispatched, false);
request = await h.step(s, null, null);
assert.ok(s.deriveMessages().find(m => m.source.kind === name).content.length);
await assert.rejects(() => h.dispatch({ ...request, maxTokens: 20000 }), { code: 'CONTEXT_WINDOW_EXCEEDED' });
''', {"contextWindow": 12000, "maxTokens": 1200})
        self.assertEqual(events, 1)
        self.assertEqual(deliveries, 0)

    def test_no_remaining_budget_still_saves_user_and_delivery(self):
        events, deliveries = self.run_plugin(r'''
const s = h.session('full-window');
await h.step(s, '真实用户消息仍然保存', 'u1');
assert.equal(s.deriveMessages().filter(m => m.source.kind === name).length, 0);
assert.equal(h.budgets.at(-1).memory_status, 'skipped_budget');
await h.handlers.get('session/event')(s, { type: 'assistant/message', data: { id: 'a1', content: '已显示的回复' } });
h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 't1', signal: new AbortController().signal });
''', {"contextWindow": 8200})
        self.assertEqual(events, 2)
        self.assertEqual(deliveries, 1)

    def test_unknown_limits_skip_memory_and_do_not_claim_verified_budget(self):
        events, _ = self.run_plugin(r'''
h.ctx.get = () => undefined;
const s = h.session('unknown-window');
const request = await h.step(s, '你好', 'u1');
assert.equal(h.budgets.at(-1).status, 'unavailable');
assert.equal(s.deriveMessages().filter(m => m.source.kind === name).length, 0);
s.setContext(undefined);
assert.equal((await h.dispatch(request)).called, true);
assert.equal(h.budgets.at(-1).status, 'unavailable');
''')
        self.assertEqual(events, 1)

    def test_native_envelope_keeps_memory_under_hard_cap(self):
        self.run_plugin(r'''
const s = h.session('cap-window');
await h.step(s, '海边故事', 'u1');
const owned = s.deriveMessages().filter(m => m.source.kind === name);
assert.ok(owned.length);
assert.ok(h.budgets.at(-1).total_tokens <= 131072);
const json = JSON.stringify(owned);
const chinese = (json.match(/[\u4e00-\u9fff]/gu) ?? []).length;
assert.ok(Math.floor(chinese + (Array.from(json).length - chinese) / 4) + 1 <= 1200);
''')

    def test_auxiliary_requests_with_same_session_id_are_not_intercepted(self):
        self.run_plugin(r'''
const s = h.session('auxiliary-window');
const request = await h.step(s, '你好', 'u1');
const count = h.budgets.length;
const auxiliary = { ...request, signal: new AbortController().signal, maxTokens: 9999999 };
assert.equal((await h.dispatch(auxiliary)).called, true);
assert.equal(h.budgets.length, count);
''')

    def test_final_bound_metadata_change_is_rechecked(self):
        self.run_plugin(r'''
const s = h.session('changed-model');
const request = await h.step(s, '你好', 'u1');
s.setContext({ provider: request.provider, model: request.model, contextWindow: 8200 });
await assert.rejects(() => h.dispatch(request), { code: 'CONTEXT_WINDOW_EXCEEDED' });
''')

    def test_missing_surface_api_preserves_event_and_reports_unavailable(self):
        events, _ = self.run_plugin(r'''
const s = h.session('old-host');
delete s.surface;
await h.step(s, '旧宿主上仍保存这句话', 'u1');
assert.equal(h.budgets.at(-1).memory_status, 'skipped_unavailable');
assert.equal(h.budgets.at(-1).reason, 'REQUEST_PREVIEW_UNAVAILABLE');
''')
        self.assertEqual(events, 1)

    def test_minimum_core_does_not_fit_then_budget_can_be_restored(self):
        events, _ = self.run_plugin(r'''
const s = h.session('minimum-core');
await h.step(s, '海边故事', 'u1');
assert.equal(h.budgets.at(-1).memory_status, 'skipped_minimum_core');
assert.equal(s.deriveMessages().filter(m => m.source.kind === name).length, 0);
config.maxTokens = 1200;
await h.step(s, null, null);
assert.ok(h.budgets.at(-1).memory_status.startsWith('included'));
assert.equal(s.deriveMessages().filter(m => m.source.kind === 'user').length, 1);
''', {"maxTokens": 128})
        self.assertEqual(events, 1)

    def test_multiple_queued_user_inputs_are_each_persisted(self):
        events, _ = self.run_plugin(r'''
const s = h.session('queued-inputs');
const second = { role: 'user', id: 'u2', source: { kind: 'user' }, content: [{ type: 'text', text: '第二句话' }] };
await h.step(s, '第一句话', 'u1', { pending: [second] });
assert.equal(s.deriveMessages().filter(m => m.source.kind === 'user').length, 2);
await h.handlers.get('session/event')(s, { type: 'assistant/message', data: { id: 'a1', content: '回应第二句话' } });
h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 't1', signal: new AbortController().signal });
''')
        self.assertEqual(events, 3)

    def test_native_target_request_counter_is_used_at_final_dispatch(self):
        self.run_plugin(r'''
config.requestTokenCounter = { mode: 'exact', count(input) {
  // Deterministic fake, NOT a DeepSeek tokenizer.
  return Buffer.byteLength(JSON.stringify(input), 'utf8');
} };
const s = h.session('exact-counter');
const request = await h.step(s, '海边故事', 'u1');
await h.dispatch(request);
assert.equal(h.budgets.at(-1).count_mode, 'exact');
assert.ok(h.budgets.at(-1).total_tokens <= 131072);
''', {"maxTokens": 4096})

    def test_final_guard_checks_actual_retained_memory_cap(self):
        self.run_plugin(r'''
const s = h.session('changed-memory');
const request = await h.step(s, '你好', 'u1');
const changed = { ...request, messages: request.messages.map(m => m.source.kind === name
  ? { ...m, content: [{ type: 'text', text: '字'.repeat(5000) }] } : m) };
await assert.rejects(() => h.dispatch(changed), { code: 'MEMORY_BUDGET_EXCEEDED' });
// Even if bookkeeping says skipped, actual retained memory must not bypass unknown limits.
s.setContext(undefined);
await assert.rejects(() => h.dispatch(request), { code: 'REQUEST_BUDGET_UNAVAILABLE' });
''')

    def test_cancelled_turn_saves_committed_visible_prefix_once(self):
        def check(connection):
            row = connection.execute("SELECT delivery_state FROM host_deliveries").fetchone()
            self.assertEqual(row[0], "interrupted")
            self.assertEqual(connection.execute("SELECT content FROM events WHERE speaker='character'").fetchone()[0], "已显示的半句话")

        events, deliveries = self.run_plugin(r'''
const s = h.session('cancelled');
const controller = new AbortController();
await h.step(s, '讲个故事', 'u1', { signal: controller.signal, turn: 1 });
controller.abort();
await h.handlers.get('session/event')(s, { type: 'assistant/message', data: { turn: 1, interrupted: true,
  message: { id: 'partial-1', content: [{ type: 'reasoning', text: '不可保存的推理' }, { type: 'text', text: '已显示的半句话' }] } } });
await h.handlers.get('session/event')(s, { type: 'turn/end', data: { turn: 1, reason: { kind: 'aborted' } } });
await h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 1, signal: controller.signal });
await h.handlers.get('session/disposed')(s);
''', inspect_database=check)
        self.assertEqual((events, deliveries), (2, 1))

    def test_old_assistant_turn_cannot_attach_to_new_user(self):
        def check(connection):
            row = connection.execute("SELECT e.content,u.content FROM host_deliveries d JOIN events e ON e.event_id=d.event_id JOIN events u ON u.event_id=d.user_event_id").fetchone()
            self.assertEqual(row, ("新回合回复", "新问题"))

        events, deliveries = self.run_plugin(r'''
const s = h.session('turn-fence');
await h.step(s, '旧问题', 'u1', { turn: 1 });
await h.handlers.get('session/event')(s, { type: 'turn/start', data: { turn: 2 } });
await h.step(s, '新问题', 'u2', { turn: 2 });
await h.handlers.get('session/event')(s, { type: 'assistant/message', data: { turn: 1, id: 'old', content: '迟到的旧回复' } });
await h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 1 });
await h.handlers.get('session/event')(s, { type: 'assistant/message', data: { turn: 2, id: 'new', content: '新回合回复' } });
await h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 2 });
''', inspect_database=check)
        self.assertEqual((events, deliveries), (3, 1))

    def test_late_metadata_failure_does_not_clear_new_turn_memory(self):
        self.run_plugin(r'''
const s = h.session('late-metadata');
await h.step(s, '旧问题', 'u1', { turn: 1 });
const original = h.ctx.get;
let rejectOld, entered;
const started = new Promise(resolve => { entered = resolve });
h.ctx.get = () => ({ resolveModelInfo() { entered(); return new Promise((_, reject) => { rejectOld = reject }) } });
const old = h.step(s, null, null, { turn: 1 });
await started;
h.ctx.get = original;
await h.handlers.get('session/event')(s, { type: 'turn/start', data: { turn: 2 } });
await h.step(s, '新问题', 'u2', { turn: 2 });
const current = s.deriveMessages().find(m => m.source.kind === name).content[0].text;
const count = h.budgets.length;
rejectOld(new Error('old lookup failure'));
await old;
assert.equal(s.deriveMessages().find(m => m.source.kind === name).content[0].text, current);
assert.equal(h.budgets.length, count);
''')

    def test_cancel_or_dispose_during_lookup_cannot_install_late_memory(self):
        self.run_plugin(r'''
for (const disposeSession of [false, true]) {
  const s = h.session(`lookup-${disposeSession}`);
  const controller = new AbortController();
  let entered;
  const started = new Promise(resolve => { entered = resolve });
  h.ctx.get = () => ({ resolveModelInfo() { entered(); return new Promise(() => {}) } });
  const pending = h.step(s, '先保存输入', 'u1', { signal: controller.signal });
  await started;
  if (disposeSession) await h.handlers.get('session/disposed')(s);
  else controller.abort();
  await pending;
  assert.equal(s.deriveMessages().filter(m => m.source.kind === name).length, 0);
}
''')

    def test_metadata_lookup_deadline_preserves_user_and_returns_degraded(self):
        events, _ = self.run_plugin(r'''
const s = h.session('metadata-timeout');
h.ctx.get = () => ({ resolveModelInfo() { return new Promise(() => {}) } });
await h.step(s, '不能挂起的用户输入', 'u1');
assert.equal(h.budgets.at(-1).memory_status, 'skipped_unavailable');
assert.equal(s.deriveMessages().filter(m => m.source.kind === name).length, 0);
''', {"bridgeTimeoutMs": 500})
        self.assertEqual(events, 1)

    def test_hot_reload_rebinds_retained_user_and_replaces_existing_memory(self):
        events, deliveries = self.run_plugin(r'''
const s = h.session('reload');
await h.step(s, '还记得我吗', 'u1');
await h.handlers.get('dispose')();
apply(h.ctx, config);
await h.handlers.get('session/created')(s);
await h.step(s, null, null);
const memory = s.deriveMessages().filter(m => m.source.kind === name);
assert.equal(memory.length, 1);
assert.ok(memory[0].content.length);
await h.handlers.get('session/event')(s, { type: 'assistant/message', data: { id: 'a1', content: '重载后的回复' } });
await h.handlers.get('agent/turn-stopping')({ agent: { session: s }, turn: 't1' });
''')
        self.assertEqual((events, deliveries), (2, 1))

    def test_compaction_notification_does_not_convert_summary_to_user_fact(self):
        events, _ = self.run_plugin(r'''
const s = h.session('compacting');
const request = await h.step(s, '实际用户消息', 'u1');
await h.handlers.get('session/event')(s, { type: 'compaction/start', seq: 123, data: { compactionId: 'cp1' } });
// The actual summarizer may reuse the turn signal; skip the conversation guard.
assert.equal((await h.dispatch({ ...request, maxTokens: 9999999 })).called, true);
await h.handlers.get('session/event')(s, { type: 'user/message', data: { id: 'summary', role: 'user',
  source: { kind: 'compaction-checkpoint' }, content: [{ type: 'text', text: '模型生成的压缩摘要' }] } });
await h.handlers.get('session/event')(s, { type: 'compaction/end', data: { compactionId: 'cp1' } });
await assert.rejects(() => h.dispatch({ ...request, maxTokens: 9999999 }), { code: 'CONTEXT_WINDOW_EXCEEDED' });
''')
        self.assertEqual(events, 1)

    def test_repeated_reload_releases_exit_hooks_and_disposal_is_idempotent(self):
        self.run_plugin(r'''
const baseline = process.listenerCount('exit') - 1;
for (let index = 0; index < 12; index++) {
  const s = h.session(`reload-${index}`);
  await h.step(s, '你好', `u-${index}`);
  const once = h.handlers.get('dispose')();
  const twice = h.handlers.get('dispose')();
  assert.equal(once, twice);
  await once;
  assert.equal(process.listenerCount('exit'), baseline);
  if (index < 11) apply(h.ctx, config);
}
''')

    def test_cordis_effect_owns_disposal_without_emitting_dispose_event(self):
        self.run_plugin(r'''
await h.handlers.get('dispose')();
const baseline = process.listenerCount('exit');
let teardown;
h.ctx.effect = (setup, label) => { assert.equal(label,'memorydreaming lifecycle'); teardown=setup(); };
apply(h.ctx,config);
const s=h.session('effect-owned');
await h.step(s,'真实 effect 卸载回归','u-effect');
assert.equal(process.listenerCount('exit'),baseline+1);
await teardown();
assert.equal(process.listenerCount('exit'),baseline);
await teardown();
''')


if __name__ == "__main__":
    unittest.main()
