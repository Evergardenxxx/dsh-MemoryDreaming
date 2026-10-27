"""Pinned local tokenizer contracts and native text Messages projection."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memorydreaming.tokenization import LocalDeepSeekTokenizer, TokenizationError, create_manifest


ROOT = Path(__file__).resolve().parents[1]


class LocalTokenizerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        (self.path / "tokenizer.json").write_text('{"test_fixture":true}', encoding="utf-8")
        (self.path / "tokenizer_config.json").write_text(json.dumps({"chat_template": "{{bos_token}}{{messages[0]['content']}}",
                                                                    "bos_token": "<bos>"}), encoding="utf-8")
        create_manifest(self.path, provider_ids=["deepseek"], model_ids=["explicit-local-model"], revision="fixture-sha")

    @contextmanager
    def tokenizer(self):
        # Fixture counts codepoints to test the contract, not model quality.
        class FakeTokenizer:
            def no_truncation(self): pass
            def no_padding(self): pass
            def encode(self, value, add_special_tokens):
                if add_special_tokens:
                    raise AssertionError("Implicit BOS would double count template markers")
                return SimpleNamespace(ids=list(value))
        factory = SimpleNamespace(from_str=lambda text: FakeTokenizer())
        with patch.dict(sys.modules, {"tokenizers": SimpleNamespace(Tokenizer=factory)}):
            yield LocalDeepSeekTokenizer(self.path)

    def test_text_counter_is_exact_and_does_not_add_unrequested_special_tokens(self):
        with self.tokenizer() as tokenizer:
            self.assertEqual(tokenizer.mode, "exact")
            self.assertEqual(tokenizer.count("爸爸😀"), 3)
            with self.assertRaises(TokenizationError):
                tokenizer.count(123)

    def test_modified_tokenizer_asset_fails_pin_validation(self):
        (self.path / "tokenizer.json").write_text('{}', encoding="utf-8")
        with self.assertRaises(TokenizationError) as caught:
            LocalDeepSeekTokenizer(self.path)
        self.assertEqual(caught.exception.code, "TOKENIZER_HASH_MISMATCH")

    def test_provider_and_model_aliases_must_be_explicitly_bound(self):
        with self.tokenizer() as tokenizer:
            for provider, model in (("foreign", "explicit-local-model"), ("deepseek", "deepseek-flash"), ("deepseek", "deepseek-chat")):
                with self.subTest(model=model), self.assertRaises(TokenizationError) as caught:
                    tokenizer.check_binding(provider, model)
                self.assertEqual(caught.exception.code, "TOKENIZER_MODEL_MISMATCH")

    def test_complete_request_never_claims_remote_template_exactness(self):
        with self.tokenizer() as tokenizer:
            result = tokenizer.count_request({"provider": "deepseek", "model": "explicit-local-model",
                "projection": "deepseek-messages-text-v1", "projected": {"messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]}})
            self.assertEqual(result["count_mode"], "estimated")
            self.assertEqual(result["text_tokenization"], "exact")
            self.assertEqual(result["template_fidelity"], "wire-json-estimate")
            self.assertGreater(result["input_tokens"], 2)

    def test_manifest_refuses_overwrite_and_dependency_error_does_not_download(self):
        with self.assertRaises(TokenizationError):
            create_manifest(self.path, provider_ids=["deepseek"], model_ids=["other"], revision="other")
        with patch.dict(sys.modules, {"tokenizers": None}), self.assertRaises(TokenizationError) as caught:
            LocalDeepSeekTokenizer(self.path)
        self.assertEqual(caught.exception.code, "TOKENIZER_DEPENDENCY_MISSING")

    def node(self, source):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node not installed")
        entry = (ROOT / "integrations/deepseek-harness/lib/local-tokenizer.js").as_uri()
        budget = (ROOT / "integrations/deepseek-harness/lib/request-budget.js").as_uri()
        source = ("import assert from 'node:assert/strict';\n"
                  f"import {{ projectDeepSeekTextRequest,configureLocalTokenizer }} from '{entry}';\n"
                  f"import {{ measureRequest }} from '{budget}';\n" + source)
        result = subprocess.run([node, "--input-type=module", "-e", source], cwd=ROOT,
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_tool_history_projection_includes_tool_schemas_and_results(self):
        self.node(r'''
const projected=projectDeepSeekTextRequest({model:'explicit-local-model',messages:[
 {role:'system',content:[{type:'text',text:'你是小梦'}]},
 {role:'user',content:[{type:'text',text:'查资料'}]},
 {role:'assistant',content:[{type:'reasoning',text:'可见历史推理'},{type:'tool-call',id:'c1',name:'lookup',arguments:'{"q":"海边"}'}]},
 {role:'tool',toolCallId:'c1',content:[{type:'text',text:'资料结果'}]},
 {role:'user',content:[{type:'text',text:'继续'}]}
],tools:[{name:'lookup',description:'查找',parameters:{type:'object'},deferLoading:true}]});
assert.equal(projected.system,'你是小梦');
assert.equal(projected.messages[1].content[0].type,'thinking');
assert.equal(projected.messages[1].content[1].input.q,'海边');
assert.equal(projected.messages[2].content[0].type,'tool_result');
assert.equal(projected.messages[2].content[1].text,'继续');
assert.equal(projected.tools[0].input_schema.type,'object');
assert.equal(projected.tools[0].defer_loading,true);
''')

    def test_media_unknown_extensions_and_native_signatures_fail_explicitly(self):
        self.node(r'''
const user={role:'user',content:[{type:'text',text:'你好'}]};
for(const extra of [
 {messages:[{role:'user',content:[{type:'image',attachment:{}}]}]},
 {messages:[{role:'user',content:[{type:'file',text:'secret'}]}]},
 {messages:[{role:'assistant',source:{replayState:{}},content:[{type:'reasoning',text:'x'}]}]},
 {extension:{hiddenInput:'x'}}, {toolHistory:{updates:['change']}},
]) assert.throws(()=>projectDeepSeekTextRequest({messages:[user],...extra}),{code:'TOKENIZER_PROJECTION_UNSUPPORTED'});
assert.throws(()=>configureLocalTokenizer({localTokenizer:{}}),{code:'TOKENIZER_CONFIG_INVALID'});
assert.throws(()=>configureLocalTokenizer({localTokenizer:{directory:'x'},requestTokenCounter:{}}),{code:'TOKENIZER_CONFIG_INVALID'});
''')

    def test_precision_telemetry_does_not_leak_counter_request_state(self):
        self.node(r'''
const request={provider:'deepseek',model:'explicit-local-model',messages:[],maxTokens:10};
const precision={text_tokenization:'exact',template_fidelity:'wire-json-estimate',private:'secret'};
const result=measureRequest(request,{contextWindow:1000,requestTokenCounter:{mode:'estimated',count:()=>5,precision}});
assert.equal(result.precision.text_tokenization,'exact');
assert.equal(result.precision.private,undefined);
assert.equal(result.count_mode,'estimated');
''')
