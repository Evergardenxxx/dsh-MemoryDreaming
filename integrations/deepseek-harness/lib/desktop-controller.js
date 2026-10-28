// Owner-only native desktop RPC. No network listener or LLM tool is added.
// The versioned SRC descriptor below is the public source-mode equivalent of
// Harness's @Remote decorator; compatibility is checked against its Gateway.
import { isSubagent, sessionId } from './native-session.js'

const SERVICE = 'memorydreamingDesktop'
const COMMANDS = Object.freeze({ on: '记忆开启', off: '记忆关闭', sleep: '记忆睡眠', wake: '记忆苏醒' })
const ACTIONS = new Set(['overview', 'memories', 'record', 'proposals', 'proposal_decide',
  'memory_forget', 'memory_correct', 'initialize_role', 'role', 'sleep',
  'role_catalog', 'role_history', 'role_save', 'role_transition'])
const fail = (code, message) => Object.assign(new Error(message), { code, isDSHRemoteError: true, details: {} })
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const validId = value => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value)

export class DesktopController {
  constructor(ctx, { bridge, controls, states }) {
    this.ctx = ctx
    this.name = SERVICE
    this.bridge = bridge
    this.controls = controls
    this.states = states
    this.progress = new Map()
    this.running = new Set()
    this.queued = new Map()
    this.receipts = new Map()
    this.closed = false
    this.lifetime = new AbortController()
    // Cordis uses this public tracker to bind cross-Service calls to the
    // invocation Context. Capturing the original ctx here would lose principal.
    Object.defineProperty(this, Symbol.for('cordis.tracker'), {
      value: { associate: SERVICE, property: 'ctx' },
    })
    this.typertRemote = Object.freeze({ service: this, serviceKey: SERVICE, namespace: SERVICE })
  }

  async request(action, args, signal) {
    let invocation
    try { invocation = this.ctx.invocation } catch {}
    if (invocation?.peer?.principal?.kind !== 'owner') {
      throw fail('remote-access/denied', '仅本机拥有者的管理界面可以访问记忆')
    }
    if (this.closed) throw fail('DESKTOP_CLOSED', '记忆管理服务已卸载')
    const combined = signal ? AbortSignal.any([signal, this.lifetime.signal]) : this.lifetime.signal
    combined.throwIfAborted()
    try {
      if (typeof action !== 'string' || !object(args) || JSON.stringify(args).length > 24000) {
        throw fail('INVALID_ARGUMENT', '管理请求格式无效或过大')
      }
      if (action === 'windows') {
        if (Object.keys(args).length) throw fail('INVALID_ARGUMENT', '窗口列表不接受身份参数')
        return this.windowSnapshot()
      }
      const id = args.conversation_id
      if (id !== undefined && (typeof id !== 'string' || !id || id.length > 200)) throw fail('INVALID_ARGUMENT', '窗口标识无效')
      const session = id === undefined ? null : this.liveSessions().find(item => sessionId(item) === id)
      if (id !== undefined && !session) throw fail('NOT_FOUND_OR_FORBIDDEN', '窗口已关闭或不可管理，请刷新')
      if (action === 'window_control' || action === 'window_cancel') {
        if (!session) throw fail('INVALID_ARGUMENT', '请先选择普通对话窗口')
        return action === 'window_control' ? await this.control(session, args, combined) : await this.cancel(session, args, combined)
      }
      if (!ACTIONS.has(action)) throw fail('UNSUPPORTED', '未知的桌面管理操作')
      const { conversation_id, ...arguments_ } = args
      // The Python process owns the identity binding. Page arguments cannot
      // choose user/role/database paths, even when the caller is an owner.
      return await this.bridge.request('desktop', { conversation_id: conversation_id ?? 'desktop-management-ui', action, arguments: arguments_ }, combined)
    } catch (error) {
      if (combined.aborted) throw error
      // Preserve expected version/source errors as page feedback. Never send
      // stack traces, process arguments, credentials or database paths to UI.
      return { error: { code: error.code ?? 'UNAVAILABLE', message: String(error.message ?? '管理操作未完成').slice(0, 400) } }
    }
  }

  liveSessions() {
    const sessions = this.ctx.get?.('sessions')
    const list = typeof sessions?.list === 'function' ? sessions.list() : [...this.states.values()].map(state => state.session)
    return list.filter(session => sessionId(session) && !isSubagent(session))
  }

  windowSnapshot() {
    const agents = this.ctx.get?.('agents')
    const live = this.liveSessions()
    return { items: live.slice(0, 200).map(session => {
      const id = sessionId(session), state = this.states.get(id), control = this.controls.get(id)
      const running = this.running.has(id) || agents?.get?.(id)?.status === 'running'
      const busy = running || this.queued.has(id)
      return { id, title: typeof session.header?.title === 'string' ? session.header.title.slice(0, 100) : id,
        mode: control.mode, revision: control.revision,
        busy, can_cancel: running && control.mode === 'sleeping' && state?.command?.type === 'sleep',
        sleep_progress: this.progress.get(id) ?? null, last_budget: state?.lastBudget ?? null }
    }), has_more: live.length > 200 }
  }

  async control(session, args, signal) {
    if (Object.keys(args).some(key => !['conversation_id', 'command', 'expected_revision', 'request_id'].includes(key))
        || !Object.hasOwn(COMMANDS, args.command)
        || !Number.isSafeInteger(args.expected_revision) || args.expected_revision < 0
        || !validId(args.request_id)) {
      throw fail('INVALID_ARGUMENT', '窗口指令、版本或请求ID无效')
    }
    const id = sessionId(session), key = args.request_id, fingerprint = JSON.stringify(['control', id, args.command, args.expected_revision])
    const old = this.receipts.get(key)
    if (old) {
      if (old.fingerprint !== fingerprint) throw fail('IDEMPOTENCY_CONFLICT', '请求ID已用于其他操作')
      return old.result
    }
    if (this.controls.get(id).revision !== args.expected_revision) throw fail('STALE_REFERENCE', '窗口状态已变化，请刷新后再操作')
    const agents = this.ctx.get?.('agents')
    if (this.running.has(id) || this.queued.has(id) || agents?.get?.(id)?.status === 'running') {
      throw fail('WINDOW_BUSY', '窗口正在处理消息，请等待完成或停止整理')
    }
    const controller = this.ctx.get?.('sessionController')
    if (typeof controller?.prompt !== 'function') throw fail('HOST_CONTROL_UNAVAILABLE', '当前宿主没有原生窗口指令接口')
    this.queued.set(id, key)
    try {
      await controller.prompt({ sessionId: id, requestId: key,
        content: [{ type: 'text', text: COMMANDS[args.command] }], mode: 'followup' }, signal)
      // Admission is not completion. Only native lifecycle events and later
      // snapshots report changed window mode, review progress or a saved dream.
      const result = { status: 'accepted', conversation_id: id, command: args.command, request_id: key }
      this.receipts.set(key, { fingerprint, result })
      while (this.receipts.size > 200) this.receipts.delete(this.receipts.keys().next().value)
      return result
    } catch (error) {
      this.queued.delete(id)
      throw error
    }
  }

  async cancel(session, args, signal) {
    if (Object.keys(args).some(key => !['conversation_id', 'expected_revision', 'request_id'].includes(key))
        || !Number.isSafeInteger(args.expected_revision) || args.expected_revision < 0 || !validId(args.request_id)) {
      throw fail('INVALID_ARGUMENT', '停止整理参数无效')
    }
    const id = sessionId(session), current = this.controls.get(id)
    const fingerprint = JSON.stringify(['cancel', id, args.expected_revision]), old = this.receipts.get(args.request_id)
    if (old) {
      if (old.fingerprint !== fingerprint) throw fail('IDEMPOTENCY_CONFLICT', '请求ID已用于其他操作')
      return old.result
    }
    if (current.revision !== args.expected_revision) throw fail('STALE_REFERENCE', '窗口状态已变化，请刷新')
    if (current.mode !== 'sleeping') throw fail('NOT_SLEEPING', '只能在睡眠整理期间请求停止')
    const busy = this.running.has(id) || this.ctx.get?.('agents')?.get?.(id)?.status === 'running'
    if (!busy || this.states.get(id)?.command?.type !== 'sleep') throw fail('NOT_RUNNING', '没有正在执行的睡眠整理')
    const controller = this.ctx.get?.('sessionController')
    if (typeof controller?.cancel !== 'function') throw fail('HOST_CONTROL_UNAVAILABLE', '当前宿主不支持停止')
    signal.throwIfAborted()
    await controller.cancel({ sessionId: id })
    const result = { status: 'cancel_requested', conversation_id: id,
      message: '已请求停止；已完成的审阅保留，可再次发送睡眠指令继续。' }
    this.receipts.set(args.request_id, { fingerprint, result })
    while (this.receipts.size > 200) this.receipts.delete(this.receipts.keys().next().value)
    return result
  }

  close() {
    this.closed = true
    this.lifetime.abort()
    this.progress.clear()
    this.queued.clear()
    this.receipts.clear()
    this.running.clear()
  }
}

Object.defineProperty(DesktopController.prototype, '@deepseek-ai/dsh-typert-protocol/remote-methods', {
  value: Object.freeze({ version: 1, methods: Object.freeze([
    Object.freeze({ method: 'request', invocation: Object.freeze({ kind: 'direct' }) }),
  ]) }),
})

export function installDesktop(ctx, dependencies) {
  // Older embedding hosts and offline lifecycle fixtures do not have native
  // service registration. Memory turns remain usable without a desktop UI.
  if (typeof ctx.reflect?.provide !== 'function') return () => {}
  const desktop = new DesktopController(ctx, dependencies)
  const release = ctx.reflect.provide(SERVICE, desktop)
  const off = []
  const subscribe = (event, handler) => { const dispose = ctx.on(event, handler); if (typeof dispose === 'function') off.push(dispose) }
  subscribe('memorydreaming/sleep-progress', (id, report) => {
    if (typeof id !== 'string' || !object(report)) return
    desktop.progress.set(id, { sleep_id: report.sleep_id, reviewed_batches: report.reviewed_batches, total_batches: report.total_batches })
    while (desktop.progress.size > 200) desktop.progress.delete(desktop.progress.keys().next().value)
  })
  subscribe('memorydreaming/window-state', id => desktop.queued.delete(String(id)))
  subscribe('session/event', (session, event) => {
    const id = sessionId(session)
    if (event?.type === 'turn/start') { desktop.running.add(id); desktop.queued.delete(id) }
    if (event?.type === 'turn/end') { desktop.running.delete(id); desktop.queued.delete(id) }
  })
  subscribe('session/disposed', session => {
    const id = sessionId(session)
    desktop.running.delete(id); desktop.queued.delete(id); desktop.progress.delete(id)
  })
  return () => {
    desktop.close()
    for (const dispose of off) dispose()
    const unregister = typeof release === 'function' ? release() : undefined
    return Promise.allSettled([unregister, dependencies.bridge.close?.()])
  }
}
