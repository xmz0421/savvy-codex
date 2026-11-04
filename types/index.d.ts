export type Phase = 'plan' | 'design' | 'delegate' | 'review' | 'close'

export type PlannedTask = {
  title: string
  tier: string
  after: number[]
  provider?: 'claude' | 'codex'
  model?: string
  effort?: string
}

export type Flow = {
  title: string
  total: number
  done: number
  running: number
  phase: Phase
  isFinished: boolean
  tasks: PlannedTask[]
}

export type AgentStatus = 'running' | 'done' | 'failed'

export type AgentRun = {
  id: string
  agentId?: string
  type: string
  description: string
  model: string
  effort?: string
  status: AgentStatus
  startedAt: number
  endedAt?: number
  contextTokens: number
  contextMax: number
  tokens: number
  costUsd: number
  steps: number
  round: number
  /** Self-reported by the worker through the `step` tool. */
  stepDone?: number
  stepTotal?: number
  stepNote?: string
}

export type Panel = {
  isCompact: boolean
  isDoneCollapsed: boolean
  autoOpenedFor: string
}

declare module 'claude-code' {
  interface PluginState {
    'savvy-codex': {
      flow: Flow | null
      agents: AgentRun[]
      panel: Panel
      now: number
      codexJobs: CodexJob[]
      codexPanel: CodexPanel
    }
  }
}

export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type CodexStatus = 'queued' | 'preparing' | 'running' | 'complete' | 'blocked' | 'failed' | 'cancelled' | 'timeout' | 'unknown'

export type CodexResult = {
  status: string
  summary: string
  changed_files: string[]
  checks: string[]
  findings: string[]
  limitations: string[]
}

export type CodexJob = {
  id: string
  provider: 'external-codex'
  workspace: string
  runDir: string
  statePath: string
  reportPath: string
  resultPath: string
  model: string
  effort: string
  goal: string
  reason: string
  status: CodexStatus
  rawStatus: string
  startedAt: string
  finishedAt: string
  updatedAt: string
  summary: string
  lastTool: string
  lastMessage: string
  changedFiles: string[]
  readError: string
  authenticationVerified: boolean
  tokens?: number
  result?: CodexResult
}

export type CodexPanel = {
  now: number
  selectedId: string
  previewPath: string
  previewText: string
  previewNote: string
}
