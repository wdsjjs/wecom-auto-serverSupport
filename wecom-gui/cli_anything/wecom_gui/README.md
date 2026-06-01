# cli-anything-wecom-gui

CLI-Anything harness for WeCom desktop GUI customer-service automation.

This harness is for situations where external-contact customer messages cannot
be handled through an official send/receive API. It drives the desktop GUI with
macOS Accessibility and AppleScript.

## Install

```bash
pip install -e .
```

This exposes:

```bash
cli-anything-wecom-gui
```

## Quick Validation

```bash
cli-anything-wecom-gui doctor
cli-anything-wecom-gui app focus
cli-anything-wecom-gui inbox scan --json
cli-anything-wecom-gui chat open --name "客户A"
cli-anything-wecom-gui chat read --last 10 --json
cli-anything-wecom-gui reply send --text "您好，我帮您看一下" --dry-run
```

If your app is not named `企业微信`, `WeCom`, or `WeChat Work`, set:

```bash
export WECOM_GUI_APP_NAME="Your App Name"
```

## AI Drafting

UDA single-question drafting is the default provider:

```bash
export WECOM_GUI_UDA_API_KEY=...
cli-anything-wecom-gui ai draft --last 10 --provider uda --json
```

The request body is:

```json
{
  "history": [
    {"type": "human", "data": {"content": "用户: 鱼油含量"}},
    {"type": "human", "data": {"content": "客服: 请问是哪款鱼油？"}}
  ],
  "ai_reply": true
}
```

`ai draft` returns the reply as both `text` and `message`. It accepts both UDA
response shapes: `data.message` and top-level `message`.

History behavior:

- Default: `WECOM_GUI_UDA_HISTORY_MODE=recent`, sends recent messages ending at
  the latest `用户` message.
- `WECOM_GUI_UDA_HISTORY_MAX=8` controls how many recent messages are included.
- `WECOM_GUI_UDA_HISTORY_MODE=latest_user` sends only the latest user message.
- `WECOM_GUI_UDA_HISTORY_MODE=full` sends all visible messages from `chat read`.

Without an API key, `ai draft` returns a safe fallback reply:

```bash
cli-anything-wecom-gui ai draft --last 10
```

With an OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o-mini
cli-anything-wecom-gui ai draft --last 10 --provider openai
```

## AI Agent Mode

Recommended real customer-service loop:

```bash
export WECOM_GUI_UDA_API_KEY=...
python -u -m cli_anything.wecom_gui agent --mode auto --poll 0.5 --scan-interval 1 --max-drafts 4 --last 12 --log-interval 5
```

For a non-sending rehearsal:

```bash
python -u -m cli_anything.wecom_gui agent --mode dry-run --poll 0.5 --scan-interval 1 --max-drafts 4 --last 12 --log-interval 5
```

`agent` behaves like an AI customer-service dispatcher:

- scan inbox rows into the local SQLite queue;
- briefly open new customer chats and read recent context;
- send AI draft requests concurrently while continuing to scan/read others;
- when a draft is ready, re-open that customer and send it;
- skip stale replies if the customer's latest message changed while AI was
  drafting.

GUI operations are still serialized with a global process lock. AI waiting is
not serialized, so several customers can have drafts in flight at the same time.

Useful queue commands:

```bash
cli-anything-wecom-gui queue list --json
cli-anything-wecom-gui queue list --status drafting --json
cli-anything-wecom-gui queue list --status ready --json
cli-anything-wecom-gui queue clear --status done
```

Modes:

- `dry-run`: draft and log only.
- `auto`: send automatically.

By default, scan-only filters out very long technical text, token/header-like
content, department rows, external group rows, rows without an `@微信` tag, and
rows with `unread_count == 0`.
Set `WECOM_GUI_ALLOW_UNTAGGED=1` only after verifying your WeCom build exposes
single external contacts without tags. Set `WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=1`
only if group replies are deliberately in scope.
Set `WECOM_GUI_REQUIRE_UNREAD=0` only for manual debugging of preview/time
change detection.

The older two-process queue flow is still available for debugging:

```bash
cli-anything-wecom-gui watch --scan-only --poll 5 --inbox-limit 12
cli-anything-wecom-gui worker --poll 2 --last 12 --mode approve
```

The single-chat loop is still available for calibration:

```bash
cli-anything-wecom-gui watch --poll 5 --last 10 --mode approve
```

`inbox scan` and `chat open` are accessibility heuristics in this version. Use
them to calibrate the target desktop before enabling `agent --mode auto`.
