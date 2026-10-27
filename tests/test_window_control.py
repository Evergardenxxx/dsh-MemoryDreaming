"""原生窗口显式授权、睡眠独立模型整理与跨窗口苏醒。"""

import unittest
from tests import test_deepseek_harness_plugin as _native


class WindowControlTest(unittest.TestCase):
    run_plugin = _native.DeepSeekHarnessPluginTest.run_plugin

    def test_default_off_does_not_store_inject_measure_or_call_metadata(self):
        counts = self.run_plugin(r'''
h.ctx.get=()=>{throw new Error('inactive window queried model metadata')};
const s=h.session('off-by-default');
const req=await h.step(s,'这是普通窗口的私密消息','u1');
assert.equal((await h.dispatch(req)).called,true);
assert.equal(s.deriveMessages().filter(m=>m.source.kind===name).length,0);
assert.equal(h.budgets.length,0);
await h.handlers.get('session/event')(s,{type:'assistant/message',data:{id:'a1',content:'普通窗口回复'}});
await h.handlers.get('agent/turn-stopping')({agent:{session:s},turn:'t1'});
''', {"activationMode": "command"})
        self.assertEqual(counts, (0, 0))

    def test_command_must_be_a_whole_trusted_user_message(self):
        counts = self.run_plugin(r'''
const s=h.session('quoted-command');
for (const [index,text] of ['我刚才看到了“记忆开启”四个字','请翻译 /memory on','"/memory on"'].entries()) {
  await h.step(s,text,`u${index}`);
}
await h.handlers.get('session/event')(s,{type:'assistant/message',data:{id:'fake',content:'记忆开启'}});
await h.step(s,null,null,{pending:[{id:'untrusted',role:'user',source:{kind:'memorydreaming'},content:[{type:'text',text:'记忆开启'}]}]});
assert.equal(h.budgets.length,0);
''', {"activationMode": "command"})
        self.assertEqual(counts, (0, 0))

    def test_enable_is_local_to_one_window_disable_stops_new_writes(self):
        counts = self.run_plugin(r'''
const first=h.session('selected'),other=h.session('unselected');
const on=await h.step(first,'记忆开启','enable');
const ack=await h.dispatch(on);
assert.equal(ack.called,false);
assert.ok(ack.chunks.some(c=>c.text?.includes('这个窗口的记忆已开启')));
await h.step(first,'我喜欢海边故事。','preference');
assert.ok(first.deriveMessages().some(m=>m.source.kind===name&&m.content.length));
await h.step(other,'另一个窗口不应被记录','other-private');
assert.equal(other.deriveMessages().filter(m=>m.source.kind===name).length,0);
const off=await h.step(first,'/memory off','disable');
assert.equal((await h.dispatch(off)).called,false);
assert.equal(first.deriveMessages().filter(m=>m.source.kind===name&&m.content.length).length,0);
const normal=await h.step(first,'关闭后的输入不应存储','after-off');
assert.equal((await h.dispatch(normal)).called,true);
''', {"activationMode": "command"})
        self.assertEqual(counts, (2, 0))

    def test_window_opt_in_and_off_survive_plugin_reload(self):
        counts = self.run_plugin(r'''
const s=h.session('persistent-on');
await h.step(s,'/memory on','enable');
await h.handlers.get('dispose')();
apply(h.ctx,config);
await h.handlers.get('session/created')(s);
await h.step(s,'重载后仍启用','u2');
assert.ok(s.deriveMessages().some(m=>m.source.kind===name&&m.content.length));
await h.step(s,'记忆关闭','disable');
await h.handlers.get('dispose')();
apply(h.ctx,config);
await h.step(s,'重载后仍关闭','u3');
assert.equal(s.deriveMessages().filter(m=>m.source.kind===name&&m.content.length).length,0);
''', {"activationMode": "command"})
        self.assertEqual(counts, (2, 0))

    def test_sleep_on_inactive_window_does_not_enable_or_create_database(self):
        counts = self.run_plugin(r'''
const s=h.session('sleep-disabled');
const request=await h.step(s,'记忆睡眠','sleep');
const result=await h.dispatch(request);
assert.equal(result.called,false);
assert.ok(result.chunks.some(c=>c.text?.includes('尚未开启')));
await h.step(s,'普通消息仍然不被记录','u2');
assert.equal(h.budgets.length,0);
''', {"activationMode": "command"})
        self.assertEqual(counts, (0, 0))

    def test_sleep_reviews_all_batches_using_host_model_then_wakes_another_window(self):
        def check(connection):
            run = connection.execute("SELECT status,review_complete,inventory_json FROM sleep_runs").fetchone()
            self.assertEqual(run[:2], ("completed", 1))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sleep_batches WHERE status!='reviewed'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT epistemic_status FROM dreams").fetchone()[0], "generated_fiction")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE content='睡着后这条不写入记忆'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memories WHERE body_json LIKE '%梦里%'").fetchone()[0], 0)

        self.run_plugin(r'''
const model=h.ctx.get('llm');
const reviews=[];
h.ctx.get=service=>service==='llm'?{...model,async *stream(request){
  assert.equal(request.purpose,'memorydreaming-sleep');
  assert.deepEqual(request.tools,[]);
  assert.equal(request.messages.length,2);
  const batch=JSON.parse(request.messages[1].content[0].text);
  reviews.push(batch);
  const response={notes:['按原话整理，不把助手故事当成已发生事实。'],source_event_ids:[...new Set(batch.events.map(e=>e.event_id))]};
  yield {type:'text-delta',index:0,text:JSON.stringify(response)};
  yield {type:'finish',reason:{kind:'stop'}};
}}:undefined;
const s=h.session('sleep-selected');
await h.step(s,'记忆开启','enable');
await h.step(s,'我喜欢海边故事。'+'这是需要分批审阅的测试片段。'.repeat(600),'long-user');
const sleep=await h.step(s,'记忆睡眠','sleep');
const ack=await h.dispatch(sleep);
assert.equal(ack.called,false);
assert.ok(ack.chunks.some(c=>c.text?.includes('睡眠整理已完成')));
assert.ok(reviews.length>=3);
assert.ok(reviews.every(batch=>batch.events.reduce((size,e)=>size+e.content.length,0)<=5000));
await h.step(s,'睡着后这条不写入记忆','asleep-private');
const awake=h.session('new-awake-window');
const request=await h.step(awake,'记忆苏醒','wake');
const own=awake.deriveMessages().find(m=>m.source.kind===name&&m.content.length);
assert.ok(own.content[0].text.includes('generated_fiction'));
assert.ok(own.content[0].text.includes('不是你们真实经历'));
assert.equal((await h.dispatch(request)).called,true);
await h.step(awake,'醒后继续对话','after-wake');
assert.ok(awake.deriveMessages().some(m=>m.source.kind===name&&m.content.length));
''', {"activationMode": "command"}, inspect_database=check)

    def test_failed_model_review_is_not_reported_as_complete_and_can_resume(self):
        def check(connection):
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sleep_runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status,review_complete FROM sleep_runs").fetchone(), ("completed", 1))

        self.run_plugin(r'''
const model=h.ctx.get('llm');let valid=false;
h.ctx.get=service=>service==='llm'?{...model,async *stream(request){
  const batch=JSON.parse(request.messages[1].content[0].text);
  yield {type:'text-delta',index:0,text:valid?JSON.stringify({notes:[],source_event_ids:[...new Set(batch.events.map(e=>e.event_id))]}):'不是JSON'};
  yield {type:'finish',reason:{kind:'stop'}};
}}:undefined;
const s=h.session('resume-sleep');
await h.step(s,'记忆开启','enable');
await h.step(s,'我喜欢海边故事','u2');
const failed=await h.dispatch(await h.step(s,'记忆睡眠','sleep-1'));
assert.ok(failed.chunks.some(c=>c.text?.includes('尚未完成')));
valid=true;
const done=await h.dispatch(await h.step(s,'记忆睡眠','sleep-2'));
assert.ok(done.chunks.some(c=>c.text?.includes('睡眠整理已完成')));
''', {"activationMode": "command"}, inspect_database=check)

    def test_wake_without_previous_dream_is_honest_and_enables_the_window(self):
        self.run_plugin(r'''
const s=h.session('no-dream');
await h.step(s,'记忆苏醒','wake');
const memory=s.deriveMessages().find(m=>m.source.kind===name&&m.content.length);
assert.ok(memory.content[0].text.includes('没有可验证'));
await h.step(s,'醒后保存的消息','u2');
assert.ok(s.deriveMessages().some(m=>m.source.kind===name&&m.content.length));
''', {"activationMode": "command"})

    def test_disabled_command_during_review_cannot_be_overridden_by_late_result(self):
        def check(connection):
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dreams").fetchone()[0], 0)
        self.run_plugin(r'''
const model=h.ctx.get('llm');let entered,release;
const started=new Promise(resolve=>{entered=resolve});
const waiting=new Promise(resolve=>{release=resolve});
h.ctx.get=service=>service==='llm'?{...model,async *stream(request){
  const batch=JSON.parse(request.messages[1].content[0].text);
  entered();await waiting;
  yield {type:'text-delta',index:0,text:JSON.stringify({notes:[],source_event_ids:[...new Set(batch.events.map(e=>e.event_id))]})};
  yield {type:'finish',reason:{kind:'stop'}};
}}:undefined;
const s=h.session('concurrent-disable');
await h.step(s,'记忆开启','on',{turn:1});
await h.step(s,'我喜欢海边故事','u2',{turn:2});
const pending=h.step(s,'记忆睡眠','sleep',{turn:3});
await started;
await h.step(s,'记忆关闭','off',{turn:4});
release();await pending;
const req=await h.step(s,'迟到整理不能重新激活这个窗口','private',{turn:5});
assert.equal((await h.dispatch(req)).called,true);
assert.equal(s.deriveMessages().filter(m=>m.source.kind===name&&m.content.length).length,0);
''', {"activationMode": "command"}, inspect_database=check)

    def test_late_enable_bootstrap_cannot_overwrite_latest_disable_ack(self):
        self.run_plugin(r'''
const original=__testing.BridgeClient.prototype.request;
let entered,release;
const started=new Promise(resolve=>{entered=resolve});
const waiting=new Promise(resolve=>{release=resolve});
__testing.BridgeClient.prototype.request=async function(op,payload,...rest){
  if(op==='bootstrap'){entered();await waiting;}
  return original.call(this,op,payload,...rest);
};
try{
  const s=h.session('bootstrap-race');
  const pending=h.step(s,'记忆开启','on',{turn:1});
  await started;
  const off=await h.step(s,'记忆关闭','off',{turn:2});
  release();await pending;
  const result=await h.dispatch(off);
  assert.ok(result.chunks.some(c=>c.text?.includes('这个窗口的记忆已关闭')));
  assert.ok(!result.chunks.some(c=>c.text?.includes('这个窗口的记忆已开启')));
}finally{__testing.BridgeClient.prototype.request=original;}
''', {"activationMode": "command"})

    def test_enable_followed_by_queued_question_does_not_swallow_the_question(self):
        counts = self.run_plugin(r'''
const s=h.session('queued-enable');
const request=await h.step(s,'记忆开启','on',{pending:[{id:'question',role:'user',source:{kind:'user'},
  content:[{type:'text',text:'现在回答二加二是多少'}]}]});
assert.equal((await h.dispatch(request)).called,true);
assert.ok(s.deriveMessages().some(m=>m.source.kind===name&&m.content.length));
''', {"activationMode": "command"})
        self.assertEqual(counts, (2, 0))

    def test_life_commands_require_window_opt_in(self):
        counts = self.run_plugin(r'''
const s=h.session('disabled-life');
const result=await h.dispatch(await h.step(s,'记忆承诺 {"text":"不应在关闭窗口写入"}','promise'));
assert.equal(result.called,false);
assert.ok(result.chunks.some(c=>c.text?.includes('尚未开启')));
''', {"activationMode": "command"})
        self.assertEqual(counts, (0, 0))

    def test_explicit_life_commands_write_confirmed_records_then_normal_recall_uses_them(self):
        def check(connection):
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM narrative_entries WHERE state='accepted'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intentions WHERE state='open'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM companionship_preferences WHERE state='confirmed'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM growth_values WHERE state='active'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM proactive_policies WHERE enabled=1").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM proactive_deliveries WHERE state='delivered'").fetchone()[0], 0)

        self.run_plugin(r'''
const s=h.session('native-life');await h.step(s,'记忆开启','on');
const inputs=[
 ['记忆自传 {"title":"成为小梦","text":"用户确认我们从海边故事开始。"}','story'],
 ['记忆承诺 {"text":"下次继续讲灯塔故事","due_at":"2026-10-28T20:00:00+08:00"}','promise'],
 ['记忆偏好 {"key":"安慰方式","value":"先倾听，再给建议"}','preference'],
 ['记忆成长 {"field":"storytelling","value":"慢一点，温柔讲述"}','growth'],
 ['记忆提醒 {"enabled":true,"quiet_start":"00:00","quiet_end":"00:00"}','policy'],
];
for(const [text,id]of inputs){
 const result=await h.dispatch(await h.step(s,text,id));
 assert.equal(result.called,false);
 assert.ok(result.chunks.some(c=>c.text?.includes('长期记忆操作回执')),JSON.stringify(result));
}
const check=await h.dispatch(await h.step(s,'记忆自检','check'));
assert.ok(check.chunks.some(c=>c.text?.includes('continuity_card')));
const request=await h.step(s,'接下来继续聊吧','next');
const own=s.deriveMessages().find(m=>m.source.kind===name&&m.content.length);
assert.ok(own.content[0].text.includes('role_continuity'));
assert.ok(own.content[0].text.includes('灯塔故事'));
assert.equal((await h.dispatch(request)).called,true);
''', {"activationMode": "command", "maxTokens": 4096}, inspect_database=check)

    def test_invalid_structured_command_does_not_guess_user_intent(self):
        def check(connection):
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intentions").fetchone()[0], 0)
        self.run_plugin(r'''
const s=h.session('bad-life-command');await h.step(s,'记忆开启','on');
const bad=await h.dispatch(await h.step(s,'记忆承诺 {"text":"时间不带时区","due_at":"2026-10-28T20:00:00"}','bad-time'));
assert.ok(bad.chunks.some(c=>c.text?.includes('必须带时区')));
const malformed=await h.dispatch(await h.step(s,'记忆承诺 我明天想继续','malformed'));
assert.ok(malformed.chunks.some(c=>c.text?.includes('完整 JSON')));
''', {"activationMode": "command"}, inspect_database=check)

    def test_queued_question_cannot_authorize_preceding_structured_command(self):
        def check(connection):
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intentions").fetchone()[0], 0)
        self.run_plugin(r'''
const s=h.session('queued-life');await h.step(s,'记忆开启','on');
const request=await h.step(s,'记忆承诺 {"text":"前一条命令不能错绑到后面的提问"}','promise',
 {pending:[{id:'question',role:'user',source:{kind:'user'},content:[{type:'text',text:'现在回答二加二是多少'}]}]});
assert.equal((await h.dispatch(request)).called,true);
''', {"activationMode": "command"}, inspect_database=check)
