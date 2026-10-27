// Shared crash-safe local file writes. Callers own path/binding validation.
import { randomUUID } from 'node:crypto'
import { dirname } from 'node:path'
import { openSync, writeFileSync, closeSync, fsyncSync, renameSync, unlinkSync } from 'node:fs'

export function syncDirectory(path) {
  let fd
  try { fd = openSync(path, 'r'); fsyncSync(fd) } catch {} finally { if (fd !== undefined) closeSync(fd) }
}

export function atomicWrite(path, text) {
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
