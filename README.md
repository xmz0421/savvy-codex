# Savvy Codex

Claude 主导，按任务选择 Claude 子代理、Codex worker 或直接完成；两家工作项显示在同一个 Savvy Agents 面板。基于 [johnnyvizz/claude-kit](https://github.com/johnnyvizz/claude-kit) 的 Savvy Flow / Progress 扩展，保留 MIT 许可证和原作者署名。

## 先决定什么时候协作

插件默认 **手动模式**，不会写入全局“所有项目自动协作”的指令。

| 模式 | 新派工行为 |
| --- | --- |
| `off` 关闭 | 拒绝新的 Savvy Claude / Codex 派工；已有任务和记录仍可查看 |
| `manual` 手动（默认） | 用户在侧栏点开始或运行 `/savvy-start` 后，允许当前任务的跨回合协作；明确完成时结束 |
| `auto` 自动 | 用户明确选定后，Claude 按任务判断是否值得派工 |

侧栏按钮或 `/savvy-mode off`、`/savvy-mode manual`、`/savvy-mode auto` 切换**当前会话**。插件设置的 `workflowMode` 决定**新会话**默认值，更新插件不覆盖宿主保存的设置。查看面板、切换模式和普通状态刷新不发起模型调用。关闭模式不强行终止已运行的进程。

也可在终端保存新会话默认值（未列出的设置保留）：

```sh
echo '{"workflowMode":"manual"}' | claude plugin configure savvy-codex@skills-dir --values-stdin
```

将 manual 换为 off 或 auto 后重新开启会话。通过 `claude plugin configure savvy-codex@skills-dir --json` 查看当前插件选项；自定义 Python 也可用同一接口设置 `pythonPath`。

手动试用：新建 Claude Desktop **Code** 会话，直接输入 `/savvy-start 检查这个项目的测试入口，按需要安排 Claude 和 Codex 配合`。这一条会开启本轮协作、立即打开同一个 Agents 侧栏，并提交任务，日常不必再输入 `/agents-info`。`/savvy-codex:savvy-flow` 是流程说明入口，单独加载技能不会绕过手动开关。`codex-worker` 是内部组件，不是另一个日常入口。

不带文字的 `/savvy-start` 只开启并打开侧栏，不调用模型；重复运行会聚焦同一个面板。`/agents-info` 保留用于手动查看或重新打开面板。off 模式仍需先切回 manual 或 auto；若宿主暂时无法打开侧栏，会明确提示重试方式，任务文字不会因此丢失。

**先给计划、等你批准、再派工可以跨多个回合**，普通答复不会自动关闭许可。任务完成后由流程报告 `finished: true`、调用 `workflow_control finish`，或由你点击“结束本次协作”。需要立即停用新派工可切 `off`。不要把仍显示激活的上一任务当成已自动结束。

输入 **`/savvy-check`** 可直接检查当前宿主里的状态、历史列表与本地模型目录工具返回，不启动模型；它会区分工具返回协议是否正常与 Codex 前置条件是否就绪。工具大部分按需发现，只有 worker 的 step 工具常驻。

## 安装和前置检查

需要 Git、Python 3.10+、支持 `claude-code` Mod/function-hooks 的 Claude Code，以及已通过 **ChatGPT 登录**的官方 Codex CLI。Windows 的 Claude Code **2.1.314 与 2.1.317** 已运行原生 Hook 测试，并验证 Codex CLI **0.166.0** 的配置与功能；其他宿主版本会显示未验证或未知状态。普通网页 Chat / Cowork 不保证侧栏能力；macOS 的自动化测试与实际桌面交互分开验证，具体范围见 [macOS 修复记录](docs/MACOS_ISSUE_1.md)。

原生测试框架的 mock 不替生产宿主校验工具结果，本项目另外在原始返回边界检查契约：自注册工具返回 JSON 文本，内置工具拒绝返回 `deny`。测试会故意注入旧版错误对象，确认检查能发现它；测试数不能代替真实桌面对话中的复验。

先确认 `claude --version`、`codex --version`、`codex login status`。Windows 建议克隆到短路径（例如 `C:/src/savvy-codex`）；长路径可加 Git 参数：

```sh
git -c core.longpaths=true clone https://github.com/xmz0421/savvy-codex.git
cd savvy-codex
python scripts/install.py
```

`python` 可替换为已有解释器完整路径，手工命令也可使用 `py -3`。无需新虚拟环境或第三方 Python 依赖。

macOS 系统自带的 Python 3.9 不满足要求。用现有 Python 3.10+ 的完整路径运行安装器（例如 `/opt/homebrew/bin/python3.12 scripts/install.py`，以实际位置为准）。插件在没有显式 `pythonPath` 或 `SAVVY_CODEX_PYTHON` 设置时，会从校验通过的安装记录沿用该解释器；无有效记录才使用平台默认的 python/python3。已有显式设置优先，升级不会覆盖它们。

Codex 优先从 PATH 查找；macOS 还会检查 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` 和用户 `~/Applications/ChatGPT.app` 内同一位置。不需要为了插件修改 shell PATH；仍须是官方 CLI、ChatGPT 登录且支持所需能力。应用升级可能改变打包位置，找不到时按 doctor 提示处理。

安装器先检查自身 Python 版本，再运行不调用模型的 doctor，检查 CLI、所需能力、登录和账号模型目录；不兼容时给出原因并停止，不显示“已就绪”。新登录账号若尚无模型缓存，按 doctor 提示显式刷新目录后重试。`--skip-doctor` 仅用于明确的离线安装/测试，结果会标注未检查，不能等同可运行，也不能绕过 Python 最低版本要求。

安装到 `~/.claude/skills/savvy-codex`，仅修改相关插件开关，备份原内容；不更改服务地址、模型或凭据。原 Savvy Flow / Progress 会关闭以避免重复侧栏。重开 Code 会话加载更新。

旧版 `--enable-default-workflow` 已弃用：只做兼容迁移，不再追加全局自动指令。升级会清除本插件带标记的旧全局段落，并保留其他提示。想自动运行，请在插件设置选择 `auto`。

## 点开看工作内容

同一面板中，Claude 保留原小 Claude 形象，Codex 使用独立机器人形象。点任务的详情入口查看任务内容、实际模型/effort、状态、工具活动、可取得的结果和文件。文件预览与复制路径仍在同一面板中。

只显示宿主实际提供的信息；无法取得的 Claude 文件/输出会注明未知，不编造思考过程或完成状态。历史列表和工具日志可能含项目内容，请勿未经脱敏上传 issue。

## 派工与互审

负责人可以亲自干活。低风险、约 2 个文件 / 60 行以内的修改通常直接完成；复杂的一行安全/并发修改仍可能值得独立复核。先比较上下文转移、执行和验收成本，再拆任务。Codex 的同源项目任务使用独立执行副本与任务锁，可以同时运行；最终合并仍由负责人协调。

Codex 的模型和 effort 从账号实时可用目录选择，不在规则里固定某代模型。Claude 的五个旧 tier 名称仅为角色/effort 预设，默认继承当前模型，可按宿主支持的 Agent 参数指定当前可用模型。每次解释具体选择理由。

有实质行为变化的实现安排一次另一家的只读审查：Codex 写 → Claude 审，Claude 写 → Codex 审；用真实 `review_of` 关联任务。未执行的审查保持待完成，不能把负责人重读当独立审查。小的可逆修改可说明原因后跳过。审查不自动无限循环。

## 文件隔离和交付

任务包区分四类输入与输出，不再用写入授权换取读取上下文：

| 字段 | 用途 |
| --- | --- |
| `scope_paths` | 实施允许修改的源码文件/目录 |
| `context_paths` | 相关源码、规范、测试配置等只读输入 |
| `dependency_paths` | 显式复制的已有依赖，只读，拒绝链接及敏感路径 |
| `verification_output_paths` | 实施验证可新建的产物，排除在源码补丁外，不得覆盖已有输入 |

适用的 AGENTS.md / CLAUDE.md 作为只读上下文自动带入；当前脏文件内容照实复制。状态与详情显示实际清单、依赖数量及采样截断，不把大依赖列表全塞回模型上下文。依赖复制有磁盘与时间成本，需选必要路径；不自动安装新环境，也不通过可写链接暴露原依赖。

实施的变更和补丁留在副本，**不自动写回原项目**。程序检查源码范围、不可改输入和新验证产物，越界或改动只读输入就阻止交付。只读任务保持只读沙箱，可运行不落盘的检查；需要生成文件的验证交由负责人在已授权的验证环境执行，不会暗中提高复核权限。

实施若显式授权了新的验证产物目录，临时目录会设置到该目录的 `tmp` 下（TMP/TEMP/TMPDIR），避免标准测试又去系统临时目录写入；缓存也应配置在授权输出目录。只声明单个产物文件或只读任务时，不会自动增加临时目录写权限。

Claude worker 不再固定 worktree。只读 `savvy-review` 使用当前文件，不要求 Git、HEAD 或干净工作树；写任务只有在明确请求、Git 已提交且工作区干净时才用宿主 worktree。非 Git 或含未提交输入时，选择保留当前文件的 Codex 副本，或由负责人串行修改；不为隔离而重置、提交或隐藏用户改动。负责人检查候选补丁、另一家的审查和原文件是否变化，再合并。

Claude 子代理的模型覆盖与 worktree 选项以[宿主文档](https://code.claude.com/docs/en/sub-agents)和实际工具 schema 为准；已有 tier 不是某个固定模型的承诺。

这些是最小输入副本与交付范围控制，**不等于操作系统逐文件访问围栏**。外部路径/工具权限由 CLI 与宿主沙箱控制；不能承诺任何宿主上都完全无法读到外部文件。敏感目录拒绝规则仍是附加防护，不能代替任务允许清单。目录范围应尽量小，不把整个项目加入只为方便。

实施显式固定 workspace-write、禁用额外 writable roots 与直接网络，并读取实际生效策略。检测到不兼容的现代命名权限配置或无法证明策略符合要求时会阻止运行，不改写用户全局配置。CLI 静态能力有短时缓存，但每次仍核对实际权限；认证缓存只表示本地登录状态，不保证远端额度或令牌仍有效。策略依据见 [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

## 失败和记录维护

失败详情包含可取得的原因分类与下一步。默认不暗中重试、不换模型、不切 API 计费、不绕过权限；瞬时错误最多一次有记录的重试，超时则缩小任务或明确延时。额度/登录/版本问题先处理前置条件。每次新尝试关联原任务，保留原失败记录。

macOS 系统 Git 可能打印 `/tmp/xcrun_db-*` 或系统 `/var/folders/.../T/` 内的缓存权限诊断。只有明确成功（退出码 0）的单条 Git/xcrun 命令中的已知缓存行会作为“平台警告”保留在详情和报告；它本身不再将已完成任务判为 blocked。未知/失败退出、其他权限拒绝及无法归属命令的 stderr 仍会阻止交付，不会为消除警告增加沙箱写权限。

记录位于 `~/.claude/codex-worker-runs/`。清理按保留天数处理已结束任务，默认 30 天，先看 dry run；运行中、未知或不安全目录受保护。清理不调用模型，也不创建后台计划。具体命令见 [执行器说明](skills/codex-worker/references/execution.md)。

插件默认只显示 tokens 和用时。上游静态 API 价格估算默认关闭；显式开启 `priceEstimates` 后仍不是订阅账单，不能作为实时价格依据。

## 更新、卸载与验证

更新：`git pull` 后重新运行安装器。已有包内自定义修改会检测并备份、报告；持久偏好应放插件设置，别改 skill 源码。包和配置提交失败会回滚；提交成功后的旧目录清理失败只给出警告并保留恢复路径。安装器默认只保留最近 5 份带本插件成功标记的备份，可用 `--keep-backups N` 调整；失败、未知和链接备份不会自动裁剪。

卸载：

```sh
python scripts/install.py --uninstall
```

只撤销本插件有记录的修改，按仍受本插件管理的键还原原插件开关；保留安装后新增设置、其他提示、备份和任务记录。不会整份覆盖 settings.json。卸载后重开会话。

开发检查（无真实模型调用）：

```sh
python -m unittest discover -s skills/codex-worker/scripts -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
python scripts/sync_policy.py --check
claude plugin validate --json .
claude plugin test .
```

GitHub CI 检查 Windows / Ubuntu / macOS 的 Python 3.10、3.12，在 Ubuntu / macOS 安装固定官方 Claude Code 2.1.317 实际运行 `claude plugin test`，并用真实 Python 3.9 检查安装拒绝；共享 JSON 与生成 TS 的一致性也必须通过。CI 不包含账号或模型推理。具备 Codex CLI 的本机可设置 `SAVVY_CLI_INTEGRATION=1` 执行无模型的真实 CLI 配置测试。

已知宿主限制：预览 API 只提供解码文本，合法 U+FFFD 会保留，无法保证识别宿主静默替换的损坏字节；宿主无法安全解析符号链接时仍拒绝预览，macOS 的 `/var`、`/tmp` 别名请使用解析后的真实路径或用户目录。未提供的上下文窗口容量显示未知。旧记录缺少可信执行副本时只开放报告/JSON/补丁，不会把主项目当前文件当成 worker 的候选文件。

本轮逐项处理与验证边界见 [修复记录](docs/REPAIR_1_5.md)。反馈请附版本、复现步骤和脱敏错误。见 [NOTICE](NOTICE) 与 [LICENSE](LICENSE)。本社区项目与 Anthropic / OpenAI 无隶属关系。
