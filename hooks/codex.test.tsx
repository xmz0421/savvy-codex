import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { EngineInterface, On } from 'claude-code'
import type { CodexJob } from '../types'
import { absolutePath, CODEX_DELEGATE, CODEX_STATUS, mergeState, normalizeStatus, POLL_MS, previewFiles, sensitivePath, within, workerCommand } from './codex'

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
  on('ui.toast', () => ok(undefined))
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
    throw new Error('Unexpected helper command')
  })
  return { clock, files, runs, opened, copied, reads, registered, links, sizes, modelCalls: () => modelCalls }
}

const start = ($: Engine) => $.session.start({ source: 'startup', cwd: ROOT, surface: 'desktop', isInteractive: true } as never)
const call = ($: Engine, value: object) => $.tool.call(value as never)
const mount = ($: Engine, surface: 'desktop' | 'terminal' = 'desktop') => $.ui.mount({ plugin: 'savvy-codex', surface, component: 'Pane', requestId: 'savvy-agents', props: PANE })

const environment = (env: Record<string, string>) => ({ env: { get: async (key: string) => env[key] } }) as unknown as EngineInterface

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

test('a helper start failure is visible and never adds a fake worker row', async ($, on) => {
  const w = world(on, { startError: 'process launch denied' })
  await start($)
  expect(await call($, INPUT)).toMatchObject({ result: { status: 'blocked', summary: expect.stringContaining('process launch denied') } })
  expect(await call($, { tool: CODEX_STATUS })).toMatchObject({ result: { jobs: [] } })
  expect(w.opened).toEqual([])
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
  expect(await call($, { ...INPUT, effort: 'ultra' })).toMatchObject({ result: { status: 'blocked' } })
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
  expect(() => absolutePath('E:/workspace/CON.txt')).toThrow()
  expect(() => absolutePath('E:/workspace/readme.')).toThrow()
})
