"""Local/LAN review server for approving AI reply drafts."""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import time
import uuid
import base64
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cli_anything.wecom_gui import __version__
from cli_anything.wecom_gui.core import state
from cli_anything.wecom_gui.core.text import clean_customer_reply_text, clean_history_message_text
from cli_anything.wecom_gui.utils import macos_backend


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8122
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend" / "review"


def _load_frontend_text(name: str) -> str:
    try:
        return (FRONTEND_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return ""


REVIEW_HTML = _load_frontend_text("index.html")
REVIEW_FRONTEND_JS = _load_frontend_text("app.js")


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


def _send_static_response(handler: BaseHTTPRequestHandler, relative_path: str) -> None:
    try:
        target = (FRONTEND_DIR / relative_path.lstrip("/")).resolve()
        root = FRONTEND_DIR.resolve()
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
    if explicit in {"customer", "reply", "unknown"}:
        return explicit
    role = str(message.get("role") or "").strip().lower()
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
        role = "客服" if message_type == "reply" else "用户" if message_type == "customer" else message.get("role", "")
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
        if parsed.path.startswith("/static/"):
            _send_static_response(self, parsed.path.removeprefix("/static/"))
            return
        if parsed.path == "/health":
            _json_response(self, 200, {"ok": True, "version": __version__})
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
