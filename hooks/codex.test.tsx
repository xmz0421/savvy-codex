import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { EngineInterface, On } from 'claude-code'
import type { CodexJob } from '../types'
import { absolutePath, CODEX_DELEGATE, CODEX_STATUS, mergeState, normalizeScopes, normalizeStatus, POLL_MS, previewFiles, sensitivePath, within, workerCommand } from './codex'

const ROOT = 'E:/workspace'
const PYTHON = 'python'
const HOME = 'C:/Users/Test'
const PACKET = `${ROOT}/packet.json`
const ID = '20261103T010000Z-test'
const RUN = `${HOME}/.claude/codex-worker-runs/abc123/${ID}`
const STATE = `${RUN}/state.json`
const REPORT = `${RUN}/report.md`
const FINAL = `${RUN}/final.json`
const HELPER = `${HOME}/.claude/skills/savvy-codex/skills/codex-worker/scripts/codex_delegate.py`
const MODEL = 'gpt-6.1-sol'
const GOAL = '检查边界条件'
const REASON = '范围明确的只读检查，使用 Sol 与 medium。'
const STARTED = '2026-11-03T01:00:00Z'
const INPUT = { tool: CODEX_DELEGATE, workspace: ROOT, packet_file: PACKET, model: MODEL, effort: 'medium', background: true }
const PANE = { title: 'Agents', isFocused: true, bodyColumns: 70, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } as const
const BAND = { hasSurvey: false, isWorking: true, maxRows: 4, bodyColumns: 100, scroll: { offset: 0, bodyRows: 4 }, view: {} }
const ok = (value: unknown) => ({ value }) as never

function queued() {
  return {
    status: 'queued', id: ID, job_id: ID, workspace: ROOT, run_dir: RUN,
    state_path: STATE, report_path: REPORT, result_path: FINAL,
    model: MODEL, effort: 'medium', goal: GOAL, task: GOAL, reason: REASON,
    started_at: STARTED, finished_at: null, authentication: { verified: false, method: 'ChatGPT' },
    artifacts: { state: STATE, report: REPORT, final: FINAL },
  }
}

function completed(status = 'complete') {
  const result = { status, summary: status === 'complete' ? '已核对 3 个边界条件，无需修改。' : '工具访问被拒绝。', changed_files: ['src/checked.ts'], checks: ['只读核对 3 项'], findings: [], limitations: status === 'complete' ? [] : ['exec_command permission denied'] }
  return { ...queued(), status, result, summary: result.summary, changed_files: result.changed_files, finished_at: '2026-11-03T01:00:05Z', authentication: { verified: true, method: 'ChatGPT' } }
}

type WorldOptions = {
  env?: Record<string, string>
  packet?: object
  start?: object
  wait?: object
  waitError?: string
  startError?: string
  missingState?: boolean
  waitStates?: object[]
  waitDelays?: number[]
}

function world(on: On, options: WorldOptions = {}) {
  const clock = mock.clock(on, { now: Date.parse(STARTED) })
  mock.store(on)
  mock.env(on, { USERPROFILE: HOME, OS: 'Windows_NT', LANG: 'zh_CN.UTF-8', ...options.env })
  const files: Record<string, string> = {
    [PACKET]: JSON.stringify(options.packet ?? { goal: GOAL, reason: REASON }),
    [STATE]: JSON.stringify(queued()),
    [REPORT]: '# Codex worker report\n\n已核对。\n',
    [FINAL]: JSON.stringify(completed().result),
    [`${ROOT}/src/checked.ts`]: 'export const checked = true\n',
  }
  if (options.missingState) delete files[STATE]
  const runs: { argv: string[]; cwd?: string; timeoutMs?: number }[] = []
  const opened: { id: string; title?: string }[] = []
  const copied: string[] = []
  const notices: string[] = []
  const reads: string[] = []
  const registered: string[] = []
  const links = new Set<string>()
  const sizes: Record<string, number> = {}
  let modelCalls = 0
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ok(ROOT))
  on('session.id', () => ok('codex-ui-test'))
  on('settings.read', () => ok({ language: 'zh' }))
  on('tool.register', (_$, e) => {
    registered.push(e.name)
    return ok({ tool: `mcp__savvy-codex__${e.name}` })
  })
  on('command.register', (_$, e) => ok({ command: e.name }))
  on('ui.open', (_$, e) => { opened.push({ id: e.id, title: e.title }); return ok({ isPlaced: true }) })
  on('ui.copy', (_$, e) => { copied.push(e.text); return ok({ isCopied: true }) })
  on('ui.toast', (_$, e) => { notices.push(e.message ?? e.text); return ok(undefined) })
  on('model.complete', () => { modelCalls++; throw new Error('No models are allowed in UI tests') })
  on('fs.stat', (_$, e) => {
    const path = absolutePath(e.path)
    const file = files[path]
    if (file !== undefined) return ok({ kind: 'file', size: sizes[path] ?? new TextEncoder().encode(file).length, mtimeMs: Date.parse(STARTED), isLink: links.has(path) })
    if (/^[A-Za-z]:\/$/.test(path) || Object.keys(files).some(candidate => within(path, candidate))) return ok({ kind: 'dir', size: 0, mtimeMs: 0, isLink: links.has(path) })
    throw new Error(`ENOENT ${path}`)
  })
  on('fs.read', (_$, e) => {
    const path = absolutePath(e.path)
    reads.push(path)
    if (!(path in files)) throw new Error(`ENOENT ${path}`)
    return ok(files[path])
  })
  on('process.run', async (_$, e) => {
    const argv = [...e.argv]
    runs.push({ argv, cwd: e.init?.cwd, timeoutMs: e.init?.timeoutMs })
    if (argv[0] !== PYTHON || argv[1] !== HELPER) throw new Error('Unexpected executable in a mocked test')
    const response = (data: object, exitCode = 0) => ok({ exitCode, stdout: JSON.stringify(data), stderr: '', isStdoutTruncated: false, isStderrTruncated: false })
    if (argv[2] === 'start') {
      if (options.startError) return ok({ exitCode: 1, stdout: '', stderr: options.startError, isStdoutTruncated: false, isStderrTruncated: false })
      return response(options.start ?? queued())
    }
    if (argv[2] === 'wait') {
      const delay = options.waitDelays?.shift()
      if (delay) await clock.sleep(delay)
      if (options.waitError) return ok({ exitCode: 1, stdout: '', stderr: options.waitError, isStdoutTruncated: false, isStderrTruncated: false })
      const finished = options.waitStates?.shift() ?? options.wait ?? completed()
      files[STATE] = JSON.stringify(finished)
      return response(finished, (finished as { status: string }).status === 'blocked' ? 1 : 0)
    }
    if (argv[2] === 'status') {
      const saved = files[STATE]
      return response(saved === undefined ? { status: 'blocked', summary: 'ENOENT state.json' } : JSON.parse(saved))
    }
    if (argv[2] === 'catalog') return response({ status: 'ready', models: [{ id: 'model-from-local-catalog', supported_reasoning_levels: ['medium', 'ultra'] }] })
    if (argv[2] === 'cleanup') return response({ status: 'complete', dry_run: argv.includes('--dry-run'), deleted: [], would_delete: [], skipped: [], errors: [] })
    throw new Error('Unexpected helper command')
  })
  return { clock, files, runs, opened, copied, notices, reads, registered, links, sizes, modelCalls: () => modelCalls }
}

const start = async ($: Engine) => {
  await $.session.start({ source: 'startup', cwd: ROOT, surface: 'desktop', isInteractive: true } as never)
  await $.command.run({ command: 'savvy-start', args: '' } as never)
}
const call = ($: Engine, value: object) => $.tool.call(value as never)
const mount = ($: Engine, surface: 'desktop' | 'terminal' = 'desktop') => $.ui.mount({ plugin: 'savvy-codex', surface, component: 'Pane', requestId: 'savvy-agents', props: PANE })

const environment = (env: Record<string, string>) => ({ env: { get: async (key: string) => env[key] } }) as unknown as EngineInterface

test('savvy-start forwards a task once after the command returns, without a helper or wrapper agent', async ($, on) => {
  const w = world(on)
  const sent: string[] = []
  on('prompt.submit', (_$, e) => { sent.push(e.text); return { text: e.text } })
  await $.session.start({ source: 'startup', cwd: ROOT, surface: 'desktop', isInteractive: true } as never)
  const task = '检查测试入口\n保留 `literal` 和 $(literal)，按需要选择两家的 worker。'
  expect(await $.command.run({ command: 'savvy-start', args: task } as never)).toMatchObject({ text: '已启用本次协作，正在将任务提交给当前 Claude 会话。' })
  expect(sent).toEqual([])
  await w.clock.advance(1)
  expect(sent.length).toBe(1)
  expect(sent[0]).toContain('savvy-codex:savvy-flow')
  expect(sent[0]).toContain(task)
  expect(w.runs).toEqual([])
  expect(w.modelCalls()).toBe(0)
  await w.clock.advance(POLL_MS)
  expect(sent.length).toBe(1)
})

test('savvy-start with no task does not submit; off blocks activation and task forwarding', async ($, on) => {
  const w = world(on)
  const sent: string[] = []
  on('prompt.submit', (_$, e) => { sent.push(e.text); return { text: e.text } })
  await start($)
  await w.clock.advance(1)
  expect(sent).toEqual([])
  await $.command.run({ command: 'savvy-mode', args: 'off' } as never)
  await $.command.run({ command: 'savvy-start', args: '不要启动这个任务' } as never)
  await w.clock.advance(1)
  expect(sent).toEqual([])
  expect(await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'status' })).toMatchObject({ result: { mode: 'off', allowed: false } })
})

test('a dropped task is visible and consumes the manual activation', async ($, on) => {
  const w = world(on)
  on('prompt.submit', () => ({ drop: 'test policy refused delivery' }))
  await start($)
  await $.command.run({ command: 'savvy-start', args: '这次任务' } as never)
  await w.clock.advance(1)
  expect(await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'status' })).toMatchObject({ result: { mode: 'manual', allowed: false } })
  expect(JSON.stringify(w.notices)).toContain('test policy refused delivery')
})

test('switching off before a queued task is delivered prevents submission', async ($, on) => {
  const w = world(on)
  const sent: string[] = []
  on('prompt.submit', (_$, e) => { sent.push(e.text); return { text: e.text } })
  await start($)
  await $.command.run({ command: 'savvy-start', args: '取消这个待提交任务' } as never)
  await $.command.run({ command: 'savvy-mode', args: 'off' } as never)
  await w.clock.advance(1)
  expect(sent).toEqual([])
  expect(JSON.stringify(w.notices)).toContain('协作已在提交前关闭')
})

test('worker defaults remain portable and resolve the actual host plugin root', async () => {
  const $ = environment({ OS: 'Windows_NT', CLAUDE_PLUGIN_ROOT: 'C:\\Plugins\\Savvy Codex' })
  expect(await workerCommand($, {})).toEqual(['python', 'C:/Plugins/Savvy Codex/skills/codex-worker/scripts/codex_delegate.py'])
})

test('Unix Python default and explicit settings override environment paths without a shell', async () => {
  const $ = environment({ SAVVY_CODEX_PLUGIN_ROOT: '/opt/savvy', SAVVY_CODEX_PYTHON: '/opt/Python 3/python' })
  expect(await workerCommand($, {})).toEqual(['/opt/Python 3/python', '/opt/savvy/skills/codex-worker/scripts/codex_delegate.py'])
  expect(await workerCommand($, { pythonPath: '/custom/python', pluginRoot: '/custom/plugin' })).toEqual(['/custom/python', '/custom/plugin/skills/codex-worker/scripts/codex_delegate.py'])
  expect(await workerCommand($, { helperPath: '/custom/helper.py' })).toEqual(['/opt/Python 3/python', '/custom/helper.py'])
})

test('Unix default uses python3 and helper overrides take precedence over plugin roots', async () => {
  const $ = environment({ SAVVY_CODEX_HELPER: '/installed/helper.py', CLAUDE_PLUGIN_ROOT: '/marketplace/plugin' })
  expect(await workerCommand($, {})).toEqual(['python3', '/installed/helper.py'])
  expect(await workerCommand($, { helperPath: '/settings/helper.py' })).toEqual(['python3', '/settings/helper.py'])
})

test('startup registers native tools; an idle clock never starts a process or model', async ($, on) => {
  const w = world(on)
  await start($)
  expect(w.registered).toContain('codex_delegate')
  expect(w.registered).toContain('codex_status')
  expect(w.registered).toContain('progress')
  for (let index = 0; index < 3; index++) await w.clock.advance(POLL_MS)
  expect(w.runs).toEqual([])
  expect(w.modelCalls()).toBe(0)
})

for (const surface of ['desktop', 'terminal'] as const) {
  test(`${surface}: background Codex is in the same Savvy pane, fully labelled, and not duplicated as planned`, async ($, on) => {
    const w = world(on)
    await start($)
    await call($, { tool: 'mcp__savvy-codex__progress', title: '联合检查', phase: 'delegate', tasks: [{ title: GOAL, provider: 'codex', model: MODEL, effort: 'medium' }] })
    const response = await call($, INPUT)
    expect(response).toMatchObject({ result: { provider: 'external Codex worker', job_id: ID, status: 'queued', model: MODEL, effort: 'medium' } })
    expect(w.runs).toHaveLength(1)
    expect(w.runs[0]?.argv).toEqual([PYTHON, HELPER, 'start', '--workspace', ROOT, '--packet', PACKET, '--model', MODEL, '--effort', 'medium', '--permission', 'read-only', '--approval', 'never', '--timeout', '900'])
    expect(w.opened.every(pane => pane.id === 'savvy-agents')).toBe(true)
    const pane = await mount($, surface)
    const drawn = JSON.stringify(await pane.drawn())
    expect(drawn).toContain('Codex · 外部 worker')
    expect(drawn).toContain(MODEL)
    expect(drawn).toContain('medium')
    expect(drawn).toContain('已排队')
    expect(drawn).not.toContain('计划中 · 1')
    const band = await $.ui.mount({ plugin: 'savvy-codex', surface, component: 'AbovePrompt', props: BAND })
    expect((await band.find({ key: 'savvy-agents' }))?.props.label).toBe('×1')
    expect(w.modelCalls()).toBe(0)
  })

  test(`${surface}: click report renders native Code in the same pane and copies the full path`, async ($, on) => {
    const w = world(on)
    await start($)
    await call($, INPUT)
    const pane = await mount($, surface)
    await pane.press({ key: `codex-row-${ID}` })
    await pane.press({ key: `codex-file-${ID}-0` })
    expect((await pane.findAll({ type: 'Code' }))[0]?.props).toMatchObject({ path: REPORT, source: '# Codex worker report\n\n已核对。\n' })
    await pane.press({ key: 'codex-copy-preview' })
    expect(w.copied).toEqual([REPORT])
    await pane.press({ key: 'codex-back' })
    expect(await pane.find({ key: `codex-file-${ID}-0` })).toBeDefined()
    expect(w.opened).toHaveLength(1)
    expect(w.runs).toHaveLength(1)
  })
}

test('default delegate waits once in the helper and returns the concrete worker result', async ($, on) => {
  const w = world(on)
  await start($)
  expect(await call($, { ...INPUT, background: false })).toMatchObject({ result: { status: 'complete', summary: '已核对 3 个边界条件，无需修改。', result: { checks: ['只读核对 3 项'] } } })
  expect(w.runs.map(run => run.argv[2])).toEqual(['start', 'wait'])
  expect(w.runs[1]?.argv).toEqual([PYTHON, HELPER, 'wait', '--run-dir', RUN, '--timeout', '45'])
  expect(w.runs[1]?.timeoutMs).toBeLessThanOrEqual(60_000)
  expect(w.modelCalls()).toBe(0)
})

test('clock observes real running and blocked states without process or model polling', async ($, on) => {
  const w = world(on)
  await start($)
  await call($, INPUT)
  w.files[STATE] = JSON.stringify({ ...queued(), status: 'running', last_tool: 'exec_command', last_message: '已读取实际输出。', reasoning: 'must never display this' })
  await w.clock.advance(POLL_MS)
  let pane = await mount($)
  let text = JSON.stringify(await pane.drawn())
  expect(text).toContain('执行中')
  expect(text).toContain('exec_command')
  expect(text).toContain('已读取实际输出。')
  expect(text).not.toContain('must never display this')
  w.files[STATE] = JSON.stringify(completed('blocked'))
  await w.clock.advance(POLL_MS)
  await pane.press({ key: `codex-row-${ID}` })
  text = JSON.stringify(await pane.drawn())
  expect(text).toContain('受阻')
  expect(text).toContain('exec_command permission denied')
  expect(w.runs).toHaveLength(1)
})

test('missing state stays queued; unknown states and a wait timeout never become success', async ($, on) => {
  const w = world(on, { missingState: true, wait: { ...queued(), status: 'running', wait_timed_out: true } })
  await start($)
  await call($, INPUT)
  await w.clock.advance(POLL_MS)
  expect(await call($, { tool: CODEX_STATUS, job_id: ID })).toMatchObject({ result: { status: 'queued' } })
  expect(await call($, { tool: CODEX_STATUS, job_id: ID, wait: true, timeout: 2 })).toMatchObject({ result: { status: 'running', wait_timed_out: true } })
  w.files[STATE] = JSON.stringify({ ...queued(), status: 'unrecognized_phase' })
  expect(await call($, { tool: CODEX_STATUS, job_id: ID })).toMatchObject({ result: { status: 'unknown', raw_status: 'unrecognized_phase' } })
})

test('worker failures and wait transport errors are visible without inventing completion', async ($, on) => {
  const w = world(on, { waitError: 'native process transport failed' })
  await start($)
  expect(await call($, { ...INPUT, background: false })).toMatchObject({ result: { job_id: ID, status: 'queued', wait_error: expect.stringContaining('native process transport failed') } })
  expect(w.runs).toHaveLength(2)
})

test('a helper start failure is retained as a blocked launch with no invented run directory', async ($, on) => {
  const w = world(on, { startError: 'process launch denied' })
  await start($)
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked', summary: expect.stringContaining('process launch denied') } })
  expect(await call($, { tool: CODEX_STATUS })).toMatchObject({ result: { jobs: [{ status: 'blocked', run_dir: '', failure_kind: 'launch_failed' }] } })
  expect(w.opened).toHaveLength(1)
  const pane = await mount($)
  expect(JSON.stringify(await pane.drawn())).toContain('process launch denied')
})

test('a blocked helper wait returns its exact limitations even with a nonzero exit code', async ($, on) => {
  const w = world(on, { wait: completed('blocked') })
  await start($)
  expect(await call($, { ...INPUT, background: false })).toMatchObject({ result: { status: 'blocked', summary: '工具访问被拒绝。', result: { limitations: ['exec_command permission denied'] } } })
  expect(w.runs.map(run => run.argv[2])).toEqual(['start', 'wait'])
})

test('invalid effort, absent packet reason, and subagent recursion do not start a worker', async ($, on) => {
  const w = world(on)
  await start($)
  expect(await call($, { ...INPUT, effort: 'bad; effort' })).toMatchObject({ result: { status: 'blocked' } })
  w.files[PACKET] = JSON.stringify({ goal: GOAL })
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked', summary: expect.stringContaining('reason') } })
  expect(await call($, { ...INPUT, agentId: 'claude-child' })).toMatchObject({ result: { status: 'blocked', summary: expect.stringContaining('主会话') } })
  expect(w.runs).toEqual([])
})

test('worker environment prevents recursion and unknown run directories grant no file access', async ($, on) => {
  const w = world(on, { env: { CLAUDE_CODEX_WORKER: '1' } })
  await start($)
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked', summary: expect.stringContaining('递归') } })
  const reads = w.reads.length
  expect(await call($, { tool: CODEX_STATUS, run_dir: 'C:/Users/Test/.claude/another-run' })).toMatchObject({ result: { status: 'blocked' } })
  expect(w.reads).toHaveLength(reads)
  expect(w.runs).toEqual([])
})

test('preview rejects a symlink and size growth; it never executes changed files', async ($, on) => {
  const w = world(on)
  await start($)
  await call($, INPUT)
  w.files[STATE] = JSON.stringify(completed())
  await call($, { tool: CODEX_STATUS, job_id: ID })
  w.links.add(`${ROOT}/src`)
  const pane = await mount($)
  await pane.press({ key: `codex-row-${ID}` })
  await pane.press({ key: `codex-file-${ID}-2` })
  expect(JSON.stringify(await pane.drawn())).toContain('符号链接或目录联接')
  expect(w.reads).not.toContain(`${ROOT}/src/checked.ts`)
  await pane.press({ key: 'codex-back' })
  w.sizes[REPORT] = 300_000
  await pane.press({ key: `codex-file-${ID}-0` })
  expect(JSON.stringify(await pane.drawn())).toContain('预览上限')
  expect(w.runs.map(run => run.argv[2])).toEqual(['start', 'status'])
})

test('preview rejects invalid UTF-8 and a file that grows beyond its stat size', async ($, on) => {
  const w = world(on)
  await start($)
  await call($, INPUT)
  w.files[REPORT] = 'invalid \uFFFD text'
  const pane = await mount($)
  await pane.press({ key: `codex-row-${ID}` })
  await pane.press({ key: `codex-file-${ID}-0` })
  expect(JSON.stringify(await pane.drawn())).toContain('UTF-8')
  expect(await pane.findAll({ type: 'Code' })).toHaveLength(0)
  await pane.press({ key: 'codex-back' })
  w.files[REPORT] = '汉'.repeat(100_000)
  w.sizes[REPORT] = 1
  await pane.press({ key: `codex-file-${ID}-0` })
  expect(JSON.stringify(await pane.drawn())).toContain('读取时文件超过预览上限')
  expect(await pane.findAll({ type: 'Code' })).toHaveLength(0)
})

test('Claude and Codex share the pane and crew count while retaining their own model data', async ($, on) => {
  const w = world(on)
  on('agent.spawn', () => ({ model: 'claude-sonnet-5-5', agentId: 'claude-reviewer' }))
  await start($)
  await call($, { tool: 'mcp__savvy-codex__progress', title: '联合任务', phase: 'delegate', tasks: [
    { title: 'Claude 复核', tier: 'medium', provider: 'claude' },
    { title: GOAL, provider: 'codex', model: MODEL, effort: 'medium' },
  ] })
  await $.agent.spawn({ tool_use_id: 'claude-start', prompt: 'Read and review.', description: 'Claude 复核', subagentType: 'savvy-codex:savvy-medium', provider: 'engine', parentModel: 'claude-sonnet-5-5', background: false, fork: false } as never)
  await call($, INPUT)
  const pane = await mount($)
  const text = JSON.stringify(await pane.drawn())
  expect(text).toContain('Claude 复核')
  expect(text).toContain('Sonnet')
  expect(text).toContain(MODEL)
  expect(text).not.toContain('计划中 · 1')
  const band = await $.ui.mount({ plugin: 'savvy-codex', surface: 'desktop', component: 'AbovePrompt', props: BAND })
  expect((await band.find({ key: 'savvy-agents' }))?.props.label).toBe('×2')
  expect(w.opened.every(pane => pane.id === 'savvy-agents')).toBe(true)
})

test('native UI clock stays live during a pending helper wait and long waits use bounded chunks', async ($, on) => {
  const w = world(on, {
    waitStates: [{ ...queued(), status: 'running', wait_timed_out: true }, completed()],
    waitDelays: [10_000, 0],
  })
  await start($)
  const pending = call($, { ...INPUT, background: false, timeout: 90 })
  await w.clock.settle()
  w.files[STATE] = JSON.stringify({ ...queued(), status: 'running', last_tool: 'exec_command' })
  await w.clock.advance(POLL_MS)
  const pane = await mount($)
  expect(JSON.stringify(await pane.drawn())).toContain('exec_command')
  await w.clock.advance(10_000)
  expect(await pending).toMatchObject({ result: { status: 'complete' } })
  expect(w.runs.map(run => run.argv[2])).toEqual(['start', 'wait', 'wait'])
  expect(w.runs.every(run => (run.timeoutMs ?? 0) <= 60_000)).toBe(true)
  expect(w.modelCalls()).toBe(0)
})

test('a new flow clears finished Codex rows while keeping a running worker', async ($, on) => {
  const w = world(on)
  await start($)
  await call($, INPUT)
  await call($, { tool: 'mcp__savvy-codex__progress', title: 'First flow', phase: 'delegate', total: 1 })
  expect(await call($, { tool: CODEX_STATUS })).toMatchObject({ result: { jobs: [{ job_id: ID }] } })
  w.files[STATE] = JSON.stringify(completed())
  await call($, { tool: CODEX_STATUS, job_id: ID })
  await call($, { tool: 'mcp__savvy-codex__progress', title: 'Second flow', phase: 'plan', total: 1 })
  expect(await call($, { tool: CODEX_STATUS })).toMatchObject({ result: { jobs: [] } })
})

test('file allowlist rejects traversal, credential files, ADS and unrelated artifacts', () => {
  const job = {
    id: ID, workspace: ROOT, runDir: RUN, reportPath: REPORT, resultPath: FINAL,
    changedFiles: ['src/checked.ts', '../outside.txt', '.env', '.env.local', '.aws/credentials', 'auth.json', 'private.pem', 'safe.ts:secret', 'https://example.test/x', 'C:/outside.ts'],
  } as CodexJob
  expect(previewFiles(job).map(file => file.path)).toEqual([REPORT, FINAL, `${ROOT}/src/checked.ts`])
  expect(sensitivePath('E:/p/.ssh/id_ed25519')).toBe(true)
  expect(sensitivePath('E:/p/tokenizer.ts')).toBe(false)
  expect(within('E:/workspace', 'E:/workspace-other/x')).toBe(false)
  expect(normalizeStatus('strange')).toBe('unknown')
  expect(normalizeStatus(undefined)).toBe('unknown')
})

test('a state file cannot swap the job identity or redirect result access outside its run', () => {
  const job = { id: ID, workspace: ROOT, runDir: RUN } as CodexJob
  expect(() => mergeState(job, { job_id: 'other' })).toThrow()
  expect(() => mergeState(job, { result_path: 'C:/Users/Test/.codex/auth.json' })).toThrow()
  expect(() => mergeState({ ...job, reportPath: REPORT, resultPath: FINAL }, { report_path: `${RUN}/stdout.jsonl` })).toThrow()
  expect(() => mergeState(job, { execution_workspace: { path: 'C:/Users/Test/Documents', scope_paths: ['.'] } })).toThrow()
  expect(() => absolutePath('E:/workspace/CON.txt')).toThrow()
  expect(() => absolutePath('E:/workspace/readme.')).toThrow()
})

test('manual is the default; model tools cannot activate it; off gates both providers without processes', async ($, on) => {
  const w = world(on)
  let nativeSpawns = 0
  on('agent.spawn', () => { nativeSpawns++; return { model: 'claude-test', agentId: 'ordinary' } })
  await $.session.start({ source: 'startup', cwd: ROOT, surface: 'desktop', isInteractive: true } as never)
  expect(await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'status' })).toMatchObject({ result: { mode: 'manual', allowed: false } })
  expect(await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'enable', user_requested: true })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked' } })
  await $.command.run({ command: 'savvy-mode', args: 'off' } as never)
  await $.command.run({ command: 'savvy-start', args: '' } as never)
  const args = { tool_use_id: 'blocked-native', prompt: 'task', description: 'blocked', subagentType: 'savvy-codex:savvy-medium', provider: 'engine', parentModel: 'claude-test', background: false, fork: false }
  expect(await $.agent.spawn(args as never)).toMatchObject({ deny: expect.stringContaining('关闭') })
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked' } })
  await $.agent.spawn({ ...args, subagentType: 'general-purpose' } as never)
  expect(nativeSpawns).toBe(1)
  expect(w.runs).toEqual([])
  expect(w.modelCalls()).toBe(0)
})

test('user clicks activate manual once; finish consumes it; explicit auto survives finish', async ($, on) => {
  const w = world(on)
  await $.session.start({ source: 'startup', cwd: ROOT, surface: 'desktop', isInteractive: true } as never)
  const pane = await mount($)
  await pane.press({ key: 'savvy-start' })
  expect(await call($, INPUT)).toMatchObject({ result: { job_id: ID } })
  await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'finish' })
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked' } })
  await pane.press({ key: 'savvy-mode-auto' })
  await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'finish' })
  expect(await call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'status' })).toMatchObject({ result: { mode: 'auto', allowed: true } })
  expect(w.runs).toHaveLength(1)
  expect(w.modelCalls()).toBe(0)
})

for (const surface of ['desktop', 'terminal'] as const) {
  test(`${surface}: native Claude row opens real task, tool activity and an isolated-worktree file, then returns`, async ($, on) => {
    const w = world(on)
    const isolated = 'E:/isolated-agent-worktree/src/result.ts'
    w.files[isolated] = 'export const result = 42\n'
    const task = 'KIND: implementation\nAUTHOR_PROVIDER: claude\nATTEMPT: 2\nSELECTION_REASON: This task needs careful boundary validation.\nFull task: inspect the parser, implement the authorized adjustment, and report actual checks.'
    on('agent.spawn', () => ({ model: 'claude-test-model', agentId: 'native-details' }))
    on('tool.call', (_$, event) => ({ result: event.tool === 'Write' ? 'File written' : 'ok' }))
    on('turn.complete', () => ({ text: '' }) as never)
    await start($)
    await $.agent.spawn({ tool_use_id: 'native-details', prompt: task, description: 'Native task', subagentType: 'savvy-codex:savvy-medium', provider: 'engine', parentModel: 'claude-test-model', background: false, fork: false } as never)
    await call($, { tool: 'Write', agentId: 'native-details', file_path: isolated, content: 'authorized contents' })
    await ($.turn.complete as (event: unknown) => Promise<unknown>)({ agentId: 'native-details', reason: 'answer', answer: 'Actual result: complete.' })
    const pane = await mount($, surface)
    await pane.press({ key: 'claude-row-native-details' })
    const drawn = JSON.stringify(await pane.drawn())
    expect(drawn).toContain('Full task: inspect the parser')
    expect(drawn).toContain('This task needs careful boundary validation.')
    expect(drawn).toContain('claude-test-model')
    expect(drawn).toContain('Actual result: complete.')
    expect(drawn).toContain('Write')
    expect(drawn).toContain('complete')
    await pane.press({ key: 'claude-file-native-details-0' })
    expect((await pane.findAll({ type: 'Code' }))[0]?.props).toMatchObject({ path: isolated, source: 'export const result = 42\n' })
    await pane.press({ key: 'claude-copy-preview' })
    expect(w.copied).toEqual([isolated])
    await pane.press({ key: 'claude-preview-back' })
    await pane.press({ key: 'claude-list-back' })
    expect(await pane.find({ key: 'claude-row-native-details' })).toBeDefined()
    expect(w.runs).toEqual([])
    expect(w.modelCalls()).toBe(0)
  })
}

test('native review metadata links a real completed reviewer and rejects write and shell tools', async ($, on) => {
  const w = world(on)
  let actualToolCalls = 0
  on('agent.spawn', () => ({ model: 'claude-review-model', agentId: 'reviewer' }))
  on('tool.call', () => { actualToolCalls++; return { result: 'ok' } })
  on('turn.complete', () => ({ text: '' }) as never)
  await start($)
  await $.agent.spawn({ tool_use_id: 'reviewer', prompt: `KIND: review\nREVIEW_OF: ${ID}\nAUTHOR_PROVIDER: codex\nSELECTION_REASON: Independent read-only check.`, description: 'Independent reviewer', subagentType: 'savvy-codex:savvy-careful', provider: 'engine', parentModel: 'claude-review-model', background: false, fork: false } as never)
  expect(await call($, { tool: 'Write', agentId: 'reviewer', file_path: `${ROOT}/src/checked.ts` })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, { tool: 'Bash', agentId: 'reviewer', command: 'echo mutation' })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, { tool: 'mcp__filesystem__write_file', agentId: 'reviewer', path: `${ROOT}/src/checked.ts` })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, { tool: 'mcp__document__execute_document_tool', agentId: 'reviewer', action: 'delete' })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, { tool: 'mcp__untrusted__Read', agentId: 'reviewer' })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, { tool: 'UnknownTool', agentId: 'reviewer' })).toMatchObject({ result: { status: 'blocked' } })
  expect(await call($, { tool: 'Agent', agentId: 'reviewer', subagent_type: 'general-purpose', prompt: 'nested' })).toMatchObject({ result: { status: 'blocked' } })
  await call($, { tool: 'Read', agentId: 'reviewer', file_path: `${ROOT}/src/checked.ts` })
  expect(actualToolCalls).toBe(1)
  await ($.turn.complete as (event: unknown) => Promise<unknown>)({ agentId: 'reviewer', reason: 'answer', answer: 'Reviewed actual code; no new issue.' })
  const pane = await mount($)
  await pane.press({ key: 'claude-row-reviewer' })
  expect(JSON.stringify(await pane.drawn())).toContain(ID)
  expect(JSON.stringify(await pane.drawn())).toContain('blocked: read-only review')
  expect(w.runs).toEqual([])
})

test('snapshot previews use execution scope and never substitute parent workspace files', () => {
  const snapshot = `${RUN}/workspace`
  const job = { id: ID, workspace: ROOT, runDir: RUN, reportPath: REPORT, resultPath: FINAL,
    executionWorkspace: snapshot, scopePaths: ['src'], patchPath: `${RUN}/changes.patch`, changesPath: `${RUN}/changes.json`,
    changedFiles: ['src/checked.ts', 'secrets/outside.ts', `${ROOT}/src/checked.ts`],
  } as CodexJob
  expect(previewFiles(job).map(file => file.path)).toEqual([REPORT, FINAL, `${RUN}/changes.patch`, `${RUN}/changes.json`, `${snapshot}/src/checked.ts`])
  expect(previewFiles({ ...job, scopePaths: ['../'] }).some(file => file.path === `${snapshot}/src/checked.ts`)).toBe(false)
  expect(previewFiles({ ...job, executionWorkspace: 'C:/Users/Test/Documents', scopePaths: ['.'] }).some(file => file.path.includes('Documents'))).toBe(false)
})

test('catalog and retention are explicit helper commands; defaults use cached catalog and a dry run', async ($, on) => {
  const w = world(on)
  await start($)
  expect(await call($, { tool: 'mcp__savvy-codex__codex_catalog' })).toMatchObject({ result: { status: 'ready' } })
  expect(await call($, { tool: 'mcp__savvy-codex__codex_catalog', refresh: true })).toMatchObject({ result: { status: 'ready' } })
  expect(await call($, { tool: 'mcp__savvy-codex__codex_cleanup', workspace: ROOT })).toMatchObject({ result: { dry_run: true } })
  expect(w.runs.map(run => run.argv.slice(2))).toEqual([
    ['catalog', '--cached'], ['catalog', '--refresh'], ['cleanup', '--workspace', ROOT, '--days', '30', '--dry-run'],
  ])
  expect(w.modelCalls()).toBe(0)
})

test('new effort names reach the catalog-verifying runner instead of a static UI enum', async ($, on) => {
  const w = world(on, { start: { ...queued(), effort: 'ultra' } })
  await start($)
  expect(await call($, { ...INPUT, effort: 'ultra' })).toMatchObject({ result: { effort: 'ultra', status: 'queued' } })
  expect(w.runs[0]?.argv).toContain('ultra')
  const pane = await mount($)
  await pane.press({ key: `codex-row-${ID}` })
  expect(JSON.stringify(await pane.drawn())).toContain('ultra')
})

test('Codex detail preserves the actual packet, cross-provider review metadata and snapshot file preview', async ($, on) => {
  const w = world(on, { packet: { goal: GOAL, reason: REASON, scope_paths: [`${ROOT}/src`], constraints: 'Keep the authorized ownership constraint.' } })
  await start($)
  await call($, INPUT)
  const snapshot = `${RUN}/workspace`
  w.files[`${snapshot}/src/checked.ts`] = 'export const snapshot = "actual candidate"\n'
  w.files[`${RUN}/review-source.patch`] = 'diff --git a/src/checked.ts b/src/checked.ts\n'
  w.files[STATE] = JSON.stringify({ ...completed('blocked'), kind: 'review', review_of: 'claude-native-author', author_provider: 'claude', attempt: 2,
    failure_kind: 'validation_failed', next_action: 'Return the observed failure to the lead.', retryable: false,
    review_source_job: 'source-codex-task',
    execution_workspace: { path: snapshot, source_workspace: `${HOME}/.claude/codex-worker-runs/abc123/prior/workspace`, scope_paths: [`${ROOT}/src`], scope_entries: [{ path: 'src', type: 'directory' }] },
    task_packet: { goal: 'A partial state excerpt must not replace the full captured packet.' },
    artifacts: { ...completed().artifacts, review_source_patch: `${RUN}/review-source.patch` },
  })
  expect(await call($, { tool: CODEX_STATUS, job_id: ID })).toMatchObject({ result: { kind: 'review', review_of: 'claude-native-author', author_provider: 'claude', attempt: 2, failure_kind: 'validation_failed' } })
  const pane = await mount($)
  await pane.press({ key: `codex-row-${ID}` })
  const text = JSON.stringify(await pane.drawn())
  expect(text).toContain('Keep the authorized ownership constraint.')
  expect(text).toContain('claude-native-author')
  expect(text).toContain('Return the observed failure to the lead.')
  expect(text).toContain('source-codex-task')
  await pane.press({ key: `codex-file-${ID}-3` })
  expect((await pane.findAll({ type: 'Code' }))[0]?.props).toMatchObject({ path: `${snapshot}/src/checked.ts`, source: 'export const snapshot = "actual candidate"\n' })
  expect(w.reads).not.toContain(`${ROOT}/src/checked.ts`)
})

test('absolute scope input is normalized and controller scope entries remain the snapshot allowlist', () => {
  expect(normalizeScopes(ROOT, [`${ROOT}/src`, 'src/checked.ts', 'C:/outside', '../escape', '.', '.env'])).toEqual(['src', 'src/checked.ts'])
  const seed = { id: ID, workspace: ROOT, runDir: RUN, reportPath: REPORT, resultPath: FINAL, scopePaths: ['wrong-legacy-seed'] } as CodexJob
  const merged = mergeState(seed, { execution_workspace: { path: `${RUN}/workspace`, scope_paths: [`${ROOT}/src`], scope_entries: [{ path: 'src/checked.ts', type: 'file' }] } })
  expect(merged.scopePaths).toEqual(['src/checked.ts'])
})

test('a failed cross-provider review launch retains author, parent and attempt metadata', async ($, on) => {
  const w = world(on, { packet: { goal: GOAL, reason: REASON, kind: 'review', author_provider: 'claude', review_of: 'claude-author-123', parent_job: 'prior-review-456', attempt: 3 }, startError: 'CLI unavailable' })
  await start($)
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked', kind: 'review', author_provider: 'claude', review_of: 'claude-author-123', parent_job: 'prior-review-456', attempt: 3 } })
  expect(w.runs).toHaveLength(1)
})

test('a main final answer ends idle manual activation; child completions and background work do not', async ($, on) => {
  const w = world(on)
  on('agent.spawn', () => ({ model: 'claude-test-model', agentId: 'background-native' }))
  on('turn.complete', () => ({ text: '' }) as never)
  const finish = (agentId?: string) => ($.turn.complete as (event: unknown) => Promise<unknown>)({ agentId, reason: 'answer', answer: 'Actual completion.' })
  const allowed = () => call($, { tool: 'mcp__savvy-codex__workflow_control', action: 'status' })
  await start($)
  await $.agent.spawn({ tool_use_id: 'background-native', prompt: 'Actual native task.', description: 'background native', subagentType: 'savvy-codex:savvy-medium', provider: 'engine', parentModel: 'claude-test-model', background: true, fork: false } as never)
  await finish()
  expect(await allowed()).toMatchObject({ result: { mode: 'manual', allowed: true } })
  await finish('background-native')
  expect(await allowed()).toMatchObject({ result: { allowed: true } })
  await call($, INPUT)
  for (const status of ['queued', 'preparing', 'running', 'unknown']) {
    w.files[STATE] = JSON.stringify({ ...queued(), status })
    await w.clock.advance(POLL_MS)
    await finish()
    expect(await allowed()).toMatchObject({ result: { allowed: true } })
  }
  w.files[STATE] = JSON.stringify(completed())
  await w.clock.advance(POLL_MS)
  await finish()
  expect(await allowed()).toMatchObject({ result: { mode: 'manual', allowed: false } })
  await $.command.run({ command: 'savvy-mode', args: 'auto' } as never)
  await finish()
  expect(await allowed()).toMatchObject({ result: { mode: 'auto', allowed: true } })
  expect(w.runs).toHaveLength(1)
  expect(w.modelCalls()).toBe(0)
})
