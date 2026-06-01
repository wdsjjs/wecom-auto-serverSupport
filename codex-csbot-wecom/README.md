# codex-csbot-wecom

Dual-retrieval customer-service bot prototype.

The production knowledge path now combines:

- script retrieval over PostgreSQL `kb_docs` generated from Feishu Bitable and Weiban FAQ
- MEM0/global-kb recall with a fixed 1024-dimensional embedding boundary

Product facts should come from script retrieval sources. Vector memory is used for
customer profile and fuzzy recall.

## Quick start

```bash
/opt/homebrew/bin/python3 -m csbot doctor
/opt/homebrew/bin/python3 -m csbot sync all --progress
/opt/homebrew/bin/python3 -m csbot retrieve --customer-id test --query "女维怎么吃"
```

Excel/SQLite import is still available only as a manual fallback:

```bash
/opt/homebrew/bin/python3 -m csbot kb import --xlsx "../AI 知识库.xlsx" --vector
```

## Knowledge sync

Set these in `codex-csbot-wecom/.env`:

```env
CSBOT_PG_DSN='postgresql://csbot_app:<password>@192.168.x.x:5432/csbot_wecom'
FEISHU_APP_ID='<set>'
FEISHU_APP_SECRET='<set>'
FEISHU_APP_TOKEN='HyP6bKXVvaK9nXsOMO3cwZTenzb'
WEIBAN_BASE_URL='https://open.weibanzhushou.com'
WEIBAN_CORP_ID='<set>'
WEIBAN_SECRET='<set>'
CSBOT_MEM0_URL='http://192.168.x.x:8888'
CSBOT_MEM0_API_KEY='<set>'
```

Commands:

```bash
python -m csbot feishu sync --dry-run
python -m csbot weiban sync --dry-run
python -m csbot sync all --progress
python -m csbot kb rebuild --vector
```

`sync all` runs Feishu sync, Weiban FAQ sync, rebuilds `kb_docs/kb_aliases`,
and imports the unified KB into MEM0 `global-kb`. The WeCom GUI queue SQLite is
separate and is not migrated by this knowledge sync.

## Fixed dual-retrieval worker contract

The GUI sender should pass each customer turn to a Codex worker with one hard
requirement: run `csbot retrieve` first, then return JSON matching
`schemas/codex_reply.schema.json`.

Facts about products, shipping, usage, contraindications, composition, price, or
specification must use `used_script_sources` from `script_hits`. Vector hits and
profile memories can disambiguate or enrich context, but cannot override script
facts.

## Autonomous mode

The debug server also supports `mode=autonomous`. In this mode the main process
starts a Codex worker directly and lets it decide how to retrieve evidence: it
may write temporary scripts, call the local `csbot` CLI, and query MEM0. The
worker must still return JSON matching
`schemas/codex_reply.schema.json`, with autonomous trace fields such as
`commands_run`, `retrieval_summary`, and `decision_basis`.

The autonomous prompt is intentionally compact. It passes only the database
path, key table names, customer context, and command examples; the worker should
use `csbot retrieve`, `csbot kb search`, `csbot mem search`, and `csbot ops`
instead of receiving a full schema dump in the prompt.

Autonomous mode is still SOP-first. The worker prompt tells Codex to run
`csbot --db ... retrieve` first, then `csbot --db ... mem search`, and only use
direct SQLite checks when the retrieval result needs source verification or
conflict diagnosis. It explicitly tells the worker not to inspect project source
files to answer customer questions.

The worker does not pass Codex CLI `--output-schema` by default because that
parameter can trigger `502` responses from the current `new-api.uda.cn`
Responses gateway. JSON is still validated by the Python caller after Codex
returns. Re-enable CLI-level schema enforcement with
`CSBOT_CODEX_OUTPUT_SCHEMA=1` when the provider supports it reliably.

Autonomous mode intentionally does not require the main program to pre-run
`csbot retrieve`. Product facts, MEM0 memories, conflicts, and final confidence
are judged inside the Codex worker. The main process validates only the JSON
shape and shows the full prompt/command/stdout/stderr trace in the debug UI.

Run the debug UI:

```bash
/opt/homebrew/bin/python3 -m csbot debug-ui --host 127.0.0.1 --port 8899
```

Run autonomous mode from CLI:

```bash
/opt/homebrew/bin/python3 -m csbot autonomous-reply --customer-id test --query "女维怎么吃" --timeout 120
```

Worker Codex starts in `CSBOT_CODEX_WORKDIR` when configured; the Mac install
script sets it to the repo-local `ai-knowledge` directory so it reads that
directory's `AGENTS.md` and does not treat the implementation repo as its
working directory.

When Codex CLI is configured to use `new-api.uda.cn`, its `/v1/models`
response may be OpenAI-shaped (`data`) while the Codex model catalog refresh
expects `models`. This project avoids that mismatch by generating a local
bundled catalog and passing it into every `codex exec` call:

```bash
-c 'model_catalog_json="$HOME/.codex-csbot-wecom/codex-model-catalog.json"'
```

The catalog is created automatically from `codex debug models --bundled`.
Override the path with `CSBOT_CODEX_MODEL_CATALOG=/path/catalog.json`, disable
it with `CSBOT_CODEX_MODEL_CATALOG=disabled`, and append custom Codex flags with
`CSBOT_CODEX_EXTRA_ARGS`. The worker defaults to
`CSBOT_CODEX_REASONING_EFFORT=low` so customer-service test runs do not inherit a
slow global `xhigh` setting; set `CSBOT_CODEX_MODEL` or
`CSBOT_CODEX_REASONING_EFFORT` to override this per project.

By default, worker Codex runs with an isolated `CODEX_HOME` at
`$HOME/.codex-csbot-wecom/codex-home`. It copies only the auth file and a
minimal provider config, so the worker keeps full Codex CLI behavior without
loading the user's global plugins, skills, MCP servers, memories, or large
session indexes. Disable it with `CSBOT_CODEX_ISOLATED_HOME=disabled`.

Open `http://127.0.0.1:8899`, choose `Codex 自主检索`, enter the customer
message, and run the case. The trace dialog shows the request, autonomous mode
marker, Codex prompt, command, stdout/stderr, final reply JSON, and the complete
debug payload. Choosing `固定双召回` keeps the previous Script + MEM0 retrieval
path and passes that retrieval result into the Codex worker.

## Notes

The fixed dual-retrieval path stays read-only and source-first. Autonomous mode
uses Codex with `danger-full-access` in the local project root so it can create
temporary scripts and choose its own retrieval commands; use the debug trace
before wiring it into any real auto-send surface.
