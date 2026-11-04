# Changelog

## 1.4.0-codex.2

- `/savvy-start <task>` activates the workflow and submits the task to the current Claude session after the command returns. Empty arguments only activate; off mode blocks submission. Submission errors are visible and clear manual activation.

## 1.4.0-codex.1

- Default manual mode with visible off/manual/auto controls. Only user commands/buttons activate manual delegation. Removed unconditional global workflow instructions and source-level auto-trigger requirements.
- Clickable Claude/Codex task details with actual task text, available activity/result metadata and file previews; added a Codex robot. Unknown host data stays unknown.
- Scope-only execution snapshots and guarded patch export. The controller does not write back to the source project; external filesystem access remains governed by the host sandbox.
- Linked cross-provider review metadata, Claude review-tool restrictions, bounded failure/retry policy, and direct completion for small low-risk work.
- Account catalog driven Codex model/effort choice; Claude templates inherit models and use worktrees. Static price estimates disabled by default and labeled unverified if enabled.
- Inference-free installation doctor, local edit detection/backups, conservative per-key uninstall, and expired terminal-run cleanup protecting active/unknown/linked paths.
- Documentation for short Windows clone paths, mode persistence, actual isolation limits, compatibility and maintenance.

## 1.3.0-codex.2

Initial public preview with integrated official Codex CLI workers in the Savvy Agents panel, portable installation and cross-platform Python tests.
