import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const MAX_LINE_BYTES = 1024 * 1024
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024

export function boundedMaxTokens(value) {
  return Number.isInteger(value) && value >= 128 && value <= 4096 ? value : 1200
}

function bounded(value, fallback, min, max) {
  return Number.isSafeInteger(value) && value >= min && value <= max ? value : fallback
}

function failure(code, message) {
  return Object.assign(new Error(message), { code })
}

function waitForExit(record, milliseconds) {
  return new Promise((resolvePromise) => {
    const timer = setTimeout(() => resolvePromise(false), milliseconds)
    record.exited.then(() => { clearTimeout(timer); resolvePromise(true) })
  })
}

// One serial Python bridge is shared by this plugin instance. Cancelling work
// already sent to it terminates that worker, rejecting its other in-flight
// requests explicitly. Nothing is automatically replayed by the transport.
export class BridgeClient {
  constructor(config, logger, { spawnProcess = spawn } = {}) {
    this.config = config
    this.logger = logger
    this.spawnProcess = spawnProcess
    this.active = null
    this.pending = new Map()
    this.closed = false
    this.closePromise = null
    this.retiring = Promise.resolve(true)
    this.generation = 0
    this.timeoutMs = bounded(config.bridgeTimeoutMs, 15000, 100, 120000)
    this.shutdownMs = bounded(config.bridgeShutdownMs, 2000, 100, 10000)
    this.maxPending = bounded(config.bridgeMaxPending, 64, 1, 256)
    this.onProcessExit = () => {
      this.active?.child.kill('SIGKILL')
      if (this.lastRetired?.child.exitCode === null && this.lastRetired?.child.signalCode === null) this.lastRetired.child.kill('SIGKILL')
    }
    process.once('exit', this.onProcessExit)
  }

  get child() { return this.active?.child ?? null }

  async start(entry) {
    const stopped = await this.retiring
    if (entry && !this.pending.has(entry.id)) return null
    if (!stopped) throw failure('BRIDGE_STOP_FAILED', 'Previous memory worker has not exited')
    if (this.closed) throw failure('BRIDGE_CLOSED', 'Memory bridge is closed')
    if (this.active) return this.active
    const python = this.config.pythonCommand ?? process.env.MEMORYDREAMING_PYTHON ?? 'python'
    const repoPath = this.config.repoPath ?? REPO_ROOT
    const dbPath = this.config.dbPath ?? resolve(repoPath, '.memorydreaming', 'deepseek-harness.db')
    const extractor = this.config.extractor === 'deepseek' ? 'deepseek' : 'rules'
    const args = [
      '-X', 'utf8', '-m', 'memorydreaming', '--db', dbPath, 'bridge',
      '--user', String(this.config.userId ?? 'u1'),
      '--character', String(this.config.characterId ?? 'c1'),
      '--relationship', String(this.config.relationshipId ?? 'r1'),
      '--domain', String(this.config.domain ?? 'real'),
      '--max-tokens', String(boundedMaxTokens(this.config.maxTokens)), '--extractor', extractor,
    ]
    if (this.config.autoConsolidation !== false || extractor === 'deepseek') args.push('--auto-consolidation')
    const env = { ...process.env, PYTHONUTF8: '1' }
    const options = {
      embeddingProvider: 'EMBEDDING_PROVIDER', embeddingModelPath: 'EMBEDDING_MODEL_PATH',
      embeddingRevision: 'EMBEDDING_REVISION', embeddingDim: 'EMBEDDING_DIM',
      embeddingQueryPrefix: 'EMBEDDING_QUERY_PREFIX', embeddingDocumentPrefix: 'EMBEDDING_DOCUMENT_PREFIX',
      embeddingMaxSeqLength: 'EMBEDDING_MAX_SEQ_LENGTH', embeddingBatchSize: 'EMBEDDING_BATCH_SIZE',
      vectorMinScore: 'VECTOR_MIN_SCORE',
    }
    for (const [key, suffix] of Object.entries(options)) {
      if (this.config[key] !== undefined && this.config[key] !== null) env[`MEMORYDREAMING_${suffix}`] = String(this.config[key])
    }
    const child = this.spawnProcess(python, args, { cwd: repoPath, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    const record = { child, generation: ++this.generation, buffer: '', retired: false }
    record.exited = new Promise(resolvePromise => { record.resolveExit = resolvePromise })
    this.active = record
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', chunk => this.onStdout(record, chunk))
    child.stderr.on('data', chunk => {
      if (this.active !== record || record.retired) return
      const line = String(chunk).slice(0, 4096).trim()
      if (line) this.logger?.debug?.(`[memorydreaming] ${line}`)
    })
    child.stdin.on('error', () => this.retire(record, failure('BRIDGE_IO_ERROR', 'Memory bridge input failed')))
    child.on('error', () => this.retire(record, failure('BRIDGE_START_FAILED', 'Memory worker failed to start')))
    child.on('exit', () => this.retire(record, failure('BRIDGE_EXITED', 'Memory worker exited before replying')))
    child.on('close', () => {
      record.resolveExit(true)
      this.retire(record, failure('BRIDGE_EXITED', 'Memory worker closed before replying'))
      // A delayed confirmed close can recover a previously expired stop wait.
      if (this.lastRetired === record) this.retiring = Promise.resolve(true)
    })
    return record
  }

  settle(entry, error, data) {
    if (!this.pending.delete(entry.id)) return
    clearTimeout(entry.timer)
    entry.signal?.removeEventListener('abort', entry.onAbort)
    if (error) entry.reject(error)
    else entry.resolve(data)
  }

  retire(record, error) {
    if (record.retired) return
    record.retired = true
    this.lastRetired = record
    record.buffer = ''
    if (this.active === record) this.active = null
    for (const entry of this.pending.values()) {
      if (entry.record === record) this.settle(entry, error)
    }
    // Do not overlap a replacement worker with one whose termination has not
    // been confirmed. Late stdout is ignored using the record identity.
    this.retiring = waitForExit(record, this.shutdownMs)
    if (record.child.exitCode === null && record.child.signalCode === null) record.child.kill('SIGKILL')
  }

  onStdout(record, chunk) {
    if (record !== this.active || record.retired) return
    record.buffer += chunk
    while (true) {
      const newline = record.buffer.indexOf('\n')
      if (newline < 0) break
      const line = record.buffer.slice(0, newline).trim()
      record.buffer = record.buffer.slice(newline + 1)
      if (!line) continue
      if (Buffer.byteLength(line, 'utf8') > MAX_RESPONSE_BYTES) {
        this.retire(record, failure('BRIDGE_PROTOCOL_ERROR', 'Memory worker response exceeded size limit'))
        return
      }
      let response
      try { response = JSON.parse(line) } catch {
        this.retire(record, failure('BRIDGE_PROTOCOL_ERROR', 'Memory worker returned invalid JSON'))
        return
      }
      if (!response || typeof response.id !== 'string' || typeof response.ok !== 'boolean'
          || (response.ok && (!response.data || typeof response.data !== 'object' || Array.isArray(response.data)))
          || (!response.ok && (typeof response.error?.code !== 'string' || typeof response.error?.message !== 'string'))) {
        this.retire(record, failure('BRIDGE_PROTOCOL_ERROR', 'Memory worker returned an invalid response'))
        return
      }
      const entry = this.pending.get(response.id)
      if (!entry || entry.record !== record) continue
      const error = response.ok ? null : failure(response.error?.code ?? 'UNAVAILABLE', response.error?.message ?? 'Memory request failed')
      this.settle(entry, error, response.data)
    }
    if (Buffer.byteLength(record.buffer, 'utf8') > MAX_RESPONSE_BYTES) {
      this.retire(record, failure('BRIDGE_PROTOCOL_ERROR', 'Memory worker response exceeded size limit'))
    }
  }

  request(op, payload, signal, internal = false) {
    if (this.closed && !internal) return Promise.reject(failure('BRIDGE_CLOSED', 'Memory bridge is closed'))
    if (signal?.aborted) return Promise.reject(failure('ABORTED', 'Memory request was already cancelled'))
    if (this.pending.size >= this.maxPending) return Promise.reject(failure('BRIDGE_BUSY', 'Memory request queue is full'))
    const id = randomUUID()
    let encoded
    try { encoded = `${JSON.stringify({ ...payload, id, op })}\n` } catch {
      return Promise.reject(failure('INVALID_REQUEST', 'Memory request is not serializable'))
    }
    if (Buffer.byteLength(encoded, 'utf8') > MAX_LINE_BYTES) return Promise.reject(failure('REQUEST_TOO_LARGE', 'Memory request exceeds size limit'))
    return new Promise((resolvePromise, rejectPromise) => {
      const entry = { id, resolve: resolvePromise, reject: rejectPromise, signal, record: null }
      const cancel = code => {
        const record = entry.record
        this.settle(entry, failure(code, code === 'ABORTED' ? 'Memory request cancelled' : 'Memory request deadline exceeded'))
        if (record) this.retire(record, failure('BRIDGE_RESTARTED', 'Shared memory worker reset; retry idempotent operations only'))
      }
      entry.onAbort = () => cancel('ABORTED')
      entry.timer = setTimeout(() => cancel('BRIDGE_TIMEOUT'), internal ? this.shutdownMs : this.timeoutMs)
      this.pending.set(id, entry)
      signal?.addEventListener('abort', entry.onAbort, { once: true })
      const launch = async () => {
        try {
          // Shutdown uses the current worker; it must never resurrect one.
          const record = internal ? this.active : await this.start(entry)
          if (!this.pending.has(id)) return
          if (!record || record.retired) throw failure('BRIDGE_CLOSED', 'Memory worker is unavailable')
          entry.record = record
          record.child.stdin.write(encoded, error => {
            if (error) this.retire(record, failure('BRIDGE_IO_ERROR', 'Memory bridge write failed'))
          })
        } catch (error) { this.settle(entry, error) }
      }
      void launch()
    })
  }

  close() {
    if (this.closePromise) return this.closePromise
    this.closed = true
    this.closePromise = (async () => {
      const record = this.active
      if (record && !record.retired) {
        try { await this.request('shutdown', {}, undefined, true) } catch {}
        if (!await waitForExit(record, this.shutdownMs)) {
          this.retire(record, failure('BRIDGE_CLOSED', 'Memory worker forced to stop during disposal'))
        }
      }
      await this.retiring
      for (const entry of this.pending.values()) this.settle(entry, failure('BRIDGE_CLOSED', 'Memory bridge is closed'))
    })().finally(() => process.removeListener('exit', this.onProcessExit))
    return this.closePromise
  }
}
