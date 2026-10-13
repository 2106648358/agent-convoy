# Agent Convoy

Agent Convoy 是一个本地优先的会话迁移与历史浏览工具，提供 CLI 和只监听本机的 HTML 历史中心。它支持 Codex CLI、Claude Code、WorkBuddy 三端之间的六条迁移方向，并提供可靠的 Markdown 降级导出。

macOS 用户可以双击项目目录中的 `启动 Agent Convoy.command`，或点击程序坞的 Agent Convoy 图标启动 HTML 历史中心。应用 logo 位于 `assets/agent-convoy-logo.png`。

原生 Dock 启动器会启动一个本机服务，并使用默认浏览器打开页面。关闭浏览器页面后再次点击 Dock 图标，会重新打开已有服务；用应用菜单或 Command-Q 退出 Agent Convoy 时会同时关闭该应用启动的服务。启动失败时会弹出提示，日志位于 `~/Library/Logs/AgentConvoy/launcher.log`。

从 GitHub 下载源码后，可双击 `desktop/build.command` 在当前 Mac 上生成 `Agent Convoy.app`（需要 Xcode Command Line Tools）。应用需保持在项目目录旁，再把它拖进 Dock。无需配置机器专用绝对路径。

它不是“把历史拼成一个 Prompt”。中间层先解析源端的原生历史，归一到 `agent-convoy/v1`，再由目标 adapter 创建目标端能枚举和恢复的原生会话。

## 本机验证结论

本项目是在以下真实本机数据上完成结构验证的，但不会把会话正文、凭据或令牌写入报告：

| 端 | 本机版本/格式 | 读取 | 写入策略 | 状态 |
|---|---|---:|---|---|
| Codex CLI | `0.162.0-alpha.17.2`；日期目录下 rollout JSONL + `state_5.sqlite`/`thread_history_1.sqlite` | 是 | Claude 源调用 `externalAgentConfig/import`；其他源调用 `thread/start` + `thread/inject_items`；不直接写 SQLite | Claude→Codex 的原生导入已有本机 21 条历史记录；其他源可按 ID 恢复，但尚不能立即出现在原生历史列表 |
| Claude Code | 本机历史记录版本 `2.1.236`；`~/.claude/projects/<project>/<session>.jsonl` | 是 | 只创建新的项目 JSONL | 结构已由真实记录验证；本机当前无 `claude` 可执行文件，因此“打开并继续”尚未在本机做最终运行验证 |
| WorkBuddy | App `5.7.6`，内置 CodeBuddy CLI `2.156.0` | 是 | 当前 CLI：新建 `~/.workbuddy/projects/.../*.jsonl` | 当前目标写入为实验性；旧 CodeBuddyExtension 历史可读、不可写 |

本机还存在旧版 WorkBuddy/CodeBuddyExtension 数据，位置为 `~/Library/Application Support/CodeBuddyExtension/Data/.../history`。Agent Convoy 可以把这些历史迁出，但不会修改旧版的多文件索引，因为没有可靠官方写入能力。

## 安装与运行

无需第三方依赖，Python 3.9+ 即可：

```bash
cd agent-convoy
./agent-convoy doctor
./agent-convoy list claude
./agent-convoy preview claude <session-id>
```

也可以安装成命令：

```bash
python3 -m pip install -e .
agent-convoy doctor
```

### HTML 历史中心

```bash
./agent-convoy ui
```

服务仅监听 `127.0.0.1`，启动时生成随机访问令牌并自动打开浏览器。若不希望自动打开：

```bash
./agent-convoy ui --no-open
```

页面提供：

- 三端会话列表、搜索、日期筛选和安全预览；
- 完整迁移的 dry-run、确认应用、ledger 与回滚；
- 用户指定目录的 Markdown 导出；
- macOS 系统文件夹选择器和“在 Finder 中显示”。

HTML 工具是独立历史中心，不伪装成 WorkBuddy 原生历史列表。

### Markdown 降级导出

```bash
./agent-convoy export claude <session-id> --output-dir "/用户选择的目录"
./agent-convoy export workbuddy <session-id> --output-dir "/用户选择的目录" --reveal
```

Markdown 包含 YAML 元数据、用户/助手正文及明确的工具结果；默认省略思考过程、工具调用参数和高置信度凭据值。文件使用原子写入和 `0600` 权限，相同内容在同一目录重复导出时复用已有文件。

### 迁移

所有迁移默认是 dry-run：

```bash
agent-convoy migrate claude codex <session-id>
agent-convoy migrate workbuddy claude <session-id>
agent-convoy migrate codex workbuddy <session-id>
```

确认预览无误后显式应用：

```bash
agent-convoy migrate claude codex <session-id> --apply
```

省略 `<session-id>` 时会在交互终端显示编号列表供选择。可用 `list --query`、`--since`、`--limit` 筛选：

```bash
agent-convoy list workbuddy --query parser --since 2026-09-01
```

### 回滚

成功导入会返回 `import_id`，也会写入权限为仅当前用户可读写的 `~/.agent-convoy/ledger.json`：

```bash
agent-convoy rollback <import-id>
agent-convoy rollback <import-id> --apply
```

- Claude Code / WorkBuddy：删除本次新建的 transcript 文件。
- Codex：调用 app-server 的原生 `thread/delete`，不手工改数据库。
- 来源文件永不删除或覆盖。

每次导入会在 `~/.agent-convoy/backups/<import-id>/` 保存新建 transcript 的恢复副本；ledger 更新前也会自动备份上一版 ledger。

## 安全约束

- dry-run 是默认行为；只有 `--apply` 才写入。
- 不直接写 Codex SQLite，也不写 WorkBuddy 旧版索引。
- 目标根目录必须由当前用户拥有，不能是符号链接。
- 目标 transcript 用原子替换写入，权限为 `0600`。
- 内容哈希用于重复导入检测。
- 日志只记录端、计数、状态和耗时；不记录正文、工具参数、令牌或凭据。
- 回滚只允许删除 ledger 明确记录、且位于受管根目录内的文件。

## 六条方向与能力边界

| 方向 | 可用性 | 说明 |
|---|---|---|
| Claude → Codex | 可用，Codex 原生通道 | 默认 `~/.claude/projects` 下尚未导入的会话走 Codex 内建 importer；本机已有 21 条原生列表/索引记录 |
| WorkBuddy → Codex | 实验性、部分可用 | 已实测创建原生 thread 且 `codex resume <id>` 能加载；注入项目前只进入 model-visible history，尚不会生成列表所需的可见 turn/preview，因此不会立即出现在 Codex 原生列表 |
| Codex → Claude | 实验性 | 新建 Claude 项目 transcript；需在装有 Claude Code 的环境执行 `claude --resume <id>` 做最终确认 |
| WorkBuddy → Claude | 实验性 | 同上 |
| Codex → WorkBuddy | 实验性 | 新建 WorkBuddy 项目 transcript；当前 App 包缺少交互 TUI bundle，建议通过 HTML 历史中心或 Markdown 降级查看 |
| Claude → WorkBuddy | 实验性 | 同上 |

“实验性”并不表示会直接修改活跃数据库；它表示目标端没有公开、稳定的第三方写入 API，格式或协议可能随版本变化。CLI 会先由 `doctor` 报告检测到的版本和路径，版本变化后应先 dry-run 并用一条非关键会话验证。WorkBuddy→Codex 若要求“导入后立刻出现在历史列表”，当前版本会诚实报告这一阻塞，不把“可按 ID 恢复”冒充“列表可见”。

## 验证目标端

导入后可用目标 CLI 验证原生恢复：

```bash
codex resume <target-id>
claude --resume <target-id>
```

WorkBuddy AI 5.7.6 随 App 附带的脚本缺少交互模式所需的 `dist/codebuddy.js`，不能直接使用 `codebuddy --resume <id>` 打开 TUI。它的 headless bundle 只能在提供新提示时执行单次恢复请求，因此不作为只读验收方式。请优先使用 Agent Convoy HTML 历史中心或 Markdown 导出查看历史。

若目标端版本不兼容，运行 `rollback <import-id> --apply`。对 `codex-native-resumable-by-id-experimental` 状态，必须保存返回的 target ID；在首次真实 turn 产生前，它不会出现在 Codex 历史列表。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

13 项测试覆盖三种 source adapter、六条方向 dry-run、Markdown 导出与去重、敏感内容过滤、ledger v1→v2、HTTP capability token、Origin、请求大小、两阶段迁移/回滚、Finder 白名单和路径越界保护。Codex app-server 的真实写入不会在单元测试中执行，以免测试污染用户历史；它只会在显式 `--apply` 时调用。

浏览器视觉与真实 Finder 弹窗验收按约定交由用户执行，自动测试不会启动浏览器或弹出系统窗口。

