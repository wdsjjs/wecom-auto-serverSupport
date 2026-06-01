from __future__ import annotations

import os
from pathlib import Path


DEFAULT_PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DEPLOY_ROOT = DEFAULT_PROJECT_DIR.parent


def load_project_env(path: str | Path | None = None) -> None:
    raw_path = path or os.environ.get("CSBOT_ENV_FILE") or (DEFAULT_PROJECT_DIR / ".env")
    env_path = Path(raw_path).expanduser()
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_project_env()
DEFAULT_CODEX_WORKDIR = DEFAULT_DEPLOY_ROOT / "ai-knowledge"
DEFAULT_STATE_DIR = Path(os.environ.get("CSBOT_STATE_DIR", str(Path.home() / ".codex-csbot-wecom"))).expanduser()
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "state.sqlite"
DEFAULT_KB_XLSX = Path(os.environ.get("CSBOT_KB_XLSX", str(DEFAULT_DEPLOY_ROOT / "AI 知识库.xlsx")))
DEFAULT_PG_DSN = os.environ.get("CSBOT_PG_DSN", "")
DEFAULT_MEM0_URL = os.environ.get("CSBOT_MEM0_URL", "http://127.0.0.1:8888")
DEFAULT_MEM0_API_KEY = os.environ.get("CSBOT_MEM0_API_KEY", "")
DEFAULT_MEM0_ENV_PATH = Path(os.environ.get("CSBOT_MEM0_ENV", str(DEFAULT_DEPLOY_ROOT / "mem0/server/.env")))
MEM0_GLOBAL_USER_ID = os.environ.get("CSBOT_MEM0_GLOBAL_USER_ID", "global-kb")


def ensure_state_dir() -> Path:
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_STATE_DIR


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is None:
        ensure_state_dir()
        return DEFAULT_DB_PATH
    return Path(db_path).expanduser()


def resolve_pg_dsn(dsn: str | None = None) -> str:
    return (dsn if dsn is not None else os.environ.get("CSBOT_PG_DSN", DEFAULT_PG_DSN)).strip()


def using_pg() -> bool:
    return bool(resolve_pg_dsn())


def resolve_codex_workdir(workdir: str | Path | None = None) -> Path:
    if workdir is not None:
        return Path(workdir).expanduser()
    return Path(os.environ.get("CSBOT_CODEX_WORKDIR", str(DEFAULT_CODEX_WORKDIR))).expanduser()


def read_env_value(path: str | Path, key: str) -> str:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return ""
    prefix = f"{key}="
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip().strip('"').strip("'")
    return ""


def resolve_mem0_url(url: str | None = None) -> str:
    return (url if url is not None else os.environ.get("CSBOT_MEM0_URL", DEFAULT_MEM0_URL)).rstrip("/")


def resolve_mem0_api_key(api_key: str | None = None) -> str:
    if api_key:
        return api_key
    env_key = os.environ.get("CSBOT_MEM0_API_KEY", DEFAULT_MEM0_API_KEY)
    if env_key:
        return env_key
    return read_env_value(os.environ.get("CSBOT_MEM0_ENV", str(DEFAULT_MEM0_ENV_PATH)), "ADMIN_API_KEY")
