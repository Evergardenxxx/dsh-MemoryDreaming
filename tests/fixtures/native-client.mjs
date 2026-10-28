// Exercise the shipped lazy client entry, rather than a copied registration.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import vm from 'node:vm'

const plugin = resolve(process.argv[2], 'integrations/deepseek-harness')
const manifest = JSON.parse(readFileSync(resolve(plugin, 'package.json'), 'utf8'))
let registration, touched = 0
const context = vm.createContext({
  window: { __ModuleLoader__: { load(value) { registration = value } } },
  document: { createElement(tag) { touched++; return { tag, dataset: {}, remove() {} } },
    head: { append() { touched++ } } },
})
vm.runInContext(readFileSync(resolve(plugin, manifest.exports['./client']), 'utf8'), context)
assert.equal(registration.id, manifest.name)
assert.equal(touched, 0, 'registering a client factory must not read or mutate the page')
const React = { createElement(type, props, ...children) { return { type, props, children } } }
const entry = registration.factory(name => { assert.equal(name, 'react'); return React })
assert.equal(touched, 0, 'materializing the module must not start a page or a connection')
assert.deepEqual(Array.from(entry.inject), ['slots', 'layout', 'connection'])
const slots = [], cleanups = []
const ctx = {
  effect(callback) { const cleanup = callback(); if (cleanup) cleanups.push(cleanup) },
  slots: { inject(_name, callback) { return callback() }, register(options, component) { slots.push({ options, component }); return () => {} } },
  layout: { selectPanel() {} },
  connection: { rpc: { call() { throw new Error('opening a sidebar entry must not send RPC or model input') } } },
}
entry.apply(ctx)
assert.equal(slots.length, 2)
assert.equal(slots[0].options.name, 'main')
assert.equal(slots[0].options.key, 'memorydreaming')
assert.equal(slots[1].options.name, 'sidebar.panellist')
assert.equal(slots[1].options.id, 'memorydreaming')
assert.equal(slots[1].options.label, '记忆管理')
const beforeDisabled = touched
entry.apply(ctx, { enabled: false })
assert.equal(touched, beforeDisabled)
assert.equal(slots.length, 2)
for (const cleanup of cleanups) cleanup()
console.log(JSON.stringify({ status: 'passed', checked: ['lazy factory', 'shared React only', 'native main and sidebar registration', 'no eager RPC', 'disabled config', 'style cleanup'] }))
