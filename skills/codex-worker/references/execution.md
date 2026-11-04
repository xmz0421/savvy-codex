# 官方 Codex 执行器

优先使用整合版的 codex_delegate / codex_status 工具，负责同一 Agents 侧栏中的记录。仅当用户接受没有侧栏的方式时，直接调用脚本。

安装器把脚本放在 `~/.claude/skills/savvy-codex/skills/codex-worker/scripts/codex_delegate.py`。Python 需要 3.10+，无需第三方依赖。不要复制账号令牌；脚本要求官方 CLI 显示 ChatGPT 登录，不使用 API key。

原生插件设置提供 `pythonPath`（单个解释器路径或名称，不带命令参数）、`pluginRoot`（插件根目录）、`helperPath`（执行脚本完整路径）。设置值优先，其次分别读取 `SAVVY_CODEX_PYTHON`、`SAVVY_CODEX_PLUGIN_ROOT`、`SAVVY_CODEX_HELPER`。未配置时 Windows 使用 PATH 中的 `python`，其他系统使用 `python3`；脚本路径先看 `CLAUDE_PLUGIN_ROOT`，再使用上述 skills-dir 位置。解释器路径支持空格，不要在设置里额外加引号，也不要填入 `py -3` 这种多参数命令。命令行手工安装仍可以使用 `py -3 scripts/install.py`。

PowerShell 示例（Python 可换为本机解释器路径）：

```powershell
$worker = Join-Path $HOME '.claude/skills/savvy-codex/skills/codex-worker/scripts/codex_delegate.py'
python $worker catalog --cached
python $worker run --workspace 'C:/projects/example' --packet "$HOME/.claude/codex-worker-runs/requests/task.json" --model gpt-6.1-sol --effort medium --permission read-only --approval never
```

模型名是示例；先用 catalog 检查账号支持的模型和 effort。任务内容写入 JSON，不拼入 shell。

实施模式使用 `--permission workspace-write --approval auto-review`，要求任务包 `kind=implement`、`authorization.write_intent=true` 且用户已授权修改。权限不扩大任务范围。

`start` 后台运行并返回任务编号；`status` 读取真实状态，`wait` 用程序等待。参数以 `--help` 为准。不要让模型反复轮询或在没有结果时宣称完成。

每项任务保留任务包、模型、effort、原因、事件、结果及文件列表。记录含实际项目内容，请留在本机。额度未知时不猜百分比，不把 API 等价价格当订阅费用。
