# Savvy Codex

让 Claude 主导项目，同时调度 **Claude 原生子代理和 Codex worker**，在同一个 Savvy Agents 侧栏查看任务、模型、思考强度、真实进度和产物。

基于 [johnnyvizz/claude-kit](https://github.com/johnnyvizz/claude-kit) 的 Savvy Flow / Savvy Progress 整合扩展，使用 MIT 许可证。这是社区试用版，与 Anthropic / OpenAI 无隶属关系。

## 工作方式

- Claude 拆解任务、协调依赖、检查实际结果，可以使用 Claude worker、Codex worker，或两者配合。
- 每次 Codex 派工分别选择模型和 reasoning effort，记录理由；只使用账号目录里可用的模型，不静默替换。
- 同一个 Agents 面板显示真实任务、模型、effort、状态、工具活动与文件，可预览并复制路径。
- Codex 使用官方 CLI 的 ChatGPT 登录。不启动 Claude CLI，不复制登录凭据；任务消耗各自账号的实际额度。面板刷新、状态查询、程序等待不调用模型。

## 运行条件

已在 **Windows + Claude Desktop 的 Code 会话**、Claude Code **2.1.314** 与 Codex CLI **0.166.0** 验证。原生面板使用 `claude-code` Mod/function-hooks 接口，宿主必须支持；普通网页 Chat / Cowork 不保证可用。macOS / Linux 尚未实际验证。

需要 Git、Python 3.10+、能在终端运行的 Claude Code 和官方 Codex CLI。先各自完成登录，检查 `claude --version`、`codex --version` 和 `codex login status`。Codex 必须显示 ChatGPT 登录。CLI 参数和模型由版本及账号决定，执行器会检查兼容性。

## 安装

在终端执行；Windows 的 `python` 可替换为 Python 完整路径或 `py -3`：

```sh
git clone https://github.com/xmz0421/savvy-codex.git
cd savvy-codex
python scripts/install.py --enable-default-workflow
```

安装到 `~/.claude/skills/savvy-codex`，启用 `savvy-codex@skills-dir`，关闭原 `savvy-flow@claude-kit` / `savvy-progress@claude-kit` 避免重复面板，修改前备份已有内容。不改模型、服务地址或认证设置，不发起推理。

`--enable-default-workflow` 在全局 `~/.claude/CLAUDE.md` 追加带标记的协作偏好：实质项目任务自动进入 Savvy 流程，Claude 决定如何分工；小问题不用派工，也可以说“这次只用 Claude”。不加此参数则通过 `/savvy-codex:savvy-flow` 按需启用。

**安装后重新开启 Claude Code 会话**。`savvy-flow` 是整体流程，`codex-worker` 是内部执行组件，日常无需单独选择 worker。

试用：“检查这个项目的测试入口，让 Codex 做一次只读检查，告诉我选了什么模型和 effort。” 用 `/agents-info` 或 `/codex-agents` 打开同一个侧栏。

## 权限与记录

模型与 effort 按任务决定；技能里的 Terra / Sol / Astra 是候选示例，以账号目录为准。只读任务使用 read-only；实施需要已有修改授权和明确文件范围。workspace-write 是工作区级隔离，Claude 仍需对照实际差分验收。必要检查被拒绝时保留 blocked，可由 Claude 实际补验并说明结果。

任务包、CLI 事件、报告保存在 `~/.claude/codex-worker-runs/`，可能含项目内容，不要上传公开 issue。Codex 不递归派工，同一工作区有执行锁；并行写入请分配独立 worktree。API 标价不等于订阅账单。

## 验证与维护

```sh
python -m unittest discover -s skills/codex-worker/scripts -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
claude plugin validate --json .
claude plugin test .
```

Python 测试使用临时目录和 mock，不调用模型。原生面板测试需要支持 plugin test 的宿主。GitHub CI 只执行无需账号的 Python 检查，不能替代桌面实测。

工具或侧栏不可见时，检查新的 Code 会话、插件启用状态、宿主 function-hooks 支持及原 Savvy Progress 是否关闭。Python 不在 PATH 时见 [执行器配置](skills/codex-worker/references/execution.md)。工具未加载时不应伪称已有可视化；直接脚本模式须经用户接受。

更新：`git pull` 后重跑安装命令。恢复：关闭整合版，重新启用原两个 Savvy 插件；移除全局提示中 `BEGIN SAVVY-CODEX-DELEGATION` 至 `END SAVVY-CODEX-DELEGATION` 块。原文件在安装器打印的备份目录；恢复时保留其他新设置及运行记录。

源码仅包含插件、执行器、测试和安装文档，不含作者项目、账号设置或运行记录。反馈请附系统、CLI 版本、脱敏错误及复现步骤。原作者和修改说明见 [NOTICE](NOTICE)，许可证见 [LICENSE](LICENSE)。
