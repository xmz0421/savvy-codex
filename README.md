# Savvy Codex

Claude 主导，按任务选择 Claude 子代理、Codex worker 或直接完成；两家工作项显示在同一个 Savvy Agents 面板。基于 [johnnyvizz/claude-kit](https://github.com/johnnyvizz/claude-kit) 的 Savvy Flow / Progress 扩展，保留 MIT 许可证和原作者署名。

## 先决定什么时候协作

插件默认 **手动模式**，不会写入全局“所有项目自动协作”的指令。

| 模式 | 新派工行为 |
| --- | --- |
| `off` 关闭 | 拒绝新的 Savvy Claude / Codex 派工；已有任务和记录仍可查看 |
| `manual` 手动（默认） | 用户在侧栏点开始或运行 `/savvy-start` 后，允许当前一轮协作；完成后关闭本轮激活 |
| `auto` 自动 | 用户明确选定后，Claude 按任务判断是否值得派工 |

侧栏按钮或 `/savvy-mode off`、`/savvy-mode manual`、`/savvy-mode auto` 切换**当前会话**。插件设置的 `workflowMode` 决定**新会话**默认值，更新插件不覆盖宿主保存的设置。查看面板、切换模式和普通状态刷新不发起模型调用。关闭模式不强行终止已运行的进程。

也可在终端保存新会话默认值（未列出的设置保留）：

```sh
echo '{"workflowMode":"manual"}' | claude plugin configure savvy-codex@skills-dir --values-stdin
```

将 manual 换为 off 或 auto 后重新开启会话。通过 `claude plugin configure savvy-codex@skills-dir --json` 查看当前插件选项；自定义 Python 也可用同一接口设置 `pythonPath`。

手动试用：新建 Claude Desktop **Code** 会话，运行 `/savvy-start`，再说“用 Savvy 检查这个项目的测试入口，让 Codex 做一次只读调查，显示模型和思考强度”。`/savvy-codex:savvy-flow` 是流程说明入口，单独加载技能不会绕过手动开关。`codex-worker` 是内部组件，不是另一个日常入口。

也可一条命令开启并提交任务，例如：`/savvy-start 检查这个项目的测试入口，按需要安排 Claude 和 Codex 配合`。后面的文字会作为任务交给当前 Claude 会话；不带文字时只开启待机，不调用模型。off 模式仍需先切回 manual 或 auto。

## 安装和前置检查

需要 Git、Python 3.10+、支持 `claude-code` Mod/function-hooks 的 Claude Code，以及已通过 **ChatGPT 登录**的官方 Codex CLI。已验证的桌面组合为 Windows、Claude Code 2.1.314、Codex CLI 0.166.0；兼容性按实际所需参数/功能检测，旧版 CLI 可能不兼容。普通网页 Chat / Cowork 不保证侧栏能力，macOS / Linux 桌面尚未实测。

先确认 `claude --version`、`codex --version`、`codex login status`。Windows 建议克隆到短路径（例如 `C:/src/savvy-codex`）；长路径可加 Git 参数：

```sh
git -c core.longpaths=true clone https://github.com/xmz0421/savvy-codex.git
cd savvy-codex
python scripts/install.py
```

`python` 可替换为已有解释器完整路径，手工命令也可使用 `py -3`。无需新虚拟环境或第三方 Python 依赖。

安装器先运行不调用模型的 doctor，检查 CLI、所需能力、登录和账号模型目录；不兼容时给出原因并停止，不显示“已就绪”。新登录账号若尚无模型缓存，按 doctor 提示显式刷新目录后重试。`--skip-doctor` 仅用于明确的离线安装/测试，结果会标注未检查，不能等同可运行。

安装到 `~/.claude/skills/savvy-codex`，仅修改相关插件开关，备份原内容；不更改服务地址、模型或凭据。原 Savvy Flow / Progress 会关闭以避免重复侧栏。重开 Code 会话加载更新。

旧版 `--enable-default-workflow` 已弃用：只做兼容迁移，不再追加全局自动指令。升级会清除本插件带标记的旧全局段落，并保留其他提示。想自动运行，请在插件设置选择 `auto`。

## 点开看工作内容

同一面板中，Claude 保留原小 Claude 形象，Codex 使用独立机器人形象。点任务的详情入口查看任务内容、实际模型/effort、状态、工具活动、可取得的结果和文件。文件预览与复制路径仍在同一面板中。

只显示宿主实际提供的信息；无法取得的 Claude 文件/输出会注明未知，不编造思考过程或完成状态。历史列表和工具日志可能含项目内容，请勿未经脱敏上传 issue。

## 派工与互审

负责人可以亲自干活。低风险、约 2 个文件 / 60 行以内的修改通常直接完成；复杂的一行安全/并发修改仍可能值得独立复核。先比较上下文转移、执行和验收成本，再拆任务。

Codex 的模型和 effort 从账号实时可用目录选择，不在规则里固定某代模型。Claude 的五个旧 tier 名称仅为角色/effort 预设，默认继承当前模型，可按宿主支持的 Agent 参数指定当前可用模型。每次解释具体选择理由。

有实质行为变化的实现安排一次另一家的只读审查：Codex 写 → Claude 审，Claude 写 → Codex 审；用真实 `review_of` 关联任务。未执行的审查保持待完成，不能把负责人重读当独立审查。小的可逆修改可说明原因后跳过。审查不自动无限循环。

## 文件隔离和交付

Codex 只复制任务 `scope_paths` 明确允许的文件/目录到独立执行副本，保留相关脏文件的当前内容。实施的变更和补丁留在副本，**不自动写回原项目**。程序检查实际新增、修改、删除等变更，越界则阻止补丁交付。测试所需配置、输入与依赖也需在任务范围里明确提供。

Claude worker 使用宿主原生 git worktree 隔离。负责人检查候选补丁、另一家的审查和原文件是否变化，再合并；不让多个写入者同时改主目录。worktree 通常基于提交状态，涉及未提交输入时需显式传入对应改动。

Claude 子代理的模型覆盖与 worktree 选项以[宿主文档](https://code.claude.com/docs/en/sub-agents)和实际工具 schema 为准；已有 tier 不是某个固定模型的承诺。

这些是最小输入副本与交付范围控制，**不等于操作系统逐文件访问围栏**。外部路径/工具权限由 CLI 与宿主沙箱控制；不能承诺任何宿主上都完全无法读到外部文件。敏感目录拒绝规则仍是附加防护，不能代替任务允许清单。目录范围应尽量小，不把整个项目加入只为方便。

## 失败和记录维护

失败详情包含可取得的原因分类与下一步。默认不暗中重试、不换模型、不切 API 计费、不绕过权限；瞬时错误最多一次有记录的重试，超时则缩小任务或明确延时。额度/登录/版本问题先处理前置条件。每次新尝试关联原任务，保留原失败记录。

记录位于 `~/.claude/codex-worker-runs/`。清理按保留天数处理已结束任务，默认 30 天，先看 dry run；运行中、未知或不安全目录受保护。清理不调用模型，也不创建后台计划。具体命令见 [执行器说明](skills/codex-worker/references/execution.md)。

插件默认只显示 tokens 和用时。上游静态 API 价格估算默认关闭；显式开启 `priceEstimates` 后仍不是订阅账单，不能作为实时价格依据。

## 更新、卸载与验证

更新：`git pull` 后重新运行安装器。已有包内自定义修改会检测并备份、报告；持久偏好应放插件设置，别改 skill 源码。

卸载：

```sh
python scripts/install.py --uninstall
```

只撤销本插件有记录的修改，按仍受本插件管理的键还原原插件开关；保留安装后新增设置、其他提示、备份和任务记录。不会整份覆盖 settings.json。卸载后重开会话。

开发检查（无真实模型调用）：

```sh
python -m unittest discover -s skills/codex-worker/scripts -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
claude plugin validate --json .
claude plugin test .
```

GitHub CI 检查 Windows / Ubuntu 的 Python 3.10、3.12；原生界面另由宿主插件测试验证，不能把 CI 通过等同所有桌面版本实测。反馈请附版本、复现步骤和脱敏错误。见 [NOTICE](NOTICE) 与 [LICENSE](LICENSE)。本社区项目与 Anthropic / OpenAI 无隶属关系。
