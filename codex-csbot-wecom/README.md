# codex-csbot-wecom

Knowledge retrieval, memory, and operations tools for the WeCom customer-service
assistant.

The production knowledge path combines:

- Feishu Bitable and Weiban FAQ sync into PostgreSQL source tables.
- `kb_docs` and `kb_aliases` rebuild from those source tables.
- MEM0/global-kb recall with a fixed 1024-dimensional embedding boundary.
- Ops tools for order, logistics, work-order, and WeCom customer checks.

Product facts should come from PostgreSQL/script retrieval sources. Vector memory
is used for customer profile, history, and fuzzy recall, but it must not override
script facts.

## Quick Start

```bash
python -m csbot doctor
python -m csbot sync all --progress
python -m csbot retrieve --customer-id test --query "女维怎么吃"
python -m csbot debug-ui --host 127.0.0.1 --port 8899
```

Excel/SQLite import remains available only as a manual fallback:

```bash
python -m csbot kb import --xlsx "../AI 知识库.xlsx" --vector
```

## Knowledge Sync

Set these in `codex-csbot-wecom/.env`:

```env
CSBOT_PG_DSN='postgresql://csbot_app:<password>@192.168.x.x:5432/csbot_wecom'
FEISHU_APP_ID='<set>'
FEISHU_APP_SECRET='<set>'
FEISHU_APP_TOKEN='<set>'
WEIBAN_BASE_URL='https://open.weibanzhushou.com'
WEIBAN_CORP_ID='<set>'
WEIBAN_SECRET='<set>'
CSBOT_MEM0_URL='http://192.168.x.x:8888'
CSBOT_MEM0_API_KEY='<set>'
CSBOT_MEM0_GLOBAL_USER_ID='global-kb'
```

Commands:

```bash
python -m csbot feishu sync --dry-run
python -m csbot weiban sync --dry-run
python -m csbot sync all --progress
python -m csbot kb rebuild --vector
```

Weiban sync writes diagnostic progress logs to stderr by default and keeps the
final JSON result on stdout. Useful diagnosis knobs:

```bash
WEIBAN_GROUP_FETCH_MODE=top_level \
WEIBAN_SYNC_WORKERS=4 \
WEIBAN_SYNC_MAX_GROUPS=20 \
WEIBAN_REQUEST_TIMEOUT_SECONDS=10 \
WEIBAN_TOKEN_TIMEOUT_SECONDS=10 \
python -m csbot weiban sync --dry-run > /tmp/weiban-sync.json 2> /tmp/weiban-sync.log
```

`WEIBAN_GROUP_FETCH_MODE=top_level` is the default and avoids re-fetching child
groups that are already included in top-level group results. Use
`WEIBAN_GROUP_FETCH_MODE=all` only when diagnosing parent/child data gaps. Set
`WEIBAN_SYNC_LOG=0` to silence the progress logs.

`sync all` runs Feishu sync, Weiban FAQ sync, rebuilds `kb_docs/kb_aliases`, and
imports the unified KB into MEM0 `global-kb`. The WeCom GUI queue SQLite is
local to each Mac and is not migrated by this knowledge sync.

## Retrieval Contract

The GUI drafting layer should pass each customer turn to a worker that returns
JSON matching `schemas/codex_reply.schema.json`.

Facts about products, shipping, usage, contraindications, composition, price, or
specification must use `used_script_sources` from script retrieval. Vector hits
and profile memories can disambiguate or enrich context, but cannot override
script facts.

## Autonomous Mode

The debug server supports `mode=autonomous`. In this mode the main process starts
a Codex/Pi worker and lets it decide how to retrieve evidence. The worker may
write temporary scripts, call the local `csbot` CLI, query MEM0, and use ops
commands, but it must still return JSON matching `schemas/codex_reply.schema.json`.

The worker prompt is SOP-first. It tells the worker to use `csbot retrieve`,
`csbot kb search`, `csbot mem search`, and `csbot ops` instead of inspecting
project source files to answer customer questions. Product facts, memories,
conflicts, and confidence are judged inside the worker; the Python caller
validates the returned JSON shape and exposes the trace in the debug UI.

Run autonomous mode from CLI:

```bash
python -m csbot autonomous-reply --customer-id test --query "女维怎么吃" --timeout 120
```

Open the debug UI, choose `Codex 自主检索`, enter the customer message, and run a
case. Choosing `固定双召回` keeps the previous Script + MEM0 retrieval path and
passes that retrieval result into the worker.

## Notes

The fixed dual-retrieval path stays read-only and source-first. Autonomous mode
is intended for supervised debugging and review-gated drafting before any live
auto-send workflow.
