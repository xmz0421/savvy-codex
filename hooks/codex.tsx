import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderElement, RenderSurface } from 'claude-code'
import type { CodexJob, CodexPanel, CodexResult, CodexStatus, Effort } from '../types'
import { workflowAllowed, workflowRefusal } from './workflow'

// These are external official Codex CLI workers. They never impersonate a Claude
// AgentRun, and nothing in this module calls a Claude/model completion API.
export const CODEX_DELEGATE = 'mcp__savvy-codex__codex_delegate'
export const CODEX_STATUS = 'mcp__savvy-codex__codex_status'
export const CODEX_CATALOG = 'mcp__savvy-codex__codex_catalog'
export const CODEX_CLEANUP = 'mcp__savvy-codex__codex_cleanup'
export const AGENTS_PANE = 'savvy-agents'
export const POLL_MS = 1500
const MAX_JOBS = 100
const MAX_JSON = 256 * 1024
const MAX_PACKET = 64 * 1024
const MAX_PREVIEW = 256 * 1024
const MAX_PREVIEW_CHARS = 24_000
const ACTIVE: CodexStatus[] = ['queued', 'preparing', 'running', 'unknown']
const TERMINAL: CodexStatus[] = ['complete', 'blocked', 'failed', 'cancelled', 'timeout']

const jobsAtom = atom({ plugin: 'savvy-codex', key: 'codexJobs' } as const, [])
const panelAtom = atom({ plugin: 'savvy-codex', key: 'codexPanel' } as const, {
  now: 0,
  selectedId: '',
  previewPath: '',
  previewText: '',
  previewNote: '',
})

type Dict = Record<string, unknown>
type WorkerOptions = { pythonPath?: unknown; pluginRoot?: unknown; helperPath?: unknown; retentionDays?: unknown }
let workerOptions: WorkerOptions = {}
type DelegateInput = {
  workspace?: unknown
  packet_file?: unknown
  model?: unknown
  effort?: unknown
  permission?: unknown
  approval?: unknown
  timeout?: unknown
  background?: unknown
}
type StatusInput = { job_id?: unknown; run_dir?: unknown; wait?: unknown; timeout?: unknown }

export const cleanText = (value: unknown, limit = 1200): string =>
  typeof value === 'string' ? value.replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '').slice(0, limit) : ''
const dict = (value: unknown): Dict => value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}
const string = (value: unknown): string => typeof value === 'string' ? value.trim() : ''
const strings = (value: unknown, limit = 200): string[] =>
  Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string').slice(0, limit).map(v => cleanText(v, 2000)) : []
const samePath = (left: string, right: string): boolean => pathKey(left) === pathKey(right)
const pathKey = (path: string): string => /^[A-Za-z]:\//.test(path) ? path.toLowerCase() : path

// Do not depend on a shell or on host node:fs/path imports in a Mod sandbox.
// Reject Windows aliases (ADS, trailing dot/space, device names), UNC and URLs.
export function absolutePath(value: unknown): string {
  const source = string(value).replace(/\\/g, '/')
  if (!source || /[\u0000-\u001f\u007f<>"|?*]/.test(source) || source.startsWith('//')) throw new Error('路径必须是本机绝对路径。')
  const drive = /^[A-Za-z]:\//.test(source) ? source.slice(0, 2).toUpperCase() : ''
  if (!drive && !source.startsWith('/')) throw new Error('路径必须是本机绝对路径。')
  const tail = drive ? source.slice(3) : source.slice(1)
  if (tail.includes(':')) throw new Error('路径包含不允许的冒号。')
  const parts: string[] = []
  for (const part of tail.split('/')) {
    if (!part || part === '.') continue
    if (part === '..') {
      if (!parts.length) throw new Error('路径超出根目录。')
      parts.pop()
    } else {
      if (/[. ]$/.test(part) || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(part)) throw new Error('路径包含 Windows 别名。')
      parts.push(part)
    }
  }
  return (drive ? `${drive}/` : '/') + parts.join('/')
}

export function within(root: string, path: string): boolean {
  const base = pathKey(root).replace(/\/$/, '')
  const candidate = pathKey(path)
  return candidate === base || candidate.startsWith(`${base}/`)
}

export function normalizeScopes(workspace: string, value: unknown): string[] {
  const root = absolutePath(workspace).replace(/\/$/, '')
  return strings(value).flatMap(scope => {
    try {
      const path = absolutePath(/^(?:[A-Za-z]:[\\/]|\/)/.test(scope) ? scope : `${root}/${scope}`)
      return within(root, path) && !samePath(root, path) && !sensitivePath(path) ? [path.slice(root.length + 1)] : []
    } catch { return [] }
  }).filter((scope, index, all) => all.indexOf(scope) === index)
}

export function sensitivePath(path: string): boolean {
  return path.replace(/\\/g, '/').split('/').some(part =>
    /^(?:\.env(?:\..*)?|\.ssh|\.aws|\.azure|\.gnupg|\.kube|\.git|\.npmrc|\.netrc|\.pypirc|auth\.json|credentials(?:\..*)?|secrets?(?:\..*)?|tokens?(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?)$/i.test(part) ||
    /\.(?:pem|key|p12|pfx|keystore)$/i.test(part),
  )
}

function ancestors(path: string): string[] {
  const drive = /^[A-Za-z]:\//.test(path) ? path.slice(0, 3) : '/'
  const tail = path.slice(drive.length).split('/').filter(Boolean)
  const out = [drive]
  for (const part of tail) out.push(`${out[out.length - 1]!.replace(/\/$/, '')}/${part}`)
  return out
}

export async function readSafe($: EngineInterface, path: string, root: string, maxBytes = MAX_PREVIEW): Promise<string> {
  if (!within(root, path) || sensitivePath(path)) throw new Error('该路径不在允许查看的任务文件中，或属于凭证文件。')
  let size = 0
  for (const [index, part] of ancestors(path).entries()) {
    const info = await $.fs.stat(part)
    if (info.isLink) throw new Error('拒绝通过符号链接或目录联接读取文件。')
    const last = index === ancestors(path).length - 1
    if (last) {
      if (info.kind !== 'file') throw new Error('该路径不是普通文件。')
      size = info.size
      if (!Number.isFinite(size) || size > maxBytes) throw new Error(`文件超过 ${Math.round(maxBytes / 1024)} KiB 预览上限。`)
    } else if (info.kind !== 'dir') throw new Error('路径的父级不是目录。')
  }
  const text = String(await $.fs.read(path))
  if (text.length > maxBytes || text.includes('\0') || text.includes('\uFFFD')) throw new Error('文件不是可安全显示的 UTF-8 文本。')
  // Guard growth between stat and read as well as broken byte decoders.
  if (new TextEncoder().encode(text).length > maxBytes) throw new Error('读取时文件超过预览上限。')
  return text.replace(/^\uFEFF/, '')
}

async function readJson($: EngineInterface, path: string, root: string, maxBytes = MAX_JSON): Promise<Dict> {
  const parsed: unknown = JSON.parse(await readSafe($, path, root, maxBytes))
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('JSON 必须是对象。')
  return parsed as Dict
}

async function home($: EngineInterface): Promise<string> {
  return absolutePath((await $.env.get('USERPROFILE')) || (await $.env.get('HOME')) || '')
}
// Register's options are the host's userConfig values. Keep argv elements
// separate: paths with spaces are executable paths, never shell fragments.
export async function workerCommand($: EngineInterface, options: WorkerOptions = workerOptions): Promise<string[]> {
  const python = string(options.pythonPath) || string(await $.env.get('SAVVY_CODEX_PYTHON')) ||
    ((await $.env.get('OS')) === 'Windows_NT' ? 'python' : 'python3')
  if (/[\u0000-\u001f\u007f]/.test(python)) throw new Error('pythonPath 包含无效字符。')
  const explicitHelper = string(options.helperPath) || string(await $.env.get('SAVVY_CODEX_HELPER'))
  if (explicitHelper) return [python, absolutePath(explicitHelper)]
  const configuredRoot = string(options.pluginRoot) || string(await $.env.get('SAVVY_CODEX_PLUGIN_ROOT')) ||
    string(await $.env.get('CLAUDE_PLUGIN_ROOT'))
  const root = configuredRoot ? absolutePath(configuredRoot) : `${await home($)}/.claude/skills/savvy-codex`
  return [python, `${root.replace(/\/$/, '')}/skills/codex-worker/scripts/codex_delegate.py`]
}
async function runRoot($: EngineInterface): Promise<string> {
  return `${await home($)}/.claude/codex-worker-runs`
}

export function normalizeStatus(value: unknown): CodexStatus {
  const raw = string(value).toLowerCase()
  if (['complete', 'completed', 'done', 'success', 'succeeded'].includes(raw)) return 'complete'
  if (raw === 'canceled') return 'cancelled'
  if (raw === 'timed_out') return 'timeout'
  return [...ACTIVE, ...TERMINAL].includes(raw as CodexStatus) ? raw as CodexStatus : 'unknown'
}
export const isActive = (job: CodexJob): boolean => ACTIVE.includes(job.status)

function resultOf(value: unknown): CodexResult | undefined {
  const raw = dict(value)
  if (!Object.keys(raw).length) return undefined
  return {
    status: cleanText(raw.status, 40),
    summary: cleanText(raw.summary, 12_000),
    changed_files: strings(raw.changed_files),
    checks: strings(raw.checks, 50),
    findings: strings(raw.findings, 50),
    limitations: strings(raw.limitations, 50),
  }
}

export function mergeState(previous: CodexJob, raw: Dict): CodexJob {
  const id = string(raw.job_id) || string(raw.id)
  if (id && id !== previous.id) throw new Error('状态文件的 job_id 与当前任务不一致。')
  if (raw.workspace && !samePath(absolutePath(raw.workspace), previous.workspace)) throw new Error('状态文件的 workspace 与当前任务不一致。')
  if (raw.run_dir && !samePath(absolutePath(raw.run_dir), previous.runDir)) throw new Error('状态文件的 run_dir 与当前任务不一致。')
  const artifacts = dict(raw.artifacts)
  const result = resultOf(raw.result) ?? previous.result
  const actualStatus = string(raw.status)
  const resultPath = string(raw.result_path) || string(artifacts.final)
  const reportPath = string(raw.report_path) || string(artifacts.report)
  const checkedPath = (value: string, fallback: string): string => {
    if (!value) return fallback
    const path = absolutePath(value)
    if (!within(previous.runDir, path) || !samePath(path, fallback) || sensitivePath(path)) throw new Error('状态文件把结果指向了约定的 report.md/final.json 之外。')
    return path
  }
  const progress = dict(raw.progress)
  const tool = typeof raw.last_tool === 'string' ? raw.last_tool : dict(raw.last_tool).name
  const usage = dict(raw.usage)
  const tokenCount = typeof raw.tokens === 'number' ? raw.tokens : usage.total_tokens
  const execution = dict(raw.execution_workspace)
  const packet = dict(raw.task_packet)
  const executionPath = execution.path ? absolutePath(execution.path) : previous.executionWorkspace
  if (executionPath && !samePath(executionPath, `${previous.runDir}/workspace`)) throw new Error('执行快照路径不在当前任务的 workspace 目录。')
  const stateScopes = Array.isArray(execution.scope_entries) ? execution.scope_entries.map(entry => dict(entry).path) : execution.scope_paths ?? raw.scope_paths
  const artifactPath = (value: unknown, filename: string): string => {
    if (!value) return ''
    const candidate = absolutePath(value)
    if (!samePath(candidate, `${previous.runDir}/${filename}`)) throw new Error('任务产物路径超出约定文件。')
    return candidate
  }
  return {
    ...previous,
    model: cleanText(raw.model || raw.selected_model || previous.model, 160),
    effort: cleanText(raw.effort || raw.selected_effort || previous.effort, 40),
    goal: cleanText(raw.goal || raw.task || previous.goal, MAX_PACKET),
    reason: cleanText(raw.reason || packet.reason || previous.reason, MAX_PACKET),
    taskPrompt: previous.taskPrompt || (Object.keys(packet).length ? cleanText(JSON.stringify(Object.fromEntries(['goal', 'reason', 'context', 'acceptance', 'tests', 'scope_paths', 'kind'].filter(key => packet[key] !== undefined).map(key => [key, packet[key]])), null, 2), MAX_PACKET) : undefined),
    executionWorkspace: executionPath,
    scopePaths: stateScopes === undefined ? previous.scopePaths ?? [] : normalizeScopes(previous.workspace, stateScopes),
    patchPath: artifactPath(artifacts.patch_path ?? artifacts.patch, 'changes.patch'),
    changesPath: artifactPath(artifacts.changes_path ?? artifacts.changes, 'changes.json'),
    reviewSourcePatchPath: artifactPath(artifacts.review_source_patch, 'review-source.patch'),
    reviewSourceJob: cleanText(raw.review_source_job || previous.reviewSourceJob, 160),
    isolationNotice: cleanText(execution.notice || dict(raw.isolation).notice || previous.isolationNotice, 2000),
    kind: cleanText(raw.kind || previous.kind, 80),
    reviewOf: cleanText(raw.review_of || previous.reviewOf, 160),
    parentJob: cleanText(raw.parent_job || previous.parentJob, 160),
    authorProvider: cleanText(raw.author_provider || previous.authorProvider, 80),
    attempt: typeof raw.attempt === 'number' && Number.isInteger(raw.attempt) ? raw.attempt : previous.attempt,
    failureKind: cleanText(raw.failure_kind || previous.failureKind, 300),
    nextAction: cleanText(raw.next_action || previous.nextAction, 2000),
    retryable: typeof raw.retryable === 'boolean' ? raw.retryable : previous.retryable,
    status: actualStatus ? normalizeStatus(actualStatus) : previous.status,
    rawStatus: actualStatus || previous.rawStatus,
    startedAt: cleanText(raw.started_at || previous.startedAt, 100),
    finishedAt: cleanText(raw.finished_at || previous.finishedAt, 100),
    updatedAt: cleanText(raw.updated_at || previous.updatedAt, 100),
    summary: cleanText(raw.summary || result?.summary || previous.summary, 12_000),
    lastTool: cleanText(tool || previous.lastTool, 180),
    lastMessage: cleanText(raw.last_message || progress.note || previous.lastMessage, 1800),
    changedFiles: strings(raw.changed_files ?? result?.changed_files ?? previous.changedFiles),
    reportPath: checkedPath(reportPath, previous.reportPath),
    resultPath: checkedPath(resultPath, previous.resultPath),
    tokens: typeof tokenCount === 'number' && Number.isFinite(tokenCount) && tokenCount >= 0 ? tokenCount : previous.tokens,
    authenticationVerified: raw.authentication === undefined ? previous.authenticationVerified : dict(raw.authentication).verified === true,
    result,
    readError: '',
  }
}

export async function codexJobs($: EngineInterface): Promise<CodexJob[]> { return read($, jobsAtom) }
export async function codexPreviewIsOpen($: EngineInterface): Promise<boolean> {
  return Boolean((await read($, panelAtom)).selectedId)
}
export async function beginCodexFlow($: EngineInterface): Promise<void> {
  const active = await update($, jobsAtom, list => list.filter(isActive))
  await update($, panelAtom, panel => active.some(job => job.id === panel.selectedId) ? panel : { ...panel, selectedId: '', previewPath: '', previewText: '', previewNote: '' })
}

async function saveJob($: EngineInterface, job: CodexJob): Promise<void> {
  await update($, jobsAtom, list => {
    const existing = list.find(value => value.id === job.id)
    if (existing && (JSON.stringify(existing) === JSON.stringify(job) || (TERMINAL.includes(existing.status) && isActive(job)))) return list
    const merged = [...list.filter(value => value.id !== job.id), job].sort((a, b) => a.startedAt.localeCompare(b.startedAt))
    const active = merged.filter(isActive)
    const finished = merged.filter(value => !isActive(value))
    const keepFinished = Math.max(0, MAX_JOBS - active.length)
    return [...(keepFinished ? finished.slice(-keepFinished) : []), ...active].sort((a, b) => a.startedAt.localeCompare(b.startedAt))
  })
}

async function refreshJob($: EngineInterface, job: CodexJob): Promise<CodexJob> {
  if (!job.statePath) return job // Failed launch entries have no executor directory.
  let next: CodexJob
  try {
    const raw = await readJson($, job.statePath, job.runDir)
    next = mergeState(job, raw)
  } catch (error) {
    // Missing/partial/locked state is never evidence that a job has succeeded.
    next = { ...job, readError: `等待状态文件：${cleanText(error instanceof Error ? error.message : String(error), 300)}` }
  }
  await saveJob($, next)
  return next
}

export async function refreshCodex($: EngineInterface, all = false): Promise<void> {
  const list = await codexJobs($)
  await Promise.all(list.filter(job => all || isActive(job)).map(job => refreshJob($, job)))
  if (list.some(isActive)) {
    const at = await $.clock.now()
    await update($, panelAtom, panel => ({ ...panel, now: at }))
  }
}

async function openAgents($: EngineInterface): Promise<void> {
  await $.ui.open({ id: AGENTS_PANE, title: 'Agents' })
}

function toolSummary(job: CodexJob, extra: Dict = {}): Dict {
  return {
    provider: 'external Codex worker',
    job_id: job.id,
    status: job.status,
    raw_status: job.rawStatus,
    model: job.model,
    effort: job.effort,
    task: job.goal,
    reason: job.reason,
    workspace: job.workspace,
    run_dir: job.runDir,
    state_path: job.statePath,
    report_path: job.reportPath,
    result_path: job.resultPath,
    summary: job.summary,
    changed_files: job.changedFiles,
    kind: job.kind || '', review_of: job.reviewOf || '', parent_job: job.parentJob || '', author_provider: job.authorProvider || '',
    attempt: job.attempt, failure_kind: job.failureKind || '', next_action: job.nextAction || '', retryable: job.retryable,
    execution_workspace: job.executionWorkspace || '', scope_paths: job.scopePaths || [],
    ...(job.result ? { result: job.result } : {}),
    ...(job.readError ? { state_note: job.readError } : {}),
    ...extra,
  }
}

const timeoutOf = (value: unknown): number => {
  if (value === undefined) return 900
  if (!Number.isInteger(value) || Number(value) < 1 || Number(value) > 3600) throw new Error('timeout 必须是 1–3600 秒的整数。')
  return Number(value)
}
function parseProcess(result: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated?: boolean }): Dict {
  if (result.isStdoutTruncated) throw new Error('执行器返回的 JSON 被截断。')
  let parsed: Dict
  try {
    const raw: unknown = JSON.parse(result.stdout.trim())
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('not an object')
    parsed = raw as Dict
  } catch {
    throw new Error(`执行器未返回有效 JSON（exit ${result.exitCode}）。${cleanText(result.stderr, 600)}`)
  }
  // blocked/failed is a real worker result and must reach the caller unchanged.
  if (result.exitCode !== 0 && !parsed.status) throw new Error(`执行器失败（exit ${result.exitCode}）。${cleanText(result.stderr, 600)}`)
  return parsed
}

async function awaitJob($: EngineInterface, job: CodexJob, seconds: number): Promise<Dict> {
  if (TERMINAL.includes(job.status)) return toolSummary(job)
  // The Mod host caps process.run at 600,000 ms. Short chunks also leave room
  // for UI updates and cancellation, with zero additional model requests.
  let remaining = seconds
  let current = job
  while (remaining > 0) {
    const chunk = Math.min(45, remaining)
    const result = await $.process.run([...await workerCommand($), 'wait', '--run-dir', job.runDir, '--timeout', String(chunk)], {
      cwd: job.workspace,
      timeoutMs: (chunk + 10) * 1000,
    })
    const raw = parseProcess(result)
    const merged = mergeState(current, raw)
    await saveJob($, merged)
    // Re-read authoritative state to catch the worker's last atomic write.
    current = await refreshJob($, merged)
    if (TERMINAL.includes(current.status)) return toolSummary(current)
    if (raw.wait_timed_out !== true) return toolSummary(current, { wait_note: '执行器返回了在途状态，尚无终态。' })
    remaining -= chunk
  }
  return toolSummary(current, { wait_timed_out: true })
}

async function diagnoseJob($: EngineInterface, job: CodexJob): Promise<Dict> {
  // Only an explicit status request performs the helper's PID/liveness checks.
  // The UI timer itself reads files and never spawns repeated processes.
  try {
    if (!job.runDir) return toolSummary(job)
    const result = await $.process.run([...await workerCommand($), 'status', '--run-dir', job.runDir], { cwd: job.workspace, timeoutMs: 15_000 })
    const raw = parseProcess(result)
    if (!string(raw.job_id) && !string(raw.id)) throw new Error(cleanText(raw.summary, 600) || '执行器没有返回此任务的状态。')
    const merged = mergeState(job, raw)
    await saveJob($, merged)
    return toolSummary(merged)
  } catch (error) {
    return toolSummary(job, { status_error: cleanText(error instanceof Error ? error.message : String(error), 1000) })
  }
}

async function recursionBlocked($: EngineInterface, agentId: unknown): Promise<string> {
  if (agentId) return 'Codex 委派只允许主会话调用，不能由 Claude 子代理再次包装。'
  if ((await $.env.get('CLAUDE_CODEX_WORKER')) === '1' || (await $.env.get('MANIFOLD_COLLAB_WORKER')) === '1') return '当前会话已经是 worker，禁止递归委派。'
  return ''
}

async function delegate($: EngineInterface, input: DelegateInput): Promise<Dict> {
  const workspace = absolutePath(input.workspace)
  const packetPath = absolutePath(input.packet_file)
  const model = string(input.model)
  const effort = string(input.effort) as Effort
  const permission = input.permission === undefined ? 'read-only' : string(input.permission)
  const approval = input.approval === undefined ? 'never' : string(input.approval)
  const timeout = timeoutOf(input.timeout)
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(model)) throw new Error('必须明确提供完整的 Codex model id。')
  if (!/^[a-z][a-z0-9_-]{0,39}$/.test(effort)) throw new Error('必须明确提供合法 effort；可用值由本机 Codex catalog 核验。')
  if (!['read-only', 'workspace-write'].includes(permission) || !['never', 'auto-review'].includes(approval)) throw new Error('permission 或 approval 无效。')
  if ((permission === 'read-only' && approval !== 'never') || (permission === 'workspace-write' && approval !== 'auto-review')) throw new Error('只读任务使用 never；可写任务使用 auto-review。')
  if (input.background !== undefined && typeof input.background !== 'boolean') throw new Error('background 必须是布尔值。')
  const workspaceInfo = await $.fs.stat(workspace)
  if (workspaceInfo.kind !== 'dir' || workspaceInfo.isLink) throw new Error('workspace 必须是现有的本机普通目录。')
  // The user supplied this input packet explicitly; reading it is not a preview
  // capability. It is still bounded, UTF-8 only, and refuses credential paths.
  const packet = await readJson($, packetPath, packetPath, MAX_PACKET)
  const reason = string(packet.reason)
  const goal = string(packet.goal)
  if (!reason || !goal) throw new Error('任务包必须含非空 goal 与 reason，说明任务和模型/effort 的选择依据。')
  const failedLaunch = async (raw: Dict): Promise<Dict> => {
    const at = new Date(await $.clock.now()).toISOString()
    const failure: CodexJob = {
      id: `launch-${Date.parse(at)}-${(await codexJobs($)).length}`, provider: 'external-codex', workspace,
      runDir: '', statePath: '', reportPath: '', resultPath: '', model, effort,
      goal, reason, taskPrompt: cleanText(JSON.stringify(packet, null, 2), MAX_PACKET),
      status: 'blocked', rawStatus: string(raw.status) || 'blocked', startedAt: at, finishedAt: at, updatedAt: at,
      summary: cleanText(raw.summary, 12_000) || '执行器未能启动 worker。', lastTool: '', lastMessage: '', changedFiles: [], readError: '', authenticationVerified: false,
      kind: cleanText(packet.kind, 80) || 'launch', reviewOf: cleanText(packet.review_of, 160), parentJob: cleanText(packet.parent_job, 160),
      authorProvider: ['claude', 'codex'].includes(string(packet.author_provider)) ? string(packet.author_provider) : packet.kind === 'review' ? '' : 'codex',
      attempt: typeof packet.attempt === 'number' && Number.isInteger(packet.attempt) && packet.attempt > 0 ? packet.attempt : 1,
      failureKind: cleanText(raw.failure_kind, 300) || 'launch_failed', nextAction: cleanText(raw.next_action, 2000), retryable: raw.retryable === true,
    }
    await saveJob($, failure)
    await openAgents($)
    return toolSummary(failure)
  }
  let started: Dict
  try {
    started = parseProcess(await $.process.run([
      ...await workerCommand($), 'start', '--workspace', workspace, '--packet', packetPath,
      '--model', model, '--effort', effort, '--permission', permission, '--approval', approval, '--timeout', String(timeout),
    ], { cwd: workspace, timeoutMs: 30_000 }))
  } catch (error) { return failedLaunch({ summary: cleanText(error instanceof Error ? error.message : String(error), 2000) }) }
  const id = string(started.job_id) || string(started.id)
  if (!id || !/^[A-Za-z0-9_-]{1,120}$/.test(id)) {
    return failedLaunch({ ...started, summary: started.summary || '执行器没有返回有效 job_id。' })
  }
  const runDir = absolutePath(started.run_dir)
  if (!within(await runRoot($), runDir) || runDir.split('/').pop() !== id) throw new Error('执行器返回的任务目录不在 codex-worker-runs 内。')
  const statePath = absolutePath(started.state_path || dict(started.artifacts).state || `${runDir}/state.json`)
  if (!within(runDir, statePath)) throw new Error('状态文件路径超出任务目录。')
  const seed: CodexJob = {
    id, provider: 'external-codex', workspace, runDir, statePath,
    reportPath: `${runDir}/report.md`, resultPath: `${runDir}/final.json`,
    goal: cleanText(goal, MAX_PACKET), reason: cleanText(reason, MAX_PACKET), taskPrompt: cleanText(JSON.stringify(packet, null, 2), MAX_PACKET), scopePaths: normalizeScopes(workspace, packet.scope_paths), model, effort,
    status: 'queued', rawStatus: 'queued',
    startedAt: new Date(await $.clock.now()).toISOString(), finishedAt: '', updatedAt: '',
    summary: '', lastTool: '', lastMessage: '', changedFiles: [], readError: '', authenticationVerified: false,
  }
  const job = mergeState(seed, started)
  await saveJob($, job)
  await openAgents($)
  if (input.background === true) return toolSummary(job)
  try { return await awaitJob($, job, timeout) }
  catch (error) {
    const current = await refreshJob($, job)
    return toolSummary(current, { wait_error: cleanText(error instanceof Error ? error.message : String(error), 1000) })
  }
}

export async function startCodex($: EngineInterface): Promise<void> {
  let refreshing = false
  await $.tool.register({
    name: 'codex_delegate', isDeferred: false,
    description: '主会话把一个已授权的有限任务交给外部官方 Codex CLI worker，沿用 ChatGPT 登录。必须先写 JSON 任务包（goal、reason 和授权范围），明确完整 model id 与 effort。不得用 Claude Agent 工具包装。默认等待真实结果后返回；background=true 可继续独立工作，之后 codex_status(wait=true) 一次收回。与 Claude 子代理显示在同一 Agents 侧栏。不生成订阅费用估算。',
    inputSchema: {
      type: 'object', additionalProperties: false,
      properties: {
        workspace: { type: 'string', description: '已授权工作区的本机绝对路径。' },
        packet_file: { type: 'string', description: '已写好的 JSON 任务包绝对路径；含 goal、reason 等 runner 要求字段。' },
        model: { type: 'string', description: '明确指定的完整 Codex model id。' },
        effort: { type: 'string', description: '从 codex_catalog 选择当前模型支持的 effort；执行器按真实 catalog 核验。' },
        permission: { type: 'string', enum: ['read-only', 'workspace-write'], default: 'read-only' },
        approval: { type: 'string', enum: ['never', 'auto-review'], default: 'never' },
        timeout: { type: 'integer', minimum: 1, maximum: 3600, default: 900 },
        background: { type: 'boolean', default: false, description: 'true 立即返回任务编号；false 在一个工具调用内等候结果，不轮询模型。' },
      },
      required: ['workspace', 'packet_file', 'model', 'effort'],
    },
  })
  await $.tool.register({ name: 'codex_catalog', isDeferred: false,
    description: 'Read the official local Codex model catalog. refresh=true updates the CLI cache without a model completion; otherwise use the cache. Report availability honestly.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { refresh: { type: 'boolean', default: false } } } })
  await $.tool.register({ name: 'codex_cleanup', isDeferred: false,
    description: 'Clean expired terminal worker artifacts for an explicitly supplied workspace. Default is a dry run; never removes active tasks or auth/config files. This does not call a model.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { workspace: { type: 'string' }, days: { type: 'integer', minimum: 1, maximum: 3650, default: 30 }, dry_run: { type: 'boolean', default: true } }, required: ['workspace'] } })
  await $.tool.register({
    name: 'codex_status', isDeferred: false,
    description: '读取本会话已跟踪 Codex worker 的真实状态与结果，不重交任务。wait=true 在本次工具调用内等到终态或超时；无需频繁向模型发状态查询。只能查询已跟踪 job_id/run_dir。',
    inputSchema: {
      type: 'object', additionalProperties: false,
      properties: {
        job_id: { type: 'string' }, run_dir: { type: 'string' },
        wait: { type: 'boolean', default: false },
        timeout: { type: 'integer', minimum: 1, maximum: 3600, default: 900 },
      },
    },
  })
  // This alias always focuses the existing Savvy pane. It never creates a tab.
  await $.command.register({ name: 'codex-agents', description: '在同一 Agents 侧栏中查看 Codex worker；open/refresh 均刷新真实状态。' })
  $.clock.every(POLL_MS, () => {
    if (refreshing) return
    refreshing = true
    void refreshCodex($).catch(() => undefined).finally(() => { refreshing = false })
  })
}

export const registerCodex: Register = (on, options) => {
  workerOptions = options
  on('command.run', { command: 'codex-agents' }, async ($) => {
    await refreshCodex($, true)
    await openAgents($)
    return { text: '已打开同一 Agents 侧栏并刷新 Codex 状态。' }
  })
  on('tool.call', { tool: CODEX_DELEGATE }, async ($, e) => {
    const refusal = await recursionBlocked($, e.agentId)
    if (refusal) return { result: { provider: 'external Codex worker', status: 'blocked', summary: refusal } }
    if (!await workflowAllowed($)) return { result: { provider: 'external Codex worker', status: 'blocked', summary: await workflowRefusal($) } }
    try { return { result: await delegate($, e as unknown as DelegateInput) } }
    catch (error) { return { result: { provider: 'external Codex worker', status: 'blocked', summary: cleanText(error instanceof Error ? error.message : String(error), 1200) } } }
  })
  on('tool.call', { tool: CODEX_CATALOG }, async ($, event) => {
    if (event.agentId) return { result: { status: 'blocked', summary: 'Only the main session may inspect the catalog.' } }
    try {
      const refresh = (event as unknown as { refresh?: unknown }).refresh === true
      return { result: parseProcess(await $.process.run([...await workerCommand($), 'catalog', refresh ? '--refresh' : '--cached'], { timeoutMs: 30_000 })) }
    } catch (error) { return { result: { status: 'unavailable', summary: cleanText(String(error), 1200) } } }
  })
  on('tool.call', { tool: CODEX_CLEANUP }, async ($, event) => {
    if (event.agentId) return { result: { status: 'blocked', summary: 'Only the main session may clean worker files.' } }
    try {
      const input = event as unknown as { workspace: unknown; days?: unknown; dry_run?: unknown }
      const workspace = absolutePath(input.workspace)
      const configured = Number(workerOptions.retentionDays)
      const days = input.days ?? (Number.isInteger(configured) && configured >= 1 && configured <= 3650 ? configured : 30)
      if (!Number.isInteger(days) || Number(days) < 1 || Number(days) > 3650 || (input.dry_run !== undefined && typeof input.dry_run !== 'boolean')) throw new Error('Invalid retention arguments.')
      return { result: parseProcess(await $.process.run([...await workerCommand($), 'cleanup', '--workspace', workspace, '--days', String(days), ...(input.dry_run === false ? [] : ['--dry-run'])], { cwd: workspace, timeoutMs: 30_000 })) }
    } catch (error) { return { result: { status: 'blocked', summary: cleanText(String(error), 1200) } } }
  })
  on('tool.call', { tool: CODEX_STATUS }, async ($, e) => {
    try {
      const input = e as unknown as StatusInput
      const list = await codexJobs($)
      const id = string(input.job_id)
      const runDir = input.run_dir ? absolutePath(input.run_dir) : ''
      if (!id && !runDir) {
        if (input.wait) throw new Error('等待结果时必须指定已跟踪的 job_id 或 run_dir。')
        await refreshCodex($, true)
        return { result: { jobs: (await codexJobs($)).map(job => toolSummary(job)) } }
      }
      const job = list.find(value => (!id || value.id === id) && (!runDir || samePath(value.runDir, runDir)))
      if (!job) throw new Error('该任务未由本会话跟踪；不能读取任意任务目录。')
      const refreshed = await refreshJob($, job)
      return { result: input.wait === true ? await awaitJob($, refreshed, timeoutOf(input.timeout)) : await diagnoseJob($, refreshed) }
    } catch (error) {
      return { result: { status: 'blocked', summary: cleanText(error instanceof Error ? error.message : String(error), 1200) } }
    }
  })
}

export function previewFiles(job: CodexJob): { path: string; label: string; root: string }[] {
  const files: { path: string; label: string; root: string }[] = []
  for (const [label, candidate, expected] of [['报告', job.reportPath, `${job.runDir}/report.md`], ['结果 JSON', job.resultPath, `${job.runDir}/final.json`]]) {
    try {
      const path = absolutePath(candidate)
      if (within(job.runDir, path) && samePath(path, expected) && !sensitivePath(path)) files.push({ path, label, root: job.runDir })
    } catch { /* A malformed artifact grants no access. */ }
  }
  for (const [label, candidate, name] of [['补丁', job.patchPath, 'changes.patch'], ['变更清单', job.changesPath, 'changes.json'], ['复核证据补丁', job.reviewSourcePatchPath, 'review-source.patch']]) {
    if (!candidate) continue
    try { const path = absolutePath(candidate); if (samePath(path, `${job.runDir}/${name}`)) files.push({ path, label, root: job.runDir }) } catch { /* Invalid metadata grants no access. */ }
  }
  // New writable runs are isolated. Never accidentally preview the source file
  // as though it were the worker's modified snapshot.
  const executionRoot = job.executionWorkspace || job.workspace
  if (job.executionWorkspace && !samePath(executionRoot, `${job.runDir}/workspace`)) return files
  for (const candidate of job.changedFiles) {
    try {
      const path = absolutePath(/^(?:[A-Za-z]:[\\/]|\/)/.test(candidate) ? candidate : `${executionRoot}/${candidate}`)
      const inScope = !job.executionWorkspace || (job.scopePaths ?? []).some(scope => {
        const scopePath = absolutePath(`${executionRoot}/${scope}`)
        return within(executionRoot, scopePath) && within(scopePath, path)
      })
      if (inScope && within(executionRoot, path) && !sensitivePath(path) && !files.some(file => samePath(file.path, path))) files.push({ path, label: candidate, root: executionRoot })
    } catch { /* Reject traversal, URL, ADS and credential paths. */ }
  }
  return files.slice(0, 202)
}

async function preview($: EngineInterface, id: string, path: string): Promise<void> {
  const job = (await codexJobs($)).find(value => value.id === id)
  const file = job && previewFiles(job).find(value => samePath(value.path, path))
  if (!file) return
  let text = ''
  let note = ''
  try {
    const source = await readSafe($, file.path, file.root, MAX_PREVIEW)
    text = cleanText(source, MAX_PREVIEW_CHARS).replace(/\r\n?/g, '\n')
    if (source.length > MAX_PREVIEW_CHARS) note = `仅预览前 ${MAX_PREVIEW_CHARS.toLocaleString()} 个字符。`
  } catch (error) { note = cleanText(error instanceof Error ? error.message : String(error), 500) }
  await update($, panelAtom, panel => ({ ...panel, selectedId: id, previewPath: path, previewText: text, previewNote: note }))
}

const STATUS_LABEL: Record<CodexStatus, string> = {
  queued: '已排队', preparing: '准备中', running: '执行中', complete: '已完成',
  blocked: '受阻', failed: '失败', cancelled: '已取消', timeout: '已超时', unknown: '状态待核实',
}
const elapsed = (job: CodexJob, now: number): string => {
  const start = Date.parse(job.startedAt)
  const end = job.finishedAt ? Date.parse(job.finishedAt) : now
  if (!Number.isFinite(start) || !Number.isFinite(end)) return ''
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export const codexRobotSvg = (status: CodexStatus): string => {
  const eye = status === 'complete' ? '<path d="M17 21l3-2 3 2m8 0 3-2 3 2" fill="none" stroke="#213731" stroke-width="2" stroke-linecap="round"/>' : '<rect x="18" y="18" width="4" height="6" rx="2" fill="#213731"/><rect x="32" y="18" width="4" height="6" rx="2" fill="#213731"/>'
  return `<svg xmlns="http://www.w3.org/2000/svg" width="54" height="54" viewBox="0 0 54 54"><path d="M27 6v6" stroke="#73b8a5" stroke-width="3"/><circle cx="27" cy="5" r="3" fill="#97d7be"/><rect x="10" y="12" width="34" height="25" rx="8" fill="#bce6d5" stroke="#68a58f" stroke-width="2"/><rect x="6" y="21" width="5" height="10" rx="2" fill="#80bba4"/><rect x="43" y="21" width="5" height="10" rx="2" fill="#80bba4"/>${eye}<path d="M23 29q4 4 8 0" stroke="#213731" stroke-width="2" fill="none" stroke-linecap="round"/><circle cx="15" cy="27" r="2" fill="#e7b3aa"/><circle cx="39" cy="27" r="2" fill="#e7b3aa"/><rect x="17" y="39" width="20" height="9" rx="4" fill="#80bba4"/><path d="M20 47v3m14-3v3" stroke="#568d79" stroke-width="3" stroke-linecap="round"/></svg>`
}

export async function renderCodex($: EngineInterface, event: Parameters<EngineInterface['ui']['resolve']>[0]): Promise<RenderElement | null> {
  const list = await codexJobs($)
  if (!list.length) return null
  const ui = $.ui.resolve(event)
  const { Box, Text, Button, Code } = ui
  const panel: CodexPanel = await read($, panelAtom)
  const now = Math.max(await $.clock.now(), panel.now)
  const surface = event.surface as RenderSurface
  const copyPath = async (path: string) => {
    const copied = await $.ui.copy({ text: path, surface })
    if (!copied.isCopied) await $.ui.toast('复制失败；完整路径显示在预览内。')
  }
  const current = list.find(job => job.id === panel.selectedId)
  if (panel.previewPath && current && previewFiles(current).some(file => samePath(file.path, panel.previewPath))) {
    return (
      <Box key="codex-preview" flexDirection="column" marginTop={1} gap={1}>
        <Box flexDirection="row" gap={1}>
          <Button key="codex-back" label="返回任务详情" onPress={() => update($, panelAtom, value => ({ ...value, previewPath: '', previewText: '', previewNote: '' }))} />
          <Button key="codex-copy-preview" label="复制路径" onPress={() => copyPath(panel.previewPath)} />
          <Button key="codex-copy-text" label="复制内容" onPress={() => copyPath(panel.previewText)} />
        </Box>
        <Text bold>Codex 文件预览</Text>
        <Text>{panel.previewPath}</Text>
        {panel.previewNote && <Text>{panel.previewNote}</Text>}
        {panel.previewText && <Code source={panel.previewText} path={panel.previewPath} wrap="wrap" />}
      </Box>
    )
  }
  const detail = current ? (
    <Box key="codex-detail" flexDirection="column" gap={1}>
      <Button key="codex-list-back" label="返回全部任务" onPress={() => update($, panelAtom, value => ({ ...value, selectedId: '' }))} />
      <Text bold>Codex · {STATUS_LABEL[current.status]} · {current.id}</Text>
      <Text>{current.model} · effort {current.effort} · {elapsed(current, now)}{current.tokens !== undefined ? ` · ${current.tokens.toLocaleString()} tokens` : ''}</Text>
      <Text>选择依据：{current.reason || '未知：任务未提供选择依据。'}</Text>
      <Text>kind: {current.kind || '未知'} · review_of: {current.reviewOf || '无'} · author_provider: {current.authorProvider || '未知'} · attempt: {current.attempt ?? '未知'}</Text>
      {current.parentJob && <Text>parent_job: {current.parentJob}</Text>}
      {current.reviewSourceJob && <Text>复核候选任务：{current.reviewSourceJob}</Text>}
      {current.failureKind && <Text>failure_kind: {current.failureKind}</Text>}
      {current.nextAction && <Text>next_action: {current.nextAction}</Text>}
      <Text>{current.authenticationVerified ? 'ChatGPT 登录已核实' : '等待执行器核实 ChatGPT 登录'}</Text>
      <Text bold>任务全文</Text>
      <Code source={current.taskPrompt || current.goal} path="task.txt" wrap="wrap" />
      <Button key="codex-copy-task" label="复制任务" plain onPress={() => copyPath(current.taskPrompt || current.goal)} />
      <Text>最近工具：{current.lastTool || '未知：执行器未提供工具活动。'}</Text>
      {current.lastMessage && <Text>活动：{current.lastMessage}</Text>}
      {current.summary && <Text>{current.summary}</Text>}
      {current.readError && <Text>{current.readError}</Text>}
      {current.isolationNotice && <Text>{current.isolationNotice}</Text>}
      {current.executionWorkspace && <Text>执行目录：{current.executionWorkspace}</Text>}
      {current.result?.limitations.map((value, index) => <Text key={`limitation-${index}`}>限制：{value}</Text>)}
      {previewFiles(current).map((file, index) => (
        <Box key={`file-${current.id}-${index}`} flexDirection="row" gap={1}>
          <Button key={`codex-file-${current.id}-${index}`} label={`查看 ${file.label}`} plain onPress={() => preview($, current.id, file.path)} />
          <Button key={`codex-copy-${current.id}-${index}`} label="复制路径" plain onPress={() => copyPath(file.path)} />
        </Box>
      ))}
    </Box>
  ) : null
  if (detail) return detail
  return (
    <Box key="codex-jobs" flexDirection="column" marginTop={1} gap={1}>
      <Box flexDirection="row" gap={1}>
        <Text bold>Codex · 外部 worker · {list.length}</Text>
        <Button key="codex-refresh" label="刷新" plain onPress={() => refreshCodex($, true)} />
      </Box>
      <Text dimColor>官方 Codex CLI · 显示实际状态与输出；订阅用量由 Codex 管理。</Text>
      {[...list].reverse().map(job => (
        <Box key={`codex-${job.id}`} flexDirection="column" marginBottom={1}>
          <Box flexDirection="row" gap={1}>
            {event.surface === 'desktop' && 'Svg' in ui && <ui.Svg source={codexRobotSvg(job.status)} alt="Codex robot" width={54} height={54} />}
            <Button key={`codex-row-${job.id}`} label={`${job.goal.slice(0, 160)} ›`} plain onPress={() => update($, panelAtom, value => ({ ...value, selectedId: job.id }))} />
          </Box>
          <Text>{job.model} · effort {job.effort}</Text>
          <Text dimColor>{job.authenticationVerified ? 'ChatGPT 登录已核实' : '等待执行器核实 ChatGPT 登录'}</Text>
          <Text color={job.status === 'complete' ? 'green' : TERMINAL.includes(job.status) ? 'red' : undefined}>
            {STATUS_LABEL[job.status]}{job.status === 'unknown' ? `（${job.rawStatus || '未返回'}）` : ''} · {elapsed(job, now)}{job.tokens !== undefined ? ` · ${job.tokens.toLocaleString()} tokens` : ''}
          </Text>
          {job.lastTool && <Text>最近工具：{job.lastTool}</Text>}
          {job.lastMessage && <Text>输出摘要：{job.lastMessage}</Text>}
          {job.summary && <Text>{job.summary}</Text>}
          {job.readError && <Text>{job.readError}</Text>}
        </Box>
      ))}
    </Box>
  )
}
