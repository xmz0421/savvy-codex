---
name: savvy-flow
description: Optional Claude-led collaboration with Claude and Codex workers in one Agents panel. Check workflow_control before dispatch. Manual mode needs user activation; off forbids dispatch; auto applies only when selected by the user. Choose whether delegation is worth its cost.
argument-hint: <task description>
---

# Savvy Flow

Claude owns the user's goal, context, decomposition, review and integration. Claude may also do the work directly. This skill never enables itself and never requires both providers for every task.

## 1. Respect the mod switch

Discover `mcp__savvy-codex__workflow_control` and call its status action before any Savvy dispatch. The runtime is authoritative:

- `off`: no new Savvy workers. Continue ordinary work directly; reading existing tasks is allowed.
- `manual` without an active run: do not dispatch. Explain that the user can press Start in the Agents pane or run `/savvy-start`, then give the task. Loading this skill is not activation. Never change the mode or run a shell fallback to evade this gate.
- Active manual run or user-selected `auto`: use this workflow for the current authorized goal. Close a manual run with workflow_control finish after the goal is handled; do not silently reuse it for unrelated work.

`/savvy-mode off|manual|auto` and the panel control the current session. The plugin's workflowMode setting chooses the default for new sessions. Opening the pane and changing a mode never launches a model. If tools are unavailable, report that the mod is not loaded; continue ordinary work without an invisible Codex fallback.

## 2. Decide whether to delegate

Estimate context transfer, runtime and review cost before splitting. A low-risk change in at most 2 files and roughly 60 changed lines is normally done by the lead. These are heuristics, not quotas: a one-line concurrency or authorization change can still need independent review. Do not create a worker just to read one file or execute one known test command.

Delegate when an independent investigation, implementation, or review has a clear result and is likely to save effort or catch a material error. Keep summaries short. Do not expand a small task into a spec approval ceremony or exhaustive test suite.

For substantial work, show a compact plan: owner/provider, task, actual model/effort and reason, dependencies, file scope, verification and review cost. Ask only when missing information materially changes the outcome.

## 3. Choose current models and effort

For Codex, first read the account catalog through `mcp__savvy-codex__codex_catalog` (refresh defaults false) or the bundled executor. Choose an exact supported model id and supported effort from that result. Do not copy an old example, invent a model, silently substitute one or refresh the catalog by running a model.

Use a lower-cost available candidate with medium effort for clear mechanical work; a general coding candidate for implementation; stronger reasoning/high effort for uncertainty, complex invariants or difficult review. Reserve the highest effort for stages that justify it. Record the concrete reason and account availability; model choice and effort are separate decisions. No provider is inherently the planner or reviewer for every task.

For Claude, check the actual Agent tool schema and available worker definitions. The bundled tier names are effort/role presets and inherit the lead's model unless explicitly overridden. Select an available model per invocation when supported; report actual resolved model/effort. Do not interpret a tier name as proof of a particular model or price.

## 4. Dispatch with ownership

- Codex: read [codex-worker](../codex-worker/SKILL.md), write a bounded JSON packet, then use codex_delegate directly. No extra Claude wrapper agent.
- Claude: use a bundled `savvy-codex:savvy-*` worker with a self-contained brief. Put `KIND: implement|review|inspect`, `REVIEW_OF: <source task id>` for a review, `AUTHOR_PROVIDER: claude|codex`, allowed paths, expected output, and checks in the brief. A review is read-only.
- Writers work in independent staged workspaces or git worktrees. Read current dirty content as input where required; a clean-HEAD worktree alone omits dirty edits, so explicitly transfer the relevant diff. Never reset the source project. Never have two writers modify the main checkout together.
- A Codex implementation produces files and a patch in its scoped execution snapshot. It does not update the main project automatically. Inspect the actual patch and file list, review, then apply through the lead's authorized tools. Check that source files have not changed since dispatch; resolve conflicts visibly.
- Include only necessary context and authorized inputs, including test configuration/dependencies when needed. If the snapshot lacks an input, request a precise scope expansion instead of exploring the original project behind the boundary.
- Default synchronous calls return actual results. Background calls are useful when independent work remains; wait once when the result is needed. State refresh and ordinary program waiting do not require model polling.

Maintain the progress tool with real planned tasks, providers and dependencies; mark a task accepted only after verification. Do not label GPT tasks as Claude tiers to fit a UI.

## 5. Cross-provider review

For a nontrivial implementation, schedule one independent review by the other provider before integration:

- Codex author → a Claude review worker reads the actual exported patch, affected files and test evidence.
- Claude author → call Codex with workspace set to the author's actual isolated worktree (or a prepared candidate snapshot), not the unchanged main checkout. A `kind: review` packet reads the actual candidate change and tests, with `author_provider: claude` and `review_of` pointing to the author task. Include the candidate diff and relevant baseline in the allowed inputs when needed to explain the change.

Link the actual task IDs in the pane; do not call a lead reread an independent worker review. A review that has not run stays pending/unknown. If a provider is unavailable, disclose the missing review and obtain a deliberate user decision for changes whose acceptance depends on it; continue unrelated authorized work.

For a trivial, reversible edit, the lead may skip cross-review and state the reason. Do not repeatedly review unchanged patches. Review checks scope, edge cases, reported tests, and material regressions; use actual files, not only the author's summary. After a revision that materially changes behavior, review the new patch once.

## 6. Bounded recovery and completion

Read failure_kind, next_action, limitations and verification evidence. Default is no automatic retry, model substitution, API billing switch, or permission escalation.

- Transient service failure: at most one justified retry, preserving the original record and linking parent_job/attempt.
- Timeout: narrow the task or deliberately extend the next attempt; show the reason.
- Quota/model unavailable: report it and choose another available model/provider only within user preferences; record that new choice before dispatch.
- Auth/version/permission failure: stop that dispatch and fix the prerequisite through authorized tools; do not retry the same denial or bypass the sandbox.
- Incomplete/invalid result or out-of-scope edits: keep blocked, explain the concrete defect, allow one focused revision; then re-plan instead of looping.

The lead verifies the candidate and the review, checks actual diffs, and integrates only accepted work. A worker that could not run a necessary check remains blocked even if the lead later performs that check; identify who actually verified it. Final delivery states results, meaningful checks, review status and remaining limitations. Close the progress and the manual workflow activation.

Never delegate recursively when CLAUDE_CODEX_WORKER=1 or MANIFOLD_COLLAB_WORKER=1. Delegation does not expand user authorization.
