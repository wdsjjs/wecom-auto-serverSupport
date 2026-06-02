# cli-anything-wecom-gui

`cli-anything-wecom-gui` drives the WeCom desktop app on macOS for
customer-service workflows where no official external-contact send/receive API
is available.

It reads visible conversations through macOS Accessibility, drafts replies,
stores them in a local SQLite queue, and sends only through a locked,
rechecked GUI path.

## Install

From `wecom-gui`:

```bash
pip install -e .
```

This exposes:

```bash
cli-anything-wecom-gui
```

The repository's npm scripts are just shortcuts around the same Python module.

## Validation

Run these before any live sending:

```bash
cli-anything-wecom-gui doctor
cli-anything-wecom-gui app focus
cli-anything-wecom-gui inbox scan --limit 5
cli-anything-wecom-gui chat read --last 12
cli-anything-wecom-gui reply send --text "您好，我帮您看一下" --dry-run
```

If the app name is not detected:

```bash
export WECOM_GUI_APP_NAME=企业微信
```

## Agent Modes

The fast agent scans, reads, drafts concurrently, then sends through one GUI
lock.

Dry run:

```bash
python -u -m cli_anything.wecom_gui agent --mode dry-run --poll 0.5 --scan-interval 1 --inbox-limit 5 --max-drafts 4 --last 12 --log-interval 5
```

Review mode:

```bash
python -u -m cli_anything.wecom_gui agent --mode review --poll 0.5 --scan-interval 1 --inbox-limit 5 --max-drafts 4 --last 12 --log-interval 5
```

Auto mode:

```bash
python -u -m cli_anything.wecom_gui agent --mode auto --poll 0.5 --scan-interval 1 --inbox-limit 5 --max-drafts 4 --last 12 --log-interval 5
```

Modes:

- `dry-run`: generate drafts and mark work complete without sending.
- `review`: generate drafts, wait for web approval, then recheck and send.
- `auto`: recheck and send ready drafts automatically.

## Review Page

Start the review server:

```bash
python -m cli_anything.wecom_gui review --host 0.0.0.0 --port 8122
```

Or from `wecom-gui`:

```bash
npm run review
```

Or as a background screen session:

```bash
./scripts/wecom-agent review-start
./scripts/wecom-agent review-status
./scripts/wecom-agent review-logs
```

The server prints a URL with a token:

```text
http://192.168.x.x:8122/
```

The browser can list pending drafts, approve them, or reject them. Approval
only moves a queue item from `ready` to `approved`; the agent still performs the
send-time context check before touching WeCom.

Review API:

```text
GET  /api/review/items?status=ready
GET  /api/review/counts
POST /api/review/items/{id}/save
POST /api/review/items/{id}/regenerate
POST /api/review/items/{id}/approve
POST /api/review/items/{id}/reject
```

The review page reads live queue items from the local SQLite state database
under `~/.cli-anything-wecom-gui/state.sqlite`; it does not serve mock items.

SQLite schema notes:

- `reply_queue` stores `handoff_type` and `handoff_reason` so direct customer
  handoff requests and AI-decided handoffs can share the review queue.
- `conversation_messages` stores stable `message_type` values
  (`customer`, `reply`, or `unknown`) and image metadata in `media_json`.
  Missing columns are added automatically by schema initialization.

## Queue States

```text
pending       scanned and waiting to be opened
reading       agent is opening/reading the chat
drafting      AI draft is in flight
ready         draft is ready; in review mode this means waiting for approval
approved      reviewer approved the draft; agent may send after recheck
sending       agent is reopening/rechecking/sending
done          finished
skipped       intentionally not sent
failed        error while reading, drafting, or sending
```

Useful queue commands:

```bash
cli-anything-wecom-gui queue list --limit 20
cli-anything-wecom-gui queue list --status ready
cli-anything-wecom-gui queue list --status approved
cli-anything-wecom-gui queue clear --status done
```

## Drafting Providers

Provider selection is controlled by `WECOM_GUI_AI_PROVIDER`.

Common values:

- `pi`: local Pi coding-agent provider configured by the installer.
- `uda`: UDA single-question API.
- `codex`: Codex CLI based drafting.
- `openai`: OpenAI-compatible HTTP endpoint.
- `fallback`: safe static fallback for calibration.

UDA format:

```json
{
  "history": [
    {"type": "human", "data": {"content": "用户: 鱼油含量"}},
    {"type": "human", "data": {"content": "客服: 请问是哪款鱼油？"}}
  ],
  "ai_reply": true
}
```

The UDA response is read from `data.message` or top-level `message`.

## Filtering Defaults

By default the agent ignores:

- rows without `@微信`;
- rows without unread badges;
- external groups;
- system or department rows;
- very long technical text;
- token/header/API-key-like text.

Important switches:

```env
WECOM_GUI_REQUIRE_WECHAT_TAG=1
WECOM_GUI_REQUIRE_UNREAD=1
WECOM_GUI_ALLOW_UNTAGGED=0
WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=0
```

Keep these defaults for production. Relax them only while calibrating a target
WeCom build.

## npm Shortcuts

From `wecom-gui`:

```bash
npm run doctor
npm run dev:dry
./scripts/wecom-agent start
./scripts/wecom-agent review-start
npm run queue
npm run agent:start
npm run agent:stop
npm run agent:logs
```

`npm run dev` starts `agent --mode auto` and is intended only for explicit auto
mode testing. For review-gated production, prefer:

```bash
WECOM_AGENT_MODE=review ./scripts/wecom-agent start
./scripts/wecom-agent review-start
```

## Tests

```bash
python -m pytest -q cli_anything/wecom_gui/tests
```

Unit tests monkeypatch GUI backends and should not click the real desktop.
Manual GUI validation still requires WeCom to be open and visible.
