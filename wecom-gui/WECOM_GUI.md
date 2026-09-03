# WeCom GUI Agent SOP

This SOP covers the macOS desktop automation agent in `wecom-gui`.

The agent is intentionally conservative. It does not use an official WeCom
messaging API; it operates the visible desktop app through Accessibility,
AppleScript, clipboard paste, and a local SQLite queue.

## Operating Modes

Use the modes in this order:

1. `dry-run`: read chats and generate drafts without sending.
2. `review`: require a human to approve each draft in the LAN review page.
3. `auto`: send drafts automatically after the send-time context check.

Recommended production default:

```bash
WECOM_AGENT_MODE=review ./scripts/wecom-agent start
./scripts/wecom-agent review-start
```

Use `auto` only after the business has explicitly approved fully automatic
responses.

## Safety Invariants

- GUI operations are serialized by `state.gui_lock()`.
- AI calls run outside the GUI lock.
- Sending always reopens the target chat and checks that the latest customer
  message still matches the message used for drafting.
- If the customer sent a newer message, the draft is marked `skipped`.
- `review` approval never sends directly from the browser.
- The default scanner requires `@微信` and unread status.
- External groups and system/department rows are ignored by default.
- Local queue and event logs are audit data under
  `~/.cli-anything-wecom-gui/`.

## Main Flow

```text
scan visible inbox rows
  -> filter external-contact customer candidates
  -> enqueue changed conversations
  -> open one pending chat under GUI lock
  -> read recent visible messages and latest customer turn
  -> draft reply concurrently
  -> mark queue item ready
  -> auto mode: claim ready
     review mode: wait for web approval, then claim approved
  -> reopen chat under GUI lock
  -> recheck latest customer message
  -> paste and submit reply
  -> verify reply is visible
  -> mark done
```

Queue state meanings:

```text
pending       waiting to be opened
reading       current GUI read step
drafting      AI request in progress
ready         draft generated; in review mode waits for human approval
approved      human approved; agent may send after recheck
sending       current GUI send step
done          completed
skipped       intentionally not sent
failed        error
```

## First Validation On A Mac

Open WeCom and make sure the target account is logged in. Then run:

```bash
npm run doctor
python -m cli_anything.wecom_gui --json inbox scan --limit 5
python -m cli_anything.wecom_gui --json chat read --last 12
```

Expected:

- WeCom is detected as running.
- Accessibility checks pass.
- Inbox rows include customer conversations with expected tags.
- `chat read` returns recent messages with inferred `用户` / `客服` roles.

If Accessibility fails, grant permission to the terminal app, Codex app, or
LaunchAgent runner that starts the process. Restart that app before retrying.

## Start And Stop

Start agent:

```bash
./scripts/wecom-agent start
```

Stop agent:

```bash
./scripts/wecom-agent stop
```

Logs:

```bash
./scripts/wecom-agent logs
```

One-time foreground dry run:

```bash
python -u -m cli_anything.wecom_gui agent --mode dry-run --poll 0.5 --scan-interval 1 --inbox-limit 5 --max-drafts 4 --last 12 --log-interval 5
```

Export the current WeCom window payload that would be passed to the Agent,
without enqueueing, calling AI, or sending anything:

```bash
python -m cli_anything.wecom_gui --json agent-input --last 12 --output /tmp/wecom-agent-input.json
```

The JSON contains:

- `read`: raw current-window read result, including message roles and hash.
- `agent_input`: messages and customer identity passed to the GUI Agent layer.
- `csbot_input`: query and context passed to the csbot autonomous Agent.

## New Customer Welcome

Newly added WeCom customers can appear without an unread badge. The agent treats
sidebar previews like this as a fixed welcome trigger:

```text
你已添加了三水儿，现在可以开始聊天了。
```

Configure the fixed welcome text before using `auto` mode:

```bash
export WECOM_GUI_WELCOME_MESSAGE='替换成固定欢迎话术'
```

The welcome draft is marked `reply_source=welcome`. It does not call the ordinary
AI drafter. With `WECOM_GUI_REQUIRE_UNREAD=1`, the scanner still enqueues this
new-customer system preview even when the row has no red unread badge.

## Review Server

Start:

```bash
./scripts/wecom-agent review-start
```

The printed URL includes the token:

```text
http://<LAN-IP>:8122/
```

The page shows `ready` drafts. The reviewer can approve or reject. Approved
items become `approved`; rejected items become `skipped` with
`review_rejected`.

API endpoints:

```text
GET  /api/review/items?status=ready
GET  /api/review/counts
POST /api/review/items/{id}/save
POST /api/review/items/{id}/regenerate
POST /api/review/items/{id}/approve
POST /api/review/items/{id}/reject
```

The review page reads live queue items from
`~/.cli-anything-wecom-gui/state.sqlite`; it does not serve mock items.

## Central Channel Edge Client

`edge-channel` is independent from the AI/review queue. It is the unattended
Mac executor for the central customer-service channel: unread external direct
messages are first spooled in SQLite, then uploaded; centrally issued text or
image commands are re-read against the current customer message before send.

```bash
python -m cli_anything.wecom_gui edge-channel status
python -m cli_anything.wecom_gui edge-channel run --once
python -m cli_anything.wecom_gui edge-channel run
```

Set `WECOM_CHANNEL_BASE_URL` (HTTPS only), `WECOM_CHANNEL_DEVICE_ID`,
`WECOM_CHANNEL_DEVICE_TOKEN`, and `WECOM_CHANNEL_ACCOUNT_ID` in `.env.local`.
The client records command ids before GUI execution and reports an unconfirmed
post-click state as `needs_reconciliation`; it never blindly sends it again.

SQLite schema notes:

- `reply_queue` includes `handoff_type` and `handoff_reason` for direct/AI
  handoff review items. These columns are added automatically on startup.
- `conversation_messages` includes persisted `message_type` values
  (`customer`, `reply`, or `unknown`) plus `media_json`; review image URLs are
  served only from recorded media rows and never from arbitrary file paths.

## Configuration

Primary file:

```text
wecom-gui/.env.local
```

Common production values:

```env
WECOM_GUI_APP_NAME=企业微信
WECOM_GUI_AI_PROVIDER=pi
WECOM_AGENT_MODE=review
WECOM_AGENT_INBOX_LIMIT=8
WECOM_AGENT_MAX_DRAFTS=10
WECOM_AGENT_TEXT_WORKERS=7
WECOM_AGENT_IMAGE_WORKERS=3
WECOM_GUI_REQUIRE_WECHAT_TAG=1
WECOM_GUI_REQUIRE_UNREAD=1
WECOM_GUI_ALLOW_UNTAGGED=0
WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=0
WECOM_REVIEW_HOST=0.0.0.0
WECOM_REVIEW_PORT=8122
```

Use `WECOM_GUI_CAPTURE_IMAGES=1` when image questions are in scope. Captured
images are stored under `WECOM_GUI_CAPTURE_IMAGE_DIR`.

## Troubleshooting

Queue:

```bash
npm run queue
python -m cli_anything.wecom_gui --json queue list --status ready
python -m cli_anything.wecom_gui --json queue list --status approved
```

Clear old completed jobs:

```bash
python -m cli_anything.wecom_gui --json queue clear --status done
```

Common failures:

- `osascript` or System Events is denied: grant Accessibility permission and
  restart the runner.
- Agent drafts but does not send: check for `stale_context`, which means the
  customer sent a newer message.
- Browser review page shows nothing: confirm the agent is in `review` mode and
  queue has `ready` items.
- `approved` items do not send: confirm the agent process is running and can
  access the WeCom window.
- Replies target the wrong chat: stop immediately, return to `dry-run`, and
  recalibrate `inbox scan`, `chat read`, and window geometry.

## Development Rules

- Do not hard-code secrets.
- Do not remove `dry-run`, review mode, or send-time context checks.
- Do not make browser approval directly operate the GUI.
- Do not allow multiple threads to click WeCom at once.
- Do not default to untagged contacts or external groups.
- Unit tests should monkeypatch GUI backends and avoid real desktop actions.
