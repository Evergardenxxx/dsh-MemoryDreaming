import { randomUUID } from 'node:crypto'
import { countMemory, measureRequest, previewRequest, RequestBudgetError } from './request-budget.js'

import { BridgeClient, boundedMaxTokens } from './bridge-client.js'
import { DeliveryOutbox } from './delivery-outbox.js'
import { configureLocalTokenizer } from './local-tokenizer.js'

export const name = 'memorydreaming'
// The plugin uses Cordis lifecycle events directly. It deliberately registers
// no tools and therefore adds no MCP/tool schema to model requests.
export const inject = []

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
      deliveryGuard: null,
      memoryMessages: [],
      latestAssistant: '',
      latestAssistantId: null,
      turn: null,
      errorTurn: null,
      decision: null,
      assembly: null,
      pendingSurfaceUsers: new Set(),
      lifetime: new AbortController(),
      requestRevision: 0,
      deliveryPending: new Map(),
      delivered: new Set(),
      latestInterrupted: false,
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
  state.deliveryGuard = null
  state.memoryMessages = []
  state.latestAssistant = ''
  state.latestAssistantId = null
  state.latestInterrupted = false
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

function scopedSignal(state, signal) {
  return signal ? AbortSignal.any([signal, state.lifetime.signal]) : state.lifetime.signal
}

async function boundedLookup(operation, signal, milliseconds) {
  const controller = new AbortController()
  const combined = AbortSignal.any([controller.signal, signal])
  let timer, onAbort
  try {
    return await Promise.race([
      Promise.resolve().then(() => { combined.throwIfAborted(); return operation(combined) }),
      new Promise((_, reject) => {
        onAbort = () => reject(Object.assign(new Error('Memory metadata lookup stopped'), {
          code: signal.aborted ? 'ABORTED' : 'METADATA_TIMEOUT',
        }))
        combined.addEventListener('abort', onAbort, { once: true })
        timer = setTimeout(() => controller.abort(), milliseconds)
      }),
    ])
  } finally {
    clearTimeout(timer)
    combined.removeEventListener('abort', onAbort)
  }
}

export function apply(ctx, config = {}) {
  if (config.enabled === false) return
  config = configureLocalTokenizer(config)
  const logger = safeLogger(ctx)
  const bridge = new BridgeClient(config, logger)
  const outbox = new DeliveryOutbox(config, bridge, logger, report => ctx.emit?.('memorydreaming/health', report))
  const states = new Map()
  const disposedSessions = new WeakSet()
  const deliveryTasks = new Set()
  let disposed = false
  let disposalPromise
  const usable = session => !disposed && session && !disposedSessions.has(session) && !isSubagent(session)
  const live = (state, pending) => usable(state.session) && states.get(sessionId(state.session)) === state
    && !state.lifetime.signal.aborted && (pending === undefined || state.pendingUser === pending)

  const saveDelivery = (state, turn, deliveryState) => {
    if (!state?.prepared || !state.userEventId || !state.latestAssistant.trim()) return Promise.resolve()
    if (state.turn !== null && turn !== undefined && state.turn !== turn) return Promise.resolve()
    const request = {
      conversation_id: sessionId(state.session),
      message_id: String(state.latestAssistantId ?? `assistant-${turn}-${state.pendingUser?.messageId}`),
      user_event_id: state.userEventId, content: state.latestAssistant,
      delivery_state: state.latestInterrupted ? 'interrupted' : deliveryState,
      used_references: [],
      expected_guard: state.deliveryGuard,
    }
    const key = JSON.stringify([request.user_event_id, request.message_id])
    if (state.delivered.has(key)) return Promise.resolve()
    if (state.deliveryPending.has(key)) return state.deliveryPending.get(key)
    // Commit to the local durable queue before returning from admission.
    // Acknowledgement removes the file; uncertain results survive a restart.
    const task = (async () => {
      if (await outbox.deliver(request)) {
        state.delivered.add(key)
        while (state.delivered.size > 128) state.delivered.delete(state.delivered.values().next().value)
      }
    })()
    state.deliveryPending.set(key, task)
    deliveryTasks.add(task)
    void task.finally(() => { state.deliveryPending.delete(key); deliveryTasks.delete(task) })
    return task
  }

  const observeEvent = (session, event) => {
    if (!usable(session)) return
    const state = stateFor(states, session)
    if (!state) return
    if (event?.type === 'turn/start') {
      state.turn = event.data?.turn
      state.latestAssistant = ''
      state.latestAssistantId = null
      state.latestInterrupted = false
      state.requestRevision++
      return
    }
    if (event?.type === 'turn/end') {
      if (event.data?.turn !== undefined && state.turn !== null && event.data.turn !== state.turn) return
      const kind = event.data?.reason?.kind
      void saveDelivery(state, event.data?.turn, kind === 'completed' || kind === 'max-tokens' ? 'final' : 'interrupted')
      state.requestRevision++
      return
    }
    if (event?.type === 'compaction/start') {
      state.compacting = true
      // This installed Harness exposes a notification, not an awaited pre-hook.
      // Best-effort persist the EXISTING activity checkpoint; never treat its
      // generated compaction summary as new user facts or an approved core.
      void beforeCompression({ session, idempotency_key: `compaction-${event.data?.compactionId ?? event.seq}` })
      return
    }
    if (event?.type === 'compaction/end') {
      state.compacting = false
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
      if (event.data?.turn !== undefined && state.turn !== null && event.data.turn !== state.turn) return
      const content = eventContent(event)
      if (content.trim()) {
        state.latestAssistant = content
        state.latestAssistantId = event.data?.id ?? event.data?.message?.id ?? state.latestAssistantId
        state.latestInterrupted = event.data?.interrupted === true
        void saveDelivery(state, event.data?.turn, state.latestInterrupted ? 'interrupted' : 'final')
      }
    }
  }

  ctx.on('session/created', (session) => {
    if (!usable(session)) return
    const previous = states.get(sessionId(session))
    if (previous && previous.session !== session) {
      disposedSessions.add(previous.session)
      previous.lifetime.abort()
      previous.requestRevision++
      states.delete(sessionId(session))
    }
    stateFor(states, session)
  })
  ctx.on('session/event', observeEvent)
  ctx.on('session/disposed', async (session) => {
    const id = sessionId(session)
    const state = states.get(id)
    disposedSessions.add(session)
    if (state?.session !== session) return
    states.delete(id)
    state.requestRevision++
    state.lifetime.abort()
    await saveDelivery(state, state.turn, 'interrupted')
    if (bridge.child) await bridge.request('close_session', { conversation_id: id }).catch(() => {})
  })

  // Verified against the installed Harness: assembly contains sections,
  // contexts, variables and tools. pre-step contains pending additions only.
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next()
    const session = context?.agent?.session
    if (usable(session)) stateFor(states, session).assembly = assembly
    return assembly
  })

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    if (decision?.kind !== 'enter' || !Array.isArray(decision.messages)) return decision
    const agent = payload?.agent
    const session = agent?.session
    if (!usable(session) || payload?.signal?.aborted) return decision
    const state = stateFor(states, session)
    const revision = ++state.requestRevision
    if (payload?.turn !== undefined) state.turn = payload.turn
    state.pendingSurfaceUsers.clear()
    // The real user/message surface event is committed AFTER pre-step. Read
    // trusted pending input here too, so the first request restores memory.
    const actualUsers = decision.messages.filter((m) => m.source?.kind === 'user')
    // Hot reload / resume can enter a tool step with no new pending input.
    // Rebind the last retained real user via the same durable idempotency key.
    if (actualUsers.length === 0 && !state.pendingUser && typeof session.deriveMessages === 'function') {
      const previous = session.deriveMessages().filter((m) => m.source?.kind === 'user').at(-1)
      if (previous) actualUsers.push(previous)
    }
    for (const user of actualUsers) {
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
        }, state.lifetime.signal)
        if (live(state, pending) && state.requestRevision === revision) {
          state.prepared = true
          state.userEventId = stored.user_event_id
          state.deliveryGuard = stored.delivery_guard
        }
      } catch (error) {
        logger.warn?.(`[memorydreaming] user event persistence failed: ${error.code ?? 'UNAVAILABLE'}`)
      }
    }
    if (!live(state) || state.requestRevision !== revision || payload?.signal?.aborted) return decision
    state.decision = { ...decision, messages: [...decision.messages] }
    return state.decision
  })

  ctx.on('agent/request', async (payload, next) => {
    const route = await next()
    const session = payload?.agent?.session
    if (!usable(session) || payload?.signal?.aborted) return route
    const state = stateFor(states, session)
    state.requestSignal = payload?.signal
    if (!state.pendingUser || !state.decision) return route
    const pending = state.pendingUser
    const revision = ++state.requestRevision
    const signal = scopedSignal(state, payload?.signal)
    const current = () => live(state, pending) && state.requestRevision === revision && !signal.aborted
    let memoryMessages = [], status = 'skipped_budget', budget = { status: 'unavailable', count_mode: null }
    try {
      // Metadata lookup is advisory; the final guard uses the bound request's
      // logged context and resolved maxTokens, after all admission changes.
      let metadata = {}
      const llm = ctx.get?.('llm')
      if (typeof llm?.resolveModelInfo === 'function') {
        const info = await boundedLookup(
          lookupSignal => llm.resolveModelInfo(route.provider, route.model, lookupSignal), signal, bridge.timeoutMs,
        )
        metadata = { contextWindow: info.context?.contextWindow, defaultMaxTokens: info.defaultMaxTokens }
      }
      if (!current()) return route
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
          }, signal)
        } catch (error) {
          if (error.code !== 'CONTEXT_BUDGET_TOO_SMALL' || limit === 0) throw error
          limit = 0
          status = 'skipped_minimum_core'
          continue
        }
        if (!current()) return route
        state.prepared = true
        state.userEventId = prepared.user_event_id
        state.deliveryGuard = prepared.delivery_guard
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
      if (!current()) return route
      // A broken metadata/surface/counter path must still persist real input.
      memoryMessages = []
      state.memoryMessages = []
      try { installMemory(session, null, state.decision) } catch {}
      try {
        const stored = await bridge.request('prepare_turn', {
          conversation_id: sessionId(session), message_id: pending.messageId, content: pending.content, max_tokens: 0,
        }, state.lifetime.signal)
        if (current()) {
          state.prepared = true
          state.userEventId = stored.user_event_id
          state.deliveryGuard = stored.delivery_guard
        }
      } catch {}
      budget = { status: 'unavailable', reason: error.code ?? 'REQUEST_PREVIEW_UNAVAILABLE', count_mode: null }
      status = 'skipped_unavailable'
    }
    if (!current()) return route
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
    if (!state || state.compacting || isSubagent(state.session) || !state.requestSignal || state.requestSignal !== options.signal) return next()
    return (async function* () {
      if (!live(state) || options.signal?.aborted) {
        throw Object.assign(new Error('Memory request belongs to a closed or cancelled session'), { code: 'ABORTED' })
      }
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
    if (!usable(session)) return
    return saveDelivery(states.get(sessionId(session)), turn, signal?.aborted ? 'interrupted' : 'final')
  })

  // 不同 Harness 版本对压缩事件的名称略有差异。收到宿主提供的
  // checkpoint 时先持久化短 handoff；没有 state 时也会保存当前活动快照。
  const beforeCompression = async (payload) => {
    const session = payload?.agent?.session ?? payload?.session
    if (!usable(session)) return
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
      }, scopedSignal(state, payload?.signal))
    } catch (error) {
      logger.warn?.(`[memorydreaming] pre_compress skipped: ${error.message}`)
    }
  }
  ctx.on('agent/context-compressing', beforeCompression)
  ctx.on('session/context-compressing', beforeCompression)

  const dispose = () => {
    if (disposalPromise) return disposalPromise
    outbox.close()
    for (const state of states.values()) {
      state.requestRevision++
      state.lifetime.abort()
      void saveDelivery(state, state.turn, 'interrupted')
    }
    disposed = true
    states.clear()
    disposalPromise = (async () => {
      let timer
      try {
        await Promise.race([Promise.allSettled([...deliveryTasks]), new Promise(resolve => { timer = setTimeout(resolve, bridge.shutdownMs) })])
      } finally { clearTimeout(timer); await bridge.close() }
    })()
    return disposalPromise
  }
  // Installed Cordis 4 owns teardown via effects; it does not emit a plugin
  // "dispose" event. Legacy hosts and the small compatibility fixture retain
  // the event fallback. Both routes return the same bounded disposal promise.
  if (typeof ctx.effect === 'function') ctx.effect(() => dispose, 'memorydreaming lifecycle')
  else ctx.on('dispose', dispose)
}

export const __testing = { textOfContent, hasMemoryContext, boundedMaxTokens, BridgeClient, installMemory }
