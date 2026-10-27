import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { countMemory, measureRequest, previewRequest, RequestBudgetError } from './request-budget.js'

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const REPO_ROOT = resolve(PACKAGE_ROOT, '..', '..')

export const name = 'memorydreaming'
// The plugin uses Cordis lifecycle events directly. It deliberately registers
// no tools and therefore adds no MCP/tool schema to model requests.
export const inject = []

const DEFAULT_MAX_TOKENS = 1200
const MIN_MAX_TOKENS = 128
const HARD_MAX_TOKENS = 4096

function boundedMaxTokens(value) {
  return Number.isInteger(value) && value >= MIN_MAX_TOKENS && value <= HARD_MAX_TOKENS
    ? value
    : DEFAULT_MAX_TOKENS
}

function textOfContent(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map((block) => {
      if (typeof block === 'string') return block
      if (block?.type === 'text' && typeof block.text === 'string') return block.text
      return ''
    })
    .join('')
}

function eventContent(event) {
  const data = event?.data ?? {}
  return textOfContent(data.content ?? data.message?.content)
}

function sessionId(session) {
  return session?.id === undefined ? '' : String(session.id)
}

function isSubagent(session) {
  return session?.header?.parentSession !== undefined
}

function hasMemoryContext(messages) {
  return Array.isArray(messages) && messages.some((message) => textOfContent(message?.content).includes('<memory-context'))
}

class BridgeClient {
  constructor(config, logger) {
    this.config = config
    this.logger = logger
    this.child = null
    this.buffer = ''
    this.pending = new Map()
    this.closed = false
  }

  start() {
    if (this.child && !this.child.killed) return this.child
    this.closed = false
    const python = this.config.pythonCommand ?? process.env.MEMORYDREAMING_PYTHON ?? 'python'
    const repoPath = this.config.repoPath ?? REPO_ROOT
    const dbPath = this.config.dbPath ?? resolve(repoPath, '.memorydreaming', 'deepseek-harness.db')
    const maxTokens = boundedMaxTokens(this.config.maxTokens)
    const extractor = this.config.extractor === 'deepseek' ? 'deepseek' : 'rules'
    const args = [
      '-X', 'utf8', '-m', 'memorydreaming', '--db', dbPath, 'bridge',
      '--user', String(this.config.userId ?? 'u1'),
      '--character', String(this.config.characterId ?? 'c1'),
      '--relationship', String(this.config.relationshipId ?? 'r1'),
      '--domain', String(this.config.domain ?? 'real'),
      '--max-tokens', String(maxTokens),
      '--extractor', extractor,
    ]
    if (this.config.autoConsolidation !== false || extractor === 'deepseek') args.push('--auto-consolidation')
    const env = { ...process.env, PYTHONUTF8: '1' }
    const embeddingOptions = {
      embeddingProvider: 'EMBEDDING_PROVIDER',
      embeddingModelPath: 'EMBEDDING_MODEL_PATH',
      embeddingRevision: 'EMBEDDING_REVISION',
      embeddingDim: 'EMBEDDING_DIM',
      embeddingQueryPrefix: 'EMBEDDING_QUERY_PREFIX',
      embeddingDocumentPrefix: 'EMBEDDING_DOCUMENT_PREFIX',
      embeddingMaxSeqLength: 'EMBEDDING_MAX_SEQ_LENGTH',
      embeddingBatchSize: 'EMBEDDING_BATCH_SIZE',
      vectorMinScore: 'VECTOR_MIN_SCORE',
    }
    for (const [key, suffix] of Object.entries(embeddingOptions)) {
      if (this.config[key] !== undefined && this.config[key] !== null) {
        env[`MEMORYDREAMING_${suffix}`] = String(this.config[key])
      }
    }
    const child = spawn(python, args, {
      cwd: repoPath,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.child = child
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.onStdout(chunk))
    child.stderr.on('data', (chunk) => {
      const line = String(chunk).trim()
      if (line) this.logger?.debug?.(`[memorydreaming] ${line}`)
    })
    child.on('error', (error) => this.failPending(error))
    child.on('exit', (code, signal) => {
      if (this.child !== child) return
      this.child = null
      if (!this.closed && (code !== 0 || signal !== null)) {
        this.failPending(new Error(`memory bridge exited (${code ?? signal ?? 'unknown'})`))
      }
    })
    return child
  }

  onStdout(chunk) {
    this.buffer += chunk
    while (true) {
      const newline = this.buffer.indexOf('\n')
      if (newline < 0) return
      const line = this.buffer.slice(0, newline).trim()
      this.buffer = this.buffer.slice(newline + 1)
      if (!line) continue
      let response
      try {
        response = JSON.parse(line)
      } catch {
        this.logger?.warn?.('[memorydreaming] bridge emitted invalid JSON')
        continue
      }
      const entry = this.pending.get(String(response.id))
      if (!entry) continue
      this.pending.delete(String(response.id))
      if (response.ok === true) entry.resolve(response.data)
      else entry.reject(Object.assign(new Error(response.error?.message ?? 'memory bridge request failed'), {
        code: response.error?.code,
      }))
    }
  }

  failPending(error) {
    for (const entry of this.pending.values()) entry.reject(error)
    this.pending.clear()
  }

  request(op, payload, signal) {
    if (this.closed) return Promise.reject(new Error('memory bridge is closed'))
    const child = this.start()
    const id = randomUUID()
    return new Promise((resolvePromise, rejectPromise) => {
      const key = String(id)
      const onAbort = () => {
        this.pending.delete(key)
        rejectPromise(new Error('memory bridge request aborted'))
      }
      this.pending.set(key, {
        resolve: (value) => {
          signal?.removeEventListener?.('abort', onAbort)
          resolvePromise(value)
        },
        reject: (error) => {
          signal?.removeEventListener?.('abort', onAbort)
          rejectPromise(error)
        },
      })
      signal?.addEventListener?.('abort', onAbort, { once: true })
      try {
        child.stdin.write(`${JSON.stringify({ id, op, ...payload })}\n`)
      } catch (error) {
        this.pending.delete(key)
        rejectPromise(error)
      }
    })
  }

  async close() {
    const child = this.child
    if (!child) return
    try {
      await this.request('shutdown', {}, undefined)
    } catch {
      child.kill()
    }
    this.closed = true
    this.child = null
  }
}

function safeLogger(ctx) {
  return ctx?.logger ?? console
}

function stateFor(states, session) {
  const id = sessionId(session)
  if (!id) return null
  let state = states.get(id)
  if (!state) {
    state = {
      session,
      pendingUser: null,
      prepared: false,
      userEventId: null,
      memoryMessages: [],
      latestAssistant: '',
      latestAssistantId: null,
      deliveredTurn: null,
      turn: null,
      errorTurn: null,
      decision: null,
      assembly: null,
      pendingSurfaceUsers: new Set(),
    }
    states.set(id, state)
  } else {
    state.session = session
  }
  return state
}

function resetUser(state, event) {
  const data = event?.data ?? {}
  const content = eventContent(event)
  if (!content.trim()) return
  if (state.pendingUser?.messageId === String(data.id)) return
  state.pendingUser = {
    messageId: String(data.id ?? `user-${event.seq ?? randomUUID()}`),
    content,
  }
  state.prepared = false
  state.userEventId = null
  state.memoryMessages = []
  state.latestAssistant = ''
  state.latestAssistantId = null
  state.deliveredTurn = null
  state.errorTurn = null
}

// Harness surface messages require an id, a typed content array and a source.
// Own one replaceable user-role node; repeated turns must not append memory
// snapshots indefinitely or overwrite the host's system prompt.
function installMemory(session, messages, decision) {
  if (!session.surface || typeof session.eventAt !== 'function' || typeof session.append !== 'function') {
    throw new RequestBudgetError('REQUEST_PREVIEW_UNAVAILABLE', 'host does not expose replaceable surface nodes')
  }
  const owned = [...session.surface.nodes].filter((seq) => {
    const event = session.eventAt(seq)
    return event?.type === 'user/message' && event.data?.source?.kind === name
  })
  const body = messages?.map((message) => message.content).join('\n\n') ?? ''
  const makeMessage = (text) => ({
    id: randomUUID(), role: 'user', source: { kind: name },
    content: text ? [{ type: 'text', text }] : [],
  })
  // This is our cloned pending array, consumed by Harness after agent/request.
  // Remove a candidate left by an earlier request attempt before replacing it.
  if (decision) {
    const retained = decision.messages.filter((message) => message.source?.kind !== name)
    decision.messages.splice(0, decision.messages.length, ...retained)
  }
  if (owned.length === 0) {
    if (body) {
      const hasSystem = [...session.surface.nodes].some((seq) => session.eventAt(seq)?.type === 'system/message')
      // On the very first request the loop has not reserved its system node
      // yet. Let admission commit our candidate AFTER that system prompt.
      if (!hasSystem && decision) decision.messages.unshift(makeMessage(body))
      else session.append('user/message', makeMessage(body), { surfaceOp: 'append' })
    }
    return
  }
  for (let index = 0; index < owned.length; index++) {
    const seq = owned[index]
    const text = index === owned.length - 1 ? body : ''
    if (textOfContent(session.eventAt(seq).data.content) === text) continue
    session.append('user/message', makeMessage(text), {
      surfaceOp: { op: 'replace', startSeq: seq, endSeq: seq }, sourceEventSeqs: [seq],
    })
  }
}

function nativeMemory(messages) {
  return { role: 'user', source: { kind: name }, id: '00000000-0000-0000-0000-000000000000',
    content: [{ type: 'text', text: messages.map((message) => message.content).join('\n\n') }] }
}

function reportBudget(ctx, state, stage, budget, memoryStatus) {
  // Trusted lifecycle telemetry, never a model tool or a copied request body.
  const report = { stage, ...budget, memory_status: memoryStatus }
  state.lastBudget = report
  ctx.emit?.('memorydreaming/budget', sessionId(state.session), report)
}

export function apply(ctx, config = {}) {
  if (config.enabled === false) return
  const logger = safeLogger(ctx)
  const bridge = new BridgeClient(config, logger)
  const states = new Map()

  const observeEvent = (session, event) => {
    if (isSubagent(session)) return
    const state = stateFor(states, session)
    if (!state) return
    if (event?.type === 'turn/start') {
      state.turn = event.data?.turn
      return
    }
    if (event?.type === 'user/message' && event.data?.source?.kind === 'user') {
      // Already observed from this step's trusted pending input. Older queued
      // messages must not reset the latest prepared user's delivery linkage.
      if (state.pendingSurfaceUsers.delete(String(event.data.id))) return
      resetUser(state, event)
      return
    }
    if (event?.type === 'assistant/message') {
      const content = eventContent(event)
      if (content.trim()) {
        state.latestAssistant = content
        state.latestAssistantId = event.data?.id ?? event.data?.message?.id ?? state.latestAssistantId
      }
    }
  }

  ctx.on('session/created', (session) => stateFor(states, session))
  ctx.on('session/event', observeEvent)
  ctx.on('session/disposed', (session) => {
    const id = sessionId(session)
    states.delete(id)
    if (bridge.child) void bridge.request('close_session', { conversation_id: id }).catch(() => {})
  })

  // Verified against the installed Harness: assembly contains sections,
  // contexts, variables and tools. pre-step contains pending additions only.
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next()
    const session = context?.agent?.session
    if (session && !isSubagent(session)) stateFor(states, session).assembly = assembly
    return assembly
  })

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    if (decision?.kind !== 'enter' || !Array.isArray(decision.messages)) return decision
    const agent = payload?.agent
    const session = agent?.session
    if (!session || isSubagent(session) || payload?.signal?.aborted) return decision
    const state = stateFor(states, session)
    state.pendingSurfaceUsers.clear()
    // The real user/message surface event is committed AFTER pre-step. Read
    // trusted pending input here too, so the first request restores memory.
    for (const user of decision.messages.filter((m) => m.source?.kind === 'user')) {
      state.pendingSurfaceUsers.add(String(user.id))
      resetUser(state, { data: user })
      const pending = state.pendingUser
      if (!pending) continue
      try {
        // Persistence precedes metadata resolution, budget fitting and provider
        // dispatch. Multiple queued real inputs each keep their own event id.
        const stored = await bridge.request('prepare_turn', {
          conversation_id: sessionId(session), message_id: pending.messageId,
          content: pending.content, max_tokens: 0,
        }, payload?.signal)
        if (state.pendingUser === pending) {
          state.prepared = true
          state.userEventId = stored.user_event_id
        }
      } catch (error) {
        logger.warn?.(`[memorydreaming] user event persistence failed: ${error.code ?? 'UNAVAILABLE'}`)
      }
    }
    state.decision = { ...decision, messages: [...decision.messages] }
    return state.decision
  })

  ctx.on('agent/request', async (payload, next) => {
    const route = await next()
    const session = payload?.agent?.session
    if (!session || isSubagent(session) || payload?.signal?.aborted) return route
    const state = stateFor(states, session)
    state.requestSignal = payload?.signal
    if (!state.pendingUser || !state.decision) return route
    const pending = state.pendingUser
    let memoryMessages = [], status = 'skipped_budget', budget = { status: 'unavailable', count_mode: null }
    try {
      // Metadata lookup is advisory; the final guard uses the bound request's
      // logged context and resolved maxTokens, after all admission changes.
      let metadata = {}
      const llm = ctx.get?.('llm')
      if (typeof llm?.resolveModelInfo === 'function') {
        const info = await llm.resolveModelInfo(route.provider, route.model, payload?.signal)
        metadata = { contextWindow: info.context?.contextWindow, defaultMaxTokens: info.defaultMaxTokens }
      }
      const preview = previewRequest(session, state.decision, state.assembly, route)
      budget = measureRequest(preview, config, metadata)
      let limit = budget.status === 'fits' ? Math.min(boundedMaxTokens(config.maxTokens), budget.remaining_tokens) : 0
      // The bridge uses its text counter; the native envelope and the whole
      // request must fit too. Halving guarantees bounded retries, even with a
      // target tokenizer whose token counts differ from the Python estimate.
      while (true) {
        let prepared
        try {
          prepared = await bridge.request('prepare_turn', {
            conversation_id: sessionId(session), message_id: pending.messageId,
            content: pending.content, max_tokens: limit,
          }, payload?.signal)
        } catch (error) {
          if (error.code !== 'CONTEXT_BUDGET_TOO_SMALL' || limit === 0) throw error
          limit = 0
          status = 'skipped_minimum_core'
          continue
        }
        if (state.pendingUser !== pending || payload?.signal?.aborted || states.get(sessionId(session)) !== state) return route
        state.prepared = true
        state.userEventId = prepared.user_event_id
        if (limit === 0) break
        memoryMessages = prepared.messages.slice(0, -1)
        const native = nativeMemory(memoryMessages)
        const candidate = measureRequest({ ...preview, messages: [native, ...preview.messages] }, config, metadata)
        const isolated = measureRequest({ ...route, messages: [native] }, config, metadata)
        if (candidate.status === 'fits' && isolated.input_tokens <= boundedMaxTokens(config.maxTokens)
            && countMemory([native]) <= boundedMaxTokens(config.maxTokens)) {
          budget = candidate
          status = prepared.truncated ? 'included_truncated' : 'included'
          break
        }
        memoryMessages = []
        limit = Math.floor(limit / 2)
      }
      installMemory(session, memoryMessages, state.decision)
      state.memoryMessages = memoryMessages
    } catch (error) {
      // A broken metadata/surface/counter path must still persist real input.
      memoryMessages = []
      state.memoryMessages = []
      try { installMemory(session, null, state.decision) } catch {}
      try {
        const stored = await bridge.request('prepare_turn', {
          conversation_id: sessionId(session), message_id: pending.messageId, content: pending.content, max_tokens: 0,
        }, payload?.signal)
        if (state.pendingUser === pending) {
          state.prepared = true
          state.userEventId = stored.user_event_id
        }
      } catch {}
      budget = { status: 'unavailable', reason: error.code ?? 'REQUEST_PREVIEW_UNAVAILABLE', count_mode: null }
      status = 'skipped_unavailable'
    }
    if (!status.startsWith('included') && state.errorTurn !== state.turn) {
      state.errorTurn = state.turn
      logger.warn?.(`[memorydreaming] memory ${status}: ${budget.reason ?? budget.status}`)
    }
    reportBudget(ctx, state, 'preview', budget, status)
    return route
  })

  // Cordis next() takes NO replacement request. Frozen requests cannot be
  // rewritten here. Veto before provider dispatch if final accounting fails.
  ctx.on('llm/stream', (options, next) => {
    const state = states.get(String(options.sessionId))
    // Auxiliary calls (e.g. title generation) may share the session id. Only
    // guard the agent request carrying the turn signal observed above.
    if (!state || isSubagent(state.session) || !state.requestSignal || state.requestSignal !== options.signal) return next()
    return (async function* () {
      const context = state.session.requestContext?.()
      const metadata = context?.provider === options.provider && context?.model === options.model
        ? { contextWindow: context.contextWindow } : {}
      const budget = measureRequest(options, config, metadata)
      reportBudget(ctx, state, 'dispatch', budget, state.lastBudget?.memory_status ?? 'none')
      const retainedMemory = options.messages.filter((message) => message.source?.kind === name && textOfContent(message.content))
      if (budget.status === 'exceeded') {
        throw new RequestBudgetError('CONTEXT_WINDOW_EXCEEDED', 'Complete request exceeds context budget; host must compact history or reduce tools/output reserve')
      }
      if (budget.status === 'unavailable' && retainedMemory.length > 0) {
        throw new RequestBudgetError('REQUEST_BUDGET_UNAVAILABLE', 'Cannot verify memory injection against the bound request limits')
      }
      if (retainedMemory.length > 0 && budget.status === 'fits') {
        const isolated = measureRequest({ ...options, messages: retainedMemory, tools: [], toolHistory: undefined }, config, metadata)
        if (isolated.input_tokens > boundedMaxTokens(config.maxTokens)
            || countMemory(retainedMemory) > boundedMaxTokens(config.maxTokens)) {
          throw new RequestBudgetError('MEMORY_BUDGET_EXCEEDED', 'Retained memory exceeds the per-request memory cap')
        }
      }
      yield* next()
    })()
  })

  ctx.on('agent/turn-stopping', ({ agent, turn, signal }) => {
    const session = agent?.session
    if (!session || isSubagent(session)) return
    const state = stateFor(states, session)
    if (!state?.prepared || !state.userEventId || !state.latestAssistant.trim()) return
    if (state.deliveredTurn === turn) return
    state.deliveredTurn = turn
    void bridge.request('after_delivery', {
      conversation_id: sessionId(session),
      message_id: String(state.latestAssistantId ?? `assistant-${turn}`),
      user_event_id: state.userEventId,
      content: state.latestAssistant,
      delivery_state: signal?.aborted ? 'interrupted' : 'final',
      // The host cannot reliably know which opaque reference the model used;
      // leaving this empty avoids falsely attributing every injected record.
      used_references: [],
    }, signal).catch((error) => logger.warn?.(`[memorydreaming] after_delivery skipped: ${error.message}`))
  })

  // 不同 Harness 版本对压缩事件的名称略有差异。收到宿主提供的
  // checkpoint 时先持久化短 handoff；没有 state 时也会保存当前活动快照。
  const beforeCompression = async (payload) => {
    const session = payload?.agent?.session ?? payload?.session
    if (!session || isSubagent(session)) return
    const state = stateFor(states, session)
    if (!state) return
    const data = payload?.state ?? payload?.checkpoint ?? null
    try {
      await bridge.request('pre_compress', {
        conversation_id: sessionId(session),
        state: data && typeof data === 'object' ? data : null,
        expected_version: Number.isInteger(payload?.expected_version) ? payload.expected_version : undefined,
        idempotency_key: typeof payload?.idempotency_key === 'string' ? payload.idempotency_key : undefined,
        max_tokens: boundedMaxTokens(config.maxTokens),
      }, payload?.signal)
    } catch (error) {
      logger.warn?.(`[memorydreaming] pre_compress skipped: ${error.message}`)
    }
  }
  ctx.on('agent/context-compressing', beforeCompression)
  ctx.on('session/context-compressing', beforeCompression)

  // Cordis versions expose dispose as an event; process exit is the fallback
  // for older hosts. The bridge is local and has no network listener.
  if (typeof ctx.on === 'function') ctx.on('dispose', () => bridge.close())
  process.once('exit', () => {
    if (bridge.child && !bridge.child.killed) bridge.child.kill()
  })
}

export const __testing = { textOfContent, hasMemoryContext, boundedMaxTokens, BridgeClient, installMemory }
