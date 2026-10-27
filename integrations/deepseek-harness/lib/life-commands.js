// Structured user commands stay separate from ordinary recall. Parsing a whole
// trusted user message is authorization; a model's generated JSON is not.
const prefixes = [
  ['记忆自传', 'autobiography'], ['记忆承诺', 'promise'], ['记忆完成', 'complete'], ['记忆取消', 'cancel'],
  ['记忆偏好', 'preference'], ['记忆成长', 'growth'], ['记忆提醒', 'proactive'], ['记忆触发', 'trigger'], ['记忆自检', 'check'],
  ['/memory autobiography', 'autobiography'], ['/memory promise', 'promise'], ['/memory complete', 'complete'], ['/memory cancel', 'cancel'],
  ['/memory preference', 'preference'], ['/memory growth', 'growth'], ['/memory proactive', 'proactive'], ['/memory trigger', 'trigger'], ['/memory check', 'check'],
]

export function parseLifeCommand(input) {
  if (typeof input !== 'string' || input.length > 12000) return null
  const value = input.trim()
  for (const [prefix, operation] of prefixes) {
    if (value === prefix) return { operation, payload: {} }
    if (!value.startsWith(prefix+' ')) continue
    const rest = value.slice(prefix.length).trim()
    try {
      const payload = JSON.parse(rest)
      if (payload && typeof payload === 'object' && !Array.isArray(payload)) return { operation, payload }
    } catch {}
    return { operation, error: '命令参数须是完整 JSON 对象。' }
  }
  return null
}

export function lifeReceipt(result) {
  // This locally rendered acknowledgement avoids an extra model call or tool
  // execution. Full records remain queryable only through the bound SDK.
  const encoded = JSON.stringify(result)
  return `长期记忆操作回执：\n${encoded.length <= 7000 ? encoded : JSON.stringify({ status: 'saved', details_truncated: true, kind: result.kind })}\n这些记录保留来源与版本；提醒策略只授权本地队列，宿主领取后才发送。`
}
