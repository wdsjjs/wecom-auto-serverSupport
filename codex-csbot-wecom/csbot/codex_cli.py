from __future__ import annotations

import json
import os
import shutil
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import ensure_state_dir, resolve_codex_workdir


DEFAULT_CODEX_COMMAND = "/opt/homebrew/bin/codex"
DEFAULT_CATALOG_FILENAME = "codex-model-catalog.json"
ISOLATED_HOME_DIRNAME = "codex-home"
DISABLED_VALUES = {"0", "false", "no", "off", "disabled", "none"}


@dataclass(frozen=True)
class ModelCatalogStatus:
    enabled: bool
    path: str = ""
    generated: bool = False
    error: str = ""


@dataclass(frozen=True)
class IsolatedHomeStatus:
    enabled: bool
    path: str = ""
    generated: bool = False
    error: str = ""


def resolve_codex_command() -> str:
    return os.environ.get("CSBOT_CODEX_COMMAND", DEFAULT_CODEX_COMMAND)


def codex_extra_args() -> list[str]:
    raw = os.environ.get("CSBOT_CODEX_EXTRA_ARGS", "").strip()
    return shlex.split(raw) if raw else []


def codex_model_args() -> list[str]:
    args: list[str] = []
    model = os.environ.get("CSBOT_CODEX_MODEL", "").strip()
    if model:
        args.extend(["-m", model])
    effort = os.environ.get("CSBOT_CODEX_REASONING_EFFORT", "low").strip()
    if effort and effort.lower() not in DISABLED_VALUES:
        args.extend(["-c", f"model_reasoning_effort={_toml_string(effort)}"])
    return args


def codex_output_schema_args(schema_path: str | Path) -> list[str]:
    raw = os.environ.get("CSBOT_CODEX_OUTPUT_SCHEMA", "").strip().lower()
    if raw not in {"1", "true", "yes", "on", "enabled"}:
        return []
    return ["--output-schema", str(schema_path)]


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _disabled_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in DISABLED_VALUES


def isolated_codex_home_path() -> Path | None:
    if _disabled_env("CSBOT_CODEX_ISOLATED_HOME", "1"):
        return None
    raw = os.environ.get("CSBOT_CODEX_HOME", "").strip()
    return Path(raw).expanduser() if raw else ensure_state_dir() / ISOLATED_HOME_DIRNAME


def _extract_provider_block(config_text: str, provider_name: str) -> str:
    marker = f"[model_providers.{provider_name}]"
    start = config_text.find(marker)
    if start < 0:
        return ""
    next_section = config_text.find("\n[", start + len(marker))
    return config_text[start:] if next_section < 0 else config_text[start:next_section].rstrip()


def ensure_isolated_codex_home() -> IsolatedHomeStatus:
    home = isolated_codex_home_path()
    if home is None:
        return IsolatedHomeStatus(enabled=False)
    source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    source_config = source_home / "config.toml"
    source_auth = source_home / "auth.json"
    if not source_config.exists():
        return IsolatedHomeStatus(enabled=True, path=str(home), error=f"missing source config: {source_config}")
    try:
        home.mkdir(parents=True, exist_ok=True)
        generated = False
        if source_auth.exists():
            target_auth = home / "auth.json"
            if not target_auth.exists() or target_auth.stat().st_mtime < source_auth.stat().st_mtime:
                shutil.copy2(source_auth, target_auth)
                generated = True
        config_text = source_config.read_text(encoding="utf-8")
        provider = os.environ.get("CSBOT_CODEX_PROVIDER", "codex_local_access")
        model = os.environ.get("CSBOT_CODEX_MODEL", "gpt-5.5")
        effort = os.environ.get("CSBOT_CODEX_REASONING_EFFORT", "low")
        codex_workdir = resolve_codex_workdir()
        provider_block = _extract_provider_block(config_text, provider)
        if not provider_block:
            return IsolatedHomeStatus(enabled=True, path=str(home), error=f"missing provider block: {provider}")
        target_config_text = "\n".join(
            [
                f"model = {_toml_string(model)}",
                f"model_reasoning_effort = {_toml_string(effort)}",
                'approval_policy = "never"',
                f"model_provider = {_toml_string(provider)}",
                f'[projects.{_toml_string(codex_workdir)}]',
                'trust_level = "trusted"',
                "",
                "[model_providers]",
                "",
                provider_block,
                "",
            ]
        )
        target_config = home / "config.toml"
        if not target_config.exists() or target_config.read_text(encoding="utf-8") != target_config_text:
            target_config.write_text(target_config_text, encoding="utf-8")
            generated = True
        (home / "AGENTS.md").write_text("", encoding="utf-8")
        return IsolatedHomeStatus(enabled=True, path=str(home), generated=generated)
    except Exception as exc:
        return IsolatedHomeStatus(enabled=True, path=str(home), error=str(exc))


def _catalog_path_from_env() -> Path | None:
    raw = os.environ.get("CSBOT_CODEX_MODEL_CATALOG", "").strip()
    if raw.lower() in DISABLED_VALUES:
        return None
    if raw:
        return Path(raw).expanduser()
    return ensure_state_dir() / DEFAULT_CATALOG_FILENAME


def ensure_model_catalog(command: str | None = None) -> ModelCatalogStatus:
    catalog_path = _catalog_path_from_env()
    if catalog_path is None:
        return ModelCatalogStatus(enabled=False)
    if catalog_path.exists() and catalog_path.stat().st_size > 0:
        return ModelCatalogStatus(enabled=True, path=str(catalog_path), generated=False)

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    codex_command = command or resolve_codex_command()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(catalog_path.parent)) as tmp:
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            [codex_command, "debug", "models", "--bundled"],
            text=True,
            capture_output=True,
            timeout=45,
        )
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or f"exit_code={proc.returncode}").strip()
            return ModelCatalogStatus(enabled=True, path=str(catalog_path), error=error)
        parsed = json.loads(proc.stdout)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("models"), list):
            return ModelCatalogStatus(enabled=True, path=str(catalog_path), error="bundled_catalog_missing_models")
        tmp_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(catalog_path)
        return ModelCatalogStatus(enabled=True, path=str(catalog_path), generated=True)
    except Exception as exc:
        return ModelCatalogStatus(enabled=True, path=str(catalog_path), error=str(exc))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def codex_catalog_config_args(command: str | None = None) -> tuple[list[str], ModelCatalogStatus]:
    status = ensure_model_catalog(command)
    if status.enabled and status.path and not status.error:
        return ["-c", f"model_catalog_json={_toml_string(status.path)}"], status
    return [], status
