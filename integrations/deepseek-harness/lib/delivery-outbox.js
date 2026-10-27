import { createHash, randomUUID } from 'node:crypto'
import { dirname, join } from 'node:path'
import { mkdirSync, openSync, writeFileSync, readFileSync, closeSync, fsyncSync, renameSync, unlinkSync, readdirSync, statSync } from 'node:fs'
import { bridgeDatabasePath } from './bridge-client.js'

const hash = text => createHash('sha256').update(text).digest('hex')
const transient = new Set(['BRIDGE_EXITED', 'BRIDGE_RESTARTED', 'BRIDGE_IO_ERROR', 'BRIDGE_TIMEOUT', 'BRIDGE_START_FAILED', 'BRIDGE_BUSY', 'BRIDGE_STOP_FAILED', 'BRIDGE_CLOSED', 'UNAVAILABLE'])
const entryPattern = /^[a-f0-9]{64}\.json$/u

function syncDirectory(path) {
  let fd
  try { fd = openSync(path, 'r'); fsyncSync(fd) } catch {} finally { if (fd !== undefined) closeSync(fd) }
}

function atomicWrite(path, text) {
  const temp = `${path}.${randomUUID()}.tmp`
  let fd
  try {
    fd = openSync(temp, 'wx', 0o600)
    writeFileSync(fd, text, 'utf8')
    fsyncSync(fd)
    closeSync(fd); fd = undefined
    renameSync(temp, path)
    syncDirectory(dirname(path))
  } finally {
    if (fd !== undefined) closeSync(fd)
    try { unlinkSync(temp) } catch {}
  }
}

export class DeliveryOutbox {
  constructor(config, bridge, logger = {}, report = () => {}) {
    this.bridge = bridge; this.logger = logger; this.report = report
    this.binding = [String(config.userId ?? 'u1'), String(config.characterId ?? 'c1'), String(config.relationshipId ?? 'r1'), String(config.domain ?? 'real')]
    this.directory = join(`${bridgeDatabasePath(config)}.outbox`, hash(JSON.stringify(this.binding)))
    this.pending = new Map(); this.closed = false
    this.timer = setInterval(() => { void this.flush() }, 5000)
    this.timer.unref?.()
    void this.flush()
  }

  warn(code) {
    this.logger.warn?.(`[memorydreaming] ${code}`)
    this.report({ code, pending: this.files().length })
  }

  files() {
    try {
      const files = readdirSync(this.directory)
      const rejected = files.filter(file => /^rejected-[a-f0-9]{64}\.json$/u.test(file)).sort()
      for (const file of rejected.slice(0, Math.max(0, rejected.length - 128))) {
        try { unlinkSync(join(this.directory, file)) } catch {}
      }
      return files.filter(file => entryPattern.test(file)).sort()
    } catch (error) {
      if (error.code !== 'ENOENT') this.logger.warn?.('[memorydreaming] OUTBOX_READ_FAILED')
      return []
    }
  }

  put(request) {
    if (typeof request.occurred_at !== 'string') request = { ...request, occurred_at: new Date().toISOString() }
    if (typeof request.expected_guard !== 'string') throw Object.assign(new Error('Missing durable delivery guard'), { code: 'OUTBOX_GUARD_MISSING' })
    const encoded = JSON.stringify({ version: 1, digest: hash(JSON.stringify(request)), request })
    if (Buffer.byteLength(encoded) > 1024 * 1024) throw Object.assign(new Error('Delivery exceeds durable queue size'), { code: 'OUTBOX_ENTRY_TOO_LARGE' })
    const key = hash(JSON.stringify([request.conversation_id, request.message_id, request.user_event_id])), path = join(this.directory, `${key}.json`)
    const files = this.files()
    if (!files.includes(`${key}.json`) && files.length >= 256) throw Object.assign(new Error('Durable delivery queue full'), { code: 'OUTBOX_FULL' })
    mkdirSync(this.directory, { recursive: true })
    atomicWrite(join(this.directory, '_binding.json'), JSON.stringify(this.binding))
    if (files.includes(`${key}.json`)) {
      const previous = JSON.parse(readFileSync(path, 'utf8'))
      if (JSON.stringify({ ...previous.request, occurred_at: null }) !== JSON.stringify({ ...request, occurred_at: null })) {
        throw Object.assign(new Error('Conflicting durable delivery payload'), { code: 'OUTBOX_ID_CONFLICT' })
      }
      request = previous.request
    } else atomicWrite(path, encoded)
    return { key, path, request }
  }

  deliver(request) {
    // Synchronous fsync completes before returning to assistant/message admission.
    // Once this returns a promise, a process crash leaves a recoverable entry.
    let entry
    try { entry = this.put(request) } catch (error) {
      this.warn(error.code ?? 'OUTBOX_WRITE_FAILED')
      return Promise.resolve(false)
    }
    return this.send(entry)
  }

  send(entry) {
    if (this.pending.has(entry.key)) return this.pending.get(entry.key)
    const task = (async () => {
      try {
        await this.bridge.request('after_delivery', entry.request)
        try { unlinkSync(entry.path) } catch (error) { if (error.code !== 'ENOENT') throw error }
        syncDirectory(this.directory)
        return true
      } catch (error) {
        if (!transient.has(error.code)) {
          // Rejected or stale payload is erased; retain only a fixed error code.
          try {
            unlinkSync(entry.path)
            atomicWrite(join(this.directory, `rejected-${entry.key}.json`), JSON.stringify({ code: error.code ?? 'OUTBOX_REJECTED' }))
          } catch {}
          this.warn(error.code === 'STALE_DELIVERY' ? 'OUTBOX_STALE_DROPPED' : 'OUTBOX_REJECTED')
        } else this.warn('OUTBOX_RETRY_PENDING')
        return false
      }
    })()
    this.pending.set(entry.key, task)
    void task.finally(() => this.pending.delete(entry.key))
    return task
  }

  async flush() {
    if (this.closed) return
    for (const file of this.files().slice(0, 32)) {
      if (this.closed) return
      const path = join(this.directory, file), key = file.slice(0, -5)
      if (this.pending.has(key)) continue
      let value
      try {
        if (statSync(path).size > 1024 * 1024) throw new Error('oversized')
        const encoded = readFileSync(path, 'utf8')
        value = JSON.parse(encoded)
        if (hash(JSON.stringify([value.request?.conversation_id, value.request?.message_id, value.request?.user_event_id])) !== key
            || value.version !== 1 || value.digest !== hash(JSON.stringify(value.request))
            || typeof value.request?.expected_guard !== 'string') throw new Error('invalid')
      } catch {
        try { unlinkSync(path); atomicWrite(join(this.directory, `rejected-${key}.json`), JSON.stringify({ code: 'OUTBOX_CORRUPT' })) } catch {}
        this.warn('OUTBOX_CORRUPT_DROPPED')
        continue
      }
      await this.send({ key, path, request: value.request })
    }
  }

  close() { this.closed = true; clearInterval(this.timer) }
}
