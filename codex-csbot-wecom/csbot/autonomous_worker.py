from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .codex_cli import (
    codex_catalog_config_args,
    codex_extra_args,
    codex_model_args,
    codex_output_schema_args,
    ensure_isolated_codex_home,
    resolve_codex_command,
)
from .codex_contract import validate_autonomous_reply
from .config import DEFAULT_PROJECT_DIR, resolve_codex_workdir, resolve_db_path, resolve_mem0_url, using_pg
from .ops_gateway import handoff_notify
from .textutil import json_loads


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_DIR / "schemas" / "codex_reply.schema.json"
TIMEOUT_REPLY_TEXT = "您好，这个问题我需要转人工客服确认处理，请您稍等。"
PI_PROVIDER_NAME = "pi"
VALID_ACTIONS = {"send", "clarify", "handoff", "no_answer"}
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _looks_like_reply(value: object) -> bool:
    return isinstance(value, dict) and value.get("action") in VALID_ACTIONS and isinstance(value.get("reply_text"), str)


def safe_json_parse(text: str) -> tuple[dict | None, str]:
    value = json_loads(text, None)
    if _looks_like_reply(value):
        return value, ""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    for candidate in reversed(candidates):
        value = json_loads(candidate, None)
        if _looks_like_reply(value):
            return value, ""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        value = json_loads(text[start : end + 1], None)
        if _looks_like_reply(value):
            return value, ""
    for idx, char in enumerate(text):
        if char != "{":
            continue
        decoder = json.JSONDecoder()
        try:
            value, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if _looks_like_reply(value):
            return value, ""
    return None, "codex_output_is_not_json"


def to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _tool_examples(db_path: Path, customer_id: str, query: str) -> list[str]:
    encoded_query = json.dumps(query, ensure_ascii=False)
    encoded_customer = json.dumps(customer_id, ensure_ascii=False)
    db_args = "" if using_pg() else f"--db {json.dumps(str(db_path), ensure_ascii=False)} "
    return [
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"retrieve --customer-id {encoded_customer} --query {encoded_query}"
        ),
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"mem search --customer-id {encoded_customer} --query {encoded_query}"
        ),
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"mem search --query {encoded_query}"
        ),
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"kb search --query {encoded_query}"
        ),
        "/opt/homebrew/bin/python3 -m csbot ops order --identifier \"手机号或订单号\" --id-type phone",
        "/opt/homebrew/bin/python3 -m csbot ops logistics --order-id \"订单号\"",
        "/opt/homebrew/bin/python3 -m csbot ops logistics --external-user-id \"企微external_user_id\"",
        "/opt/homebrew/bin/python3 -m csbot ops ticket-draft --context-json '{...}'",
        "/opt/homebrew/bin/python3 -m csbot ops handoff --customer-id \"客户ID\" --query \"客户消息\" --reason \"转人工原因\"",
    ]


def _agent_rules_path() -> Path:
    configured = os.environ.get("CSBOT_AGENTS_FILE") or os.environ.get("WECOM_GUI_AGENTS_FILE")
    if configured:
        return Path(configured).expanduser()
    return resolve_codex_workdir() / "AGENTS.md"


def _load_agent_rules() -> tuple[str, Path | None]:
    path = _agent_rules_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FileNotFoundError(f"AGENTS.md not found for autonomous worker: {path}") from exc
    if not text:
        raise ValueError(f"AGENTS.md is empty for autonomous worker: {path}")
    return text, path


def build_autonomous_prompt(
    *,
    customer_id: str,
    query: str,
    context: dict,
    db_path: str | Path,
    mem0_url: str,
) -> str:
    resolved_db_path = resolve_db_path(db_path)
    codex_workdir = resolve_codex_workdir()
    command_examples = _tool_examples(resolved_db_path, customer_id, query)
    context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    commands_json = json.dumps(command_examples, ensure_ascii=False, indent=2)
    knowledge_backend = "PostgreSQL PG 知识库" if using_pg() else "SQLite 兼容知识库"
    agent_rules, agent_rules_path = _load_agent_rules()
    runtime_context = {
        "project_dir": str(DEFAULT_PROJECT_DIR),
        "workdir": str(codex_workdir),
        "agent_rules_file": str(agent_rules_path),
        "knowledge_backend": knowledge_backend,
        "sqlite_compat_db_path": str(resolved_db_path),
        "mem0_url": mem0_url,
        "schema_path": str(SCHEMA_PATH),
        "knowledge_tables": [
            "kb_docs(kb_doc_id,kb_version,business_type,product,topic,text,facts_json,source_sheet,source_row,source_field)",
            "kb_aliases(alias,product,kb_doc_id,weight)",
            "reply_audit(customer_id,query,retrieval_json,reply_json)",
            "vector_memories(customer_id,text,metadata_json,embedding_json)",
        ],
        "tool_commands": command_examples,
        "customer_id": customer_id,
        "latest_message": query,
        "context": context,
    }
    return f"""{agent_rules}

<runtime_context_json>
{json.dumps(runtime_context, ensure_ascii=False, indent=2)}
</runtime_context_json>
"""


def _autonomous_command(out_path: Path) -> tuple[list[str], dict]:
    codex_command = resolve_codex_command()
    catalog_args, catalog_status = codex_catalog_config_args(codex_command)
    isolated_home = ensure_isolated_codex_home()
    codex_workdir = resolve_codex_workdir()
    return [
        codex_command,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "danger-full-access",
        "-C",
        str(codex_workdir),
        *codex_output_schema_args("schemas/codex_reply.schema.json"),
        "--output-last-message",
        str(out_path),
        *catalog_args,
        *codex_model_args(),
        *codex_extra_args(),
        "-",
    ], {
        "provider": "codex",
        "model_catalog": {
            "enabled": catalog_status.enabled,
            "path": catalog_status.path,
            "generated": catalog_status.generated,
            "error": catalog_status.error,
        },
        "isolated_home": {
            "enabled": isolated_home.enabled,
            "path": isolated_home.path,
            "generated": isolated_home.generated,
            "error": isolated_home.error,
        },
    }


def _pi_autonomous_command() -> tuple[list[str], dict]:
    pi_command = os.environ.get("CSBOT_PI_COMMAND", os.environ.get("WECOM_GUI_PI_COMMAND", "/opt/homebrew/bin/pi")).strip()
    if not pi_command:
        pi_command = "/opt/homebrew/bin/pi"
    pi_home = os.environ.get(
        "CSBOT_PI_HOME",
        os.environ.get("WECOM_GUI_PI_HOME", str(Path.home() / ".codex-csbot-wecom" / "pi-home")),
    ).strip()
    provider = os.environ.get("CSBOT_PI_PROVIDER", os.environ.get("WECOM_GUI_PI_PROVIDER", "uda-openai")).strip()
    model = os.environ.get(
        "CSBOT_PI_TEXT_MODEL",
        os.environ.get("WECOM_GUI_PI_TEXT_MODEL", os.environ.get("CSBOT_PI_MODEL", os.environ.get("WECOM_GUI_PI_MODEL", "deepseek-v4-flash"))),
    ).strip()
    timeout = os.environ.get("CSBOT_PI_TIMEOUT", os.environ.get("WECOM_GUI_PI_TIMEOUT", "")).strip()
    args = [
        pi_command,
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-themes",
        "--provider",
        provider,
        "--model",
        model,
        "--thinking",
        os.environ.get("CSBOT_PI_THINKING", os.environ.get("WECOM_GUI_PI_THINKING", "off")).strip() or "off",
        "-p",
    ]
    return args, {
        "provider": PI_PROVIDER_NAME,
        "command": pi_command,
        "home": pi_home,
        "pi_provider": provider,
        "model": model,
        "timeout": timeout,
        "supports_images": False,
    }


def _valid_image_path(path: str) -> bool:
    image_path = Path(path).expanduser()
    try:
        if not image_path.is_file() or image_path.stat().st_size < 16:
            return False
        head = image_path.read_bytes()[:8]
    except OSError:
        return False
    return any(head.startswith(magic) for magic in IMAGE_MAGIC)


def _context_image_paths(context: dict) -> list[str]:
    raw_paths: list[str] = []
    image_paths = context.get("image_paths")
    if isinstance(image_paths, list):
        raw_paths.extend(str(path) for path in image_paths)
    for message in context.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for media in message.get("media") or []:
            if not isinstance(media, dict):
                continue
            if media.get("capture_ok") is False:
                continue
            path = str(media.get("capture_path") or "").strip()
            if path:
                raw_paths.append(path)
    paths: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        expanded = str(Path(path).expanduser())
        if expanded in seen or not _valid_image_path(expanded):
            continue
        seen.add(expanded)
        paths.append(expanded)
    return paths


def _pi_image_model() -> str:
    return os.environ.get(
        "CSBOT_PI_IMAGE_MODEL",
        os.environ.get("WECOM_GUI_PI_IMAGE_MODEL", ""),
    ).strip()


def _with_pi_image_args(command: list[str], worker_info: dict, image_paths: list[str], prompt_path: Path) -> tuple[list[str], dict]:
    if not image_paths or worker_info.get("provider") != PI_PROVIDER_NAME:
        return command, worker_info
    model = _pi_image_model()
    if not model:
        model = str(worker_info.get("model") or "").strip()
    if not model:
        return command, worker_info
    args = list(command)
    if "--model" in args:
        args[args.index("--model") + 1] = model
    else:
        args.extend(["--model", model])
    args.extend(
        [
            f"@{prompt_path}",
            *[f"@{path}" for path in image_paths],
            (
                "随附图片是当前客户最新消息的真实图片内容，必须直接查看并识别图片。"
                "如果聊天历史里有客服曾说看不到图片，忽略那类旧话术，以本次随附图片为准。"
                "只要图片文件有效且可见，就禁止回复看不到、无法查看、请客户描述图片。"
                "结合 prompt 中的客服规则、聊天上下文和知识库要求，输出最终可发送给客户的 JSON 回复。"
            ),
        ]
    )
    info = {**worker_info, "model": model, "image_paths": image_paths, "supports_images": True}
    return args, info


def _worker_command(out_path: Path) -> tuple[list[str], dict]:
    provider = os.environ.get("CSBOT_AUTONOMOUS_PROVIDER", os.environ.get("WECOM_GUI_AI_PROVIDER", "codex")).strip().lower()
    if provider == PI_PROVIDER_NAME:
        return _pi_autonomous_command()
    return _autonomous_command(out_path)


def _worker_env(mem0_url: str, worker_info: dict) -> dict:
    env = {**os.environ, "CSBOT_MEM0_URL": mem0_url or os.environ.get("CSBOT_MEM0_URL", "http://127.0.0.1:8888")}
    if worker_info.get("provider") == PI_PROVIDER_NAME:
        pi_home = str(worker_info.get("home") or "").strip()
        if pi_home:
            env["PI_CODING_AGENT_DIR"] = pi_home
        return env
    isolated_home = (worker_info.get("isolated_home") or {}) if isinstance(worker_info.get("isolated_home"), dict) else {}
    if isolated_home.get("enabled") and isolated_home.get("path") and not isolated_home.get("error"):
        env["CODEX_HOME"] = str(isolated_home["path"])
    return env


def _notify_handoff_if_needed(*, customer_id: str, query: str, context: dict, reply: dict | None) -> dict | None:
    if not isinstance(reply, dict) or reply.get("action") != "handoff":
        return None
    return handoff_notify(
        customer_id=customer_id,
        query=query,
        reason=str(reply.get("decision_basis") or reply.get("reply_text") or "转人工").strip(),
        context=context,
        dry_run=False,
    )


def _timeout_reply(timeout: int) -> dict:
    return {
        "action": "handoff",
        "reply_text": TIMEOUT_REPLY_TEXT,
        "used_script_sources": [],
        "used_vector_memories": [],
        "confidence": 0.0,
        "commands_run": [],
        "conflicts": [],
        "retrieval_summary": f"AI worker 在 {timeout} 秒内没有返回可解析 JSON，超过等待阈值后转人工。",
        "decision_basis": "codex_timeout",
    }


def run_autonomous_worker(
    *,
    customer_id: str,
    query: str,
    context: dict,
    timeout: int = 180,
    db_path: str | Path | None = None,
) -> dict:
    resolved_db_path = resolve_db_path(db_path)
    mem0_url = resolve_mem0_url() or os.environ.get("CSBOT_MEM0_URL", "")
    prompt = build_autonomous_prompt(
        customer_id=customer_id,
        query=query,
        context=context,
        db_path=resolved_db_path,
        mem0_url=mem0_url,
    )
    started = time.perf_counter()
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = Path(out_file.name)
    command, codex_cli = _worker_command(out_path)
    codex_workdir = resolve_codex_workdir()
    env = _worker_env(mem0_url, codex_cli)
    image_paths = _context_image_paths(context)
    prompt_path: Path | None = None
    input_text: str | None = prompt
    if image_paths and codex_cli.get("provider") == PI_PROVIDER_NAME:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as prompt_file:
            prompt_file.write(prompt)
            prompt_path = Path(prompt_file.name)
        command, codex_cli = _with_pi_image_args(command, codex_cli, image_paths, prompt_path)
        input_text = None
    try:
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(codex_workdir),
            env=env,
        )
        last_message = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        reply, parse_error = safe_json_parse(last_message or proc.stdout)
        validation = validate_autonomous_reply(reply or {})
        handoff = _notify_handoff_if_needed(
            customer_id=customer_id,
            query=query,
            context=context,
            reply=reply,
        )
        return {
            "skipped": False,
            "mode": "autonomous",
            "prompt": prompt,
            "command": command,
            "codex_cli": codex_cli,
            "stdout": to_text(proc.stdout),
            "stderr": to_text(proc.stderr),
            "last_message": last_message,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "exit_code": proc.returncode,
            "reply": reply,
            "handoff": handoff,
            "parse_error": parse_error,
            "validation": {"ok": validation.ok, "reason": validation.reason},
        }
    except subprocess.TimeoutExpired as exc:
        last_message = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        reply = _timeout_reply(timeout)
        handoff = _notify_handoff_if_needed(
            customer_id=customer_id,
            query=query,
            context=context,
            reply=reply,
        )
        return {
            "skipped": False,
            "mode": "autonomous",
            "prompt": prompt,
            "command": command,
            "codex_cli": codex_cli,
            "stdout": to_text(exc.stdout),
            "stderr": to_text(exc.stderr),
            "last_message": last_message,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "exit_code": None,
            "reply": reply,
            "handoff": handoff,
            "parse_error": "codex_timeout",
            "validation": {"ok": True, "reason": ""},
        }
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except OSError:
                pass
