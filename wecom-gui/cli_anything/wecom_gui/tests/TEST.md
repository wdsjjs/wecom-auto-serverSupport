# Test Plan

## Test Inventory Plan

- `test_core.py`: unit tests for chat extraction, fallback drafting, dry-run reply,
  and CLI command behavior.
- `test_full_e2e.py`: subprocess smoke tests for help and doctor JSON output.

## Unit Test Plan

- `core.chat`
  - Deduplicate visible text lines.
  - Filter common navigation noise.
  - Return deterministic message hashes.
- `core.inbox`
  - Filter navigation noise.
  - Deduplicate visible conversation labels.
  - Reject sensitive/non-customer rows before enqueueing.
- `core.llm`
  - Return fallback reply when `OPENAI_API_KEY` is absent.
- `core.reply`
  - Do not touch GUI in dry-run mode.
- `core.state`
  - Queue lifecycle covers enqueue, duplicate suppression, claim, done, clear.
  - GUI lock is created under the configured state directory.
- `core.worker`
  - Scan-only enqueues visible customer rows without opening chats.
- `core.agent`
  - Fast pipeline reads a pending chat, drafts asynchronously, marks ready, and
    sends.
  - Stale context is skipped before sending.
- CLI
  - Help renders.
  - `reply send --dry-run` emits JSON.

## E2E Test Plan

The first E2E layer avoids touching the real GUI:

- Invoke the package with `python -m cli_anything.wecom_gui --help`.
- Invoke `doctor --json` and validate the JSON shape.

True GUI validation is manual for now:

```bash
cli-anything-wecom-gui doctor
cli-anything-wecom-gui app focus
cli-anything-wecom-gui inbox scan --json
cli-anything-wecom-gui chat open --name "客户A"
cli-anything-wecom-gui chat read --last 10 --json
cli-anything-wecom-gui reply send --text "dry run test" --dry-run
cli-anything-wecom-gui watch --scan-only --once --inbox-limit 12
cli-anything-wecom-gui queue list --json
cli-anything-wecom-gui worker --once --mode dry-run
cli-anything-wecom-gui agent --once --mode dry-run
```

## Test Results

Command:

```bash
env PYTHONPATH=/private/tmp/wecom-gui-deps CLI_ANYTHING_TEST_DEPS=/private/tmp/wecom-gui-deps \
  python3 \
  -m pytest -q cli_anything/wecom_gui/tests
```

Result:

```text
......................                                                   [100%]
22 passed in 1.24s
```

Additional manual smoke checks:

- `python -m cli_anything.wecom_gui --help` renders all command groups.
- `reply send --text hello --dry-run --json` returns structured dry-run output.
- `doctor --json` detects the running WeCom app as `企业微信`.
- `inbox scan --json` reads visible sidebar conversations as structured rows.
- `chat open --name 和光同尘` selects a visible conversation row.
- `chat read --last 10 --json` reads the right-side chat table and returns
  `source: accessibility-chat-table`.
- `chat read --last 6 --json` now includes inferred `role` (`用户` / `客服`)
  plus `content`.
- `ai draft --last 6 --provider uda --json` successfully calls the UDA
  single-question API and returns the reply in `message` / `text`. The observed
  production response used top-level `message`, so the parser accepts both
  top-level `message` and `data.message`.
- `watch --scan-only` adds changed visible conversations to the local queue
  without clicking a chat row.
- The first real scan enqueued 6 rows and exposed noisy non-customer text, so
  inbox filtering was tightened. After filtering, the real scan enqueued 2
  candidate rows.
- `worker` claims one queue item at a time and holds a GUI lock while opening,
  reading, drafting, and replying.
- `agent` keeps GUI operations short, drafts AI replies concurrently, and sends
  only after re-checking that the latest customer message still matches.
