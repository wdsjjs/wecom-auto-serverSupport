# macOS Setup

Recommended project location:

```bash
~/Desktop/uda-codex-wecome
```

If the project is placed elsewhere, `scripts/install-config.sh` still rewrites
local paths to the actual project root.

Install on a new Mac:

Double-click:

```text
install.command
```

The installer requires Python 3.10 or newer and Pi coding-agent. If the Mac
only has Python 3.9, `install.command` checks Xcode Command Line Tools first,
installs Homebrew when missing, installs Python 3.12 and Node.js through
Homebrew, installs `@earendil-works/pi-coding-agent`, then creates a
project-local `.venv`. The Command Line Tools install may open a macOS popup,
and the Homebrew install may ask for the Mac login password. Homebrew and pip
use the Tsinghua Tuna mirrors by default; set `HOMEBREW_MIRROR=official` or
`PYPI_MIRROR=official` before running the script to use official sources.

`install.command` also writes Pi provider files under:

```text
~/.codex-csbot-wecom/pi-home/settings.json
~/.codex-csbot-wecom/pi-home/models.json
~/.codex-csbot-wecom/pi-home/auth.json
```

These files register `uda-openai` and `deepseek-v4-flash`. This is required;
`WECOM_GUI_PI_PROVIDER=uda-openai` alone is not enough.

PostgreSQL and MEM0 are expected to run on the main LAN machine, not on every
Mac. On the host Mac, initialize PostgreSQL and LAN access:

```bash
cd ~/Desktop/uda-codex-wecome
CSBOT_PG_PASSWORD='change-this-password' ./scripts/setup-pg-host.sh
```

The script creates database `csbot_wecom`, role `csbot_app`, appends a managed
LAN rule to PostgreSQL config, restarts PostgreSQL, and writes `CSBOT_PG_DSN`
to `deploy/mac.shared.env`.

Put the shared PG, Feishu, Weiban, and MEM0 settings in `deploy/mac.shared.env`
before delivery:

```env
CSBOT_PG_DSN='postgresql://csbot_app:<password>@192.168.110.53:5432/csbot_wecom'
FEISHU_APP_ID='replace-with-feishu-app-id'
FEISHU_APP_SECRET='replace-with-feishu-app-secret'
FEISHU_APP_TOKEN='HyP6bKXVvaK9nXsOMO3cwZTenzb'
WEIBAN_BASE_URL='https://open.weibanzhushou.com'
WEIBAN_CORP_ID='replace-with-weiban-corp-id'
WEIBAN_SECRET='replace-with-weiban-secret'
CSBOT_MEM0_URL='http://192.168.110.53:8888'
CSBOT_MEM0_API_KEY='replace-with-your-mem0-api-key'
CSBOT_MEM0_GLOBAL_USER_ID='global-kb'
```

`scripts/install-config.sh` writes these values into both `wecom-gui/.env.local`
and `codex-csbot-wecom/.env`. The install summary prints the MEM0 URL and masks
the API key, webhook, passwords, and secrets.

or:

```bash
cd ~/Desktop/uda-codex-wecome
./scripts/install-deps.sh
./scripts/install-config.sh
./scripts/install-csbot-sync-launchd.sh
cd wecom-gui
./scripts/wecom-agent start
```

Useful commands:

```bash
./start-agent.command
./stop-agent.command
./logs.command
./修复PiProvider.command
```

If an already-installed Mac reports that `uda-openai` is missing or that
`models.json` is empty, double-click `修复PiProvider.command`. It rewrites the
Pi provider JSON and verifies:

```bash
PI_CODING_AGENT_DIR="$HOME/.codex-csbot-wecom/pi-home" pi --offline --list-models deepseek
```

Terminal equivalents:

```bash
./scripts/wecom-agent stop
tail -f .codex-run/wecom-agent.log
```

Knowledge sync commands:

```bash
cd ~/Desktop/uda-codex-wecome/codex-csbot-wecom
../.venv/bin/python -m csbot feishu sync --dry-run
../.venv/bin/python -m csbot weiban sync --dry-run
../.venv/bin/python -m csbot sync all --progress
../.venv/bin/python -m csbot retrieve --customer-id test --query "鱼油起拍数量"
```

`AI 知识库.xlsx` remains only a manual fallback. The default knowledge source is
Feishu Bitable plus Weiban FAQ synced into PostgreSQL and then imported into
MEM0. The WeCom GUI local queue SQLite remains local to each Mac.

Required macOS permissions:

- Accessibility for the terminal app used to run the agent.
- Screen Recording if macOS prompts for it.
- Enterprise WeChat must be logged in and visible.
