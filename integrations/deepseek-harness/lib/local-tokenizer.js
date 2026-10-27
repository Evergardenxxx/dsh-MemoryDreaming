// Local optional tokenizer, with an explicit provider/model/hash manifest.
// Tokenization of projected JSON is exact; the hosted template remains estimated.
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { statSync } from 'node:fs'
import { join } from 'node:path'
import { RequestBudgetError } from './request-budget.js'

function unsupported(message) {
  throw new RequestBudgetError('TOKENIZER_PROJECTION_UNSUPPORTED', message)
}

function blocks(content) {
  if (typeof content === 'string') return [{ type: 'text', text: content }]
  if (!Array.isArray(content)) unsupported('Expected native typed message content')
  return content
}

export function projectDeepSeekTextRequest(request, { inHistory = true } = {}) {
  const allowed = new Set(['provider', 'model', 'messages', 'system', 'tools', 'toolHistory', 'maxTokens', 'reasoningEffort', 'temperature', 'stop', 'purpose'])
  if (Object.keys(request).some(key => !allowed.has(key))) unsupported('Unknown request fields require a provider-owned projection')
  if (request.system !== undefined && typeof request.system !== 'string') unsupported('System prompt must be text')
  if (!Array.isArray(request.messages)) unsupported('Expected complete native message history')
  if (request.toolHistory && ((request.toolHistory.updates?.length ?? 0) > 0)) {
    unsupported('Tool history updates require the route-owned developer-message projection')
  }
  const messages = [], updates = []
  let historySystem
  const flush = () => {
    if (!updates.length) return
    if (messages.at(-1)?.role !== 'user') unsupported('System updates require a preceding user turn')
    messages.push(...updates.splice(0))
  }
  for (const message of request.messages) {
    if (!['system', 'developer', 'user', 'assistant', 'tool'].includes(message.role)) unsupported('Unsupported message role')
    const input = blocks(message.content)
    if (message.role === 'developer') {
      const content = input.flatMap(block => {
        if (block.type === 'text' && typeof block.text === 'string') return block.text ? [{ type: 'text', text: block.text }] : []
        if (['tool-addition', 'tool-removal'].includes(block.type) && typeof block.toolName === 'string') {
          return [{ type: block.type.replace('-', '_'), tool: { type: 'tool_reference', name: block.toolName } }]
        }
        return unsupported('Unsupported developer content')
      })
      if (content.length) updates.push({ role: 'system', content })
      continue
    }
    if (message.role === 'system') {
      if (input.some(block => block.type !== 'text' || typeof block.text !== 'string')) unsupported('Unsupported system content')
      const text = input.map(block => block.text).join('')
      if (inHistory && messages.length) {
        if (!text) unsupported('Empty in-history system update')
        updates.push({ role: 'system', content: [{ type: 'text', text }] })
      } else historySystem = text
      continue
    }
    const content = input.flatMap((block, index) => {
      if (block.type === 'text' && typeof block.text === 'string') return block.text ? [{ type: 'text', text: block.text }] : []
      if (message.role !== 'assistant' && ['reasoning', 'tool-call'].includes(block.type)) return []
      if (message.role === 'assistant' && block.type === 'reasoning' && typeof block.text === 'string') {
        const state = message.source?.replayState
        if (state !== undefined) unsupported('Native signed replay needs a route-owned tokenizer projection')
        return [{ type: 'thinking', thinking: block.text }]
      }
      if (message.role === 'assistant' && block.type === 'tool-call') {
        if (typeof block.id !== 'string' || typeof block.name !== 'string' || typeof block.arguments !== 'string') unsupported('Malformed tool call')
        let args = {}
        try { const parsed = JSON.parse(block.arguments); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) args = parsed } catch {}
        return [{ type: 'tool_use', id: block.id, name: block.name, input: args }]
      }
      return unsupported('Images, files, replay or extended content require provider-owned projection')
    })
    const wireContent = message.role === 'tool'
      ? [{ type: 'tool_result', tool_use_id: message.toolCallId, content, ...(message.isError === undefined ? {} : { is_error: message.isError }) }]
      : content
    if (message.role !== 'tool' && !wireContent.length) continue
    if (message.role === 'assistant') flush()
    const role = message.role === 'tool' ? 'user' : message.role
    if (messages.at(-1)?.role === role) messages.at(-1).content.push(...wireContent)
    else messages.push({ role, content: wireContent })
  }
  flush()
  let pending = new Set()
  for (const message of messages) {
    if (message.role === 'assistant') {
      const calls = message.content.filter(block => block.type === 'tool_use')
      pending = new Set(calls.map(block => block.id))
      if (pending.size !== calls.length) unsupported('Duplicate tool call IDs')
    } else if (message.role === 'user') {
      const results = message.content.filter(block => block.type === 'tool_result')
      for (const result of results) if (!pending.delete(result.tool_use_id)) unsupported('Tool result without matching call')
      if (pending.size) unsupported('Unresolved tool calls')
      message.content = [...results, ...message.content.filter(block => block.type !== 'tool_result')]
    }
  }
  if (pending.size) unsupported('Unresolved tool calls')
  const system = [request.system, historySystem].filter(Boolean).join('\n\n')
  const tools = request.tools?.map(tool => {
    if (typeof tool.name !== 'string' || typeof tool.description !== 'string' || !tool.parameters || typeof tool.parameters !== 'object') unsupported('Malformed tool definition')
    return { name: tool.name, description: tool.description, input_schema: tool.parameters, ...(tool.deferLoading === true ? { defer_loading: true } : {}) }
  })
  // Only model-input-bearing fields belong in tokenizer projection. Output cap,
  // network settings, request purpose and execution handles are not prompt text.
  return { model: request.model, messages, ...(system ? { system } : {}), ...(tools ? { tools } : {}) }
}

export class LocalDeepSeekRequestCounter {
  mode = 'estimated'
  constructor(options = {}) {
    if (typeof options.directory !== 'string' || !options.directory.trim()) throw new RequestBudgetError('TOKENIZER_CONFIG_INVALID', 'localTokenizer.directory is required')
    if (options.timeoutMs !== undefined && (!Number.isSafeInteger(options.timeoutMs) || options.timeoutMs < 100 || options.timeoutMs > 30000)) throw new RequestBudgetError('TOKENIZER_CONFIG_INVALID', 'timeoutMs must be 100..30000')
    this.options = options
    this.cache = new Map()
    this.precision = { text_tokenization: 'exact', template_fidelity: 'wire-json-estimate', projection: 'deepseek-messages-text-v1' }
  }
  count(request) {
    const projected = projectDeepSeekTextRequest(request, this.options)
    const payload = JSON.stringify({ provider: request.provider, model: request.model, projection: 'deepseek-messages-text-v1', projected })
    if (Buffer.byteLength(payload) > 16 * 1024 * 1024) throw new RequestBudgetError('TOKENIZER_INPUT_TOO_LARGE', 'Tokenizer input exceeds size limit')
    let assets
    try {
      assets = ['memorydreaming-tokenizer.json', 'tokenizer.json', 'tokenizer_config.json'].map(name => {
        const info = statSync(join(this.options.directory, name))
        return [name, info.size, info.mtimeMs, info.ctimeMs]
      })
    } catch { throw new RequestBudgetError('TOKENIZER_ASSET_INVALID', 'Local tokenizer assets unavailable') }
    const key = createHash('sha256').update(payload).update(JSON.stringify(assets)).digest('hex')
    if (this.cache.has(key)) return this.cache.get(key)
    const process = spawnSync(this.options.pythonCommand ?? 'python', ['-X', 'utf8', '-m', 'memorydreaming.tokenization', '--directory', this.options.directory], {
      input: payload, encoding: 'utf8', timeout: this.options.timeoutMs ?? 5000, maxBuffer: 65536, windowsHide: true,
    })
    if (process.error) throw new RequestBudgetError(process.error.code === 'ETIMEDOUT' ? 'TOKENIZER_TIMEOUT' : 'TOKENIZER_UNAVAILABLE', 'Local tokenizer process unavailable')
    let result
    try { result = JSON.parse(process.stdout) } catch { throw new RequestBudgetError('TOKENIZER_UNAVAILABLE', 'Local tokenizer returned invalid output') }
    if (!result.ok) throw new RequestBudgetError(result.error?.code ?? 'TOKENIZER_UNAVAILABLE', result.error?.message ?? 'Local tokenizer failed')
    const value = result.data?.input_tokens
    if (!Number.isSafeInteger(value) || value < 0 || result.data.count_mode !== 'estimated') throw new RequestBudgetError('TOKENIZER_UNAVAILABLE', 'Invalid tokenizer count')
    this.precision = Object.fromEntries(Object.entries(result.data).filter(([name]) => name !== 'input_tokens' && name !== 'count_mode'))
    this.cache.set(key, value)
    if (this.cache.size > 64) this.cache.delete(this.cache.keys().next().value)
    return value
  }
}

export function configureLocalTokenizer(config) {
  if (config.localTokenizer === undefined) return config
  if (!config.localTokenizer || typeof config.localTokenizer !== 'object' || Array.isArray(config.localTokenizer)) {
    throw new RequestBudgetError('TOKENIZER_CONFIG_INVALID', 'localTokenizer must be a trusted configuration object')
  }
  if (config.requestTokenCounter !== undefined) throw new RequestBudgetError('TOKENIZER_CONFIG_INVALID', 'Configure either localTokenizer or requestTokenCounter')
  return { ...config, requestTokenCounter: new LocalDeepSeekRequestCounter({ pythonCommand: config.pythonCommand, ...config.localTokenizer }) }
}
