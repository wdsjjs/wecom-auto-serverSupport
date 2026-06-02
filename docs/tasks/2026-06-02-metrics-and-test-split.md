# Task 2: Metrics Persistence And Test Split

## Summary

Add SQLite-backed metrics for the WeCom review/agent flow, and split the current large test module so future changes can be made and reviewed without repeatedly loading one oversized test file.

## Goals

- Persist operational metrics in SQLite instead of relying only on JSONL audit logs.
- Add backend query support for metrics summaries used by the review platform.
- Track the required counters:
  - Number of received/served users.
  - Number of customer messages sent while AI reply mode was active.
  - Number of customer messages sent while human/customer-service reply mode was active.
  - Direct handoff count.
  - Indirect handoff count.
  - Response-time buckets.
- Track additional useful diagnostics:
  - Review approvals, rejections, saves, and regenerations.
  - Human-edited reply count.
  - Send success and send failure count.
  - Stale-context skips before send.
  - Image-message count and image-capture failure count.
  - AI draft latency and final send latency.
  - Handoff pending count.
- Split `wecom-gui/cli_anything/wecom_gui/tests/test_core.py` into focused test files.

## Implementation Notes

- Add SQLite tables such as `metric_events` and, if needed, `conversation_metric_state`.
- Keep JSONL `append_event` behavior for audit/debug compatibility, but also write structured metric events for important lifecycle points.
- Use `conversation_key` as the primary isolation key for metrics.
- Maintain enough per-conversation state to know whether a new customer message arrived after an AI reply or after a human/customer-service reply.
- Add a review API endpoint such as `GET /api/review/metrics?since_hours=24` for summary data.
- Keep metric aggregation simple and deterministic; default response-time buckets can be small fixed ranges such as `0-5s`, `5-15s`, `15-30s`, `30-60s`, and `60s+`.
- Avoid changing business behavior while adding metrics.

## Test Split Plan

Move tests from the current large `test_core.py` into focused modules:

- `test_review_server.py`: review item payloads, actions, counts, handoff display data, metrics API.
- `test_state.py`: queue state, conversation keys, message persistence, metrics tables.
- `test_agent.py`: fast agent read/draft/send flow, handoff detection, stale-context behavior.
- `test_macos_backend.py`: Accessibility parsing, image capture, window/preview helpers.
- `test_llm.py`: provider selection, csbot payloads, Markdown cleanup, handoff action handling.
- Keep shared fixtures in `conftest.py` where they are reused by multiple modules.

## Acceptance Criteria

- Metrics tables are created automatically by schema initialization.
- Required counters can be queried from SQLite-backed summary APIs.
- Direct and indirect handoff counts are distinguishable.
- Customer messages are counted against the latest known reply mode: AI or human/customer-service.
- Response time summaries include bucketed counts and enough raw timing fields for debugging.
- Existing JSONL events still work.
- Tests can be run by focused module without reading all previous `test_core.py` content.
- Full test suite still passes after the split.

## Suggested Tests

- Unit test metric table creation and insertion.
- Unit test aggregation for served users, AI-mode customer messages, human-mode customer messages, handoff counts, and latency buckets.
- Unit test metrics are emitted on review approve/save/regenerate/reject and agent send success/failure.
- Unit test conversation metric state transitions from AI reply to human reply.
- Run focused pytest modules plus one full regression suite after the split.
