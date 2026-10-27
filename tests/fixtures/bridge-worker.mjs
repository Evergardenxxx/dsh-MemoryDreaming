// A separate OS process for supervisor failure tests. Never uses credentials.
import readline from 'node:readline'
const mode = process.argv[2]
const input = readline.createInterface({ input: process.stdin })
input.on('line', line => {
  const request = JSON.parse(line)
  if (mode === 'partial-exit') { process.stdout.write('{"id":"partial'); process.exit(0) }
  if (mode === 'exit-zero') process.exit(0)
  if (mode === 'invalid') { process.stdout.write('invalid-json\n'); return }
  if (mode === 'invalid-shape') { process.stdout.write(JSON.stringify({ id: request.id, ok: true }) + '\n'); return }
  if (mode === 'oversized') { process.stdout.write('x'.repeat(2 * 1024 * 1024 + 1)); return }
  if (mode === 'hang') return
  const response = { id: request.id, ok: true, data: { op: request.op } }
  process.stdout.write(`${JSON.stringify(response)}\n`)
  if (request.op === 'shutdown') process.exit(0)
})
