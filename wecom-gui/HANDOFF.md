# 企微 GUI 自动客服 Demo 交接文档

## 交接范围

打包目录是：

```bash
/path/to/uda-codex-wecome/wecom-gui
```

这个目录是完整 demo，包括 CLI 代码、测试、npm 启动脚本、运行说明、Codex 交接说明。不要只打包 `cli_anything/`，否则接收方会缺少 `package.json`、`setup.py`、`.env.example` 和 SOP 文档。

建议打包时排除这些本地/缓存文件：

```text
.env.local
__pycache__/
.pytest_cache/
*.pyc
*.egg-info/
dist/
build/
```

`.env.local` 里有 UDA API Key，必须由接收方自己创建，不能一起发出去。

## Demo 能力

这个项目通过 macOS Accessibility + AppleScript 操作企业微信桌面端，在没有官方外部联系人收发接口的情况下，实现一个 GUI 自动客服流程。

核心流程：

1. 扫描企业微信左侧会话列表。
2. 只保留带 `@微信` 标签的外部联系人会话。
3. 默认只处理有未读标识的会话。
4. 打开会话，读取最近 N 条聊天记录。
5. 将聊天历史转换成 UDA 接口需要的 `history` 数组。
6. 并发调用 AI 生成回复。
7. 回复回来后重新打开会话，复核最新客户消息没有变化。
8. 真实发送或 dry-run 打印。
9. 本地 SQLite 队列记录 `pending / reading / drafting / ready / sending / done / skipped / failed` 状态。

## 目标环境

当前 demo 主要支持 macOS。

接收方需要：

- macOS。
- 已安装企业微信桌面端，并登录目标客服账号。
- 企业微信窗口不能完全退出；最小化/被遮挡时自动化稳定性会下降。
- Terminal、iTerm、Codex 或实际启动脚本的 App 需要开启辅助功能权限。
- Python 3.10+。
- Python 依赖：`click`、`requests`，测试需要 `pytest`。
- Node/npm 只用于 `npm run dev` 这种启动快捷方式，不跑前端。

Windows 目前没有完整适配。Windows 需要另做 UI Automation 后端，不能直接复用 `osascript`。

## 第一次部署

进入打包后的目录：

```bash
cd agent-harness
```

创建本地配置：

```bash
cp .env.example .env.local
chmod 600 .env.local
```

编辑 `.env.local`：

```text
WECOM_GUI_UDA_API_KEY=真实 key
WECOM_GUI_UDA_URL=https://wework-unified-api.uda.cn/chat/single_question
WECOM_GUI_APP_NAME=企业微信
```

如果接收方没有你这台机器上的 Codex Python 路径，需要在 `.env.local` 增加：

```text
WECOM_GUI_PYTHON=/absolute/path/to/python3
WECOM_GUI_PYTHONPATH=/absolute/path/to/deps
```

也可以用虚拟环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
pip install pytest
```

然后在 `.env.local` 写：

```text
WECOM_GUI_PYTHON=.venv/bin/python
WECOM_GUI_PYTHONPATH=
```

## 权限检查

先打开企业微信，再运行：

```bash
npm run doctor
```

正常结果里应看到：

```json
{
  "ok": true,
  "app_running": true,
  "accessibility_ok": true,
  "osascript_ok": true
}
```

如果遇到：

```text
“System Events”遇到一个错误：“osascript”不允许辅助访问。 (-25211)
```

处理方式：

1. 打开系统设置。
2. 进入“隐私与安全性”。
3. 进入“辅助功能”。
4. 给启动命令的 App 授权，例如 Terminal、iTerm、Codex。
5. 退出并重新打开终端后再试。
6. 如果系统明确提示 `osascript`，也尝试把 `/usr/bin/osascript` 加入辅助功能授权。

## 建议验收顺序

先做只读验证：

```bash
npm run doctor
```

扫描左侧会话：

```bash
python -m cli_anything.wecom_gui --json inbox scan --limit 5
```

确认返回的会话里有：

```json
"tags": ["@微信"]
```

打开一个会话后读取聊天：

```bash
python -m cli_anything.wecom_gui --json chat read --last 12
```

先 dry-run：

```bash
npm run dev:dry
```

真实发送：

```bash
npm run dev
```

停止：

```bash
Ctrl-C
```

如需确认没有残留进程：

```bash
pgrep -fl "cli_anything.wecom_gui agent"
```

## 关键配置项

```text
WECOM_GUI_REQUIRE_WECHAT_TAG=1
```

只处理带 `@微信` 的外部联系人会话。建议保持开启。

```text
WECOM_GUI_REQUIRE_UNREAD=1
```

只处理未读会话。建议保持开启。

```text
WECOM_GUI_ALLOW_UNTAGGED=0
```

是否允许没有 `@微信` 标签的会话进入候选。默认关闭。

```text
WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=0
```

是否允许外部群。默认关闭。

## 重要注意事项

- 这是 GUI 自动化，不是企业微信官方接口。窗口结构、系统权限、企业微信版本变化都会影响稳定性。
- 启动真实发送前，必须先跑 `dev:dry` 验证读取、角色识别、AI 回复都正确。
- 真实发送会把聊天内容发送给 UDA 接口，接收方需要确认业务授权和隐私合规。
- 自动化会操作鼠标/焦点，不建议在同一台机器上同时人工操作企业微信。
- 当前策略是多客户并发生成 AI 回复，但 GUI 点击/读取/发送通过本地锁串行执行，避免多个流程互相抢窗口。
- 发送前会复核最新客户消息。如果客户又发了新消息，旧回复会跳过，不会发送过期答案。
- 本地状态和审计日志在 `~/.cli-anything-wecom-gui/`，不在打包目录里。
- 如果要重新开始测试，可以清队列，但不要随手删 `.env.local`。

## 常用排障

查看队列：

```bash
npm run queue
```

清空队列：

```bash
python -m cli_anything.wecom_gui --json queue clear
```

只扫描不点击：

```bash
python -m cli_anything.wecom_gui --json inbox scan --limit 5
```

读取当前聊天：

```bash
python -m cli_anything.wecom_gui --json chat read --last 12
```

运行测试：

```bash
python -m pytest -q cli_anything/wecom_gui/tests
```

## 代码入口

- `package.json`：`npm run dev`、`npm run dev:dry`、`npm run doctor`。
- `cli_anything/wecom_gui/wecom_gui_cli.py`：Click CLI 入口。
- `cli_anything/wecom_gui/core/agent.py`：主自动客服循环。
- `cli_anything/wecom_gui/core/worker.py`：左侧会话扫描入队。
- `cli_anything/wecom_gui/core/inbox.py`：会话过滤和结构化。
- `cli_anything/wecom_gui/core/chat.py`：当前聊天读取和角色识别。
- `cli_anything/wecom_gui/core/llm.py`：UDA/OpenAI/fallback 回复生成。
- `cli_anything/wecom_gui/core/reply.py`：粘贴发送。
- `cli_anything/wecom_gui/core/state.py`：SQLite 队列、锁、审计。
- `cli_anything/wecom_gui/utils/macos_backend.py`：macOS Accessibility / AppleScript 后端。
