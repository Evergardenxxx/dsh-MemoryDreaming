// Run against an isolated extraction of the installed public Harness modules:
// node scripts/harness_desktop_acceptance.mjs REPOSITORY_ROOT EXTRACTED_RUNTIME
// This exercises the actual plugin controller and Cordis/Typert Gateway. Session
// admission and the Python bridge are deterministic spies; no profile is read,
// no user session is opened, and no GUI/model acceptance is claimed.
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { basename, join, resolve, sep } from 'node:path'
import { tmpdir } from 'node:os'
import { pathToFileURL } from 'node:url'
import vm from 'node:vm'

const root = resolve(process.argv[2] ?? '.')
const runtime = resolve(process.argv[3] ?? join(root, 'dist/harness-desktop-runtime'))
const hostRequire = createRequire(pathToFileURL(join(runtime, 'package.json')))
const hostImport = name => import(pathToFileURL(hostRequire.resolve(name)).href)
const [{ Context }, { TypertRegistry }, { TypertGatewayService }, { installDesktop }, { WindowControlStore }, { ClientModuleRegistry }] = await Promise.all([
  hostImport('@deepseek-ai/cordis'),
  hostImport('@deepseek-ai/dsh-typert-registry'),
  hostImport('@deepseek-ai/dsh-api-gateway'),
  import(pathToFileURL(join(root, 'integrations/deepseek-harness/lib/desktop-controller.js')).href),
  import(pathToFileURL(join(root, 'integrations/deepseek-harness/lib/window-control.js')).href),
  hostImport('@deepseek-ai/dsh-client-modules'),
])

const workspace = mkdtempSync(join(tmpdir(), 'memorydreaming-desktop-host-'))
const checked = [], controllerCalls = [], bridgeCalls = []
const ctx = new Context()
const states = new Map(), sessions = [{ id: 'desktop-ordinary', header: { title: 'Synthetic desktop acceptance' } },
  { id: 'desktop-child', header: { parentSession: 'desktop-ordinary' } }]
const agents = new Map(sessions.map(session => [session.id, { status: 'idle' }]))
for (const session of sessions) states.set(session.id, { session, command: null })
new TypertRegistry(ctx)
// Deliberately permissive to prove that the plugin itself refuses non-owners.
ctx.reflect.provide('remoteAccess', { decide: () => ({ kind: 'allow' }) })
ctx.reflect.provide('sessions', { list: () => [...sessions] })
ctx.reflect.provide('agents', { get: id => agents.get(id) })
class PromptSpy {
  constructor(ctx) {
    this.ctx = ctx
    Object.defineProperty(this, Symbol.for('cordis.tracker'), {
      value: { associate: 'sessionController', property: 'ctx' },
    })
  }
  async prompt(request, signal) {
    assert.equal(this.ctx.invocation.peer.principal.kind, 'owner')
    signal.throwIfAborted()
    controllerCalls.push({ method: 'prompt', request })
    return { accepted: true }
  }
  async cancel(request) {
    assert.equal(this.ctx.invocation.peer.principal.kind, 'owner')
    controllerCalls.push({ method: 'cancel', request })
    return { accepted: true }
  }
}
ctx.reflect.provide('sessionController', new PromptSpy(ctx))
const controls = new WindowControlStore({ dbPath: join(workspace, 'synthetic.db'), activationMode: 'command' })
let bridgeReply = async () => ({ status: 'ok', data: {} })
const bridge = { async request(operation, args, signal) {
  bridgeCalls.push({ operation, args, signal })
  return bridgeReply(operation, args, signal)
} }
const releaseDesktop = installDesktop(ctx, { bridge, controls, states })
const desktop = ctx.get('memorydreamingDesktop')
const gateway = new TypertGatewayService(ctx, {
  websocketHeartbeatIntervalMs: 2000, streamInboxBytes: 262144,
})
const peerSignal = new AbortController().signal
const owner = { ctx, signal: peerSignal, principal: { kind: 'owner' } }
const nonOwner = { ctx, signal: peerSignal, principal: { kind: 'remote', id: 'synthetic-nonowner' } }
const call = (action, args = {}, peer = owner, signal = peerSignal) => gateway.invokeRpc(
  'memorydreamingDesktop/request', { args: { action, args } }, signal, peer,
)

try {
  const windows = await call('windows')
  assert.equal(windows.ok, true, JSON.stringify(windows))
  assert.deepEqual(windows.value.items.map(item => item.id), ['desktop-ordinary'])
  assert.equal(windows.value.items[0].mode, 'off')
  assert.equal(bridgeCalls.length, 0)
  checked.push('actual DesktopController SRC endpoint discovered by installed Cordis Gateway')

  const denied = await call('windows', { principal: { kind: 'owner' } }, nonOwner)
  assert.equal(denied.ok, false)
  assert.equal(denied.error.code, 'remote-access/denied')
  assert.equal(bridgeCalls.length + controllerCalls.length, 0)
  checked.push('non-owner rejected despite forged arguments and permissive global access policy')

  const extra = await gateway.invokeRpc('memorydreamingDesktop/request', {
    args: { action: 'windows', args: {}, unexpected: true },
  }, peerSignal, owner)
  assert.equal(extra.ok, false)
  checked.push('actual Gateway rejects extra named RPC arguments')

  const invalid = await call('arbitrary_operation', { conversation_id: 'desktop-ordinary' })
  assert.equal(invalid.value.error.code, 'UNSUPPORTED')
  assert.equal(bridgeCalls.length, 0)
  checked.push('unknown desktop action cannot become an arbitrary bridge operation')

  const overview = await call('overview')
  assert.equal(overview.ok, true)
  assert.deepEqual(bridgeCalls[0].args, { conversation_id: 'desktop-management-ui', action: 'overview', arguments: {} })
  assert.equal(bridgeCalls[0].operation, 'desktop')
  assert.equal(controls.get('desktop-ordinary').mode, 'off')
  checked.push('owner bound-role read uses desktop envelope without enabling a window')

  const requestId = randomUUID()
  const args = { conversation_id: 'desktop-ordinary', command: 'on', expected_revision: 0, request_id: requestId }
  const accepted = await call('window_control', args)
  assert.equal(accepted.ok, true, JSON.stringify(accepted))
  assert.equal(accepted.value.status, 'accepted')
  assert.equal(controls.get('desktop-ordinary').mode, 'off')
  assert.deepEqual(controllerCalls[0], { method: 'prompt', request: {
    sessionId: 'desktop-ordinary', requestId, content: [{ type: 'text', text: '记忆开启' }], mode: 'followup',
  } })
  const replay = await call('window_control', { request_id: requestId, expected_revision: 0,
    command: 'on', conversation_id: 'desktop-ordinary' })
  assert.deepEqual(replay, accepted)
  assert.equal(controllerCalls.length, 1)
  checked.push('owner invocation survives actual Cordis cross-service tracing; command admission is idempotent')

  ctx.emit('memorydreaming/window-state', 'desktop-ordinary', { mode: 'sleeping' })
  const sleeping = controls.set('desktop-ordinary', { mode: 'sleeping' })
  states.get('desktop-ordinary').command = { type: 'sleep' }
  ctx.emit('session/event', sessions[0], { type: 'turn/start' })
  const cancel = await call('window_cancel', { conversation_id: 'desktop-ordinary',
    expected_revision: sleeping.revision, request_id: randomUUID() })
  assert.equal(cancel.ok, true, JSON.stringify(cancel))
  assert.equal(cancel.value.status, 'cancel_requested')
  assert.deepEqual(controllerCalls[1], { method: 'cancel', request: { sessionId: 'desktop-ordinary' } })
  checked.push('running sleep cancellation retains actual owner invocation')

  bridgeReply = (_operation, _args, signal) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
  const cancelling = new AbortController()
  const pending = call('overview', {}, owner, cancelling.signal)
  await new Promise(resolve => setImmediate(resolve))
  cancelling.abort()
  assert.equal((await pending).ok, false)
  assert.equal(bridgeCalls.at(-1).signal.aborted, true)
  checked.push('owner transport cancellation reaches pending bridge read')

  const unloading = call('overview')
  await new Promise(resolve => setImmediate(resolve))
  await releaseDesktop()
  assert.equal((await unloading).ok, false)
  assert.equal(desktop.closed, true)
  assert.equal(desktop.progress.size + desktop.running.size + desktop.queued.size + desktop.receipts.size, 0)
  checked.push('unload cancels active reads and releases controller state')

  assert.equal((await call('windows')).ok, false)
  checked.push('unregistered service is unavailable to the actual Gateway')

  // Use the installed module registry to discover the actual plugin manifest,
  // compose its advertised resource, and load the shipped lazy client factory.
  const clientContext = new Context()
  try {
    clientContext.reflect.provide('loader', { entries: () => [{
      options: { name: pathToFileURL(join(root, 'integrations/deepseek-harness/lib/index.js')).href },
      fiber: {}, parent: { tree: { ctx: { baseUrl: pathToFileURL(join(runtime, 'package.json')).href } } },
    }] })
    const registry = new ClientModuleRegistry(clientContext)
    const row = registry.graph().entries.find(item => item.id === '@local/memorydreaming')
    assert.ok(row)
    assert.ok(row.inject.includes('@deepseek-ai/dsh-client-ui-slots'))
    assert.ok(row.external.includes('react'))
    checked.push('installed client registry discovers native manifest and desktop dependencies')
    const resource = new URL(row.url, 'http://localhost/')
    const response = await registry.fetchBundle({ method: 'GET', url: resource.pathname + resource.search })
    assert.equal(response.status, 200)
    let registration
    vm.runInNewContext(await response.text(), { window: { __ModuleLoader__: { load(value) { registration = value } } } })
    assert.equal(registration.id, row.id)
    assert.equal(typeof registration.factory, 'function')
    checked.push('installed client registry serves the actual lazy desktop bundle without eager page effects')
  } finally { await clientContext.fiber.dispose() }

  const versions = {}
  for (const name of ['@deepseek-ai/cordis', '@deepseek-ai/dsh-api-gateway', '@deepseek-ai/dsh-typert-registry', '@deepseek-ai/dsh-client-modules']) {
    versions[name] = JSON.parse(readFileSync(join(runtime, 'node_modules', name, 'package.json'), 'utf8')).version
  }
  console.log(JSON.stringify({ status: 'passed', checked, versions, real_cordis_gateway: true, real_client_registry: true,
    real_desktop_ui: false, live_model: false, real_engine_bridge: false,
    note: 'Actual plugin controller and installed public Cordis/Gateway; deterministic session admission and bridge spies; no profile or credentials accessed' }))
} finally {
  await releaseDesktop()
  const temporaryRoot = resolve(tmpdir())
  if (!workspace.startsWith(temporaryRoot + sep) || !basename(workspace).startsWith('memorydreaming-desktop-host-')) {
    throw new Error('Refusing to remove an unexpected acceptance workspace')
  }
  rmSync(workspace, { recursive: true, force: true })
}
