import { atom, read, update } from 'claude-code'
import { answer } from './tool-result'
import type { EngineInterface, Register } from 'claude-code'
import type { WorkflowMode, WorkflowState } from '../types'

export const WORKFLOW_TOOL = 'mcp__savvy-codex__workflow_control'
const state = atom({ plugin: 'savvy-codex', key: 'workflow' } as const, { mode: 'manual', active: false, source: 'default', configuredMode: 'manual' })
const modes = ['off', 'manual', 'auto'] as const
let hostVersion = 'unknown'
let hostCompatibility = 'unknown: host version API unavailable'
export const savvyWorker = (type: string): boolean => /(?:^|:)savvy-(?:fable|heavy|careful|medium|light|review)$/.test(type)
export async function workflowState($: EngineInterface): Promise<WorkflowState> { return read($, state) }
export async function workflowAllowed($: EngineInterface): Promise<boolean> {
  const current = await workflowState($)
  return current.mode === 'auto' || (current.mode === 'manual' && current.active)
}
export async function workflowRefusal($: EngineInterface): Promise<string> {
  const current = await workflowState($)
  return current.mode === 'off' ? 'Savvy 已关闭；用户可运行 /savvy-mode manual 或 auto。' :
    'Savvy 为 manual；用户先运行 /savvy-start 或点击侧栏“开始本次协作”，才可派工。'
}
export async function setWorkflowMode($: EngineInterface, mode: WorkflowMode): Promise<void> {
  await update($, state, current => ({ ...current, mode, active: false, source: 'user' }))
}
export async function activateWorkflow($: EngineInterface): Promise<boolean> {
  if ((await workflowState($)).mode === 'off') return false
  await update($, state, current => ({ ...current, active: true, source: 'user' }))
  return true
}
export async function finishWorkflow($: EngineInterface): Promise<void> {
  await update($, state, current => ({ ...current, active: false }))
}
export async function startWorkflow($: EngineInterface, options: Record<string, unknown>): Promise<void> {
  hostVersion = 'unknown'
  hostCompatibility = 'unknown: host version API unavailable'
  try {
    const raw: unknown = await $.session.version()
    const value = typeof raw === 'string' ? raw : raw && typeof raw === 'object' ? String((raw as { version?: string }).version ?? '') : ''
    const match = /\b(\d+)\.(\d+)\.(\d+)\b/.exec(value)
    if (match) {
      hostVersion = match[0]
      hostCompatibility = ['2.1.314', '2.1.317'].includes(hostVersion) ? 'contract-test target; see release validation record' :
        Number(match[1]) < 2 || (Number(match[1]) === 2 && (Number(match[2]) < 1 || (Number(match[2]) === 1 && Number(match[3]) < 314))) ? 'unsupported: update to >=2.1.314' : 'untested host version'
    }
  } catch { /* Older hosts can continue with explicit unknown diagnostics. */ }
  const selected = modes.includes(options.workflowMode as WorkflowMode) ? options.workflowMode as WorkflowMode : 'manual'
  // A restored session retains its user override while the configured option
  // is unchanged. A newly selected host setting takes effect on reload.
  await update($, state, current => current.source === 'user' && current.configuredMode === selected ? current : { mode: selected, active: false, source: 'default', configuredMode: selected })
  await $.tool.register({
    name: 'workflow_control', isDeferred: true,
    description: 'Read the session Savvy gate, or finish the current manual workflow. A model cannot activate manual mode. Ask the user to run /savvy-start or press the panel start button; /savvy-mode auto is a user command. This tool never calls a model or process.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { action: { type: 'string', enum: ['status', 'finish'] } }, required: ['action'] },
  })
  await $.command.register({ name: 'savvy-mode', description: 'Session Savvy mode: off | manual | auto. No model call; manual requires /savvy-start for each workflow.' })
  await $.command.register({ name: 'savvy-start', description: 'Start this workflow and open its Agents pane; optionally append a task for the current Claude session.' })
  await $.command.register({ name: 'savvy-check', description: 'Check the real workflow/status/catalog tool wire contract without dispatching a worker or calling a model.' })
}
export const registerWorkflow: Register = (on) => {
  on('command.run', { command: 'savvy-check' }, async ($) => {
    const checks: string[] = []
    for (const tool of [WORKFLOW_TOOL, 'mcp__savvy-codex__codex_status', 'mcp__savvy-codex__codex_catalog']) {
      try {
        const raw = await $.tool.call({ tool, ...(tool === WORKFLOW_TOOL ? { action: 'status' } : tool.endsWith('__codex_catalog') ? { refresh: false } : {}) } as never)
        if (typeof raw.result !== 'string') throw new Error('expected a JSON string result')
        const parsed: unknown = JSON.parse(raw.result)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('expected a JSON object encoded as string')
        const data = parsed as Record<string, unknown>
        checks.push(`${tool}: wire OK${data.status === 'unavailable' || data.status === 'blocked' ? `; ${data.status}: ${data.summary ?? ''}` : ''}`)
      } catch (error) { checks.push(`${tool}: FAILED: ${String(error).slice(0, 1200)}`) }
    }
    return { text: `Savvy tool diagnostics (${hostVersion}; ${hostCompatibility})\n${checks.join('\n')}\nNo worker/model was dispatched. Catalog availability is reported separately from wire validity.` }
  })
  on('command.run', { command: 'savvy-mode' }, async ($, event) => {
    const mode = String(event.args ?? '').trim() as WorkflowMode
    if (!modes.includes(mode)) return { text: `Savvy: ${(await workflowState($)).mode}. 使用 /savvy-mode off|manual|auto。` }
    await setWorkflowMode($, mode)
    return { text: `Savvy: ${mode}${mode === 'manual' ? '；本次需 /savvy-start 或侧栏开始按钮激活。' : '。'}` }
  })
  on('command.run', { command: 'savvy-start' }, async ($, event) => {
    const task = String(event.args ?? '').trim()
    if (task.length > 64_000) return { text: '任务过长；请将详细材料放入项目文件，再提交简短任务。尚未开启协作。' }
    if (!await activateWorkflow($)) return { text: await workflowRefusal($) }
    let paneNotice = ''
    try {
      const pane = await $.ui.open({ id: 'savvy-agents', title: 'Agents' })
      if (!pane.isPlaced) paneNotice = '侧栏暂未打开，可用 /agents-info 重试。'
    } catch {
      paneNotice = '侧栏暂未打开，可用 /agents-info 重试。'
    }
    if (!task) return { text: '已启用本次 Savvy 协作。manual 模式在流程完成后恢复待机。' + paneNotice }
    // Run after command.run returns: awaiting the queued prompt in this handler
    // could block the composer drain that must deliver it. This is a normal
    // prompt in the current session, never another CLI or a wrapper agent.
    $.clock.after(0, () => {
      void (async () => {
        try {
          if (!await workflowAllowed($)) {
            await $.ui.toast('任务未提交：协作已在提交前关闭。')
            return
          }
          const sent = await $.prompt.submit({ text: 'Savvy collaboration has been explicitly started by the user. Read savvy-codex:savvy-flow, check workflow_control, and carry out the following user task in this session. Choose whether delegation is worthwhile; retain all existing permissions and project constraints.\n\nUser task:\n' + task })
          if (typeof sent.drop === 'string') {
            await finishWorkflow($)
            await $.ui.toast(`任务未提交：${sent.drop.slice(0, 500)}。请重新开启并发送任务。`)
          }
        } catch (error) {
          await finishWorkflow($)
          await $.ui.toast(`任务提交失败：${String(error).slice(0, 500)}。请重新开启并发送任务。`)
        }
      })()
    })
    return { text: '已启用本次协作，正在将任务提交给当前 Claude 会话。' + paneNotice }
  })
  on('tool.call', { tool: WORKFLOW_TOOL }, async ($, event) => {
    if (event.agentId) return answer({ status: 'blocked', summary: 'Only the main session may control the workflow.' })
    const action = (event as unknown as { action?: string }).action
    if (action === 'finish') await finishWorkflow($)
    else if (action !== 'status') return answer({ status: 'blocked', summary: '模型不能激活 manual；用户使用 /savvy-start 或侧栏开始按钮。' })
    return answer({ ...await workflowState($), allowed: await workflowAllowed($), activation_command: '/savvy-start', host_version: hostVersion, host_compatibility: hostCompatibility })
  })
}
