// Native Harness message/session helpers. This layer does not read the database.
import { randomUUID } from 'node:crypto'
import { RequestBudgetError } from './request-budget.js'
export const name = 'memorydreaming'

export function textOfContent(content) {
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

export function eventContent(event) {
  const data = event?.data ?? {}
  return textOfContent(data.content ?? data.message?.content)
}

export function sessionId(session) {
  return session?.id === undefined ? '' : String(session.id)
}

export function isSubagent(session) {
  return session?.header?.parentSession !== undefined
}

export function hasMemoryContext(messages) {
  return Array.isArray(messages) && messages.some((message) => textOfContent(message?.content).includes('<memory-context'))
}

export function safeLogger(ctx) {
  return ctx?.logger ?? console
}

export function stateFor(states, session) {
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
      window: null,
      command: null,
      controlReply: null,
    }
    states.set(id, state)
  } else {
    state.session = session
  }
  return state
}

export function resetUser(state, event) {
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
export function installMemory(session, messages, decision) {
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

export function nativeMemory(messages) {
  return { role: 'user', source: { kind: name }, id: '00000000-0000-0000-0000-000000000000',
    content: [{ type: 'text', text: messages.map((message) => message.content).join('\n\n') }] }
}

export function reportBudget(ctx, state, stage, budget, memoryStatus) {
  // Trusted lifecycle telemetry, never a model tool or a copied request body.
  const report = { stage, ...budget, memory_status: memoryStatus }
  state.lastBudget = report
  ctx.emit?.('memorydreaming/budget', sessionId(state.session), report)
}

export function scopedSignal(state, signal) {
  return signal ? AbortSignal.any([signal, state.lifetime.signal]) : state.lifetime.signal
}

export async function boundedLookup(operation, signal, milliseconds) {
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
