# 本机会话格式与加载方式报告

检查日期：2026-10-13（Asia/Shanghai）

检查严格只读；报告只记录路径模式、字段名、版本和计数，不包含会话正文、工具参数、凭据或令牌。

## Codex CLI

- 可执行文件：ChatGPT.app 内置 Codex CLI，版本 `0.162.0-alpha.17.2`。
- transcript：`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`。
- 主要 JSONL 记录：`session_meta`、`response_item/message`、`event_msg`、`turn_context`、`world_state`、`token_usage_record`。
- 列表索引：`~/.codex/state_5.sqlite` 的 `threads` 表。
- 分页历史：`~/.codex/thread_history_1.sqlite` 的 `thread_turns`、`thread_items`、`thread_history_projection_state`。
- 恢复入口：`codex resume [SESSION_ID]`。
- 官方本地导入协议：app-server 的 `externalAgentConfig/detect` 与 `externalAgentConfig/import`，其中 `SESSIONS` 项接收 `{cwd,path,title}`。
- 本机 `external_agent_session_imports.json` 有 21 条 Claude Code 导入记录，21 个目标 rollout 均存在并进入 Codex 索引。

结论：Claude→Codex 走内建 importer。其他来源可通过 app-server 创建持久 thread 并注入 model-visible history；本机实测可按 ID `resume`，但注入不会生成可见 turn/preview，因此尚不出现在历史列表。直接写活跃 SQLite 不可接受，CLI 不会为了补列表可见性而这样做。

## Claude Code

- 本机 CLI 当前不在 PATH，但真实 transcript 中记录的版本为 `2.1.236`。
- transcript：`~/.claude/projects/<project-slug>/<session-id>.jsonl`。
- 会话消息以 `user` / `assistant` 记录构成，通过 `uuid` / `parentUuid` 成链。
- 重要字段：`sessionId`、`cwd`、`timestamp`、`message.role`、`message.content`、`version`。
- 标题记录：`type=ai-title`、`aiTitle`。
- 恢复入口通常为 `claude --resume <session-id>`。

结论：读取已由真实数据验证；写入仅新建 transcript，目标版本实际恢复仍需在重新安装/启用 Claude Code 后确认，因此标为实验性。

## WorkBuddy / CodeBuddy

- WorkBuddy AI App：`5.7.6`。
- 内置 CodeBuddy CLI：`2.156.0`，支持 `--resume [sessionId]` 与 `--continue`。
- 当前 CLI 源码会扫描 home projects 目录下的 `*.jsonl` transcript；CLI 配置根默认为 `~/.workbuddy`。
- App 包只包含 `codebuddy-headless.js` 与 `codebuddy-lite-wb.mjs`，缺少交互恢复所需的 `dist/codebuddy.js`；直接执行 `codebuddy --resume <id>` 会报 `MODULE_NOT_FOUND`。
- 本机当前没有 `~/.workbuddy/projects` transcript。
- 本机有旧版 CodeBuddyExtension 历史：`~/Library/Application Support/CodeBuddyExtension/Data/<account>/CodeBuddyIDE/<account>/history/<workspace>/<conversation>/`。
- 旧版根索引 `history/<workspace>/index.json` 含 `current`、`conversations[]`。
- 旧版会话索引含 `messages[]` 与 `requests[]`，消息正文位于 `messages/<message-id>.json`。

结论：旧版数据可安全读出；旧版没有发现可靠公开写入 API，所以不写旧索引。目标 WorkBuddy 使用当前 CLI 的项目 JSONL，并明确标记实验性。只读查看与图形化操作由 Agent Convoy 本地 HTML 历史中心承担，Markdown 作为可靠降级文件。

## HTML 历史中心与 Markdown

- `agent-convoy ui` 仅监听 `127.0.0.1`，使用随机 capability token 和同源 POST 校验。
- 页面调用与 CLI 相同的 adapter、迁移、导出和回滚函数，不启动第二套转换逻辑。
- Markdown 使用 `agent-convoy/markdown-v1` front matter，默认省略思考过程、工具调用参数与高置信度凭据值。
- Finder 定位只接受 ledger v2 中已登记、仍存在且归当前用户所有的导出文件。

