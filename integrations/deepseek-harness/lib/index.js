import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

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
      else entry.reject(new Error(response.error?.message ?? 'memory bridge request failed'))
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

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    if (decision?.kind !== 'enter' || !Array.isArray(decision.messages)) return decision
    const agent = payload?.agent
    const session = agent?.session
    if (!session || isSubagent(session) || payload?.signal?.aborted) return decision
    const state = stateFor(states, session)
    if (!state?.pendingUser) return decision
    if (!state.prepared) {
      try {
        const prepared = await bridge.request('prepare_turn', {
          conversation_id: sessionId(session),
          message_id: state.pendingUser.messageId,
          content: state.pendingUser.content,
          max_tokens: boundedMaxTokens(config.maxTokens),
        }, payload?.signal)
        state.prepared = true
        state.userEventId = prepared.user_event_id
        state.memoryMessages = Array.isArray(prepared.messages) ? prepared.messages.slice(0, 2) : []
      } catch (error) {
        if (state.errorTurn !== state.turn) {
          state.errorTurn = state.turn
          logger.warn?.(`[memorydreaming] prepare_turn skipped: ${error.message}`)
        }
        return decision
      }
    }
    if (state.memoryMessages.length === 0 || hasMemoryContext(decision.messages)) return decision
    return { ...decision, messages: [...state.memoryMessages, ...decision.messages] }
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

export const __testing = { textOfContent, hasMemoryContext, boundedMaxTokens, BridgeClient }
