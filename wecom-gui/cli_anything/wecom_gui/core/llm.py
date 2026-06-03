"""AI reply drafting backends."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib import request


SYSTEM_PROMPT = (
    "You are a careful enterprise customer-service assistant. "
    "Reply in Chinese unless the customer used another language. "
    "Be concise, helpful, and avoid promises you cannot verify. "
    "If the customer asks for a human, says complaint, refund escalation, "
    "or the context is unclear, ask for human handoff instead of pretending."
)


ANALYSIS_LEAK_PATTERNS = (
    re.compile(r"^\s*I now have all the information I need\b", re.I),
    re.compile(r"\bLet me analyze the situation\b", re.I),
    re.compile(r"\*\*Context from conversation:\*\*", re.I),
    re.compile(r"\*\*Key knowledge from (?:PG|script|SQLite|KB)", re.I),
    re.compile(r"^\s*Context from conversation:\s*$", re.I | re.M),
    re.compile(r"^\s*Key knowledge from .+?:\s*$", re.I | re.M),
)
INTERNAL_REPLY_REPLACEMENTS = {
    "知识库中没有记录": "我这边暂时没有查到明确资料",
    "知识库没有记录": "我这边暂时没有查到明确资料",
    "数据库中没有记录": "我这边暂时没有查到明确资料",
    "数据库没有记录": "我这边暂时没有查到明确资料",
    "知识库": "资料",
    "数据库": "资料",
    "免责话术": "使用提醒",
    "合规话术": "使用提醒",
    "系统提示": "规则",
    "提示词": "规则",
    "内部资料": "资料",
    "内部规则": "规则",
    "检索": "查询",
    "prompt": "规则",
    "tool": "工具",
}

def build_prompt(messages: list[dict]) -> list[dict]:
    """Build a chat-completions message list from GUI-extracted context."""
    transcript = "\n".join(f"{m.get('role', 'unknown')}: {m.get('text', '')}" for m in messages)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Recent WeCom chat transcript:\n{transcript}\n\nDraft one reply."},
    ]


def build_uda_history(
    messages: list[dict],
    *,
    mode: str = "recent",
    max_messages: int = 8,
) -> list[dict]:
    """Build UDA single_question history payload.

    The UDA endpoint expects `type` to be `human`; we preserve actual speaker
    roles inside `data.content` as requested: "用户: ..." / "客服: ...".
    """
    selected_messages = messages
    if mode == "latest_user":
        for msg in reversed(messages):
            if msg.get("role") == "用户" and (msg.get("content") or msg.get("text")):
                selected_messages = [msg]
                break
    elif mode == "recent":
        latest_user_index = None
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.get("role") == "用户" and (msg.get("content") or msg.get("text")):
                latest_user_index = idx
                break
        if latest_user_index is not None:
            start = max(0, latest_user_index - max_messages + 1)
            selected_messages = messages[start : latest_user_index + 1]
    elif mode != "full":
        raise ValueError("UDA history mode must be latest_user, recent, or full")

    history: list[dict] = []
    for msg in selected_messages:
        role = msg.get("role") or "用户"
        content = msg.get("content") or msg.get("text") or ""
        if not content:
            continue
        history.append(
            {
                "type": "human",
                "data": {
                    "content": f"{role}: {content}",
                },
            }
        )
    return history


def build_codex_prompt(messages: list[dict]) -> str:
    """Build a strict prompt for Codex-based customer-service drafting."""
    transcript = "\n".join(
        f"{m.get('role') or '未知'}: {m.get('content') or m.get('text') or ''}"
        for m in messages
        if m.get("content") or m.get("text")
    )
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "You are drafting a WeCom customer-service reply for the latest customer message.\n"
        "Return only the reply text to send to the customer. Do not include Markdown, analysis, "
        "quotes, labels, or explanations.\n\n"
        f"Recent WeCom chat transcript:\n{transcript}\n"
    )


def latest_user_text(messages: list[dict]) -> str:
    """Return the latest customer message text from GUI-extracted messages."""
    for message in reversed(messages):
        if message.get("role") != "用户":
            continue
        text = str(message.get("content") or message.get("text") or "").strip()
        if text:
            return text
    return ""


def latest_user_turn_text(messages: list[dict]) -> str:
    """Return all consecutive customer messages after the latest service reply."""
    turn: list[str] = []
    for message in latest_user_turn_messages(messages):
        text = str(message.get("content") or message.get("text") or "").strip()
        if not text:
            continue
        if text != "[图片]" or message_image_paths([message]):
            turn.append(text)
    return "\n".join(turn)


def _message_has_image(message: dict) -> bool:
    for media in message.get("media") or []:
        if not isinstance(media, dict):
            continue
        media_type = str(media.get("type") or "image").strip()
        if media_type == "image" and not media.get("skip_capture"):
            return True
    return False


def _is_preview_turn_anchor(message: dict) -> bool:
    return str(message.get("role_confidence") or "") in {"preview_fallback", "unread_preview_after_image"}


def latest_user_turn_messages(messages: list[dict]) -> list[dict]:
    """Return consecutive customer messages after the latest service reply."""
    for index in range(len(messages) - 1, -1, -1):
        if not _is_preview_turn_anchor(messages[index]):
            continue
        start = index
        while start > 0:
            previous = messages[start - 1]
            if _is_preview_turn_anchor(previous) or _message_has_image(previous):
                start -= 1
                continue
            break
        return messages[start : index + 1]

    turn: list[dict] = []
    for message in reversed(messages):
        role = message.get("role")
        text = str(message.get("content") or message.get("text") or "").strip()
        has_image = _message_has_image(message)
        if not text and not has_image:
            continue
        if role == "用户":
            turn.append(message)
            continue
        if turn:
            break
    turn.reverse()
    return turn


def message_image_paths(messages: list[dict]) -> list[str]:
    paths: list[str] = []
    for message in messages:
        for media in message.get("media") or []:
            if not isinstance(media, dict):
                continue
            path = str(media.get("capture_path") or "").strip()
            if path and media.get("capture_ok", True):
                paths.append(path)
    return paths


def latest_user_turn_image_paths(messages: list[dict]) -> list[str]:
    return message_image_paths(latest_user_turn_messages(messages))


def build_csbot_context(
    messages: list[dict],
    *,
    customer_name: str = "",
    agent_mode: str = "",
    agent_context: dict | None = None,
) -> dict:
    """Build compact context for the csbot autonomous worker."""
    history = []
    for message in messages:
        text = str(message.get("content") or message.get("text") or "").strip()
        if not text:
            continue
        item = {"role": message.get("role") or "未知", "text": text}
        media_payload = []
        for media in message.get("media") or []:
            if not isinstance(media, dict):
                continue
            media_item = {
                "type": media.get("type") or "image",
                "capture_path": str(media.get("capture_path") or ""),
                "capture_ok": bool(media.get("capture_ok")),
                "error": str(media.get("error") or ""),
            }
            media_payload.append(media_item)
        if media_payload:
            item["media"] = media_payload
        history.append(item)
    context = {"source": "wecom-gui", "messages": history, "known_facts": {}}
    mode = str(agent_mode or "").strip()
    if mode:
        context["agent_mode"] = mode
    if agent_context:
        context["agent_context"] = agent_context
    image_paths = latest_user_turn_image_paths(messages)
    if image_paths:
        context["image_paths"] = image_paths
    customer_name = customer_name.strip()
    if customer_name:
        context["customer_name"] = customer_name
        context["conversation_title"] = customer_name
    return context


def _json_request(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def draft_reply_uda(messages: list[dict]) -> dict:
    """Draft a reply via UDA's unified chat API."""
    api_key = os.environ.get("WECOM_GUI_UDA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("WECOM_GUI_UDA_API_KEY is not set.")
    url = os.environ.get(
        "WECOM_GUI_UDA_URL",
        "https://wework-unified-api.uda.cn/chat/single_question",
    ).strip()
    history_mode = os.environ.get("WECOM_GUI_UDA_HISTORY_MODE", "recent").strip()
    max_messages = int(os.environ.get("WECOM_GUI_UDA_HISTORY_MAX", "8"))
    payload = {
        "history": build_uda_history(messages, mode=history_mode, max_messages=max_messages),
        "ai_reply": True,
    }
    body = _json_request(
        url,
        payload,
        {
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    message = None
    if isinstance(body.get("data"), dict):
        message = body["data"].get("message")
    if message is None:
        message = body.get("message")
    if not message:
        raise RuntimeError(f"UDA response missing message: {body}")
    return {
        "ok": True,
        "provider": "uda-single-question",
        "text": message,
        "message": message,
        "raw": body,
    }


def _csbot_project_dir() -> Path:
    raw = os.environ.get("WECOM_GUI_CSBOT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[4] / "codex-csbot-wecom"


def _csbot_python() -> str:
    return os.environ.get("WECOM_GUI_CSBOT_PYTHON") or os.environ.get("WECOM_GUI_PYTHON") or "/opt/homebrew/bin/python3"


def _codex_customer_id(customer_uid: str = "", *, agent_context: dict | None = None) -> str:
    explicit_id = str(customer_uid or "").strip()
    if not explicit_id and isinstance(agent_context, dict):
        for key in ("customer_key", "customer_id", "external_user_id", "wecom_uid"):
            value = str(agent_context.get(key) or "").strip()
            if value:
                explicit_id = value
                break
    return explicit_id or os.environ.get("WECOM_GUI_CSBOT_CUSTOMER_ID", "wecom-customer").strip() or "wecom-customer"


def _csbot_db_path() -> str:
    return os.environ.get("WECOM_GUI_CSBOT_DB", os.environ.get("CSBOT_DB", os.environ.get("CSBOT_STATE_DB", ""))).strip()


def _decode_json_string_at(text: str, start: int) -> tuple[str, int] | None:
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(value, str):
        return None
    return value.strip(), start + end


def _extract_reply_text_from_stdout(stdout: str) -> str:
    """Recover customer-facing text from malformed autonomous-worker stdout."""
    text = str(stdout or "")
    if not text.strip():
        return ""

    match = re.search(
        r'"reply_text"\s*:\s*"(?P<reply>.*?)(?="\s*,\s*\n\s*"'
        r'(?:used_script_sources|used_vector_memories|confidence|commands_run|conflicts|retrieval_summary|decision_basis)"\s*:)',
        text,
        flags=re.DOTALL,
    )
    if match:
        return (
            match.group("reply")
            .replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\/", "/")
            .strip()
        )

    marker = '"reply_text"'
    idx = text.find(marker)
    while idx >= 0:
        colon = text.find(":", idx + len(marker))
        if colon < 0:
            break
        quote = text.find('"', colon + 1)
        if quote < 0:
            break
        decoded = _decode_json_string_at(text, quote)
        if decoded and decoded[0]:
            return decoded[0]
        idx = text.find(marker, idx + len(marker))

    return ""


def _looks_like_analysis_leak(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in ANALYSIS_LEAK_PATTERNS)


def _validate_customer_reply_text(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    if _looks_like_analysis_leak(text):
        raise RuntimeError("AI output contains analysis/debug text; refusing to send.")
    for old, new in INTERNAL_REPLY_REPLACEMENTS.items():
        text = text.replace(old, new)
    return text


def draft_reply_codex_direct(messages: list[dict]) -> dict:
    """Draft a reply by calling Codex CLI directly, preserving the original backend."""
    command = os.environ.get("WECOM_GUI_CODEX_COMMAND", "codex").strip() or "codex"
    model = os.environ.get("WECOM_GUI_CODEX_MODEL", "").strip()
    timeout = float(os.environ.get("WECOM_GUI_CODEX_TIMEOUT", "120"))
    prompt = build_codex_prompt(messages)

    with tempfile.NamedTemporaryFile("r+", encoding="utf-8") as out:
        cmd = [
            command,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            out.name,
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")

        result = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        out.seek(0)
        message = out.read().strip()

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Codex draft failed with exit code {result.returncode}: {detail}")
    if not message:
        message = (result.stdout or "").strip()
    if not message:
        raise RuntimeError("Codex draft returned an empty reply.")
    return {
        "ok": True,
        "provider": "codex-cli-direct",
        "model": model or None,
        "text": message,
        "message": message,
    }


def draft_reply_codex_csbot(
    messages: list[dict],
    *,
    customer_name: str = "",
    customer_uid: str = "",
    agent_mode: str = "",
    agent_context: dict | None = None,
) -> dict:
    """Draft via the sibling csbot autonomous worker with KB/MEM0 retrieval."""
    query = latest_user_turn_text(messages) or latest_user_text(messages)
    if not query:
        raise RuntimeError("No latest user message found for csbot autonomous worker.")

    timeout = float(os.environ.get("WECOM_GUI_CODEX_TIMEOUT", "120"))
    project_dir = _csbot_project_dir()
    customer_id = _codex_customer_id(customer_uid, agent_context=agent_context)
    context_json = json.dumps(
        {
            **build_csbot_context(
                messages,
                customer_name=customer_name,
                agent_mode=agent_mode,
                agent_context=agent_context,
            ),
            **({"external_user_id": customer_uid, "wecom_uid": customer_uid} if customer_uid else {"customer_id": customer_id}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cmd = [
        _csbot_python(),
        "-m",
        "csbot",
        "autonomous-reply",
        "--customer-id",
        customer_id,
        "--query",
        query,
        "--context-json",
        context_json,
        "--timeout",
        str(int(timeout)),
    ]
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout + 10,
        check=False,
        cwd=str(project_dir),
        env={**os.environ, "CSBOT_MEM0_URL": os.environ.get("CSBOT_MEM0_URL", "http://127.0.0.1:8888")},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"csbot autonomous draft failed with exit code {result.returncode}: {detail}")
    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"csbot autonomous draft returned non-JSON: {result.stdout[:500]}") from exc
    codex = body.get("codex") if isinstance(body.get("codex"), dict) else {}
    reply = codex.get("reply") if isinstance(codex.get("reply"), dict) else {}
    duration_ms = codex.get("duration_ms")
    worker_info = codex.get("codex_cli") if isinstance(codex.get("codex_cli"), dict) else {}
    worker_label = str(worker_info.get("provider") or "worker")
    model_label = str(worker_info.get("model") or "")
    validation = codex.get("validation") if isinstance(codex.get("validation"), dict) else {}
    parse_error = str(codex.get("parse_error") or "").strip()
    message = _validate_customer_reply_text(str(reply.get("reply_text") or "").strip())
    recovered_from_stdout = False
    action = str(reply.get("action") or "").strip()
    if action == "handoff":
        message = message or "这个问题我需要转人工帮您确认。"
    if not message:
        message = _extract_reply_text_from_stdout(str(codex.get("stdout") or ""))
        message = _validate_customer_reply_text(message)
        recovered_from_stdout = bool(message)
    if not message:
        detail = {
            "worker": worker_label,
            "model": model_label,
            "duration_ms": duration_ms,
            "parse_error": parse_error,
            "validation": validation,
            "exit_code": codex.get("exit_code"),
            "reply": reply,
            "last_message": str(codex.get("last_message") or "")[:1000],
        }
        raise RuntimeError(f"csbot autonomous draft returned empty reply: {detail}")
    return {
        "ok": True,
        "provider": "csbot-autonomous",
        "text": _validate_customer_reply_text(message),
        "message": _validate_customer_reply_text(message),
        "action": action or None,
        "duration_ms": duration_ms,
        "worker": worker_label,
        "model": model_label,
        "recovered_from_stdout": recovered_from_stdout,
        "raw": body,
    }


def draft_reply_codex(
    messages: list[dict],
    *,
    customer_name: str = "",
    customer_uid: str = "",
    agent_mode: str = "",
    agent_context: dict | None = None,
) -> dict:
    """Draft a reply through the configured Codex backend."""
    backend = os.environ.get("WECOM_GUI_CODEX_BACKEND", "csbot").strip().lower()
    if backend in {"direct", "cli", "codex-cli"}:
        return draft_reply_codex_direct(messages)
    return draft_reply_codex_csbot(
        messages,
        customer_name=customer_name,
        customer_uid=customer_uid,
        agent_mode=agent_mode,
        agent_context=agent_context,
    )


def draft_reply(
    messages: list[dict],
    *,
    fallback: str | None = None,
    provider: str | None = None,
    customer_name: str = "",
    customer_uid: str = "",
    agent_mode: str = "",
    agent_context: dict | None = None,
) -> dict:
    """Draft a reply using UDA/OpenAI-compatible API, or return fallback text."""
    selected = (provider or os.environ.get("WECOM_GUI_AI_PROVIDER", "uda")).strip().lower()
    if selected == "fallback":
        text = fallback or "您好，我已收到您的消息，这边马上帮您确认。"
        return {"ok": True, "provider": "fallback", "text": text, "message": text}

    if selected == "uda":
        if os.environ.get("WECOM_GUI_UDA_API_KEY", "").strip():
            return draft_reply_uda(messages)
        raise RuntimeError("WECOM_GUI_UDA_API_KEY is not set.")

    if selected == "codex":
        return draft_reply_codex(
            messages,
            customer_name=customer_name,
            customer_uid=customer_uid,
            agent_mode=agent_mode,
            agent_context=agent_context,
        )

    if selected == "pi":
        return draft_reply_codex(
            messages,
            customer_name=customer_name,
            customer_uid=customer_uid,
            agent_mode=agent_mode,
            agent_context=agent_context,
        )

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"OPENAI_API_KEY is not set for provider {selected}.")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    body = _json_request(
        f"{base_url}/chat/completions",
        {"model": model, "messages": build_prompt(messages), "temperature": 0.2},
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    text = body["choices"][0]["message"]["content"].strip()
    return {"ok": True, "provider": "openai-compatible", "model": model, "text": text, "message": text}
