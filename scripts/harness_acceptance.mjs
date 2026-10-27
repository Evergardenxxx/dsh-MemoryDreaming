// Run only against packages extracted from the user's installed public ASAR.
// No profile, account module, network provider or user data is opened.
import assert from 'node:assert/strict'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { writeFileSync } from 'node:fs'
import { Context } from '@deepseek-ai/cordis'
import SessionStore from '@deepseek-ai/dsh-session'
import SessionProjections from '@deepseek-ai/dsh-session-projection'
import AgentRegistry from '@deepseek-ai/dsh-agent'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime, { defineContentToolFixture } from '@deepseek-ai/dsh-tools'
import { LlmRuntime, LlmAdapter } from '@deepseek-ai/dsh-llm'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import * as Persistence from '@deepseek-ai/dsh-session-persistence-jsonl'
import TokenMeter from '@deepseek-ai/dsh-token-meter'
import BasicCompactionEngine from '@deepseek-ai/dsh-compaction-basic'

const [repo, python, workspace, mode] = process.argv.slice(2)
const memory = await import(pathToFileURL(join(repo, 'integrations/deepseek-harness/lib/index.js')))
const config = { repoPath: repo, pythonCommand: python, dbPath: join(workspace, 'memory.db'), autoPrefetch: false, autoConsolidation: false }
const checked = [], calls = [], failures = []
let ctx, memoryFiber, providerMode = 'normal', entered
let toolOnce = false
function text(message) { return message.content?.filter(x => x.type === 'text').map(x => x.text).join('') ?? '' }
const provider = Object.assign(new LlmAdapter(), {
  providerInfo() { return { id: 'acceptance', name: 'Deterministic installed-runtime acceptance' } },
  async resolveModel(provider, id) { return { provider, id, name: id, context: { contextWindow: 131072 }, defaultMaxTokens: 8192 } },
  async *stream(options) {
    calls.push(options)
    const own = options.messages.filter(m => m.source?.kind === memory.name && text(m))
    if (options.purpose !== 'compaction') assert.equal(own.length, 1)
    if (providerMode === 'tool' && !toolOnce) {
      toolOnce = true
      yield { type: 'tool-call-delta', index: 0, id: 'acceptance-call', name: 'acceptance_echo', argumentsDelta: '{}' }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
    } else if (providerMode === 'cancel') {
      yield { type: 'text-delta', index: 0, text: '已提交的中断片段' }
      entered?.()
      await new Promise((resolve, reject) => {
        if (options.signal.aborted) return reject(options.signal.reason)
        options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true })
      })
    } else if (providerMode === 'error') {
      yield { type: 'text-delta', index: 0, text: '错误前流式片段' }
      throw new Error('Synthetic provider failure')
    } else {
      yield { type: 'text-delta', index: 0, text: '安装版运行时确认收到记忆' }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  },
})

async function start() {
  ctx = new Context()
  for (const [plugin, config] of [[Persistence, { root: join(workspace, 'sessions'), compression: 'none', listConcurrency: 1 }],
      [SessionStore, {}], [SessionProjections, {}], [AgentRegistry, {}], [SystemPrompt, {}], [ToolRuntime, {}],
      [LlmRuntime, {}], [TokenMeter, {}], [BasicCompactionEngine, { auto: false }],
      [AgentLoop, { agents: [], maxParallelToolCalls: 1 }]]) await ctx.plugin(plugin, config).await()
  ctx.on('agent/error', payload => failures.push(payload.error?.code ?? payload.error?.message))
  ctx.llm.registerAdapter(['acceptance'], provider)
  ctx.tools.register(defineContentToolFixture({ name: 'acceptance_echo', description: 'Isolated deterministic echo',
    parameters: {}, execute: async () => [{ type: 'text', text: 'Echo success' }] }))
  memoryFiber = ctx.plugin(memory, config)
  await memoryFiber.await()
}
async function say(agent, message, id) {
  agent.followup({ id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: message }] })
  await agent.whenIdle()
}
function ownCount(agent) { return agent.session.deriveMessages().filter(m => m.source?.kind === memory.name && text(m)).length }

try {
  await start()
  if (mode !== 'resume') {
  const agent = await ctx.agentLoop.create('isolated-acceptance', { provider: 'acceptance', model: 'synthetic', maxTokens: 8192 }, { cwd: workspace })
  await say(agent, '第一条实际安装版输入', 'u1')
  assert.equal(calls.length, 1)
  assert.equal(ownCount(agent), 1)
  checked.push('actual-agent-first-request-and-surface')
  providerMode = 'tool'
  await say(agent, '第二条输入' + ' 合成压缩验收资料'.repeat(800), 'u2')
  assert.equal(ownCount(agent), 1)
  assert.ok(agent.session.deriveMessages().some(m => m.role === 'tool'))
  providerMode = 'normal'
  checked.push('actual-agent-repeated-turn-replacement')
  checked.push('actual-tool-dispatch-and-next-model-step')
  const beforeUnload = process.listenerCount('exit')
  await memoryFiber.dispose()
  assert.equal(process.listenerCount('exit'), beforeUnload - 1)
  memoryFiber = ctx.plugin(memory, config); await memoryFiber.await()
  await say(agent, '热重载之后的输入', 'u3')
  assert.equal(ownCount(agent), 1)
  checked.push('actual-cordis-effect-unload-reload')
  providerMode = 'cancel'
  const started = new Promise(resolve => { entered = resolve })
  agent.followup({ id: 'u4', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '中断验收输入' }] })
  await started
  agent.cancel({ kind: 'user' }); await agent.whenIdle()
  checked.push('actual-agent-cancel-and-interrupted-commit')
  providerMode = 'error'
  await say(agent, '供应商异常输入', 'u5')
  assert.ok(failures.length)
  checked.push('actual-agent-provider-failure')
  providerMode = 'normal'
  await say(agent, '异常后下一轮恢复', 'u6')
  assert.equal(ownCount(agent), 1)
  checked.push('actual-agent-recovery-after-failure')
  const compacted = await ctx.compaction.compactNow(agent, new AbortController().signal)
  assert.ok(compacted)
  await say(agent, '真实压缩完成后的首轮', 'u7')
  assert.equal(ownCount(agent), 1)
  checked.push('actual-compaction-commit-and-next-request-restore')
  await ctx.sessions.flush(agent.session)
  checked.push('actual-isolated-host-process-exit-without-disposal')
  // A separate Node process must acquire the OS persistence lease and recover.
  // Intentionally skip finally/effect disposal; the plugin's exit fallback
  // terminates its own Python bridge, leaving durable data for the next run.
  console.log(JSON.stringify({ status: 'passed', checked }))
  process.exit(0)
  }
  const handle = await ctx.agents.resume({ resumeSessionId: 'isolated-acceptance', agentOptions: { provider: 'acceptance', model: 'synthetic', maxTokens: 8192 } })
  await say(handle.agent, '新运行时重启恢复', 'u8')
  assert.equal(ownCount(handle.agent), 1)
  checked.push('actual-jsonl-persistence-new-process-resume')
} finally {
  await ctx?.fiber.dispose()
}
// Verify durable adapter results without depending on an application-private API.
const { spawnSync } = await import('node:child_process')
const result = spawnSync(python, ['-X', 'utf8', '-c', `import sqlite3,sys,json
with sqlite3.connect(sys.argv[1]) as c:
 rows=c.execute("SELECT delivery_state,e.content FROM host_deliveries d JOIN events e ON e.event_id=d.event_id").fetchall()
 assert any(state=='interrupted' and text=='已提交的中断片段' for state,text in rows),rows
 assert c.execute("SELECT COUNT(*) FROM events WHERE speaker='user'").fetchone()[0]==8
 print(json.dumps({'deliveries':len(rows),'interrupted':sum(state=='interrupted' for state,text in rows)}))`, config.dbPath],
  { encoding: 'utf8' })
assert.equal(result.status, 0, result.stderr)
console.log(JSON.stringify({ status: 'passed', checked, ...JSON.parse(result.stdout), private_data_used: false }))
