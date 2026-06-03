"""Local/LAN review server for approving AI reply drafts."""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
import uuid
import base64
import hashlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cli_anything.wecom_gui import __version__
from cli_anything.wecom_gui.core import agent, llm, message_config, state
from cli_anything.wecom_gui.core.text import clean_customer_reply_text, clean_history_message_text
from cli_anything.wecom_gui.utils import macos_backend


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8122
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend" / "review"
SUPPLEMENT_TEST_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend" / "supplement-test"
SUPPLEMENT_FULL_TEST_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend" / "supplement-full-test"
_SUPPLEMENT_TEST_VERSION_LOCK = threading.Lock()
_SUPPLEMENT_TEST_VERSIONS: dict[str, int] = {}


def _load_frontend_text(name: str, *, root: Path = FRONTEND_DIR) -> str:
    try:
        return (root / name).read_text(encoding="utf-8")
    except OSError:
        return ""


REVIEW_HTML = _load_frontend_text("index.html")
REVIEW_FRONTEND_JS = _load_frontend_text("app.js")
SUPPLEMENT_TEST_HTML = _load_frontend_text("index.html", root=SUPPLEMENT_TEST_FRONTEND_DIR)
SUPPLEMENT_FULL_TEST_HTML = _load_frontend_text("index.html", root=SUPPLEMENT_FULL_TEST_FRONTEND_DIR)
SUPPLEMENT_WELCOME_TEMPLATE = message_config.fixed_message("welcome", "supplement_web_welcome_template")


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    value = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _send_file_response(handler: BaseHTTPRequestHandler, path: Path) -> None:
    try:
        body = path.read_bytes()
    except OSError:
        _json_response(handler, 404, {"ok": False, "error": "media_not_found"})
        return
    if not macos_backend.validate_image_file(path):
        _json_response(handler, 415, {"ok": False, "error": "invalid_image_file"})
        return
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not content_type.startswith("image/"):
        _json_response(handler, 415, {"ok": False, "error": "unsupported_media_type"})
        return
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "private, max-age=300")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_static_response(handler: BaseHTTPRequestHandler, relative_path: str, *, root_dir: Path = FRONTEND_DIR) -> None:
    try:
        target = (root_dir / relative_path.lstrip("/")).resolve()
        root = root_dir.resolve()
    except OSError:
        _json_response(handler, 404, {"ok": False, "error": "static_not_found"})
        return
    if root not in target.parents and target != root:
        _json_response(handler, 404, {"ok": False, "error": "static_not_found"})
        return
    try:
        body = target.read_bytes()
    except OSError:
        _json_response(handler, 404, {"ok": False, "error": "static_not_found"})
        return
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".js":
        content_type = "text/javascript"
    handler.send_response(200)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _attachment_dir() -> Path:
    path = state.state_dir() / "review-attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attachment_ext(filename: str, content_type: str = "") -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "")
    if guessed in {".png", ".jpg", ".jpeg"}:
        return guessed
    return ".png"


def _sanitize_attachment(item: dict) -> dict:
    return {
        "id": str(item.get("id") or uuid.uuid4().hex),
        "type": "image",
        "name": str(item.get("name") or "image"),
        "path": str(item.get("path") or ""),
        "size": int(item.get("size") or 0),
        "content_type": str(item.get("content_type") or "image/png"),
    }


def _current_attachments(job_id: int) -> list[dict]:
    item = state.get_job(job_id) or {}
    attachments = item.get("reply_attachments") if isinstance(item.get("reply_attachments"), list) else []
    return [_sanitize_attachment(attachment) for attachment in attachments if isinstance(attachment, dict)]


def _store_attachment(job_id: int, *, filename: str, content_type: str, body: bytes) -> dict:
    if not body:
        raise ValueError("empty_attachment")
    if len(body) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment_too_large")
    attachment_id = uuid.uuid4().hex
    ext = _attachment_ext(filename, content_type)
    path = _attachment_dir() / f"review-{job_id}-{attachment_id}{ext}"
    path.write_bytes(body)
    if not macos_backend.validate_image_file(path):
        try:
            path.unlink()
        except OSError:
            pass
        raise ValueError("invalid_image_file")
    return {
        "id": attachment_id,
        "type": "image",
        "name": filename or path.name,
        "path": str(path),
        "size": len(body),
        "content_type": mimetypes.guess_type(path.name)[0] or content_type or "image/png",
    }


def _context_latest_text(item: dict) -> str:
    context_raw = str(item.get("context_json") or "").strip()
    if not context_raw:
        return ""
    try:
        context = json.loads(context_raw)
    except json.JSONDecodeError:
        return ""
    latest = context.get("latest") if isinstance(context, dict) else None
    if not isinstance(latest, dict):
        return ""
    return str(latest.get("content") or latest.get("text") or "").strip()


def _fallback_messages(item: dict, latest_text: str) -> list[dict]:
    text = latest_text or str(item.get("preview") or "").strip()
    if not text:
        return []
    return [
        {
            "conversation_key": item.get("conversation_key") or "",
            "job_id": item.get("id"),
            "message_hash": item.get("last_message_hash") or "",
            "seq": 0,
            "role": "用户",
            "message_type": "customer",
            "text": text,
            "time_text": item.get("time_text") or "",
            "source": "context_json" if latest_text else "preview",
            "role_confidence": "",
            "media": [],
            "raw": {},
        }
    ]


def _sanitize_media(message: dict) -> list[dict]:
    media_items = message.get("media") if isinstance(message.get("media"), list) else []
    sanitized: list[dict] = []
    message_id = int(message.get("id") or 0)
    for index, media in enumerate(media_items):
        if not isinstance(media, dict):
            continue
        capture_ok = bool(media.get("capture_path")) and media.get("capture_ok", True) is not False
        item = {
            "type": str(media.get("type") or "image"),
            "capture_ok": capture_ok,
            "error": str(media.get("error") or ""),
            "source": str(media.get("source") or ""),
        }
        if capture_ok and message_id:
            item["url"] = f"/api/review/media/{message_id}/{index}"
        sanitized.append(item)
    return sanitized


def _message_type(message: dict) -> str:
    explicit = str(message.get("message_type") or "").strip().lower()
    if explicit in {"customer", "reply", "system", "unknown"}:
        return explicit
    role = str(message.get("role") or "").strip().lower()
    text = str(message.get("text") or "").strip()
    if "以上是打招呼内容" in text or ("你已添加了" in text and "现在可以开始聊天了" in text):
        return "system"
    if role in {"customer", "user", "human"} or "用户" in role or "客户" in role:
        return "customer"
    if (
        role in {"reply", "service", "assistant", "agent", "staff"}
        or "客服" in role
        or "坐席" in role
    ):
        return "reply"
    return "unknown"


def _position_message_type(message: dict) -> str:
    raw = message.get("raw") if isinstance(message.get("raw"), dict) else {}
    right = message.get("right")
    if right is None:
        right = raw.get("right")
    threshold = raw.get("role_threshold") or message.get("role_threshold") or 760
    try:
        right_value = float(right)
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        return ""
    return "reply" if right_value >= threshold_value else "customer"


def _classified_messages(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    for message in messages:
        message_type = _message_type(message)
        position_type = _position_message_type(message)
        if position_type and str(message.get("role_confidence") or "") in {"medium", "high"}:
            message_type = position_type
        role = (
            "客服"
            if message_type == "reply"
            else "用户"
            if message_type == "customer"
            else "系统"
            if message_type == "system"
            else message.get("role", "")
        )
        items.append(
            {
                **message,
                "role": role,
                "message_type": message_type,
                "media": _sanitize_media(message),
                "text": clean_history_message_text(message.get("text") or ""),
            }
        )
    return items


def _public_reply_attachments(item: dict) -> list[dict]:
    attachments = item.get("reply_attachments") if isinstance(item.get("reply_attachments"), list) else []
    public: list[dict] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        public.append(
            {
                "id": str(attachment.get("id") or ""),
                "type": str(attachment.get("type") or "image"),
                "name": str(attachment.get("name") or "image"),
                "size": int(attachment.get("size") or 0),
                "content_type": str(attachment.get("content_type") or ""),
            }
        )
    return public


def _review_item(item: dict) -> dict:
    latest_text = _context_latest_text(item)
    conversation_key = str(item.get("conversation_key") or "").strip()
    messages = state.list_conversation_messages(conversation_key=conversation_key) if conversation_key else []
    if not messages:
        messages = _fallback_messages(item, latest_text)
    messages = _classified_messages(messages)
    error_code = str(item.get("error") or "")
    handoff_waiting = error_code == state.HANDOFF_WAITING_ERROR
    handoff_attention = error_code == state.HANDOFF_NEW_MESSAGE_ERROR
    visible_error = "" if handoff_waiting or handoff_attention else item.get("error") or ""
    key_label = conversation_key
    if key_label.startswith("visible:"):
        key_label = key_label.split(":", 1)[1][:10]
    elif len(key_label) > 18:
        key_label = key_label[:18]
    return {
        "id": item.get("id"),
        "conversation_key": conversation_key,
        "customer_key_label": key_label,
        "title": item.get("title") or "",
        "status": item.get("status") or "",
        "preview": item.get("preview") or "",
        "latest_text": latest_text,
        "messages": messages,
        "reply_text": clean_customer_reply_text(item.get("reply_text") or ""),
        "reply_source": item.get("reply_source") or "",
        "reply_attachments": _public_reply_attachments(item),
        "handoff_type": item.get("handoff_type") or "",
        "handoff_reason": item.get("handoff_reason") or "",
        "handoff_pending": bool(item.get("handoff_type")),
        "handoff_waiting": handoff_waiting,
        "handoff_attention": handoff_attention,
        "error": visible_error,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "time_text": item.get("time_text") or "",
    }


def _issue_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "job_id": item.get("job_id"),
        "conversation_key": item.get("conversation_key") or "",
        "conversation": item.get("conversation") or "",
        "source": item.get("source") or "",
        "original_reply": clean_customer_reply_text(item.get("original_reply") or ""),
        "final_reply": clean_customer_reply_text(item.get("final_reply") or ""),
        "status": item.get("status") or "",
        "issue_text": clean_history_message_text(item.get("issue_text") or ""),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def list_review_items(*, status: str = "ready", limit: int = 100) -> list[dict]:
    state.expire_stale_handoffs()
    if status == "issues":
        return [_issue_item(item) for item in state.list_reply_issue_tasks(status="pending", limit=limit)]
    allowed = {"handoff", "pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"}
    selected_status = status if status in allowed else "ready"
    if selected_status == "handoff":
        items = state.list_ready_for_review(handoff=True, limit=limit)
    elif selected_status == "ready":
        items = state.list_ready_for_review(handoff=False, limit=limit)
    else:
        items = state.list_queue(status=selected_status, limit=limit)
    return [_review_item(item) for item in items]


def review_counts() -> dict[str, int]:
    state.expire_stale_handoffs()
    counts = state.queue_counts()
    for status in ("pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"):
        counts.setdefault(status, 0)
    counts["handoff"] = state.handoff_pending_count()
    counts["handoff_attention"] = state.handoff_attention_count()
    counts["issues"] = state.reply_issue_pending_count()
    counts["ready"] = len(state.list_ready_for_review(handoff=False, limit=1000000))
    return counts


def _supplement_test_customer_key(customer: str, *, prefix: str = "supplement-test") -> str:
    value = str(customer or "测试客户").strip() or "测试客户"
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _supplement_test_version(customer_key: str) -> int:
    with _SUPPLEMENT_TEST_VERSION_LOCK:
        return int(_SUPPLEMENT_TEST_VERSIONS.get(customer_key, 0))


def _supplement_test_bump_version(customer_key: str) -> int:
    with _SUPPLEMENT_TEST_VERSION_LOCK:
        value = int(_SUPPLEMENT_TEST_VERSIONS.get(customer_key, 0)) + 1
        _SUPPLEMENT_TEST_VERSIONS[customer_key] = value
        return value


def _supplement_test_is_current(customer_key: str, version: int) -> bool:
    return _supplement_test_version(customer_key) == version


def _supplement_trace_id(job_id: int, customer_key: str) -> str:
    return f"supp-test-{job_id}-{hashlib.sha1(customer_key.encode('utf-8')).hexdigest()[:8]}-{int(time.time() * 1000)}"


def _supplement_test_message_hash(messages: list[dict]) -> str:
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _supplement_log(
    event_type: str,
    *,
    trace_id: str,
    job: dict,
    customer_key: str,
    stage: str,
    message_hash: str = "",
    latest_text: str = "",
    details: dict | None = None,
    isolated: bool = False,
) -> None:
    logger = (
        state.log_supplement_full_test_event
        if isolated or str(customer_key or "").startswith("supplement-full-test:")
        else state.log_supplement_event
    )
    logger(
        event_type,
        trace_id=trace_id,
        job_id=job.get("id"),
        conversation_key=customer_key,
        external_user_id="",
        customer_id=customer_key,
        conversation=str(job.get("title") or ""),
        stage=stage,
        message_hash=message_hash,
        latest_text_preview=str(latest_text or "")[:120],
        details=details or {},
    )


def _supplement_test_logs(customer_key: str, trace_id: str = "", *, limit: int = 200, isolated: bool = False) -> list[dict]:
    clauses = []
    params: list[object] = []
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    else:
        clauses.append("(customer_id = ? OR conversation_key = ?)")
        params.extend([customer_key, customer_key])
    table = "supplement_full_test_logs" if isolated else "supplement_agent_logs"
    with state.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (*params, max(1, int(limit or 200))),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        items.append(item)
    return items


def _supplement_test_upsert_job(customer_key: str, customer_name: str, latest_text: str) -> dict:
    now = time.time()
    title = str(customer_name or "补剂测试客户").strip() or "补剂测试客户"
    signature = hashlib.sha1(f"{customer_key}|{latest_text}|{now}".encode("utf-8")).hexdigest()
    time_text = "网页微信"
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM reply_queue WHERE conversation_key = ?", (customer_key,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO reply_queue
                    (conversation_key, title, preview, time_text, tags_json, raw_json,
                     source, signature, status, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'drafting', NULL, ?, ?)
                """,
                (
                    customer_key,
                    title,
                    latest_text,
                    time_text,
                    json.dumps(["supplement-test"], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    "supplement-test",
                    signature,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE reply_queue
                SET title = ?, preview = ?, time_text = ?, tags_json = ?,
                    raw_json = ?, source = 'supplement-test', signature = ?,
                    status = 'drafting', error = NULL, locked_at = NULL, updated_at = ?
                WHERE conversation_key = ?
                """,
                (
                    title,
                    latest_text,
                    time_text,
                    json.dumps(["supplement-test"], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    signature,
                    now,
                    customer_key,
                ),
            )
    return state.get_job_by_conversation_key(customer_key) or {}


def _supplement_selected_needs(text: str) -> list[str]:
    return agent._supplement_selected_needs(text)


def _supplement_fixed_reply_for_stage(text: str, supplement_state: dict | None) -> tuple[str, str, str, dict] | None:
    current_stage = str((supplement_state or {}).get("stage") or "")
    selected_needs = _supplement_selected_needs(text)
    if not current_stage:
        has_profile = agent._has_supplement_profile(text)
        reply = agent.supplement_first_reply_choices_only() if has_profile else agent.supplement_first_reply_with_profile()
        return reply, state.SUPPLEMENT_COLLECTING_PROFILE, state.SUPPLEMENT_DIGGING_NEED, {
            "action": "first_prompt",
            "has_profile": has_profile,
            "selected_needs": selected_needs,
        }
    if current_stage == state.SUPPLEMENT_COLLECTING_PROFILE and agent._is_supplement_need_selection(text):
        return agent.supplement_selection_ack(), state.SUPPLEMENT_DIGGING_NEED, state.SUPPLEMENT_DIGGING_NEED, {
            "action": "need_selection_ack",
            "selected_needs": selected_needs,
            "need_numbers": agent._selected_supplement_need_numbers(text),
        }
    return None


def _supplement_welcome_text(customer_name: str) -> str:
    title = str(customer_name or "客户").strip() or "客户"
    template = message_config.fixed_message("welcome", "supplement_web_welcome_template") or SUPPLEMENT_WELCOME_TEMPLATE
    return template.replace("{用户名}", title)


def _supplement_web_welcome_message(customer_name: str) -> dict:
    title = str(customer_name or "客户").strip() or "客户"
    text = _supplement_welcome_text(title)
    return {
        "role": "客服",
        "content": text,
        "text": text,
        "source": "web-wechat",
        "message_type": "reply",
        "role_confidence": "welcome_template",
    }


def _supplement_draft_details(draft: dict) -> dict:
    raw = draft.get("raw") if isinstance(draft.get("raw"), dict) else {}
    codex = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    reply = codex.get("reply") if isinstance(codex.get("reply"), dict) else {}
    details = {
        "action": str(draft.get("action") or "") or "send",
        "provider": draft.get("provider"),
        "model": draft.get("model"),
        "duration_ms": draft.get("duration_ms"),
    }
    for key in (
        "commands_run",
        "retrieval_summary",
        "used_script_sources",
        "used_vector_memories",
        "confidence",
        "decision_basis",
        "conflicts",
    ):
        if key in reply:
            details[key] = reply.get(key)
    return details


def _supplement_agent_log_details(details: dict) -> dict:
    return {
        "action": details.get("action") or "",
        "provider": details.get("provider") or "",
        "model": details.get("model") or "",
        "duration_ms": details.get("duration_ms"),
        "commands_run": details.get("commands_run") or [],
        "retrieval_summary": details.get("retrieval_summary") or "",
        "used_script_sources": details.get("used_script_sources") or [],
        "used_vector_memories": details.get("used_vector_memories") or [],
        "confidence": details.get("confidence"),
        "decision_basis": details.get("decision_basis") or "",
        "conflicts": details.get("conflicts") or [],
    }


def _supplement_reply_for_stage(text: str, supplement_state: dict | None, *, messages: list[dict], customer_name: str, trace_id: str, customer_key: str) -> tuple[str, str, str, dict]:
    fixed = _supplement_fixed_reply_for_stage(text, supplement_state)
    if fixed is not None:
        return fixed
    current_stage = str((supplement_state or {}).get("stage") or "")
    context = agent._supplement_agent_context(
        state_row=supplement_state or {},
        route={"triggered": True, "active_state_exists": bool(supplement_state), "detected_intent": "supplement_test"},
        customer_key=customer_key,
        trace_id=trace_id,
        stage=current_stage or state.SUPPLEMENT_DIGGING_NEED,
    )
    draft = llm.draft_reply(
        messages,
        provider="pi",
        customer_name=customer_name,
        customer_uid="",
        agent_mode=agent.SUPPLEMENT_REPLY_SOURCE,
        agent_context=context,
    )
    action = str(draft.get("action") or "")
    next_stage = state.SUPPLEMENT_DIGGING_NEED if action == "clarify" else state.SUPPLEMENT_READY_TO_RECOMMEND
    pending_next = state.SUPPLEMENT_DIGGING_NEED if action == "clarify" else state.SUPPLEMENT_RECOMMENDED
    return str(draft.get("text") or draft.get("message") or ""), next_stage, pending_next, _supplement_draft_details(draft)


def _ordinary_test_reply(
    *,
    job: dict,
    customer_key: str,
    customer_name: str,
    messages: list[dict],
    message_hash: str,
) -> dict:
    started = time.perf_counter()
    state.mark_drafting(
        int(job["id"]),
        message_hash=message_hash,
        messages=messages,
        latest=messages[-1],
    )
    draft = llm.draft_reply(
        messages,
        customer_name=customer_name,
        customer_uid="",
    )
    duration_ms = round((time.perf_counter() - started) * 1000)
    reply_text = clean_customer_reply_text(str(draft.get("text") or draft.get("message") or ""))
    state.mark_ready(
        int(job["id"]),
        reply_text=reply_text,
        reply_source="ai",
        duration_ms=duration_ms,
        action=str(draft.get("action") or ""),
    )
    state.mark_done(
        int(job["id"]),
        message_hash=message_hash,
        reply_text=reply_text,
        reply_source="ai",
        duration_ms=duration_ms,
    )
    state.append_event(
        {
            "type": "supplement_test_ordinary_agent_done",
            "job_id": int(job["id"]),
            "conversation": customer_name,
            "conversation_key": customer_key,
            "duration_ms": duration_ms,
            "provider": draft.get("provider"),
            "action": draft.get("action"),
        }
    )
    return {
        "ok": True,
        "reply_text": reply_text,
        "ordinary_agent": True,
        "duration_ms": duration_ms,
    }


def _supplement_commit_test_reply(
    *,
    job: dict,
    customer_key: str,
    customer_name: str,
    trace_id: str,
    messages: list[dict],
    message_hash: str,
    latest_text: str,
    reply_text: str,
    next_stage: str,
    pending_next_stage: str,
    details: dict,
    selected_needs: list[str],
    supplement_state: dict | None,
    reason: str,
) -> list[dict]:
    final_reply = clean_customer_reply_text(reply_text)
    state.mark_drafting(
        int(job["id"]),
        message_hash=message_hash,
        messages=messages,
        latest=messages[-1],
        extra_context={
            "agent_mode": agent.SUPPLEMENT_REPLY_SOURCE,
            "supplement_trace_id": trace_id,
            "supplement_customer_key": customer_key,
            "supplement_stage": next_stage,
        },
    )
    state.mark_ready(
        int(job["id"]),
        reply_text=final_reply,
        reply_source=agent.SUPPLEMENT_REPLY_SOURCE,
        action=str(details.get("action") or "send"),
    )
    state.mark_done(
        int(job["id"]),
        message_hash=message_hash,
        reply_text=final_reply,
        reply_source=agent.SUPPLEMENT_REPLY_SOURCE,
    )
    digging_count = int((supplement_state or {}).get("digging_count") or 0)
    if str(details.get("action") or "") in {"digging_question", "clarify"}:
        digging_count = min(2, digging_count + 1)
    known_profile = dict((supplement_state or {}).get("known_profile") or {})
    known_profile["has_basic_profile"] = bool(
        known_profile.get("has_basic_profile") or agent._has_supplement_profile(latest_text)
    )
    if agent._declines_supplement_profile(latest_text):
        known_profile["profile_opt_out"] = True
        known_profile["has_basic_profile"] = False
    state.mark_supplement_state(
        customer_key,
        state.SUPPLEMENT_RECOMMENDED if pending_next_stage == state.SUPPLEMENT_RECOMMENDED else next_stage,
        conversation_key=customer_key,
        conversation=customer_name,
        job_id=int(job["id"]),
        trace_id=trace_id,
        digging_count=digging_count,
        selected_needs=selected_needs or (supplement_state or {}).get("selected_needs") or [],
        known_profile=known_profile,
        message_hash=message_hash,
        pending_next_stage=pending_next_stage,
        reason=reason,
    )
    committed_stage = state.SUPPLEMENT_RECOMMENDED if pending_next_stage == state.SUPPLEMENT_RECOMMENDED else next_stage
    _supplement_log(
        "supplement_draft_ready",
        trace_id=trace_id,
        job=job,
        customer_key=customer_key,
        stage=next_stage,
        message_hash=message_hash,
        latest_text=latest_text,
        details={"reply_preview": final_reply[:160], **details},
    )
    _supplement_log(
        "supplement_sent",
        trace_id=trace_id,
        job=job,
        customer_key=customer_key,
        stage=committed_stage,
        message_hash=message_hash,
        latest_text=latest_text,
        details={"reply_preview": final_reply[:160], "channel": "web_wechat_simulator"},
    )
    _supplement_log(
        "supplement_state_committed",
        trace_id=trace_id,
        job=job,
        customer_key=customer_key,
        stage=committed_stage,
        message_hash=message_hash,
        latest_text=latest_text,
        details={"next_stage": pending_next_stage, "commit_reason": reason},
    )
    return [
        *messages,
        {
            "role": "客服",
            "content": final_reply,
            "text": final_reply,
            "source": "supplement-test",
            "message_type": "reply",
        },
    ]


def supplement_test_status(customer: str = "测试客户", *, full_flow: bool = False) -> dict:
    customer_key = _supplement_test_customer_key(
        customer,
        prefix="supplement-full-test" if full_flow else "supplement-test",
    )
    item = state.get_job_by_conversation_key(customer_key) or {}
    supplement_state = state.get_supplement_state(customer_key)
    trace_id = str((supplement_state or {}).get("trace_id") or "")
    return {
        "customer": customer,
        "customer_key": customer_key,
        "job": _review_item(item) if item else {},
        "state": supplement_state or {},
        "messages": _classified_messages(state.list_conversation_messages(conversation_key=customer_key, limit=50)),
        "logs": _supplement_test_logs(customer_key, trace_id=trace_id, limit=200, isolated=full_flow),
    }


def supplement_test_reset(customer: str = "测试客户", *, full_flow: bool = False) -> dict:
    customer_key = _supplement_test_customer_key(
        customer,
        prefix="supplement-full-test" if full_flow else "supplement-test",
    )
    _supplement_test_bump_version(customer_key)
    with state.connect() as conn:
        conn.execute("DELETE FROM reply_queue WHERE conversation_key = ?", (customer_key,))
        conn.execute("DELETE FROM conversation_messages WHERE conversation_key = ?", (customer_key,))
        conn.execute("DELETE FROM supplement_states WHERE customer_key = ?", (customer_key,))
        if full_flow:
            conn.execute("DELETE FROM supplement_full_test_logs WHERE customer_id = ? OR conversation_key = ?", (customer_key, customer_key))
        else:
            conn.execute("DELETE FROM supplement_agent_logs WHERE customer_id = ? OR conversation_key = ?", (customer_key, customer_key))
        conn.execute("DELETE FROM metric_events WHERE conversation_key = ?", (customer_key,))
    return {"ok": True, **supplement_test_status(customer, full_flow=full_flow)}


def supplement_test_send(customer: str, text: str, *, full_flow: bool = False, new_user: bool = False) -> dict:
    body = clean_history_message_text(text)
    if not body and not new_user:
        return {"ok": False, "error": "empty_message"}
    customer_name = str(customer or "测试客户").strip() or "测试客户"
    customer_key = _supplement_test_customer_key(
        customer_name,
        prefix="supplement-full-test" if full_flow else "supplement-test",
    )
    request_version = _supplement_test_version(customer_key)
    job = _supplement_test_upsert_job(customer_key, customer_name, body or "新用户进线")
    previous_messages = state.list_conversation_messages(conversation_key=customer_key, limit=50)
    messages = [
        {"role": item.get("role") or "用户", "content": item.get("text") or "", "text": item.get("text") or ""}
        for item in previous_messages
        if item.get("text")
    ]
    supplement_state = state.get_supplement_state(customer_key)
    is_new_user_start = bool(new_user) and not bool(supplement_state)
    if is_new_user_start:
        messages.append(_supplement_web_welcome_message(customer_name))
    if body:
        messages.append({"role": "用户", "content": body, "text": body, "source": "web-wechat", "message_type": "customer"})
    message_hash = _supplement_test_message_hash(messages)
    state.record_conversation_messages(
        conversation_key=customer_key,
        job_id=job.get("id"),
        message_hash=message_hash,
        messages=messages,
    )
    trace_id = str((supplement_state or {}).get("trace_id") or "") or _supplement_trace_id(int(job.get("id") or 0), customer_key)
    stage = str((supplement_state or {}).get("stage") or state.SUPPLEMENT_COLLECTING_PROFILE)
    selected_needs = _supplement_selected_needs(body)
    route = agent._supplement_route(body, active_state=supplement_state)
    if is_new_user_start:
        route = {
            "triggered": True,
            "matched_terms": [],
            "active_state_exists": False,
            "detected_intent": "new_user_welcome_followup",
            "reason": "",
        }
    _supplement_log(
        "supplement_route_evaluated",
        trace_id=trace_id,
        job=job,
        customer_key=customer_key,
        stage=stage,
        message_hash=message_hash,
        latest_text=body,
        details={
            "triggered": bool(route.get("triggered")),
            "matched_terms": route.get("matched_terms") or [],
            "active_state_exists": bool(route.get("active_state_exists")),
            "detected_intent": route.get("detected_intent") or "supplement_test",
            "reason": route.get("reason") or "",
            "trigger_source": "new_user_welcome" if is_new_user_start else "customer_message",
            "channel": "web_wechat_simulator",
        },
    )
    if not route.get("triggered"):
        _supplement_log(
            "supplement_route_to_ordinary_agent",
            trace_id=trace_id,
            job=job,
            customer_key=customer_key,
            stage=stage,
            message_hash=message_hash,
            latest_text=body,
            details={
                "reason": route.get("reason") or "old_user_waiting_for_question",
                "channel": "web_wechat_simulator",
            },
        )
        try:
            ordinary = _ordinary_test_reply(
                job=job,
                customer_key=customer_key,
                customer_name=customer_name,
                messages=messages,
                message_hash=message_hash,
            )
        except Exception as exc:
            state.mark_read_logged(
                int(job["id"]),
                message_hash=message_hash,
                messages=messages,
                reason=str(route.get("reason") or "ordinary_agent_failed"),
            )
            return {"ok": False, "error": str(exc), **supplement_test_status(customer_name, full_flow=full_flow)}
        return {**ordinary, **supplement_test_status(customer_name, full_flow=full_flow)}
    _supplement_log(
        "supplement_state_loaded",
        trace_id=trace_id,
        job=job,
        customer_key=customer_key,
        stage=stage,
        message_hash=message_hash,
        latest_text=body,
        details={
            "current_stage": (supplement_state or {}).get("stage") or "",
            "digging_count": int((supplement_state or {}).get("digging_count") or 0),
            "selected_needs": (supplement_state or {}).get("selected_needs") or [],
        },
    )
    try:
        fixed_reply = (
            (
                agent.supplement_first_reply_with_profile(),
                state.SUPPLEMENT_COLLECTING_PROFILE,
                state.SUPPLEMENT_DIGGING_NEED,
                {
                    "action": "new_user_welcome_followup",
                    "has_profile": False,
                    "selected_needs": selected_needs,
                    "welcome_template": "luna_nutrition_factory",
                    "reset_existing_state": bool(supplement_state),
                },
            )
            if is_new_user_start
            else _supplement_fixed_reply_for_stage(body, supplement_state)
        )
        if fixed_reply is None:
            _supplement_log(
                "supplement_backend_agent_started",
                trace_id=trace_id,
                job=job,
                customer_key=customer_key,
                stage=stage,
                message_hash=message_hash,
                latest_text=body,
                details={"model": os.environ.get("WECOM_GUI_PI_MODEL", ""), "channel": "web_wechat_simulator"},
            )
        started = time.perf_counter()
        if fixed_reply is not None:
            reply_text, next_stage, pending_next_stage, details = fixed_reply
            _supplement_log(
                "supplement_fixed_flow_reply",
                trace_id=trace_id,
                job=job,
                customer_key=customer_key,
                stage=next_stage,
                message_hash=message_hash,
                latest_text=body,
                details={
                    **details,
                    "trigger_source": "new_user_welcome" if is_new_user_start else "customer_message",
                    "channel": "web_wechat_simulator",
                },
            )
        else:
            reply_text, next_stage, pending_next_stage, details = _supplement_reply_for_stage(
                body,
                supplement_state,
                messages=messages,
                customer_name=customer_name,
                trace_id=trace_id,
                customer_key=customer_key,
            )
        duration_ms = round((time.perf_counter() - started) * 1000)
        if fixed_reply is None:
            _supplement_log(
                "supplement_backend_agent_done",
                trace_id=trace_id,
                job=job,
                customer_key=customer_key,
                stage=next_stage,
                message_hash=message_hash,
                latest_text=body,
                details={**_supplement_agent_log_details(details), "duration_ms": duration_ms, "channel": "web_wechat_simulator"},
            )
    except Exception as exc:
        _supplement_log("supplement_backend_agent_failed", trace_id=trace_id, job=job, customer_key=customer_key, stage=stage, message_hash=message_hash, latest_text=body, details={"error_type": type(exc).__name__, "error": str(exc), "channel": "web_wechat_simulator"})
        return {"ok": False, "error": str(exc), **supplement_test_status(customer_name, full_flow=full_flow)}
    if not _supplement_test_is_current(customer_key, request_version):
        return {"ok": True, "stale": True, "reset_detected": True, **supplement_test_status(customer_name, full_flow=full_flow)}
    committed_messages = _supplement_commit_test_reply(
        job=job,
        customer_key=customer_key,
        customer_name=customer_name,
        trace_id=trace_id,
        messages=messages,
        message_hash=message_hash,
        selected_needs=selected_needs or (supplement_state or {}).get("selected_needs") or [],
        latest_text=body,
        reply_text=reply_text,
        next_stage=next_stage,
        pending_next_stage=pending_next_stage,
        details=details,
        supplement_state=None if is_new_user_start else supplement_state,
        reason="test_sent",
    )
    final_reply = clean_customer_reply_text(reply_text)
    if (
        str(details.get("action") or "") == "need_selection_ack"
        and _supplement_test_is_current(customer_key, request_version)
    ):
        _supplement_log(
            "supplement_ack_sent_continue_pi",
            trace_id=trace_id,
            job=job,
            customer_key=customer_key,
            stage=state.SUPPLEMENT_DIGGING_NEED,
            message_hash=message_hash,
            latest_text=body,
            details={"reply_preview": final_reply[:160]},
        )
        pi_state = state.get_supplement_state(customer_key) or {}
        pi_messages = [
            {"role": item.get("role") or "用户", "content": item.get("content") or item.get("text") or "", "text": item.get("text") or item.get("content") or ""}
            for item in committed_messages
            if item.get("content") or item.get("text")
        ]
        pi_message_hash = _supplement_test_message_hash(pi_messages)
        state.record_conversation_messages(
            conversation_key=customer_key,
            job_id=job.get("id"),
            message_hash=pi_message_hash,
            messages=pi_messages,
        )
        try:
            _supplement_log("supplement_backend_agent_started", trace_id=trace_id, job=job, customer_key=customer_key, stage=state.SUPPLEMENT_DIGGING_NEED, message_hash=pi_message_hash, latest_text=body, details={"model": os.environ.get("WECOM_GUI_PI_MODEL", ""), "after_ack": True, "channel": "web_wechat_simulator"})
            pi_started = time.perf_counter()
            pi_reply, pi_next_stage, pi_pending_next_stage, pi_details = _supplement_reply_for_stage(
                body,
                pi_state,
                messages=pi_messages,
                customer_name=customer_name,
                trace_id=trace_id,
                customer_key=customer_key,
            )
            pi_duration_ms = round((time.perf_counter() - pi_started) * 1000)
            _supplement_log("supplement_backend_agent_done", trace_id=trace_id, job=job, customer_key=customer_key, stage=pi_next_stage, message_hash=pi_message_hash, latest_text=body, details={**_supplement_agent_log_details(pi_details), "duration_ms": pi_duration_ms, "after_ack": True, "channel": "web_wechat_simulator"})
        except Exception as exc:
            _supplement_log("supplement_backend_agent_failed", trace_id=trace_id, job=job, customer_key=customer_key, stage=state.SUPPLEMENT_DIGGING_NEED, message_hash=pi_message_hash, latest_text=body, details={"error_type": type(exc).__name__, "error": str(exc), "after_ack": True, "channel": "web_wechat_simulator"})
            return {"ok": False, "error": str(exc), **supplement_test_status(customer_name, full_flow=full_flow)}
        if not _supplement_test_is_current(customer_key, request_version):
            return {"ok": True, "stale": True, "reset_detected": True, **supplement_test_status(customer_name, full_flow=full_flow)}
        final_reply = clean_customer_reply_text(pi_reply)
        _supplement_commit_test_reply(
            job=job,
            customer_key=customer_key,
            customer_name=customer_name,
            trace_id=trace_id,
            messages=pi_messages,
            message_hash=pi_message_hash,
            selected_needs=selected_needs or pi_state.get("selected_needs") or [],
            latest_text=body,
            reply_text=pi_reply,
            next_stage=pi_next_stage,
            pending_next_stage=pi_pending_next_stage,
            details={**pi_details, "after_ack": True},
            supplement_state=pi_state,
            reason="test_pi_after_ack",
        )
    return {"ok": True, "reply_text": final_reply, **supplement_test_status(customer_name, full_flow=full_flow)}


def wecom_status() -> dict:
    status = macos_backend.doctor_status()
    return {
        "ok": status.ok,
        "app_name": status.app_name or "",
        "app_running": status.app_running,
        "accessibility_ok": status.accessibility_ok,
        "osascript_ok": status.osascript_ok,
        "notes": status.notes,
        "ts": time.time(),
    }


def _attachments_for_action(job_id: int, attachment_ids: list | None = None) -> list[dict]:
    attachments = _current_attachments(job_id)
    if attachment_ids is None:
        return attachments
    allowed = {str(value) for value in attachment_ids}
    return [attachment for attachment in attachments if attachment["id"] in allowed]


def approve_item(job_id: int, *, reply_text: str | None = None, attachment_ids: list | None = None) -> dict:
    attachments = _attachments_for_action(job_id, attachment_ids)
    if not state.mark_approved(job_id, reply_text=reply_text, attachments=attachments):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event({"type": "review_approved", "job_id": job_id, "conversation": (item or {}).get("title")})
    state.record_metric(
        "review_approved",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        reply_source=str((item or {}).get("reply_source") or ""),
    )
    return {"ok": True, "item": _review_item(item or {})}


def save_item(job_id: int, *, reply_text: str, attachment_ids: list | None = None) -> dict:
    attachments = _attachments_for_action(job_id, attachment_ids)
    if not state.save_reply(job_id, reply_text=reply_text, attachments=attachments):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event({"type": "review_saved", "job_id": job_id, "conversation": (item or {}).get("title")})
    state.record_metric(
        "review_saved",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        reply_source=str((item or {}).get("reply_source") or "human"),
    )
    return {"ok": True, "item": _review_item(item or {})}


def record_reply_issue(job_id: int, *, issue_text: str) -> dict:
    issue = clean_history_message_text(issue_text)
    if not issue:
        return {"ok": False, "error": "empty_issue", "id": job_id}
    item = state.get_job(job_id)
    if not item:
        return {"ok": False, "error": "not_found", "id": job_id}
    state.append_event(
        {
            "type": "review_reply_issue",
            "job_id": job_id,
            "conversation": item.get("title"),
            "issue": issue,
        }
    )
    state.record_metric(
        "review_reply_issue",
        conversation_key=str(item.get("conversation_key") or ""),
        conversation=str(item.get("title") or ""),
        job_id=job_id,
        reply_source=str(item.get("reply_source") or "human"),
        details={"issue": issue},
    )
    return {"ok": True, "item": _review_item(item)}


def complete_reply_issue(task_id: int, *, issue_text: str) -> dict:
    item = state.complete_reply_issue_task(task_id, issue_text=issue_text)
    if item is None:
        return {"ok": False, "error": "invalid_issue_task", "id": task_id}
    state.append_event(
        {
            "type": "review_reply_issue_completed",
            "task_id": task_id,
            "job_id": item.get("job_id"),
            "conversation": item.get("conversation"),
        }
    )
    return {"ok": True, "item": _issue_item(item)}


def upload_attachment(job_id: int, *, filename: str, content_type: str, data_url: str = "", data_base64: str = "") -> dict:
    encoded = data_base64
    if data_url:
        if "," not in data_url:
            return {"ok": False, "error": "invalid_data_url", "id": job_id}
        header, encoded = data_url.split(",", 1)
        if not content_type and ";" in header:
            content_type = header.split(":", 1)[-1].split(";", 1)[0]
    try:
        body = base64.b64decode(encoded, validate=True)
        attachment = _store_attachment(job_id, filename=filename, content_type=content_type, body=body)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "id": job_id}
    attachments = _current_attachments(job_id)
    attachments.append(attachment)
    if not state.save_reply_attachments(job_id, attachments=attachments):
        try:
            Path(attachment["path"]).unlink()
        except OSError:
            pass
        return {"ok": False, "error": "not_ready", "id": job_id}
    return {"ok": True, "item": _review_item(state.get_job(job_id) or {})}


def delete_attachment(job_id: int, attachment_id: str) -> dict:
    target = str(attachment_id or "").strip()
    attachments = _current_attachments(job_id)
    kept = []
    removed = []
    for attachment in attachments:
        if attachment["id"] == target:
            removed.append(attachment)
        else:
            kept.append(attachment)
    if not state.save_reply_attachments(job_id, attachments=kept):
        return {"ok": False, "error": "not_ready", "id": job_id}
    for attachment in removed:
        try:
            Path(attachment["path"]).unlink()
        except OSError:
            pass
    return {"ok": True, "item": _review_item(state.get_job(job_id) or {})}


def regenerate_item(job_id: int, *, reason: str = "review_regenerate") -> dict:
    if not state.regenerate_reply(job_id, reason=reason):
        return {"ok": False, "error": "not_regeneratable", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_regenerate", "job_id": job_id, "conversation": (item or {}).get("title"), "reason": reason}
    )
    state.record_metric(
        "review_regenerate",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        details={"reason": reason},
    )
    return {"ok": True, "item": _review_item(item or {})}


def reject_item(job_id: int, *, reason: str = "review_rejected") -> dict:
    if not state.reject_ready(job_id, reason=reason):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_rejected", "job_id": job_id, "conversation": (item or {}).get("title"), "reason": reason}
    )
    state.record_metric(
        "review_rejected",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        details={"reason": reason},
    )
    return {"ok": True, "item": _review_item(item or {})}


def finish_item(job_id: int, *, reason: str = "handoff_finished") -> dict:
    if not state.finish_handoff(job_id, reason=reason):
        return {"ok": False, "error": "not_handoff", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_handoff_finished", "job_id": job_id, "conversation": (item or {}).get("title")}
    )
    state.record_metric(
        "review_handoff_finished",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        reply_source=str((item or {}).get("reply_source") or "human"),
    )
    return {"ok": True, "item": _review_item(item or {})}


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "WeComReview/0.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_OPTIONS(self) -> None:
        _json_response(self, 200, {"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            body = REVIEW_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in {"/supplement-test", "/supplement-test/"}:
            body = SUPPLEMENT_TEST_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in {"/supplement-full-test", "/supplement-full-test/"}:
            body = SUPPLEMENT_FULL_TEST_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/static/"):
            _send_static_response(self, parsed.path.removeprefix("/static/"))
            return
        if parsed.path.startswith("/supplement-test/static/"):
            _send_static_response(
                self,
                parsed.path.removeprefix("/supplement-test/static/"),
                root_dir=SUPPLEMENT_TEST_FRONTEND_DIR,
            )
            return
        if parsed.path.startswith("/supplement-full-test/static/"):
            _send_static_response(
                self,
                parsed.path.removeprefix("/supplement-full-test/static/"),
                root_dir=SUPPLEMENT_FULL_TEST_FRONTEND_DIR,
            )
            return
        if parsed.path == "/health":
            _json_response(self, 200, {"ok": True, "version": __version__})
            return
        if parsed.path == "/api/supplement-test/status":
            params = parse_qs(parsed.query)
            customer = (params.get("customer") or ["测试客户"])[0]
            _json_response(self, 200, {"ok": True, **supplement_test_status(customer)})
            return
        if parsed.path == "/api/supplement-full-test/status":
            params = parse_qs(parsed.query)
            customer = (params.get("customer") or ["完整问答测试客户"])[0]
            _json_response(self, 200, {"ok": True, **supplement_test_status(customer, full_flow=True)})
            return
        if parsed.path == "/api/review/items":
            params = parse_qs(parsed.query)
            status = (params.get("status") or ["ready"])[0]
            limit = int((params.get("limit") or ["100"])[0])
            _json_response(self, 200, {"ok": True, "items": list_review_items(status=status, limit=limit)})
            return
        if parsed.path == "/api/review/counts":
            _json_response(self, 200, {"ok": True, "counts": review_counts(), "ts": time.time()})
            return
        if parsed.path == "/api/review/wecom-status":
            _json_response(self, 200, {"ok": True, "status": wecom_status()})
            return
        if parsed.path == "/api/review/metrics":
            params = parse_qs(parsed.query)
            since_hours = float((params.get("since_hours") or ["24"])[0])
            _json_response(self, 200, {"ok": True, "metrics": state.metrics_summary(since_hours=since_hours)})
            return
        media_parts = [part for part in parsed.path.split("/") if part]
        if len(media_parts) == 5 and media_parts[:3] == ["api", "review", "media"]:
            try:
                message_id = int(media_parts[3])
                media_index = int(media_parts[4])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_media_id"})
                return
            media = state.get_conversation_media(message_id=message_id, media_index=media_index)
            if not media:
                _json_response(self, 404, {"ok": False, "error": "media_not_found"})
                return
            _send_file_response(self, Path(str(media["capture_path"])))
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/api/supplement-test/send":
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result = supplement_test_send(
                str(payload.get("customer") or "测试客户"),
                str(payload.get("text") or ""),
                new_user=bool(payload.get("new_user")),
            )
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        if parsed.path == "/api/supplement-full-test/send":
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result = supplement_test_send(
                str(payload.get("customer") or "完整问答测试客户"),
                str(payload.get("text") or ""),
                full_flow=True,
                new_user=bool(payload.get("new_user")),
            )
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        if parsed.path == "/api/supplement-test/reset":
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, supplement_test_reset(str(payload.get("customer") or "测试客户")))
            return
        if parsed.path == "/api/supplement-full-test/reset":
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, supplement_test_reset(str(payload.get("customer") or "完整问答测试客户"), full_flow=True))
            return
        if len(parts) == 5 and parts[:3] == ["api", "review", "issues"] and parts[4] == "complete":
            try:
                task_id = int(parts[3])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_id"})
                return
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result = complete_reply_issue(task_id, issue_text=str(payload.get("issue_text") or ""))
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        if len(parts) == 5 and parts[:3] == ["api", "review", "items"] and parts[4] == "attachments":
            try:
                job_id = int(parts[3])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_id"})
                return
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result = upload_attachment(
                job_id,
                filename=str(payload.get("filename") or "image"),
                content_type=str(payload.get("content_type") or ""),
                data_url=str(payload.get("data_url") or ""),
                data_base64=str(payload.get("data_base64") or ""),
            )
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        actions = {"approve", "reject", "save", "regenerate", "finish", "issue"}
        if len(parts) != 5 or parts[:3] != ["api", "review", "items"] or parts[4] not in actions:
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        try:
            job_id = int(parts[3])
        except ValueError:
            _json_response(self, 400, {"ok": False, "error": "invalid_id"})
            return
        try:
            payload = _read_json(self)
        except Exception as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        if parts[4] == "approve":
            reply_text = payload.get("reply_text")
            result = approve_item(
                job_id,
                reply_text=str(reply_text) if reply_text is not None else None,
                attachment_ids=payload.get("attachment_ids") if isinstance(payload.get("attachment_ids"), list) else None,
            )
        elif parts[4] == "reject":
            reason = str(payload.get("reason") or "review_rejected").strip() or "review_rejected"
            result = reject_item(job_id, reason=reason)
        elif parts[4] == "save":
            reply_text = str(payload.get("reply_text") or "")
            result = save_item(
                job_id,
                reply_text=reply_text,
                attachment_ids=payload.get("attachment_ids") if isinstance(payload.get("attachment_ids"), list) else None,
            )
        elif parts[4] == "issue":
            result = record_reply_issue(job_id, issue_text=str(payload.get("issue_text") or ""))
        elif parts[4] == "regenerate":
            reason = str(payload.get("reason") or "review_regenerate").strip() or "review_regenerate"
            result = regenerate_item(job_id, reason=reason)
        else:
            reason = str(payload.get("reason") or "handoff_finished").strip() or "handoff_finished"
            result = finish_item(job_id, reason=reason)
        _json_response(self, 200 if result.get("ok") else 409, result)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 6 and parts[:3] == ["api", "review", "items"] and parts[4] == "attachments":
            try:
                job_id = int(parts[3])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_id"})
                return
            result = delete_attachment(job_id, parts[5])
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})


def serve_review(*, host: str | None = None, port: int | None = None) -> None:
    resolved_host = host or os.environ.get("WECOM_REVIEW_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    resolved_port = int(port or os.environ.get("WECOM_REVIEW_PORT", DEFAULT_PORT))
    httpd = ThreadingHTTPServer((resolved_host, resolved_port), ReviewHandler)
    display_host = _local_ip() if resolved_host in {"0.0.0.0", ""} else resolved_host
    print(f"wecom review: http://{display_host}:{resolved_port}/", flush=True)
    httpd.serve_forever()
