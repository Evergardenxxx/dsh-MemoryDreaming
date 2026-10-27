"""Complete-request accounting contracts, with deterministic fake tokenizers."""

import shutil
import subprocess
import unittest
from pathlib import Path


class RequestBudgetTest(unittest.TestCase):
    def check_js(self, source):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 不在 PATH 中")
        entry = (Path(__file__).resolve().parents[1] /
                 "integrations/deepseek-harness/lib/request-budget.js").as_uri()
        source = ("import assert from 'node:assert/strict';\n"
                  f"import {{ measureRequest, previewRequest }} from '{entry}';\n" + source)
        result = subprocess.run([node, "--input-type=module", "-e", source],
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_complete_messages_tools_current_input_and_reserves_are_counted(self):
        self.check_js(r'''
const config = { contextWindow: 10000, outputReserve: 200, requestOverhead: 40, safetyMargin: 100 };
const request = { messages: [{ role: 'system', content: '系统' }, { role: 'user', content: '你好' }], maxTokens: 100 };
const base = measureRequest(request, config);
const tools = measureRequest({ ...request, tools: [{ description: '工具'.repeat(2000), parameters: {} }] }, config);
const history = measureRequest({ ...request, messages: [...request.messages, { role: 'assistant', content: '历史'.repeat(2000) }] }, config);
assert.ok(tools.input_tokens > base.input_tokens + 3000);
assert.ok(history.input_tokens > base.input_tokens + 3000);
assert.equal(base.output_reserve, 200);
assert.equal(base.total_tokens, base.input_tokens + 200 + 40 + 100);
assert.equal(measureRequest({ ...request, maxTokens: 5000 }, config).output_reserve, 5000);
assert.equal(base.count_mode, 'estimated');
''')

    def test_model_metadata_limits_cannot_be_enlarged_by_config(self):
        self.check_js(r'''
const b = measureRequest({ messages: [] }, { contextWindow: 999999 }, { contextWindow: 8000, defaultMaxTokens: 4096 });
assert.equal(b.context_window, 8000);
assert.equal(b.output_reserve, 4096);
assert.equal(measureRequest({ messages: [], maxTokens: 200 }, {}).status, 'unavailable');
assert.equal(measureRequest({ messages: [] }, { contextWindow: 10000 }).status, 'unavailable');
''')

    def test_exact_counter_receives_full_request_without_execution_handles(self):
        self.check_js(r'''
const config = { contextWindow: 1000, requestOverhead: 0, safetyMargin: 0,
  requestTokenCounter: { mode: 'exact', count(input) {
    assert.equal(input.provider, 'fixture');
    assert.equal(input.tools.length, 1);
    assert.equal(input.messages.length, 2);
    assert.ok(input.toolHistory);
    assert.equal(input.signal, undefined);
    assert.equal(input.sessionId, undefined);
    return 900;
  } } };
const request = { provider: 'fixture', messages: [{}, {}], tools: [{}], toolHistory: {},
  maxTokens: 100, signal: new AbortController().signal, sessionId: 'x' };
assert.equal(measureRequest(request, config).status, 'fits');
assert.equal(measureRequest(request, config).count_mode, 'exact');
assert.equal(measureRequest({ ...request, maxTokens: 101 }, config).status, 'exceeded');
''')

    def test_invalid_configuration_and_counter_results_fail_explicitly(self):
        self.check_js(r'''
const request = { messages: [], maxTokens: 100 };
for (const key of ['contextWindow', 'outputReserve', 'safetyMargin', 'requestOverhead']) {
  for (const value of [true, -1, NaN, Infinity, 1.1, '123']) {
    assert.throws(() => measureRequest(request, { contextWindow: 10000, [key]: value }), { code: 'INVALID_REQUEST_BUDGET' });
  }
}
for (const result of [true, -1, NaN, Infinity, 1.1, Promise.resolve(12)]) {
  assert.throws(() => measureRequest(request, { contextWindow: 10000,
    requestTokenCounter: { mode: 'exact', count: () => result } }), { code: 'INVALID_REQUEST_BUDGET' });
}
''')

    def test_preview_uses_pending_input_full_history_assembly_and_tool_history(self):
        self.check_js(r'''
const history = [{ role: 'system', content: 'old-system' },
  { role: 'user', content: 'old-memory', source: { kind: 'memorydreaming' } },
  { role: 'assistant', content: 'history' }];
const session = { deriveMessages: () => history, toolHistory: () => ({ updates: ['tool-change'] }) };
const assembly = { sections: [{ text: 'Hello {{name}} {{name}}' }], contexts: [{ text: '{{literal}}', interpolate: false }],
  variables: { name: 'dad' }, tools: [{ name: 't', parameters: {} }] };
const pending = { role: 'user', content: 'current' };
const result = previewRequest(session, { messages: [pending] }, assembly, { provider: 'p', model: 'm' });
assert.equal(result.messages.length, 5);
assert.equal(result.messages[2], pending);
assert.equal(result.messages[3].content[0].text, 'Hello dad dad');
assert.ok(result.messages[4].content[0].text.includes('{{literal}}'));
assert.equal(result.tools, assembly.tools);
assert.deepEqual(result.toolHistory.updates, ['tool-change']);
assert.equal(history.length, 3);
''')
