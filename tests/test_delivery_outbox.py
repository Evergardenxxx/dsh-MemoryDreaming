"""Node 持久投递：无回执进程退出、新进程恢复、拒绝和正文清理。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeliveryOutboxTest(unittest.TestCase):
    def run_node(self, script, config):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        source = """
import assert from 'node:assert/strict';
import { DeliveryOutbox } from '%ENTRY%';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const config = %CONFIG%;
const request = { conversation_id:'window', message_id:'a1', user_event_id:'evt-user',
  content:'已经显示的私密片段', delivery_state:'interrupted', used_references:[], expected_guard:'guard-v1' };
%SCRIPT%
""".replace("%ENTRY%", (ROOT/"integrations/deepseek-harness/lib/delivery-outbox.js").as_uri()).replace(
            "%CONFIG%", json.dumps(config)).replace("%SCRIPT%", script)
        result = subprocess.run([node, "--input-type=module", "-e", source], cwd=ROOT,
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_crash_before_receipt_replays_same_payload_in_new_process(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"dbPath": str(Path(folder)/"memory.db")}
            self.run_node("""
const outbox = new DeliveryOutbox(config, { request: () => new Promise(()=>{}) });
void outbox.deliver(request);
assert.equal(outbox.files().length,1);
process.exit(0);
""", config)
            self.run_node("""
let received;
const outbox = new DeliveryOutbox(config, { async request(op,payload) { received=payload; return {duplicate:true} } });
await outbox.flush();
await Promise.all(outbox.pending.values());
outbox.close();
assert.equal(received.content,request.content);
assert.equal(received.expected_guard,'guard-v1');
assert.equal(typeof received.occurred_at,'string');
assert.equal(outbox.files().length,0);
""", config)

    def test_transient_failure_keeps_payload_and_stale_failure_erases_it(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_node("""
let code='BRIDGE_TIMEOUT';
const outbox=new DeliveryOutbox(config,{async request(){throw Object.assign(new Error('failure'),{code})}});
await outbox.deliver(request);
assert.equal(outbox.files().length,1);
code='STALE_DELIVERY'; await outbox.flush();
assert.equal(outbox.files().length,0);
const rejected=readdirSync(outbox.directory).find(x=>x.startsWith('rejected-'));
assert.equal(readFileSync(join(outbox.directory,rejected),'utf8').includes(request.content),false);
outbox.close();
""", {"dbPath": str(Path(folder)/"memory.db")})

    def test_binding_isolation_duplicate_admission_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_node("""
const bridge={request:()=>new Promise(()=>{})};
const one=new DeliveryOutbox(config,bridge),two=new DeliveryOutbox({...config,relationshipId:'other'},bridge);
void one.deliver(request); void one.deliver(request);
assert.equal(one.files().length,1); assert.equal(two.files().length,0);
const file=join(one.directory,one.files()[0]);
const value=JSON.parse(readFileSync(file,'utf8')); value.request.content='损坏内容';writeFileSync(file,JSON.stringify(value));
one.close(); two.close();
const replay=new DeliveryOutbox(config,{request(){assert.fail('corrupt queue must not be replayed')}});
await replay.flush();replay.close(); assert.equal(replay.files().length,0);
process.exit(0);
""", {"dbPath": str(Path(folder)/"memory.db")})
