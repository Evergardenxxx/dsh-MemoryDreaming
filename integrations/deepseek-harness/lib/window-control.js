// Per-window opt-in is checked before any bridge, recall, or budget operation.
// Only a complete trusted user message is a command; quotes/substrings do not
// activate the plugin. Stored values contain state, never conversation text.
import { createHash } from 'node:crypto'
import { dirname, join } from 'node:path'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import { atomicWrite } from './durable-files.js'
import { bridgeDatabasePath } from './bridge-client.js'

const commands = new Map([
  ['/memory on', 'on'], ['/memory off', 'off'], ['/memory sleep', 'sleep'], ['/memory wake', 'wake'],
  ['记忆开启', 'on'], ['开启记忆', 'on'], ['记忆关闭', 'off'], ['关闭记忆', 'off'],
  ['记忆睡眠', 'sleep'], ['进入睡眠', 'sleep'], ['记忆苏醒', 'wake'], ['苏醒记忆', 'wake'],
])

export function parseMemoryCommand(text) {
  return typeof text === 'string' ? commands.get(text.trim().toLowerCase()) ?? null : null
}

const sha = value => createHash('sha256').update(value).digest('hex')

export class WindowControlStore {
  constructor(config, logger = {}) {
    this.logger = logger
    // Explicit compatibility mode exists for trusted embedding/test hosts.
    // Ordinary installed plugins always default to command activation.
    this.always = config.activationMode === 'always'
    if (config.activationMode !== undefined && !['always', 'command'].includes(config.activationMode)) {
      throw new Error('activationMode must be command or always')
    }
    const binding = [config.userId ?? 'u1', config.characterId ?? 'c1', config.relationshipId ?? 'r1', config.domain ?? 'real'].map(String)
    this.directory = join(`${bridgeDatabasePath(config)}.windows`, sha(JSON.stringify(binding)))
  }

  path(id) { return join(this.directory, `${sha(String(id))}.json`) }

  get(id) {
    // Re-read persisted state on attachment so a restart/new session object
    // recovers opt-in without reusing any sensitive prompt contents.
    const fallback = { version: 1, mode: this.always ? 'active' : 'off', sleepId: null, sleepRequestId: null, revision: 0 }
    try {
      const path = this.path(id)
      if (statSync(path).size > 4096) throw new Error('invalid size')
      const value = JSON.parse(readFileSync(path, 'utf8'))
      if (value.version !== 1 || !['off', 'active', 'sleeping'].includes(value.mode)
          || !Number.isSafeInteger(value.revision) || value.revision < 1
          || (value.sleepId !== null && typeof value.sleepId !== 'string')) throw new Error('invalid state')
      return value
    } catch (error) {
      if (error.code !== 'ENOENT') this.logger.warn?.('[memorydreaming] WINDOW_STATE_UNAVAILABLE; window remains off')
      return error.code === 'ENOENT' ? fallback : { ...fallback, mode: 'off' }
    }
  }

  set(id, update) {
    const old = this.get(id)
    const value = { version: 1, mode: update.mode ?? old.mode, sleepId: update.sleepId === undefined ? old.sleepId : update.sleepId,
      sleepRequestId: update.sleepRequestId === undefined ? old.sleepRequestId ?? null : update.sleepRequestId,
      revision: old.revision + 1 }
    const path = this.path(id)
    mkdirSync(dirname(path), { recursive: true })
    atomicWrite(path, JSON.stringify(value))
    return value
  }
}
