// Real Chromium view -> native controller -> JSONL bridge -> temporary SQLite.
// Session admission and model output are deterministic test adapters. This is
// intentionally separate from Electron, live-model, and installed-app tests.
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
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
const checked = [], requests = [], auxiliaryRequests = [], admissions = [], commandErrors = [], browserErrors = []
let browser, server, bridge, releaseDesktop, seed, page, commandTail = Promise.resolve(), releaseCommandGate = () => {}
let loseRoleReceiptOnce = false
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
    // Model an interrupted UI transport after SQLite committed the request.
    // The browser must retain its original confirmation/request ID for retry.
    if (loseRoleReceiptOnce && action === 'role_save' && !result.error) {
      loseRoleReceiptOnce = false
      throw new Error('合成验收：保存成功后回执中断，请原样重试')
    }
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
  await waitText('.md-window', '状态：未开启')
  await button('开启').click(); await confirm()
  assert.equal(admissions.length, 1)
  assert.equal(admissions[0].content[0].text, '记忆开启')
  assert.equal(controls.get(session.id).mode, 'off', 'Accepted is not a completed control command')
  assert.ok((await page.locator('.md-window').innerText()).includes('状态：未开启'))
  allowCommands(); await commandTail
  await waitText('.md-window', '状态：已开启')
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
  await confirm(); await nav('角色').click()
  await page.getByRole('group', { name: '角色资料类型' }).getByRole('button', { name: '成长', exact: true }).click()
  await waitText('.md-list', '演示成长：讲故事慢一点')
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

  // The following role forms exercise the same fixed owner channel and real
  // domain services as the ordinary memory paths. Direct reads only inspect
  // committed state; one explicit second-client write creates a real CAS race.
  async function inspectRole(action, args) {
    const result = await controller.request(action, { ...args, conversation_id: session.id })
    auxiliaryRequests.push({ action, result_error: result.error })
    assert.equal(result.error, undefined, JSON.stringify(result))
    return result
  }
  const roleCatalog = async type => inspectRole('role_catalog', { role_type: type, offset: 0, limit: 50 })
  const roleHead = async (type, id) => (await roleCatalog(type)).items.find(item => item.id === id)
  const roleHistory = async (type, item) => inspectRole('role_history', {
    role_type: type, id: item.id, ...(item.context ? { context: item.context } : {}), limit: 50,
  })
  const roleLabels = { narrative: '自传', intention: '承诺', companionship: '相处偏好', growth: '成长' }
  async function selectRoleType(type) {
    await nav('角色').click()
    await page.getByRole('group', { name: '角色资料类型' }).getByRole('button', { name: roleLabels[type], exact: true }).click()
    await page.locator('.md-list').waitFor()
  }
  const form = name => page.locator('.md-detail').getByLabel(name, { exact: true })
  async function openRole(type, item) {
    await selectRoleType(type)
    const title = item.payload?.title || item.payload?.text || item.payload?.value || item.id
    await page.locator('.md-list .md-row').filter({ hasText: title }).first().click()
    await page.getByRole('heading', { name: '编辑角色资料', exact: true }).waitFor()
    await page.locator('.md-detail').getByText('版本历史查看与对照', { exact: true }).waitFor()
  }
  async function saveRole(type, matcher) {
    if (type === 'growth') await form('允许近期明确纠正').check()
    const catalogBefore = (await roleCatalog(type)).items
    const before = requestCount('role_save')
    await button('准备保存').click()
    assert.equal(requestCount('role_save'), before, 'Preparing a role form must not send a write')
    assert.deepEqual((await roleCatalog(type)).items, catalogBefore, 'Preparing a role form must not change SQLite')
    await confirm()
    const item = (await roleCatalog(type)).items.find(matcher)
    assert.ok(item, `Saved ${type} must appear in its actual catalogue`)
    return item
  }
  async function transitionRole(type, item, label, target) {
    await openRole(type, item)
    await form('操作理由').fill(`合成角色状态：${target}`)
    const before = requestCount('role_transition')
    await button(`准备${label}`).click()
    assert.equal(requestCount('role_transition'), before)
    assert.deepEqual(await roleHead(type, item.id), item, 'Transition confirmation must precede the write')
    await confirm()
    const changed = await roleHead(type, item.id)
    assert.ok(changed, 'A terminal record must retain a catalogue header')
    assert.equal(changed.state, target)
    assert.ok(changed.version > item.version)
    return changed
  }

  await selectRoleType('narrative'); await button('新建').click()
  await form('标题').fill('合成自传：月亮茶馆')
  // A user's literal text must stay verbatim even when it spells an enum.
  await form('内容').fill('shared_fiction')
  await form('视角').selectOption('shared'); await form('性质').selectOption('shared_fiction')
  await form('操作理由').fill('演示用户明确建立共同虚构自传')
  await preserveDraftAcrossPoll(form('内容'), () => { session.header.title = '合成演示窗口：自传新建草稿' })
  let narrative = await saveRole('narrative', item => item.payload?.title === '合成自传：月亮茶馆')
  assert.equal(narrative.payload.modality, 'shared_fiction')
  await openRole('narrative', narrative)
  assert.equal(await form('性质').inputValue(), 'shared_fiction')
  await page.locator('.md-detail').getByText('shared_fiction', { exact: true }).waitFor()
  await page.locator('.md-detail').getByText('共同虚构', { exact: true }).last().waitFor()
  await form('标题').fill('合成自传：月亮茶馆续篇')
  await form('内容').fill('共同虚构的月亮茶馆挂起了一盏纸灯。')
  await form('操作理由').fill('演示用户修订虚构自传')
  const narrativeVersion = narrative.version
  narrative = await saveRole('narrative', item => item.id === narrative.id)
  assert.ok(narrative.version > narrativeVersion)
  assert.equal(narrative.payload.modality, 'shared_fiction')
  await openRole('narrative', narrative)
  assert.equal(await form('性质').inputValue(), 'shared_fiction')
  const narrativeHistory = await roleHistory('narrative', narrative)
  assert.ok(narrativeHistory.items.length >= 2)
  assert.ok(JSON.stringify(narrativeHistory).includes('shared_fiction'))
  checked.push('narrative form creates and edits a versioned shared-fiction entry without converting it into a fact')
  await page.screenshot({ path: join(output, '05-role-fiction-history.png'), fullPage: true })

  await selectRoleType('intention'); await button('新建').click()
  await form('承诺内容').fill('合成承诺：一起整理茶具')
  await form('承担方').selectOption('shared'); await form('类别').selectOption('promise')
  await form('操作理由').fill('演示用户确认承诺')
  let intention = await saveRole('intention', item => item.payload?.text === '合成承诺：一起整理茶具')
  await openRole('intention', intention)
  assert.equal(await form('承担方').isDisabled(), true)
  assert.equal(await form('类别').isDisabled(), true)
  await form('承诺内容').fill('合成承诺：先清洗再整理茶具')
  await form('操作理由').fill('演示用户修订承诺内容')
  const intentionVersion = intention.version
  intention = await saveRole('intention', item => item.id === intention.id)
  assert.ok(intention.version > intentionVersion)
  intention = await transitionRole('intention', intention, '完成', 'completed')
  await selectRoleType('intention'); await button('新建').click()
  await form('承诺内容').fill('合成承诺：取消备用茶会')
  await form('操作理由').fill('演示用户确认待取消承诺')
  let cancelledIntention = await saveRole('intention', item => item.payload?.text === '合成承诺：取消备用茶会')
  cancelledIntention = await transitionRole('intention', cancelledIntention, '取消', 'cancelled')
  assert.ok((await roleHistory('intention', intention)).items.length >= 3)
  assert.ok((await roleHistory('intention', cancelledIntention)).items.length >= 2)
  checked.push('intention form creates and edits a promise and records explicit completion and cancellation separately')

  await selectRoleType('companionship'); await button('新建').click()
  await form('偏好名称').fill('合成泡茶偏好'); await form('场景').fill('验收茶会')
  await form('偏好内容').fill('演示相处偏好：先询问茶的温度')
  await form('操作理由').fill('演示用户确认场景偏好')
  let companionship = await saveRole('companionship', item => item.id === '合成泡茶偏好' && item.context === '验收茶会')
  await openRole('companionship', companionship)
  assert.equal(await form('偏好名称').isDisabled(), true)
  assert.equal(await form('场景').isDisabled(), true)
  await form('偏好内容').fill('演示相处偏好：先询问温度，再慢慢倒茶')
  await form('操作理由').fill('演示用户更新相处偏好')
  const companionshipVersion = companionship.version
  companionship = await saveRole('companionship', item => item.id === companionship.id && item.context === companionship.context)
  assert.ok(companionship.version > companionshipVersion)
  assert.ok((await roleHistory('companionship', companionship)).items.length >= 2)
  checked.push('companionship form creates and edits an explicitly scoped preference while locking its identity and context')

  await selectRoleType('growth'); await button('新建').click()
  await form('成长字段').selectOption('support_style')
  await form('内容').fill('演示成长：先倾听再回应')
  await form('操作理由').fill('演示用户确认支持方式')
  let growth = await saveRole('growth', item => item.id === 'support_style')
  await openRole('growth', growth)
  assert.equal(await form('成长字段').isDisabled(), true)
  await form('内容').fill('演示成长：先倾听，再简短而温和地回应')
  await form('操作理由').fill('演示用户更新支持方式')
  await preserveDraftAcrossPoll(form('内容'), () => { session.header.title = '合成演示窗口：角色编辑草稿' })
  const growthVersion = growth.version
  growth = await saveRole('growth', item => item.id === growth.id)
  assert.ok(growth.version > growthVersion)
  checked.push('growth form creates and edits an allowed expression field and retains drafts during window polling')

  // A second management client commits between the UI's read and confirmation.
  await openRole('growth', growth)
  await form('内容').fill('演示成长：过期编辑应被拒绝')
  await form('操作理由').fill('合成旧版本并发验证')
  await form('允许近期明确纠正').check()
  await button('准备保存').click()
  const concurrent = await inspectRole('role_save', { role_type: 'growth', id: growth.id,
    expected_version: growth.version, payload: { value: '演示成长：另一窗口已更新' },
    reason: '合成第二管理窗口并发写入', request_id: randomUUID(), allow_recent: true })
  assert.ok(concurrent)
  growth = await roleHead('growth', growth.id)
  const afterConcurrent = await roleHistory('growth', growth)
  await button('确认提交').click()
  await waitText('.md-confirm .md-error', 'STALE_REFERENCE')
  assert.equal(await form('内容').inputValue(), '演示成长：过期编辑应被拒绝')
  assert.equal((await roleHead('growth', growth.id)).version, growth.version)
  await button('确认提交').click()
  await waitText('.md-confirm .md-error', 'STALE_REFERENCE')
  assert.equal((await roleHead('growth', growth.id)).version, growth.version)
  assert.equal((await roleHistory('growth', growth)).items.length, afterConcurrent.items.length)
  await page.screenshot({ path: join(output, '06-role-stale-conflict.png'), fullPage: true })
  await page.locator('.md-confirm').getByRole('button', { name: '取消', exact: true }).click()
  await button('刷新').click()
  await openRole('growth', growth)
  assert.equal(await form('内容').inputValue(), '演示成长：另一窗口已更新')
  checked.push('a real concurrent role edit shows a stale-version error, preserves the draft, and cannot duplicate writes on retry')

  // Commit once, lose only the response, then click the same confirmation again.
  await form('内容').fill('演示成长：合成回执恢复后只保存一次')
  await form('操作理由').fill('合成回执中断恢复')
  await form('允许近期明确纠正').check()
  await button('准备保存').click()
  loseRoleReceiptOnce = true
  await button('确认提交').click()
  await waitText('.md-confirm .md-error', '回执中断')
  const committed = await roleHead('growth', growth.id)
  assert.ok(committed.version > growth.version)
  const committedHistory = await roleHistory('growth', committed)
  const firstAttempt = requests.filter(item => item.action === 'role_save').at(-1)
  await confirm()
  const replayAttempt = requests.filter(item => item.action === 'role_save').at(-1)
  assert.equal(replayAttempt.args.request_id, firstAttempt.args.request_id)
  assert.equal((await roleHead('growth', growth.id)).version, committed.version)
  assert.equal((await roleHistory('growth', committed)).items.length, committedHistory.items.length)
  growth = committed
  checked.push('a lost committed role receipt retries the original request ID and returns without another version or history row')

  narrative = await transitionRole('narrative', narrative, '撤回', 'withdrawn')
  await openRole('narrative', narrative)
  const withdrawnHistory = await roleHistory('narrative', narrative)
  assert.ok(withdrawnHistory.items.length >= 3)
  assert.ok(JSON.stringify(withdrawnHistory).includes('withdrawn'))
  assert.ok(JSON.stringify(withdrawnHistory).includes('shared_fiction'))
  checked.push('withdrawing a fictional narrative keeps its catalogue header and a readable version history')
  await page.screenshot({ path: join(output, '07-role-withdrawn-history.png'), fullPage: true })

  // Reconfirmation starts with an empty value and the tombstone's version;
  // neither the form nor the domain may silently restore the previous body.
  for (const [type, current, label] of [['companionship', companionship, '偏好内容'], ['growth', growth, '内容']]) {
    const revoked = await transitionRole(type, current, '撤销', 'revoked')
    await openRole(type, revoked)
    assert.equal(await form(label).inputValue(), '')
    assert.equal(await button('准备撤销').isDisabled(), true)
    await form(label).fill('演示重新确认：这是用户本次明确填写的新内容')
    await form('操作理由').fill('演示撤销后按当前版本重新确认')
    const restored = await saveRole(type, item => item.id === revoked.id)
    assert.ok(restored.version > revoked.version)
    assert.equal(restored.state, type === 'growth' ? 'active' : 'confirmed')
    if (type === 'growth') growth = restored
    else companionship = restored
  }
  checked.push('revoked preferences and growth retain tombstones and require newly entered values at the current version for reconfirmation')

  await button('睡眠').click(); await confirm(); await commandTail
  assert.deepEqual(commandErrors, [])
  assert.ok(reviewedBatches > 0)
  await nav('睡眠与梦境').click(); await waitText('.md-content', '梦境虚构')
  await waitText('.md-content', '已完成')
  await waitText('.md-window', `已审阅 ${reviewedBatches}/${reviewedBatches} 批`)
  assert.ok(!(await page.locator('.md-window').innerText()).includes('sleep_id'))
  await button('苏醒').click(); await confirm(); await commandTail
  assert.equal(controls.get(session.id).mode, 'active')
  await waitText('.md-window', '状态：已开启')
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
  for (const [type, item] of [['narrative', narrative], ['intention', intention], ['companionship', companionship], ['growth', growth]]) {
    await openRole(type, item)
    const width = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: innerWidth }))
    assert.ok(width.scroll <= width.viewport, `${type} editor overflows ${width.scroll} > ${width.viewport}`)
    await page.locator('.md-detail').getByRole('heading', { name: '编辑角色资料', exact: true }).waitFor()
  }
  await page.screenshot({ path: join(output, '08-role-editor-narrow-dark.png'), fullPage: true })
  checked.push('all four role editors including terminal records and version histories fit the narrow dark host view')

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
    paths: checked.length, requests: requests.length, auxiliary_role_requests: auxiliaryRequests.length, reviewed_batches: reviewedBatches,
    browser_version: browser.version(), screenshots: ['01-role-light.png', '02-memory-source-light.png', '03-sleep-dream-light.png', '04-narrow-dark.png',
      '05-role-fiction-history.png', '06-role-stale-conflict.png', '07-role-withdrawn-history.png', '08-role-editor-narrow-dark.png'],
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
