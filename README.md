# WeCom Auto Server Support

This repository contains a macOS WeCom customer-service assistant. It has two
main parts:

- `wecom-gui`: drives the WeCom desktop app with macOS Accessibility, reads
  visible chats, drafts replies, queues them, and sends them after safety
  checks or web approval.
- `codex-csbot-wecom`: provides product knowledge retrieval, order/logistics
  tools, MEM0 recall, and a debug UI used by the reply worker.

The system is GUI automation, not an official WeCom send/receive API. It clicks
the desktop app, reads visible UI, and pastes replies into the active customer
chat.

## Current Flow

1. The agent scans WeCom's left conversation list.
2. It keeps eligible external-contact rows, normally rows tagged `@微信` with an
   unread badge.
3. It opens one chat at a time under a GUI lock and reads recent visible
   messages.
4. AI drafting runs outside the GUI lock so multiple drafts can be in flight.
5. Drafts enter the local SQLite queue.
6. In `auto` mode, the agent reopens the chat, verifies the latest customer
   message is still the same, then sends.
7. In `review` mode, drafts wait on a LAN review page. A human approves or
   rejects each reply. Approved replies are still rechecked before sending.
8. If the customer sends a newer message before send time, the old reply is
   skipped.

Queue states:

```text
pending -> reading -> drafting -> ready -> approved -> sending -> done
                                      \-> skipped / failed
```

In `auto` mode, `ready` is consumed directly. In `review` mode, `ready` means
"waiting for web review" and only `approved` is eligible for sending.

## Quick Start

Install or repair a Mac with:

```bash
./install.command
```

Then start the customer-service agent:

```bash
./start-agent.command
```

Stop it:

```bash
./stop-agent.command
```

Watch logs:

```bash
./logs.command
```

The shell equivalents are:

```bash
cd wecom-gui
./scripts/wecom-agent start
./scripts/wecom-agent review-start
./scripts/wecom-agent stop
./scripts/wecom-agent logs
```

## Review Mode

Review mode is the safest production mode because a human must approve the AI
draft before it can be sent.

Start the agent in review mode:

```bash
cd wecom-gui
WECOM_AGENT_MODE=review ./scripts/wecom-agent start
```

Start the LAN review page:

```bash
cd wecom-gui
./scripts/wecom-agent review-start
```

The review server prints a URL like:

```text
http://192.168.x.x:8122/
```

Open that URL from the reviewer machine on the same LAN. The page supports only:

- view latest customer message and AI reply;
- approve;
- reject.

Approving does not send from the browser directly. It marks the queue item
`approved`; the local agent then reopens WeCom, rechecks the latest customer
message, and sends only if the context is still current.

## Configuration

Per-machine configuration lives in:

```text
wecom-gui/.env.local
codex-csbot-wecom/.env
```

Create `wecom-gui/.env.local` from:

```bash
cp wecom-gui/.env.example wecom-gui/.env.local
chmod 600 wecom-gui/.env.local
```

Important WeCom GUI variables:

```env
WECOM_GUI_APP_NAME=企业微信
WECOM_GUI_AI_PROVIDER=pi
WECOM_AGENT_MODE=review
WECOM_GUI_REQUIRE_WECHAT_TAG=1
WECOM_GUI_REQUIRE_UNREAD=1
WECOM_GUI_ALLOW_UNTAGGED=0
WECOM_GUI_INCLUDE_EXTERNAL_GROUPS=0
WECOM_REVIEW_HOST=0.0.0.0
WECOM_REVIEW_PORT=8122
```

Shared knowledge settings are usually written by `scripts/install-config.sh`
from `deploy/mac.shared.env`.

## Knowledge Service

`codex-csbot-wecom` stores and retrieves product/customer-service knowledge.
The current production knowledge path is:

- Feishu Bitable and Weiban FAQ sync into PostgreSQL tables;
- `kb_docs` and `kb_aliases` are rebuilt from those source tables;
- KB docs are imported into MEM0 as `global-kb`;
- GUI drafting can call `csbot retrieve`, MEM0 search, and ops tools for order
  or logistics checks.

Common commands:

```bash
cd codex-csbot-wecom
python -m csbot doctor
python -m csbot sync all --progress
python -m csbot retrieve --customer-id test --query "鱼油怎么吃"
python -m csbot debug-ui --host 127.0.0.1 --port 8899
```

See `codex-csbot-wecom/README.md` for details.

## macOS Requirements

- macOS with WeCom/企业微信 installed and logged in.
- Accessibility permission for the terminal app, Codex app, or LaunchAgent
  runner that starts the automation.
- Screen Recording permission if macOS prompts for it.
- WeCom window visible and not minimized.
- Python 3.10+ with project dependencies.
- PostgreSQL and MEM0 reachable on the LAN when using shared production
  knowledge.

Run:

```bash
cd wecom-gui
npm run doctor
```

The doctor should report WeCom running, Accessibility enabled, and Swift/AX
helpers working.

## Safety Rules

- Start with `dry-run`, then `review`, and use `auto` only when the business has
  explicitly authorized fully automatic replies.
- Do not operate the same WeCom desktop manually while the agent is sending.
- Keep `@微信` and unread filters enabled unless you are calibrating.
- Do not put API keys in committed files.
- Do not delete `.env.local` or the local queue during incident review.
- Treat the local queue and logs under `~/.cli-anything-wecom-gui/` as customer
  interaction audit data.

## Important Paths

```text
wecom-gui/                         GUI automation package
wecom-gui/cli_anything/wecom_gui/  Python CLI and core modules
wecom-gui/.env.example             Local GUI config template
codex-csbot-wecom/                 Knowledge retrieval and ops tools
ai-knowledge/AGENTS.md             Worker SOP loaded by autonomous Codex/PI
deploy/mac.shared.env              Shared LAN service config generated during install
~/.cli-anything-wecom-gui/          Local queue and logs
```

## Documentation Map

- `README.md`: project overview and operating model.
- `SETUP_MAC.md`: Mac installation, host service restore, and permissions.
- `wecom-gui/cli_anything/wecom_gui/README.md`: GUI agent commands, review
  page, queue states, testing.
- `wecom-gui/WECOM_GUI.md`: GUI automation SOP and safety checklist.
- `codex-csbot-wecom/README.md`: knowledge sync, retrieval, debug UI, ops tools.
