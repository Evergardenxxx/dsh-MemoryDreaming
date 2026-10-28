import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import { apply } from '../../integrations/deepseek-harness/lib/index.js'
import { BridgeClient } from '../../integrations/deepseek-harness/lib/bridge-client.js'
import { harness } from './native-harness.mjs'

const h = harness(), services = new Map(), calls = []
const oldRequest = BridgeClient.prototype.request, before = process.listenerCount('exit')
let rejectRead = false
BridgeClient.prototype.request = function(op, args, signal) {
  calls.push({ client: this, op, args })
  if (op === 'desktop' && rejectRead) return new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
  return Promise.resolve(op === 'bootstrap' ? { data: { identity_fields: [], relationship_fields: [] } }
    : op === 'desktop' ? { binding: { user_id: 'u1' } } : { user_event_id: 'event-1', delivery_guard: {} })
}
h.ctx.reflect = { provide(name, value) { services.set(name, value); return () => services.delete(name) } }
h.ctx.invocation = { peer: { principal: { kind: 'owner' } } }
const originalGet = h.ctx.get
h.ctx.get = name => services.get(name) ?? originalGet(name)
try {
  apply(h.ctx, { dbPath: resolve(process.argv[2], 'isolation.db'), autoConsolidation: false, autoPrefetch: false })
  assert.equal(process.listenerCount('exit'), before + 1, 'UI worker must be lazy')
  const desktop = services.get('memorydreamingDesktop')
  const session = h.session('isolation-window')
  h.handlers.get('session/created')(session)
  assert.equal((await desktop.request('windows', {})).items[0].mode, 'off')
  assert.equal(calls.length, 0, 'viewing window states must not start a bridge')
  await h.step(session, '记忆开启', 'enable-isolation')
  const conversationClient = calls[0].client
  await desktop.request('overview', {})
  const managementClient = calls.find(item => item.op === 'desktop').client
  assert.notEqual(managementClient, conversationClient)
  assert.equal(managementClient.config.autoConsolidation, false)
  assert.equal(managementClient.config.autoPrefetch, false)
  rejectRead = true
  const abort = new AbortController()
  const reading = desktop.request('overview', {}, abort.signal)
  abort.abort()
  await assert.rejects(reading, { name: 'AbortError' })
  assert.equal(conversationClient.closed, false)
  await h.step(session, '继续对话', 'after-ui-cancel')
  assert.equal(calls.at(-1).client, conversationClient)
} finally {
  await h.handlers.get('dispose')?.()
  BridgeClient.prototype.request = oldRequest
}
assert.equal(process.listenerCount('exit'), before, 'both lazy workers must release exit listeners')
console.log(JSON.stringify({ status: 'passed', checked: ['lazy management worker', 'independent page cancellation', 'foreground continues', 'both workers disposed'] }))
