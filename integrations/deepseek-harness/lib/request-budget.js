// Trusted host accounting only. No request bodies or measurements go to the model.
export class RequestBudgetError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
  }
}

function natural(value, name, positive = false) {
  if (!Number.isSafeInteger(value) || value < (positive ? 1 : 0)) {
    throw new RequestBudgetError('INVALID_REQUEST_BUDGET', `${name} must be a ${positive ? 'positive' : 'nonnegative'} safe integer`)
  }
  return value
}

// Same fallback density as the Python adapter. This is NOT a token upper bound.
export function estimateTokens(text) {
  const cjk = (text.match(/[\u4e00-\u9fff]/gu) ?? []).length
  return Math.floor(cjk + (Array.from(text).length - cjk) / 4) + 1
}

export function countMemory(messages) {
  return estimateTokens(JSON.stringify(messages))
}

function tokenInput(request) {
  // Include reasoning/replay state, tool history and extended request fields;
  // exclude execution handles. The exact callback must apply the target
  // provider's projection and chat template itself, including role framing.
  const { signal, sessionId, ...input } = request
  return input
}

export function measureRequest(request, config, metadata = {}) {
  const contextWindow = config.contextWindow === undefined
    ? metadata.contextWindow
    : natural(config.contextWindow, 'contextWindow', true)
  // A configured window can tighten provider metadata, never enlarge it.
  const window = contextWindow === undefined ? undefined : Math.min(
    natural(contextWindow, 'contextWindow', true),
    metadata.contextWindow === undefined ? contextWindow : natural(metadata.contextWindow, 'model contextWindow', true),
  )
  const actualOutput = request.maxTokens ?? metadata.defaultMaxTokens
  const reserve = config.outputReserve === undefined ? undefined : natural(config.outputReserve, 'outputReserve', true)
  const margin = config.safetyMargin === undefined ? 512 : natural(config.safetyMargin, 'safetyMargin')
  const overhead = config.requestOverhead === undefined ? 64 : natural(config.requestOverhead, 'requestOverhead')
  if (window === undefined || (actualOutput === undefined && reserve === undefined)) {
    return { status: 'unavailable', reason: 'REQUEST_LIMITS_UNKNOWN', count_mode: null }
  }
  const output = Math.max(reserve ?? 0, actualOutput === undefined ? 0 : natural(actualOutput, 'maxTokens', true))
  const input = tokenInput(request)
  let tokens, mode, precision
  if (config.requestTokenCounter !== undefined) {
    const counter = config.requestTokenCounter
    if (!counter || !['exact', 'estimated'].includes(counter.mode) || typeof counter.count !== 'function') {
      throw new RequestBudgetError('INVALID_REQUEST_BUDGET', 'requestTokenCounter requires mode and count(request)')
    }
    tokens = natural(counter.count(input), 'requestTokenCounter result')
    mode = counter.mode
    if (counter.precision && typeof counter.precision === 'object' && !Array.isArray(counter.precision)) {
      // Fixed metadata only: never copy model input or arbitrary callback state.
      precision = Object.fromEntries(['text_tokenization', 'template_fidelity', 'projection', 'tokenizer_revision', 'tokenizer_sha256']
        .filter(key => typeof counter.precision[key] === 'string' && counter.precision[key].length <= 200)
        .map(key => [key, counter.precision[key]]))
    }
  } else {
    tokens = estimateTokens(JSON.stringify(input))
    mode = 'estimated'
  }
  const total = tokens + output + overhead + margin
  natural(total, 'total tokens')
  return {
    status: total <= window ? 'fits' : 'exceeded', count_mode: mode,
    context_window: window, input_tokens: tokens, output_reserve: output,
    request_overhead: overhead, safety_margin: margin, total_tokens: total,
    remaining_tokens: Math.max(0, window - total),
    ...(precision ? { precision } : {}),
  }
}

function renderSections(sections, variables) {
  return sections.map((section) => {
    if (typeof section.text !== 'string') throw new RequestBudgetError('REQUEST_PREVIEW_UNAVAILABLE', 'unrendered prompt section')
    if (section.interpolate === false) return section.text
    return section.text.replace(/\{\{([^{}]*)\}\}/gu, (_, name) => {
      if (typeof variables[name] !== 'string') throw new RequestBudgetError('REQUEST_PREVIEW_UNAVAILABLE', 'unresolved prompt variable')
      return variables[name]
    })
  }).filter(Boolean).join('\n\n')
}

export function previewRequest(session, decision, assembly, route) {
  if (!assembly || !Array.isArray(assembly.tools) || typeof session.deriveMessages !== 'function') {
    throw new RequestBudgetError('REQUEST_PREVIEW_UNAVAILABLE', 'host must expose accepted assembly and retained messages')
  }
  const variables = assembly.variables ?? {}
  const system = renderSections(assembly.sections, variables)
  const context = renderSections(assembly.contexts, variables)
  // Keep existing system/context snapshots as well as the new assembly. The
  // admission layer may replace them; counting both deliberately over-reserves.
  // Pending input may already be retained on a retry; count it again here.
  const messages = [...session.deriveMessages(), ...decision.messages]
    .filter((m) => m.source?.kind !== 'memorydreaming')
  if (system) messages.push({ role: 'system', content: [{ type: 'text', text: system }] })
  if (context) messages.push({ role: 'user', content: [{ type: 'text', text: `Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n${context}` }] })
  return { ...route, messages, tools: assembly.tools,
    ...(typeof session.toolHistory === 'function' ? { toolHistory: session.toolHistory() } : {}) }
}
