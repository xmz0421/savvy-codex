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
  taskPrompt?: string
  selectionReason?: string
  workspace?: string
  changedFiles?: string[]
  activities?: { tool: string; status: string; at: number; summary?: string }[]
  answer?: string
  kind?: string
  reviewOf?: string
  authorProvider?: string
  attempt?: number
  failureKind?: string
  nextAction?: string
}

export type Panel = {
  isCompact: boolean
  isDoneCollapsed: boolean
  autoOpenedFor: string
  selectedId?: string
  previewPath?: string
  previewText?: string
  previewNote?: string
}

export type WorkflowMode = 'off' | 'manual' | 'auto'
export type WorkflowState = { mode: WorkflowMode; active: boolean; source: string; configuredMode?: WorkflowMode }

declare module 'claude-code' {
  interface PluginState {
    'savvy-codex': {
      flow: Flow | null
      agents: AgentRun[]
      panel: Panel
      now: number
      codexJobs: CodexJob[]
      codexPanel: CodexPanel
      workflow: WorkflowState
    }
  }
}

/** Valid values are supplied by the user's local Codex catalog. */
export type Effort = string
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
  taskPrompt?: string
  executionWorkspace?: string
  scopePaths?: string[]
  patchPath?: string
  changesPath?: string
  reviewSourcePatchPath?: string
  reviewSourceJob?: string
  isolationNotice?: string
  kind?: string
  reviewOf?: string
  parentJob?: string
  authorProvider?: string
  attempt?: number
  failureKind?: string
  nextAction?: string
  retryable?: boolean
}

export type CodexPanel = {
  now: number
  selectedId: string
  previewPath: string
  previewText: string
  previewNote: string
}
