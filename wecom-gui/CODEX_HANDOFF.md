# Codex 接手说明：WeCom GUI 自动客服 Demo

## 你要先知道什么

这是一个 CLI-Anything harness，位置通常是：

```bash
wecom-gui/agent-harness
```

目标是通过 macOS GUI 自动化企业微信桌面端，对外部联系人消息做 AI 自动回复。它不是官方企业微信 API；底层依赖 `osascript`、System Events、macOS Accessibility、剪贴板粘贴和本地 SQLite 队列。

不要把它当成普通服务端机器人。它会真实点击桌面、读取可见 UI、粘贴并发送消息。

## 当前运行入口

常用命令：

```bash
npm run doctor
npm run dev:dry
npm run dev
npm run queue
```

等价 Python 入口：

```bash
python -m cli_anything.wecom_gui --json doctor
python -m cli_anything.wecom_gui --json inbox scan --limit 5
python -m cli_anything.wecom_gui --json chat read --last 12
python -m cli_anything.wecom_gui agent --mode dry-run --poll 0.5 --scan-interval 1 --inbox-limit 5 --max-drafts 4 --last 12 --log-interval 5
python -m cli_anything.wecom_gui agent --mode auto --poll 0.5 --scan-interval 1 --inbox-limit 5 --max-drafts 4 --last 12 --log-interval 5
```

真实发送前，先跑 `dev:dry`。

## 本地配置

`.env.local` 不应提交或打包，接手者应从 `.env.example` 复制：

```bash
cp .env.example .env.local
chmod 600 .env.local
```

必须配置：

```text
WECOM_GUI_UDA_API_KEY=...
WECOM_GUI_UDA_URL=https://wework-unified-api.uda.cn/chat/single_question
WECOM_GUI_APP_NAME=企业微信
```

如果接手机器没有原作者的 Python 路径，设置：

```text
WECOM_GUI_PYTHON=/absolute/path/to/python3
WECOM_GUI_PYTHONPATH=/absolute/path/to/deps
```

## 主流程代码

主循环在：

```text
cli_anything/wecom_gui/core/agent.py
```

流程是：

1. `worker.scan_once()` 扫描左侧会话。
2. `inbox.scan_visible()` 从 Accessibility 读取会话结构。
3. `watcher._should_consider()` 和 `worker._has_unread()` 过滤候选。
4. `state.enqueue_conversation()` 写入本地 SQLite。
5. `state.claim_pending_for_read()` 领取待读取会话。
6. `inbox.open_by_name()` 打开会话。
7. `chat.read_current()` 读取最近消息并做角色识别。
8. `llm.draft_reply()` 调 UDA 生成回复，在线程池中并发。
9. `state.mark_ready()` 标记待发送。
10. `state.claim_ready_to_send()` 领取待发送任务。
11. 再次打开会话并读取最新消息。
12. 如果最新客户消息没变，`reply.send_text()` 粘贴并回车发送。
13. 如果上下文变了，标记 skipped，避免发送过期回复。

GUI 操作用 `state.gui_lock()` 串行化，AI 请求在锁外并发。

## UDA 调用格式

在 `core/llm.py`。

发送给 UDA 的格式：

```json
{
  "history": [
    {
      "type": "human",
      "data": {
        "content": "用户: 鱼油含量"
      }
    }
  ],
  "ai_reply": true
}
```

注意：`type` 固定是 `human`；角色写进 `content`，格式是：

```text
${msg.role}: ${msg.content}
```

最终发给用户的是 UDA 响应里的：

```text
res.data.message
```

## 默认过滤策略

默认只处理：

- 企业微信左侧可见会话。
- 带 `@微信` 标签的外部联系人。
- 有 `unread_count > 0` 或 unread 标识的会话。
- 不是企业微信团队、行业资讯、客户联系这类系统入口。
- 不是 token/header/API key 等技术文本。
- 不是外部群，除非配置允许。

相关环境变量：

```text
WECOM_GUI_REQUIRE_WECHAT_TAG=1
WECOM_GUI_REQUIRE_UNREAD=1
WECOM_GUI_ALLOW_UNTAGGED=0
WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=0
```

## 关键文件地图

```text
package.json
setup.py
WECOM_GUI.md
HANDOFF.md
CODEX_HANDOFF.md
.env.example
cli_anything/wecom_gui/wecom_gui_cli.py
cli_anything/wecom_gui/core/agent.py
cli_anything/wecom_gui/core/worker.py
cli_anything/wecom_gui/core/inbox.py
cli_anything/wecom_gui/core/chat.py
cli_anything/wecom_gui/core/llm.py
cli_anything/wecom_gui/core/reply.py
cli_anything/wecom_gui/core/state.py
cli_anything/wecom_gui/core/watcher.py
cli_anything/wecom_gui/utils/macos_backend.py
cli_anything/wecom_gui/tests/test_core.py
cli_anything/wecom_gui/tests/test_full_e2e.py
```

## 验证顺序

接手后按这个顺序验证：

```bash
npm run doctor
python -m pytest -q cli_anything/wecom_gui/tests
python -m cli_anything.wecom_gui --json inbox scan --limit 5
python -m cli_anything.wecom_gui --json chat read --last 12
npm run dev:dry
npm run dev
```

如果要真实跑 `npm run dev`，必须确认接手者已经同意：

- 允许 GUI 自动点击企业微信。
- 允许把聊天内容发送到 UDA 接口。
- 允许自动发送 AI 回复给客户。

## 常见问题

`osascript 不允许辅助访问 (-25211)`：

给 Terminal/iTerm/Codex 开辅助功能权限，重启终端后再跑 `npm run doctor`。

扫描慢：

优先降低 `--inbox-limit`，例如 5。当前 npm 脚本默认 5。

识别不到新消息：

先看 `inbox scan` 是否有 `unread_count`；如果没有，检查企业微信左侧是否可见、会话是否真的有未读红点。

AI 有回复但没发送：

看日志是否出现“客户又发了新消息，旧回复作废”。这是设计行为。

发送到错误窗口：

立即停止，回到 dry-run，检查 `chat read` 和 `inbox open` 的定位。GUI 自动化依赖当前窗口结构，企业微信更新后要重新校准。

## 开发约束

修改代码时保持这些原则：

- 不要硬编码 API Key。
- 不要移除 dry-run 和发送前复核。
- 不要让多个线程同时点击 GUI；GUI 操作必须经过 `state.gui_lock()`。
- 不要默认处理无 `@微信` 标签的会话。
- 不要默认处理外部群。
- 不要在测试里依赖真实企业微信窗口；单元测试用 monkeypatch。
- 新增真实 GUI 测试时必须显式说明会点击桌面。

## 打包建议

接手包只需要 `agent-harness/` 目录。

打包前排除：

```text
.env.local
__pycache__/
.pytest_cache/
*.pyc
*.egg-info/
dist/
build/
```

状态库和历史日志在接手机器的：

```text
~/.cli-anything-wecom-gui/
```

这部分不要作为 demo 代码包交接，除非明确需要排查历史运行记录。
