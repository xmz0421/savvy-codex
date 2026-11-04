import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import type { WorkflowMode, WorkflowState } from '../types'

export const WORKFLOW_TOOL = 'mcp__savvy-codex__workflow_control'
const state = atom({ plugin: 'savvy-codex', key: 'workflow' } as const, { mode: 'manual', active: false, source: 'default', configuredMode: 'manual' })
const modes = ['off', 'manual', 'auto'] as const
export const savvyWorker = (type: string): boolean => /(?:^|:)savvy-(?:fable|heavy|careful|medium|light)$/.test(type)
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
  const selected = modes.includes(options.workflowMode as WorkflowMode) ? options.workflowMode as WorkflowMode : 'manual'
  // A restored session retains its user override while the configured option
  // is unchanged. A newly selected host setting takes effect on reload.
  await update($, state, current => current.source === 'user' && current.configuredMode === selected ? current : { mode: selected, active: false, source: 'default', configuredMode: selected })
  await $.tool.register({
    name: 'workflow_control', isDeferred: false,
    description: 'Read the session Savvy gate, or finish the current manual workflow. A model cannot activate manual mode. Ask the user to run /savvy-start or press the panel start button; /savvy-mode auto is a user command. This tool never calls a model or process.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { action: { type: 'string', enum: ['status', 'finish'] } }, required: ['action'] },
  })
  await $.command.register({ name: 'savvy-mode', description: 'Session Savvy mode: off | manual | auto. No model call; manual requires /savvy-start for each workflow.' })
  await $.command.register({ name: 'savvy-start', description: 'Explicitly authorize Savvy delegation for the current manual workflow.' })
}
export const registerWorkflow: Register = (on) => {
  on('command.run', { command: 'savvy-mode' }, async ($, event) => {
    const mode = String(event.args ?? '').trim() as WorkflowMode
    if (!modes.includes(mode)) return { text: `Savvy: ${(await workflowState($)).mode}. 使用 /savvy-mode off|manual|auto。` }
    await setWorkflowMode($, mode)
    return { text: `Savvy: ${mode}${mode === 'manual' ? '；本次需 /savvy-start 或侧栏开始按钮激活。' : '。'}` }
  })
  on('command.run', { command: 'savvy-start' }, async ($) => ({ text: await activateWorkflow($) ? '已启用本次 Savvy 协作。manual 模式在流程完成后恢复待机。' : await workflowRefusal($) }))
  on('tool.call', { tool: WORKFLOW_TOOL }, async ($, event) => {
    if (event.agentId) return { result: { status: 'blocked', summary: 'Only the main session may control the workflow.' } }
    const action = (event as unknown as { action?: string }).action
    if (action === 'finish') await finishWorkflow($)
    else if (action !== 'status') return { result: { status: 'blocked', summary: '模型不能激活 manual；用户使用 /savvy-start 或侧栏开始按钮。' } }
    return { result: { ...await workflowState($), allowed: await workflowAllowed($), activation_command: '/savvy-start' } }
  })
}
