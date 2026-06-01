# WeCom GUI Harness SOP

## Goal

`cli-anything-wecom-gui` automates WeCom desktop customer-service workflows when
no official send/receive API is available for external-contact chats.

The harness is intentionally conservative:

- It separates inbox scanning from chat opening/sending.
- It uses `--dry-run` and `--mode approve` before `--mode auto`.
- It records draft/reply events locally for audit.
- It serializes GUI operations with a process lock so multiple workers do not
  click the desktop at the same time.
- It runs AI drafts concurrently, outside the GUI lock, so model latency does
  not block reading other customers.
- It verifies the customer's latest message before sending and skips stale
  replies when the context changed during drafting.
- It filters long technical text, token/header-like text, department rows, and
  external groups before enqueueing by default.
- It requires an `@微信` tag by default for scan-only auto queues.
- It requires `unread_count > 0` by default before opening a conversation.
- It treats GUI automation as best effort, with `doctor` explaining missing
  permissions or app-name mismatches.

## Backend Strategy

1. macOS Accessibility and AppleScript for app focus and visible text.
2. Clipboard paste for reply entry, because Chinese input and links are more
   reliable than simulated typing.
3. A local SQLite queue stores changed visible conversations.
4. The fast agent briefly opens chats to collect context, then drafts in a
   thread pool.
5. A single locked send path returns to ready chats and sends only non-stale
   replies.
6. OCR fallback is planned but not enabled in the MVP.

## MVP Commands

```bash
cli-anything-wecom-gui doctor
cli-anything-wecom-gui app focus
cli-anything-wecom-gui inbox scan --json
cli-anything-wecom-gui chat read --last 10 --json
cli-anything-wecom-gui ai draft --last 10
cli-anything-wecom-gui reply send --text "您好，我帮您看一下" --dry-run
python -u -m cli_anything.wecom_gui agent --mode dry-run --poll 0.5 --scan-interval 1 --max-drafts 4 --last 12 --log-interval 5
cli-anything-wecom-gui queue list --json
```

## Operational Safety

Use `agent --mode dry-run` until these are stable on the target machine:

- WeCom app name detection.
- Accessibility text extraction.
- Current chat identity and latest message extraction.
- Reply paste target.
- Deduplication and cooldown policy.
- Queue claiming and skipped/done/failed audit status.

Only then use `agent --mode auto`, and only for conversations where automatic
customer-service replies are appropriate and authorized by the business.
