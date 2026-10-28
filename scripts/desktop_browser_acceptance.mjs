// Real Chromium view -> native controller -> JSONL bridge -> temporary SQLite.
// Session admission and model output are deterministic test adapters. This is
// intentionally separate from Electron, live-model, and installed-app tests.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { BridgeClient } from '../integrations/deepseek-harness/lib/bridge-client.js'
import { installDesktop } from '../integrations/deepseek-harness/lib/desktop-controller.js'
import { WindowControlStore, parseMemoryCommand } from '../integrations/deepseek-harness/lib/window-control.js'
import { SleepCoordinator } from '../integrations/deepseek-harness/lib/sleep-coordinator.js'

const arguments_ = process.argv.slice(2), options = {}
if (arguments_.includes('--help')) {
  console.log('node scripts/desktop_browser_acceptance.mjs [--playwright-module PATH] [--chromium PATH | --channel msedge] [--python COMMAND] [--root PATH] [--output PATH]')
  process.exit(0)
}
for (let index = 0; index < arguments_.length; index += 2) {
  const key = arguments_[index].slice(2), argument = arguments_[index + 1]
  if (!arguments_[index].startsWith('--') || !['root', 'output', 'python', 'playwright-module', 'chromium', 'channel'].includes(key)
      || !argument || argument.startsWith('--')) throw new Error(`Unknown or incomplete argument: ${arguments_[index]}`)
  options[key] = argument
}
const root = resolve(options.root ?? join(dirname(fileURLToPath(import.meta.url)), '..'))
const output = resolve(options.output ?? join(root, 'dist/desktop-preview'))
const python = options.python ?? 'python'
const playwright = options['playwright-module']
  ? await import(pathToFileURL(resolve(options['playwright-module'])).href)
  : await import('playwright')
const chromium = playwright.chromium ?? playwright.default?.chromium
if (!chromium) throw new Error('The supplied module does not export Playwright chromium')
const workspace = mkdtempSync(join(tmpdir(), 'memorydreaming-desktop-browser-'))
const dbPath = join(workspace, 'synthetic.db')
mkdirSync(output, { recursive: true })
const checked = [], requests = [], admissions = [], commandErrors = [], browserErrors = []
let browser, server, bridge, releaseDesktop, seed, page, commandTail = Promise.resolve(), releaseCommandGate = () => {}
const report = (status, extra = {}) => ({ status, checked, synthetic_data: true,
  real_browser: Boolean(browser), real_desktop_controller: Boolean(releaseDesktop), real_python_bridge: Boolean(bridge), real_sqlite: Boolean(seed),
  real_sleep_coordinator: checked.some(item => item.startsWith('sleep coordinator')), real_electron: false, real_cordis_gateway: false, live_model: false,
  session_admission: 'deterministic test adapter', model: 'deterministic review JSON',
  ...extra })

try {
  const seeded = spawnSync(python, ['-X', 'utf8', join(root, 'tests/fixtures/desktop-seed.py'), dbPath], {
    cwd: root, encoding: 'utf8', windowsHide: true, timeout: 30000,
  })
  assert.equal(seeded.status, 0, seeded.stderr || seeded.stdout)
  seed = JSON.parse(seeded.stdout.trim().split(/\r?\n/).at(-1))
  const config = { repoPath: root, dbPath, pythonCommand: python, activationMode: 'command',
    userId: 'desktop-test-user', characterId: 'desktop-test-character', relationshipId: 'desktop-test-relationship',
    extractor: 'rules', autoConsolidation: false, autoPrefetch: false, contextWindow: 65536,
    outputReserve: 2048, safetyMargin: 512, bridgeTimeoutMs: 30000 }
  bridge = new BridgeClient(config, {})
  const controls = new WindowControlStore(config), sessions = [], states = new Map(), agents = new Map()
  const listeners = new Map(), services = new Map()
  const ctx = {
    invocation: { peer: { principal: { kind: 'owner' } } },
    get: name => services.get(name),
    reflect: { provide(name, service) { services.set(name, service); return () => services.delete(name) } },
    on(name, handler) {
      const set = listeners.get(name) ?? new Set(); listeners.set(name, set); set.add(handler)
      return () => set.delete(handler)
    },
    emit(name, ...args) { for (const handler of listeners.get(name) ?? []) handler(...args) },
  }
  services.set('sessions', { list: () => [...sessions] })
  services.set('agents', { get: id => agents.get(id) })
  let reviewedBatches = 0, allowCommands
  const commandGate = new Promise(resolveGate => { allowCommands = resolveGate })
  releaseCommandGate = allowCommands
  services.set('llm', { async *stream(options_) {
    options_.signal.throwIfAborted()
    assert.equal(options_.purpose, 'memorydreaming-sleep')
    const batch = JSON.parse(options_.messages[1].content[0].text)
    reviewedBatches++
    yield { type: 'text-delta', text: JSON.stringify({ notes: [], source_event_ids: batch.events.map(event => event.event_id) }) }
    yield { type: 'finish', reason: { kind: 'stop' } }
  } })
  const sleeping = new SleepCoordinator(ctx, bridge, controls, config)
  async function runCommand(request, signal) {
    await commandGate
    const session = sessions.find(item => item.id === request.sessionId)
    const type = parseMemoryCommand(request.content[0]?.text)
    assert.ok(type, 'Controller must submit one complete native memory command')
    agents.set(session.id, { status: 'running' }); states.get(session.id).command = { type }
    ctx.emit('session/event', session, { type: 'turn/start' })
    try {
      if (type === 'on') controls.set(session.id, { mode: 'active' })
      else if (type === 'off') controls.set(session.id, { mode: 'off' })
      else if (type === 'sleep') await sleeping.sleep(session.id, request.requestId,
        { provider: 'synthetic-review', model: 'deterministic-json', maxTokens: 2048 }, signal,
        { contextWindow: 65536, defaultMaxTokens: 2048 })
      else await sleeping.wake(session.id, request.requestId, signal)
      ctx.emit('memorydreaming/window-state', session.id)
    } finally {
      states.get(session.id).command = null; agents.set(session.id, { status: 'idle' })
      ctx.emit('session/event', session, { type: 'turn/end' })
    }
  }
  services.set('sessionController', {
    async prompt(request, signal) {
      admissions.push(request)
      commandTail = commandTail.then(() => runCommand(request, signal)).catch(error => { commandErrors.push(String(error.stack ?? error)) })
      return { accepted: true }
    },
    async cancel() { throw new Error('This acceptance does not simulate mid-review cancellation') },
  })
  releaseDesktop = installDesktop(ctx, { bridge, controls, states })
  const controller = services.get('memorydreamingDesktop')
  const html = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MemoryDreaming synthetic acceptance</title><link rel="stylesheet" href="/view.css"><style>html,body{margin:0;min-height:100%;width:100%}#root{min-height:100vh}</style></head><body><main id="root"></main><script type="module">import {mountDesktop} from "/view.js";window.mount=()=>{window.unmount=mountDesktop(document.getElementById("root"),(action,args)=>window.desktopRequest(action,args))};window.mount();</script></body></html>'
  server = createServer((request, response) => {
    const file = request.url === '/view.js' ? ['view.js', 'text/javascript'] : request.url === '/view.css' ? ['view.css', 'text/css'] : null
    if (request.url !== '/' && !file) { response.writeHead(404).end(); return }
    response.writeHead(200, { 'Content-Type': `${file?.[1] ?? 'text/html'}; charset=utf-8`, 'Cache-Control': 'no-store' })
    response.end(file ? readFileSync(join(root, 'integrations/deepseek-harness/desktop', file[0])) : html)
  })
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen))
  browser = await chromium.launch({ headless: true, ...(options.chromium ? { executablePath: options.chromium } : {}),
    ...(options.channel ? { channel: options.channel } : {}) })
  page = await browser.newPage({ viewport: { width: 1365, height: 1000 }, colorScheme: 'light' })
  page.setDefaultTimeout(12000)
  page.on('pageerror', error => browserErrors.push(error.message))
  await page.exposeFunction('desktopRequest', async (action, args) => {
    const result = await controller.request(action, args)
    requests.push({ action, args, result })
    return result
  })
  const address = `http://127.0.0.1:${server.address().port}`
  await page.goto(address)
  const button = name => page.getByRole('button', { name, exact: true })
  const nav = name => page.getByRole('navigation', { name: '记忆管理' }).getByRole('button', { name, exact: true })
  const waitText = async (selector, text) => page.locator(selector).filter({ hasText: text }).first().waitFor()
  const waitFor = async (condition, message, milliseconds = 15000) => {
    const deadline = Date.now() + milliseconds
    while (!condition()) { assert.ok(Date.now() < deadline, message); await new Promise(resolveWait => setTimeout(resolveWait, 25)) }
  }
  async function confirm() { await button('确认提交').click(); await page.locator('.md-confirm').waitFor({ state: 'detached' }) }
  async function search(text) {
    await page.getByRole('textbox', { name: '搜索记忆', exact: true }).fill(text)
    await button('搜索').click(); await page.locator('.md-list').waitFor()
  }
  const requestCount = action => requests.filter(item => item.action === action).length
  async function preserveDraftAcrossPoll(locator, mutate) {
    await locator.focus()
    const before = await locator.evaluate(node => {
      node.setSelectionRange(1, Math.min(4, node.value.length))
      return { value: node.value, start: node.selectionStart, end: node.selectionEnd }
    })
    const windowsBefore = requestCount('windows')
    mutate()
    await waitFor(() => requestCount('windows') > windowsBefore, 'Window changes should reach a visible draft', 4500)
    const after = await locator.evaluate(node => ({ value: node.value, start: node.selectionStart,
      end: node.selectionEnd, focused: document.activeElement === node }))
    assert.deepEqual(after, { ...before, focused: true }, 'Polling must preserve entered values, focus, and selection')
  }

  // No conversation exists: initialize the role through the actual owner UI.
  await nav('绑定与诊断').click()
  await page.getByRole('heading', { name: '初始化角色', exact: true }).waitFor()
  for (const [label, text] of [['名字', '演示小梦'], ['身份', '演示陪伴角色'], ['称呼', '演示伙伴'], ['相处设定', '合成验收关系']]) {
    await page.getByLabel(label, { exact: true }).fill(text)
  }
  const initializationWindow = { id: 'initialization-poll-window', header: { title: '合成初始化期间窗口变化' } }
  await preserveDraftAcrossPoll(page.getByLabel('相处设定', { exact: true }), () => sessions.push(initializationWindow))
  sessions.pop()
  await button('准备初始化').click()
  assert.equal(requestCount('initialize_role'), 0, 'Preparation must not write')
  await confirm()
  await waitText('.md-content', '演示小梦')
  assert.equal(requests.find(item => item.action === 'initialize_role').args.conversation_id, undefined)
  assert.equal(sessions.length, 0)
  checked.push('role initialization works without an open conversation and requires explicit confirmation')
  await page.screenshot({ path: join(output, '01-role-light.png'), fullPage: true })

  // Admission remains off until the synthetic native lifecycle actually runs.
  const session = { id: 'desktop-demo', header: { title: '合成演示窗口' } }
  sessions.push(session); states.set(session.id, { session, command: null }); agents.set(session.id, { status: 'idle' })
  await page.getByRole('combobox', { name: '选择窗口' }).getByRole('option', { name: '合成演示窗口', exact: true }).waitFor({ state: 'attached' })
  await page.getByRole('combobox', { name: '选择窗口' }).selectOption(session.id)
  await waitText('.md-window', 'off')
  await button('开启').click(); await confirm()
  assert.equal(admissions.length, 1)
  assert.equal(admissions[0].content[0].text, '记忆开启')
  assert.equal(controls.get(session.id).mode, 'off', 'Accepted is not a completed control command')
  assert.ok((await page.locator('.md-window').innerText()).includes('off'))
  allowCommands(); await commandTail
  await waitText('.md-window', 'active')
  checked.push('window selection and fixed native command admission update only after the lifecycle completes')

  await nav('记忆').click()
  for (const [label, text] of [['工作', '演示工作'], ['情景', '演示情景'], ['程序', '演示程序'], ['语义', '分页热茶样本']]) {
    await page.getByRole('group', { name: '记忆类型' }).getByRole('button', { name: label, exact: true }).click()
    await waitText('.md-list', text)
  }
  assert.equal(await page.locator('.md-row').count(), 20)
  const firstPage = await page.locator('.md-row').allTextContents()
  await button('下一页').click(); await waitText('.md-pagination', '21')
  const secondPage = await page.locator('.md-row').allTextContents()
  assert.ok(secondPage.length > 0 && secondPage.length < 20)
  assert.ok(secondPage.every(item => !firstPage.includes(item)))
  checked.push('all four memory types and stable next-page navigation use real source-backed records')

  await search('热茶纠错目标')
  assert.equal(await page.locator('.md-row').count(), 1)
  await page.locator('.md-row').first().click(); await waitText('.md-source', seed.records.correct.source)
  assert.ok((await page.locator('.md-source').innerText()).includes(seed.records.correct.event_id))
  checked.push('search and detail show the actual original event and source identifier')
  await page.screenshot({ path: join(output, '02-memory-source-light.png'), fullPage: true })

  // Change approval uses the growth service without enabling core mutations.
  await nav('待审候选').click()
  await page.getByRole('group', { name: '候选类型' }).getByRole('button', { name: '成长', exact: true }).click()
  await page.locator('.md-row').first().click(); await waitText('.md-detail', '演示成长')
  await page.getByLabel('审批理由', { exact: true }).fill('合成用户在浏览器确认成长风格')
  await preserveDraftAcrossPoll(page.getByLabel('审批理由', { exact: true }), () => { session.header.title = '合成演示窗口：审批草稿' })
  await button('准备确认').click(); assert.equal(requestCount('proposal_decide'), 0)
  await confirm(); await nav('角色').click(); await waitText('.md-content', '演示成长：讲故事慢一点')
  checked.push('pending growth is explicitly approved and appears in the role view through the domain service')

  await nav('记忆').click(); await search('热茶纠错目标')
  await page.locator('.md-row').first().click(); await page.getByLabel('理由', { exact: true }).fill('合成偏好更新')
  await page.getByLabel('纠正后的结构化内容（JSON）', { exact: true }).fill(JSON.stringify({ subject: '演示用户', predicate: '喜欢', obj: '温茶已纠正', scope_note: '合成验收资料' }))
  await preserveDraftAcrossPoll(page.getByLabel('纠正后的结构化内容（JSON）', { exact: true }), () => { session.header.title = '合成演示窗口：纠错草稿' })
  assert.equal(await page.getByLabel('理由', { exact: true }).inputValue(), '合成偏好更新')
  await button('准备纠正').click(); assert.equal(requestCount('memory_correct'), 0)
  await confirm(); await search('温茶已纠正'); await page.locator('.md-row').first().click()
  await waitText('.md-source', '桌面用户明确操作')
  checked.push('confirmed correction persists a new fact with an independent desktop confirmation source')
  checked.push('window polling preserves initialization, proposal, and correction drafts including focus and selection')

  await search('删除专用薄荷茶'); await page.locator('.md-row').first().click()
  await page.getByLabel('理由', { exact: true }).fill('合成遗忘请求')
  await button('准备遗忘').click(); assert.equal(requestCount('memory_forget'), 0)
  await confirm(); assert.equal(await page.locator('.md-row').count(), 0)
  checked.push('forget preparation is read-only and confirmed forgetting removes the record from search')

  await search('合成HTML测试'); await page.locator('.md-row').first().click()
  await waitText('.md-detail', '<img src=x')
  assert.equal(await page.locator('#root img').count(), 0)
  assert.equal(await page.evaluate(() => window.__memorydreamingInjected), undefined)
  checked.push('HTML-like memory and source payloads stay text and never create executable markup')

  await button('睡眠').click(); await confirm(); await commandTail
  assert.deepEqual(commandErrors, [])
  assert.ok(reviewedBatches > 0)
  await nav('睡眠与梦境').click(); await waitText('.md-content', 'generated_fiction')
  await waitText('.md-content', 'completed')
  await button('苏醒').click(); await confirm(); await commandTail
  assert.equal(controls.get(session.id).mode, 'active')
  await waitText('.md-window', 'active')
  assert.deepEqual(commandErrors, [])
  checked.push('sleep coordinator reviews every actual inventory batch, saves a fiction-marked dream, and wakes')
  await page.screenshot({ path: join(output, '03-sleep-dream-light.png'), fullPage: true })

  await page.setViewportSize({ width: 390, height: 844 })
  for (const tab of ['记忆', '待审候选', '角色', '睡眠与梦境', '绑定与诊断']) {
    await nav(tab).click()
    await page.locator('.md-empty[role="status"]').waitFor({ state: 'detached' })
    const width = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: innerWidth }))
    assert.ok(width.scroll <= width.viewport, `${tab} overflows ${width.scroll} > ${width.viewport}`)
  }
  await page.emulateMedia({ colorScheme: 'dark' })
  await page.evaluate(() => document.body.setAttribute('data-ds-dark-theme', ''))
  const colors = await page.locator('#root').evaluate(node => ({ background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color }))
  assert.ok(colors.background !== 'rgb(248, 251, 250)' && colors.color !== 'rgb(32, 43, 41)', JSON.stringify(colors))
  await page.screenshot({ path: join(output, '04-narrow-dark.png'), fullPage: true })
  checked.push('all five pages fit a 390px viewport and dark mode changes readable foreground and surfaces')

  const beforePoll = requestCount('windows')
  await waitFor(() => requestCount('windows') > beforePoll, 'Mounted visible view should poll bounded window snapshots', 4500)
  await page.evaluate(() => window.unmount())
  const afterUnmount = requestCount('windows')
  await page.waitForTimeout(3200)
  assert.equal(requestCount('windows'), afterUnmount, 'Unmounted view must stop polling')
  assert.equal(await page.locator('#root').textContent(), '')
  await page.evaluate(() => window.mount())
  await waitFor(() => requestCount('windows') > afterUnmount, 'Remount should request a fresh window snapshot')
  await page.evaluate(() => window.unmount())
  checked.push('mount polls while visible, unmount clears the page and timer, and remount restores a fresh view')
  assert.deepEqual(browserErrors, [])
  rmSync(join(output, 'failure.png'), { force: true })
  writeFileSync(join(output, 'acceptance.json'), JSON.stringify(report('passed', {
    paths: checked.length, requests: requests.length, reviewed_batches: reviewedBatches,
    browser_version: browser.version(), screenshots: ['01-role-light.png', '02-memory-source-light.png', '03-sleep-dream-light.png', '04-narrow-dark.png'],
  }), null, 2) + '\n')
  console.log(JSON.stringify(report('passed', { paths: checked.length, requests: requests.length, reviewed_batches: reviewedBatches, output })))
} catch (error) {
  if (page) { try { await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }) } catch {} }
  const failed = report('failed', { error: String(error.stack ?? error), browser_errors: browserErrors, command_errors: commandErrors,
    recent_requests: requests.slice(-10).map(item => ({ action: item.action, error: item.result?.error })) })
  writeFileSync(join(output, 'acceptance.json'), JSON.stringify(failed, null, 2) + '\n')
  console.error(JSON.stringify(failed)); process.exitCode = 1
} finally {
  if (page) { try { await page.evaluate(() => window.unmount?.()) } catch {} }
  await browser?.close()
  releaseCommandGate()
  releaseDesktop?.()
  await commandTail
  await bridge?.close()
  if (server) await new Promise(resolveClose => server.close(resolveClose))
  if (!workspace.startsWith(resolve(tmpdir()) + sep) || !basename(workspace).startsWith('memorydreaming-desktop-browser-')) {
    throw new Error('Refusing to remove an unexpected acceptance workspace')
  }
  rmSync(workspace, { recursive: true, force: true })
}
