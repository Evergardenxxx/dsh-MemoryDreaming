// Admission order and message shape verified against the installed Harness.
// This is an offline lifecycle fixture, not the real application's acceptance.
export function harness() {
  const handlers = new Map(), budgets = [], warnings = []
  const ctx = {
    logger: { debug() {}, warn(message) { warnings.push(message) } },
    on(event, handler) { handlers.set(event, handler) },
    emit(event, session, report) { if (event === 'memorydreaming/budget') budgets.push({ session, ...report }) },
    get(name) {
      if (name === 'llm') return {
        async resolveModelInfo(provider, id) {
          return { provider, id, context: { contextWindow: 131072 }, defaultMaxTokens: 8192 }
        },
      }
    },
  }
  function session(id) {
    const events = new Map(), nodes = []
    let sequence = 0, context
    return {
      id, header: {}, surface: { nodes },
      eventAt(seq) { return events.get(seq) },
      deriveMessages() {
        return nodes.map(seq => events.get(seq)).map(e => e.type === 'user/message' ? e.data : e.data.message)
      },
      toolHistory() { return { tools: [], updates: [] } },
      requestContext() { return context },
      setContext(value) { context = value },
      append(type, data, intent = {}) {
        const message = type === 'user/message' ? data : data.message
        if (!message.id || !Array.isArray(message.content) || !message.source?.kind) throw new Error('invalid native message')
        if (type === 'user/message' && message.role !== 'user') throw new Error('invalid role for user/message')
        const event = { seq: ++sequence, type, data }
        events.set(event.seq, event)
        if (intent.surfaceOp === 'append') nodes.push(event.seq)
        else if (intent.surfaceOp?.op === 'replace') {
          const index = nodes.indexOf(intent.surfaceOp.startSeq)
          if (index < 0 || intent.surfaceOp.startSeq !== intent.surfaceOp.endSeq) throw new Error('invalid replacement')
          nodes.splice(index, 1, event.seq)
        }
        handlers.get('session/event')?.(this, event)
        return event
      },
    }
  }
  async function step(session, text, id, { tools = [], pending = [], maxTokens = 8192, sections = [], turn = 't1', signal = new AbortController().signal } = {}) {
    const agent = { session }
    const assembly = { sections, contexts: [], variables: {}, tools }
    await handlers.get('system-prompt/assemble')(assembly, { agent, signal }, async () => assembly)
    const user = text === null ? [] : [{ id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] }]
    const decision = await handlers.get('agent/pre-step')({ agent, signal, turn },
      async () => ({ kind: 'enter', messages: [...user, ...pending] }))
    const config = await handlers.get('agent/request')({ agent, signal, turn },
      async () => ({ provider: 'deepseek', model: 'fixture-model', maxTokens }))
    // Real admission reserves a system node, even for an empty prompt, before
    // committing decision.messages. The plugin must preserve this ordering.
    if (!session.deriveMessages().some(m => m.role === 'system')) {
      session.append('system/message', { message: { id: 'system-0', role: 'system',
        source: { kind: 'system-prompt' }, content: [] } }, { surfaceOp: 'append' })
    }
    for (const message of decision.messages) session.append('user/message', message, { surfaceOp: 'append' })
    session.setContext({ ...config, contextWindow: 131072 })
    return { ...config, messages: session.deriveMessages(), tools, signal, sessionId: session.id }
  }
  async function dispatch(options, beforeProvider = () => {}) {
    let called = false
    const stream = handlers.get('llm/stream')(Object.freeze(options), async function* () {
      called = true
      beforeProvider()
      yield { type: 'finish', reason: { kind: 'stop' } }
    })
    const chunks = []
    for await (const chunk of stream) chunks.push(chunk)
    return { called, chunks }
  }
  return { ctx, handlers, session, step, dispatch, budgets, warnings }
}
