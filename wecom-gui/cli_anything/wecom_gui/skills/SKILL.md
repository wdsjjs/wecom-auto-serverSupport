---
name: cli-anything-wecom-gui
description: Drive WeCom desktop GUI to read visible customer chats, draft replies, and paste/send replies when no official external-contact messaging API is available.
---

# cli-anything-wecom-gui

Use this skill when you need to operate WeCom desktop through GUI automation for
customer-service workflows.

## Safety

Start with `agent --mode dry-run`. Use `agent --mode auto`
only after the target desktop has been calibrated and reply targeting is
verified.

## Common Commands

```bash
cli-anything-wecom-gui doctor
cli-anything-wecom-gui app focus
cli-anything-wecom-gui inbox scan --json
cli-anything-wecom-gui chat open --name "客户A"
cli-anything-wecom-gui chat read --last 10 --json
cli-anything-wecom-gui ai draft --last 10
cli-anything-wecom-gui reply send --text "您好，我帮您看一下" --dry-run
python -u -m cli_anything.wecom_gui agent --mode dry-run --poll 0.5 --scan-interval 1 --max-drafts 4 --last 12 --log-interval 5
cli-anything-wecom-gui queue list --json
```

## UDA Reply API

Set `WECOM_GUI_UDA_API_KEY` and use:

```bash
cli-anything-wecom-gui ai draft --last 10 --provider uda --json
```

The harness sends each history item as `type: human` with content formatted as
`用户: ...` or `客服: ...`, and returns the API reply as `message` / `text`.

## Notes

- macOS is implemented first.
- Accessibility permission is required for Terminal/Codex.
- `agent` scans, reads, drafts concurrently, then sends ready replies behind a
  GUI process lock.
- Stale replies are skipped if the latest customer message changed while AI was
  drafting.
- Rows without `@微信` are filtered unless `WECOM_GUI_ALLOW_UNTAGGED=1`.
- External group rows are filtered unless `WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=1`.
- Rows without unread badges are ignored unless `WECOM_GUI_REQUIRE_UNREAD=0`.
- `WECOM_GUI_APP_NAME` overrides app-name detection.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` configure AI drafting.
