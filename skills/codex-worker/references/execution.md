# 执行器与维护

优先使用已激活流程的原生工具；直接脚本模式只用于明确接受无侧栏的用户，不能用来绕过 off/manual 门禁。

脚本在 `~/.claude/skills/savvy-codex/skills/codex-worker/scripts/codex_delegate.py`。Python 3.10+，标准库即可。原生设置 `pythonPath` 是单个解释器路径或名称（支持空格，不加引号或 `-3` 参数），其次看 SAVVY_CODEX_PYTHON；Windows 默认 python，其他系统 python3。pluginRoot/helperPath 可覆盖安装路径，对应 SAVVY_CODEX_PLUGIN_ROOT / SAVVY_CODEX_HELPER；默认 CLAUDE_PLUGIN_ROOT 或个人 skills-dir。

在仓库目录内：

```sh
python skills/codex-worker/scripts/codex_delegate.py doctor
python skills/codex-worker/scripts/codex_delegate.py catalog --cached
python skills/codex-worker/scripts/codex_delegate.py catalog --refresh
python skills/codex-worker/scripts/codex_delegate.py cleanup --workspace /absolute/project/path --days 30 --dry-run
python skills/codex-worker/scripts/codex_delegate.py cleanup --workspace /absolute/project/path --days 30
```

doctor/cached catalog 不发起推理；refresh 仅显式刷新账号模型目录。CLI 参数/登录不兼容时，按错误提示更新官方 CLI 或重新登录；不复制认证令牌，不改 API 计费来源。

run/start 需要 --workspace、--packet、--model、--effort，模型/effort 必须来自当前 catalog；只读用 --permission read-only --approval never，已授权实施用 --permission workspace-write --approval auto-review。写入授权在任务包中声明。所有任务在独立输入副本执行，实际交付通过 scope 检查后留在记录目录；负责人核验补丁后整合。

实施任务包示例（按真实项目替换路径与验证要求）：

```json
{
  "version": 1,
  "title": "修复解析边界",
  "plan_task_id": "task-1",
  "goal": "按已确认的复现修复解析函数，保留其他行为",
  "kind": "implement",
  "scope_paths": ["src/parser.py"],
  "context_paths": ["tests/test_parser.py", "pyproject.toml", "docs/parser-spec.md"],
  "dependency_paths": ["vendor/parser_support"],
  "verification_output_paths": [".verification/"],
  "authorization": {"write_intent": true},
  "acceptance": ["既有边界和复现案例通过", "上下文文件与依赖不变"],
  "tests": ["使用项目指定的已有解释器运行 tests/test_parser.py；临时产物限于 .verification/"],
  "reason": "已知原因的局部修复，按账号目录选择可用编码模型与适度思考"
}
```

没有某项输入时删掉对应声明，不填不存在的依赖目录。显式只读路径不得与写入范围重叠，目录尾 `/` 表示目录；绝对路径与 `..` 别名会先规范化再检查。程序自动附带适用的 AGENTS.md / CLAUDE.md，只读输入发生变化会阻止交付。验证输出只允许新位置，不进入源码补丁。只读 review/inspect/plan 不因要跑测试而自动升权；必需落盘的验证由负责人安排已授权环境。

短时预检缓存省去重复的 CLI 静态功能查询；每次仍读取实际沙箱策略。Windows 状态读取允许删除共享，写入使用有界重试与原子替换，进度写入暂时冲突不会杀掉模型进程。不要把状态文件不见或写入警告当成模型已经完成。

start 返回任务编号，status 读真实状态，wait 用程序等待，不应让模型循环查询。失败保持原记录；后续尝试记录 parent_job、attempt，独立审查记录 review_of 与 author_provider。详细字段与参数以脚本 --help 和工具 schema 为准。

Claude 中可以运行 `/savvy-check`，它通过当前宿主实际调用注册的 status/catalog 工具并检查 JSON 文本返回，不派工也不调用模型。主工具按需发现。阶段计划请使用稳定 plan_task_id 和短 title；goal 保留完整任务，不靠整句目标匹配短标题。

cleanup 只处理指定项目过期且已终结的记录，默认保留 30 天；活动/未知/越界或链接路径受保护。Windows 路径可用 `C:/projects/example`。先 dry-run 核对待清理记录，必要结果另存后再删除。原生工具 `codex_cleanup` 需要 workspace，dry_run 默认 true；实际删除要明确传 false。清理属于本机文件维护，不调用模型。不要手动删除仍在运行的目录。
