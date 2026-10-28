// Offline regression driver for the native management boundary. A separate
// installed-runtime script checks the real Cordis/Gateway context tracing.
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { installDesktop } from '../../integrations/deepseek-harness/lib/desktop-controller.js'
import { WindowControlStore } from '../../integrations/deepseek-harness/lib/window-control.js'

function fixture(directory) {
  const listeners = new Map(), services = new Map(), bridges = [], prompts = [], cancellations = []
  const sessions = [
    { id: 'ordinary-a', header: { title: '海边故事' } },
    { id: 'ordinary-b', header: {} },
    { id: 'child-agent', header: { parentSession: 'ordinary-a' } },
  ]
  const states = new Map(sessions.map(session => [session.id, { session, command: null }]))
  const agents = new Map(sessions.map(session => [session.id, { status: 'idle' }]))
  const ctx = {
    invocation: { peer: { principal: { kind: 'owner' } } },
    get: name => services.get(name),
    reflect: {
      provide(name, value) {
        services.set(name, value)
        return () => { if (services.get(name) === value) services.delete(name) }
      },
    },
    on(event, handler) {
      let subscribers = listeners.get(event)
      if (!subscribers) listeners.set(event, subscribers = new Set())
      subscribers.add(handler)
      return () => subscribers.delete(handler)
    },
  }
  services.set('sessions', { list: () => [...sessions] })
  services.set('agents', { get: id => agents.get(id) })
  let prompt = async () => ({ accepted: true })
  let bridgeReply = async () => ({ status: 'ok' })
  services.set('sessionController', {
    async prompt(request, signal) { prompts.push({ request, signal }); return prompt(request, signal) },
    async cancel(request) { cancellations.push(request); return { accepted: true } },
  })
  const bridge = {
    async request(operation, args, signal) {
      bridges.push({ operation, args, signal })
      return bridgeReply(operation, args, signal)
    },
  }
  const controls = new WindowControlStore({ dbPath: join(directory, 'memory.db'), activationMode: 'command',
    userId: 'bound-user', characterId: 'bound-character', relationshipId: 'bound-relationship' })
  const dispose = installDesktop(ctx, { bridge, controls, states })
  const desktop = services.get('memorydreamingDesktop')
  return {
    ctx, services, sessions, agents, states, controls, bridges, prompts, cancellations, desktop, dispose,
    emit(event, ...args) { for (const listener of [...(listeners.get(event) ?? [])]) listener(...args) },
    listenerCount() { return [...listeners.values()].reduce((count, set) => count + set.size, 0) },
    call: (action, args = {}, signal) => desktop.request(action, args, signal),
    setPrompt: value => { prompt = value },
    setBridge: value => { bridgeReply = value },
  }
}

const id = 'ordinary-a'
const controlArgs = (command, revision = 0, request = randomUUID()) => ({
  conversation_id: id, command, expected_revision: revision, request_id: request,
})
const expectError = (result, code) => assert.equal(result.error?.code, code, JSON.stringify(result))

const scenarios = {
  async auth(h) {
    for (const principal of [undefined, { kind: 'remote', id: 'other-owner' }, { kind: 'anonymous' }]) {
      h.ctx.invocation = principal ? { peer: { principal } } : undefined
      await assert.rejects(() => h.call('windows', { owner: true, principal: { kind: 'owner' } }), {
        code: 'remote-access/denied',
      })
    }
    assert.equal(h.bridges.length + h.prompts.length + h.cancellations.length, 0)
    h.ctx.invocation = { peer: { principal: { kind: 'owner' } } }
    assert.equal((await h.call('windows')).items.length, 2)
  },

  async windows(h, directory) {
    const result = await h.call('windows')
    assert.deepEqual(result.items.map(item => item.id), ['ordinary-a', 'ordinary-b'])
    assert.equal(result.items[0].title, '海边故事')
    for (const item of result.items) {
      assert.equal(item.mode, 'off'); assert.equal(item.revision, 0)
      assert.equal(item.busy, false); assert.equal(item.can_cancel, false)
      assert.equal(item.sleep_progress, null); assert.equal(item.last_budget, null)
    }
    assert.equal(result.has_more, false)
    assert.equal(h.bridges.length + h.prompts.length, 0)
    assert.equal(existsSync(join(directory, 'memory.db')), false)
    assert.equal(existsSync(h.controls.directory), false)
    h.services.delete('sessions')
    assert.equal((await h.call('windows')).items.length, 2, 'state fallback still excludes child agents')
    h.services.set('sessions', { list: () => Array.from({ length: 205 }, (_, index) => ({ id: `window-${index}`, header: {} })) })
    const bounded = await h.call('windows')
    assert.equal(bounded.items.length, 200); assert.equal(bounded.has_more, true)
  },

  async bridge(h) {
    const arguments_ = { query: '海边故事', limit: 4 }
    await h.call('memories', { conversation_id: id, ...arguments_ })
    assert.equal(h.bridges.length, 1)
    assert.deepEqual(h.bridges[0].args, { conversation_id: id, action: 'memories', arguments: arguments_ })
    assert.equal(h.bridges[0].operation, 'desktop')
    assert.ok(h.bridges[0].signal instanceof AbortSignal)
    assert.equal(h.prompts.length, 0)
    assert.equal(h.controls.get(id).mode, 'off', 'viewing records never enables capture')
    h.setBridge(async () => { throw Object.assign(new Error('source was withdrawn'), { code: 'STALE_REFERENCE' }) })
    expectError(await h.call('record', { conversation_id: id, ref: 'stale-ref' }), 'STALE_REFERENCE')
    h.setBridge(async () => { throw new Error('provider temporarily unavailable') })
    expectError(await h.call('overview', { conversation_id: id }), 'UNAVAILABLE')
  },

  async no_windows(h, directory) {
    h.sessions.splice(0)
    h.states.clear()
    assert.deepEqual(await h.call('windows'), { items: [], has_more: false })
    assert.equal(h.bridges.length, 0)
    await h.call('overview')
    assert.deepEqual(h.bridges[0].args, { conversation_id: 'desktop-management-ui',
      action: 'overview', arguments: {} })
    assert.equal(h.controls.get(id).mode, 'off')
    assert.equal(existsSync(h.controls.directory), false)
    assert.equal(existsSync(join(directory, 'memory.db')), false)
    expectError(await h.call('window_control', { command: 'on', expected_revision: 0,
      request_id: randomUUID() }), 'INVALID_ARGUMENT')
    expectError(await h.call('window_cancel', { expected_revision: 0, request_id: randomUUID() }), 'INVALID_ARGUMENT')
    expectError(await h.call('overview', { conversation_id: 'closed-window' }), 'NOT_FOUND_OR_FORBIDDEN')
    assert.equal(h.prompts.length + h.cancellations.length, 0)
  },

  async invalid(h) {
    expectError(await h.call('windows', { userId: 'another-user' }), 'INVALID_ARGUMENT')
    for (const args of [null, [], 'text', { conversation_id: '' }, { conversation_id: 'x'.repeat(201) },
      { conversation_id: id, query: 'x'.repeat(24000) }]) {
      expectError(await h.call('overview', args), 'INVALID_ARGUMENT')
    }
    expectError(await h.call('anything', { conversation_id: id }), 'UNSUPPORTED')
    for (const conversation_id of ['closed-window', 'child-agent']) {
      expectError(await h.call('overview', { conversation_id }), 'NOT_FOUND_OR_FORBIDDEN')
    }
    for (const args of [controlArgs('记忆开启'), controlArgs('constructor'),
      { ...controlArgs('on'), userId: 'another-user' }, { ...controlArgs('on'), request_id: '-'.repeat(36) },
      { ...controlArgs('on'), expected_revision: -1 }, { ...controlArgs('on'), expected_revision: 0.1 }]) {
      expectError(await h.call('window_control', args), 'INVALID_ARGUMENT')
    }
    assert.equal(h.bridges.length + h.prompts.length + h.cancellations.length, 0)
  },

  async controls(h) {
    const args = controlArgs('on')
    const accepted = await h.call('window_control', args)
    assert.equal(accepted.status, 'accepted')
    assert.equal(Object.hasOwn(accepted, 'completed'), false)
    assert.deepEqual(h.prompts[0].request, { sessionId: id, requestId: args.request_id,
      content: [{ type: 'text', text: '记忆开启' }], mode: 'followup' })
    assert.equal(Object.hasOwn(h.prompts[0].request, 'source'), false, 'the actual host owns user-source construction')
    assert.equal(h.controls.get(id).mode, 'off', 'admission is not command completion')
    const replay = await h.call('window_control', { request_id: args.request_id, expected_revision: 0,
      command: 'on', conversation_id: id })
    assert.deepEqual(replay, accepted); assert.equal(h.prompts.length, 1)
    expectError(await h.call('window_control', { ...args, command: 'off' }), 'IDEMPOTENCY_CONFLICT')
    expectError(await h.call('window_control', controlArgs('on')), 'WINDOW_BUSY')
    h.emit('memorydreaming/window-state', id, { mode: 'active' })
    const current = h.controls.set(id, { mode: 'active' })
    expectError(await h.call('window_control', controlArgs('off', 0)), 'STALE_REFERENCE')
    assert.deepEqual(await h.call('window_control', args), accepted, 'replay survives subsequent state changes')
    assert.equal(h.prompts.length, 1)
    assert.equal(h.controls.get('ordinary-b').mode, 'off')
    const off = await h.call('window_control', controlArgs('off', current.revision))
    assert.equal(off.status, 'accepted')
    assert.equal(h.prompts[1].request.content[0].text, '记忆关闭')
  },

  async command_modes(h) {
    const mapping = { on: '记忆开启', off: '记忆关闭', sleep: '记忆睡眠', wake: '记忆苏醒' }
    for (const [command, text] of Object.entries(mapping)) {
      const args = controlArgs(command)
      const result = await h.call('window_control', args)
      assert.equal(result.status, 'accepted')
      assert.equal(h.prompts.at(-1).request.content[0].text, text)
      h.emit('session/event', h.sessions[0], { type: 'turn/start' })
      expectError(await h.call('window_control', controlArgs(command)), 'WINDOW_BUSY')
      h.emit('session/event', h.sessions[0], { type: 'turn/end' })
    }
    assert.equal(h.bridges.length, 0)
  },

  async control_errors(h) {
    h.services.delete('sessionController')
    expectError(await h.call('window_control', controlArgs('on')), 'HOST_CONTROL_UNAVAILABLE')
    h.services.set('sessionController', { prompt() { throw Object.assign(new Error('host admission refused'), { code: 'HOST_REFUSED' }) } })
    expectError(await h.call('window_control', controlArgs('on')), 'HOST_REFUSED')
    assert.equal(h.desktop.queued.size, 0)
    assert.equal(h.desktop.receipts.size, 0)
    h.agents.get(id).status = 'running'
    expectError(await h.call('window_control', controlArgs('on')), 'WINDOW_BUSY')
    h.agents.get(id).status = 'idle'
    let finish
    h.services.set('sessionController', { prompt: () => new Promise(resolve => { finish = resolve }) })
    const args = controlArgs('on')
    const pending = h.call('window_control', args)
    expectError(await h.call('window_control', controlArgs('on')), 'WINDOW_BUSY')
    finish({ accepted: true })
    assert.equal((await pending).status, 'accepted')
  },

  async cancel(h) {
    const cancelArgs = (revision = h.controls.get(id).revision, request_id = randomUUID()) => ({
      conversation_id: id, expected_revision: revision, request_id,
    })
    expectError(await h.call('window_cancel', cancelArgs()), 'NOT_SLEEPING')
    const current = h.controls.set(id, { mode: 'sleeping' })
    expectError(await h.call('window_cancel', cancelArgs()), 'NOT_RUNNING')
    h.emit('session/event', h.sessions[0], { type: 'turn/start' })
    expectError(await h.call('window_cancel', cancelArgs()), 'NOT_RUNNING', 'ordinary chat in sleeping window is protected')
    h.states.get(id).command = { type: 'sleep' }
    assert.equal((await h.call('windows')).items[0].can_cancel, true)
    expectError(await h.call('window_cancel', { ...cancelArgs(), request_id: 'bad-id' }), 'INVALID_ARGUMENT')
    expectError(await h.call('window_cancel', cancelArgs(0)), 'STALE_REFERENCE')
    const args = cancelArgs(current.revision)
    const result = await h.call('window_cancel', args)
    assert.equal(result.status, 'cancel_requested')
    assert.deepEqual(h.cancellations, [{ sessionId: id }])
    assert.deepEqual(await h.call('window_cancel', { request_id: args.request_id,
      expected_revision: args.expected_revision, conversation_id: id }), result)
    assert.equal(h.cancellations.length, 1)
    expectError(await h.call('window_cancel', { ...args, expected_revision: 0 }), 'IDEMPOTENCY_CONFLICT')
    h.emit('session/event', h.sessions[0], { type: 'turn/end' })
    assert.equal((await h.call('windows')).items[0].can_cancel, false)
    expectError(await h.call('window_cancel', cancelArgs()), 'NOT_RUNNING')
    assert.deepEqual(await h.call('window_cancel', args), result)
    assert.equal(h.controls.get(id).mode, 'sleeping', 'stopping the model does not fabricate a completed sleep')
  },

  async lifecycle(h) {
    h.emit('memorydreaming/sleep-progress', id, { sleep_id: 'sleep-test', reviewed_batches: 2,
      total_batches: 5, private_text: 'do not forward extra event fields' })
    h.states.get(id).lastBudget = { status: 'fits', total_tokens: 123 }
    h.emit('session/event', h.sessions[0], { type: 'turn/start' })
    const snapshot = (await h.call('windows')).items[0]
    assert.equal(snapshot.busy, true)
    assert.deepEqual(snapshot.sleep_progress, { sleep_id: 'sleep-test', reviewed_batches: 2, total_batches: 5 })
    assert.deepEqual(snapshot.last_budget, { status: 'fits', total_tokens: 123 })
    h.emit('session/event', h.sessions[0], { type: 'turn/end' })
    assert.equal((await h.call('windows')).items[0].busy, false)
    for (let index = 0; index < 205; index++) h.emit('memorydreaming/sleep-progress', `old-${index}`, { reviewed_batches: index })
    assert.equal(h.desktop.progress.size, 200)
    h.emit('memorydreaming/sleep-progress', id, { reviewed_batches: 3, total_batches: 5 })
    h.desktop.queued.set(id, 'pending')
    h.emit('session/disposed', h.sessions[0])
    assert.equal(h.desktop.progress.has(id), false)
    assert.equal(h.desktop.running.has(id), false)
    assert.equal(h.desktop.queued.has(id), false)
    assert.ok(h.listenerCount() > 0)
    await h.dispose()
    assert.equal(h.listenerCount(), 0)
    assert.equal(h.services.has('memorydreamingDesktop'), false)
    assert.equal(h.desktop.lifetime.signal.aborted, true)
    assert.equal(h.desktop.progress.size + h.desktop.running.size + h.desktop.receipts.size, 0)
    h.emit('memorydreaming/sleep-progress', id, { reviewed_batches: 4 })
    assert.equal(h.desktop.progress.size, 0)
    await assert.rejects(() => h.call('windows'), { code: 'DESKTOP_CLOSED' })
  },

  async abort(h) {
    const aborted = new AbortController(); aborted.abort()
    await assert.rejects(() => h.call('overview', { conversation_id: id }, aborted.signal), { name: 'AbortError' })
    assert.equal(h.bridges.length, 0)
    h.setBridge((_operation, _args, signal) => new Promise((_, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    }))
    const owner = new AbortController()
    const pending = h.call('overview', { conversation_id: id }, owner.signal)
    owner.abort()
    await assert.rejects(pending, { name: 'AbortError' })
    const unloading = h.call('overview', { conversation_id: id })
    await h.dispose()
    await assert.rejects(unloading, { name: 'AbortError' })
    assert.equal(h.bridges.every(call => call.signal.aborted), true)
  },

  async prompt_abort(h) {
    h.setPrompt((_request, signal) => new Promise((_, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    }))
    const abort = new AbortController()
    const pending = h.call('window_control', controlArgs('sleep'), abort.signal)
    assert.equal(h.desktop.queued.has(id), true)
    abort.abort()
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(h.desktop.queued.size, 0)
    assert.equal(h.desktop.receipts.size, 0)
    assert.equal(h.controls.get(id).mode, 'off')
  },
}

const scenario = process.argv[2], directory = process.argv[3]
if (!Object.hasOwn(scenarios, scenario) || !directory) throw new Error('expected a regression scenario and temporary directory')
const h = fixture(directory)
try {
  await scenarios[scenario](h, directory)
  console.log(JSON.stringify({ status: 'passed', scenario, live_model: false, real_desktop_ui: false }))
} finally { await h.dispose() }
