"""Local/LAN review server for approving AI reply drafts."""

from __future__ import annotations

import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cli_anything.wecom_gui import __version__
from cli_anything.wecom_gui.core import state


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8122


REVIEW_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>客服回复审核</title>
    <style>
      :root { color-scheme: light; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f5f7fb;
        color: #1f2329;
      }
      header {
        position: sticky;
        top: 0;
        z-index: 2;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 14px 18px;
        background: #ffffff;
        border-bottom: 1px solid #e5e6eb;
      }
      h1 { margin: 0; font-size: 18px; font-weight: 650; }
      main { max-width: 1120px; margin: 0 auto; padding: 18px; }
      button {
        border: 1px solid #c9cdd4;
        border-radius: 6px;
        padding: 8px 12px;
        background: #fff;
        color: #1f2329;
        cursor: pointer;
        font-size: 14px;
      }
      button.primary { border-color: #165dff; background: #165dff; color: #fff; }
      button.secondary { border-color: #165dff; color: #165dff; }
      button.danger { border-color: #d92d20; color: #d92d20; }
      button:disabled { opacity: .55; cursor: not-allowed; }
      .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .status { color: #4e5969; font-size: 13px; }
      .counts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
      .count {
        background: #fff;
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        padding: 8px 10px;
        font-size: 13px;
        color: #4e5969;
      }
      .count.active { border-color: #165dff; color: #165dff; background: #f2f6ff; }
      .count strong { color: #1f2329; margin-left: 4px; }
      .list { display: grid; gap: 12px; }
      .item {
        background: #fff;
        border: 1px solid #e5e6eb;
        border-radius: 8px;
        padding: 14px;
      }
      .item-head {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 10px;
      }
      .title { font-weight: 650; font-size: 16px; overflow-wrap: anywhere; }
      .meta { color: #86909c; font-size: 12px; white-space: nowrap; }
      .label { color: #4e5969; font-size: 12px; margin: 12px 0 4px; }
      .box {
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        background: #f7f8fa;
        padding: 10px;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        line-height: 1.5;
      }
      .reply { background: #f2f6ff; border-color: #c9dcff; }
      textarea.reply-editor {
        box-sizing: border-box;
        width: 100%;
        min-height: 112px;
        resize: vertical;
        border: 1px solid #c9dcff;
        border-radius: 6px;
        background: #f2f6ff;
        color: #1f2329;
        padding: 10px;
        font: inherit;
        line-height: 1.5;
      }
      .conversation {
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        background: #f7f8fa;
        padding: 10px;
        display: grid;
        gap: 8px;
      }
      .msg {
        display: grid;
        gap: 3px;
        max-width: min(760px, 92%);
      }
      .msg.service { justify-self: end; }
      .msg-meta { color: #86909c; font-size: 12px; }
      .bubble {
        border: 1px solid #e5e6eb;
        border-radius: 8px;
        padding: 8px 10px;
        background: #fff;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        line-height: 1.5;
      }
      .msg.service .bubble {
        background: #f2f6ff;
        border-color: #c9dcff;
      }
      .msg.unknown .bubble { color: #4e5969; }
      .actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
      .empty {
        border: 1px dashed #c9cdd4;
        border-radius: 8px;
        padding: 38px 18px;
        text-align: center;
        color: #86909c;
        background: #fff;
      }
      .error { color: #b42318; }
      @media (max-width: 720px) {
        header { align-items: flex-start; flex-direction: column; }
        main { padding: 12px; }
        .item-head { flex-direction: column; }
        .meta { white-space: normal; }
        .actions { justify-content: stretch; }
        .actions button { flex: 1; }
      }
    </style>
  </head>
  <body>
    <header>
      <h1>客服回复审核</h1>
      <div class="toolbar">
        <span class="status" id="status">加载中...</span>
        <button id="refresh">刷新</button>
      </div>
    </header>
    <main>
      <div class="counts" id="counts"></div>
      <div class="list" id="list"></div>
    </main>
    <script>
      const statusEl = document.getElementById("status");
      const countsEl = document.getElementById("counts");
      const listEl = document.getElementById("list");
      const refreshBtn = document.getElementById("refresh");
      let selectedStatus = "ready";
      const labels = {
        pending: "待读取",
        reading: "读取中",
        drafting: "AI处理中",
        ready: "待审核",
        approved: "已审核待发送",
        sending: "发送中",
        done: "已完成",
        skipped: "已拒绝/跳过",
        failed: "失败"
      };
      const visibleStatuses = ["pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"];
      const replyDrafts = new Map();
      function authHeaders(extra = {}) {
        return { ...extra };
      }
      function setStatus(text, isError = false) {
        statusEl.textContent = text;
        statusEl.className = isError ? "status error" : "status";
      }
      function escapeText(value) {
        return String(value ?? "").replace(/[&<>"']/g, ch => ({
          "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[ch]));
      }
      function fmtTime(value) {
        const ts = Number(value || 0);
        if (!ts) return "";
        return new Date(ts * 1000).toLocaleString();
      }
      async function api(path, options = {}) {
        const res = await fetch(path, {
          ...options,
          headers: authHeaders(options.headers || {})
        });
        const text = await res.text();
        let data = {};
        try { data = text ? JSON.parse(text) : {}; } catch (err) { data = {raw: text}; }
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
      }
      function renderCounts(counts) {
        countsEl.innerHTML = visibleStatuses.map(key => (
          `<button class="count ${key === selectedStatus ? "active" : ""}" data-status="${key}">
            ${labels[key] || key}<strong>${Number(counts[key] || 0)}</strong>
          </button>`
        )).join("");
      }
      function roleClass(message) {
        const type = String(message.message_type || "");
        const role = String(message.role || "");
        if (type === "reply" || role.includes("客服")) return "service";
        if (type === "customer" || role.includes("用户") || role.includes("客户")) return "user";
        return "unknown";
      }
      function roleLabel(message) {
        const type = String(message.message_type || "");
        if (type === "reply") return "客服";
        if (type === "customer") return "客户";
        const role = String(message.role || "").trim();
        if (role.includes("客服")) return "客服";
        if (role.includes("用户") || role.includes("客户")) return "客户";
        return role || "消息";
      }
      function renderMessages(messages, fallbackText) {
        const source = Array.isArray(messages) ? messages.filter(item => String(item.text || "").trim()) : [];
        if (!source.length) {
          return `<div class="box">${escapeText(fallbackText || "")}</div>`;
        }
        return `<div class="conversation">${source.map(message => `
          <div class="msg ${roleClass(message)}">
            <div class="msg-meta">${escapeText(roleLabel(message))}${message.time_text ? ` · ${escapeText(message.time_text)}` : ""}</div>
            <div class="bubble">${escapeText(message.text || "")}</div>
          </div>
        `).join("")}</div>`;
      }
      function renderItems(items) {
        if (!items.length) {
          listEl.innerHTML = `<div class="empty">暂无${escapeText(labels[selectedStatus] || selectedStatus)}记录。</div>`;
          return;
        }
        listEl.innerHTML = items.map(item => `
          <article class="item" data-id="${item.id}">
            <div class="item-head">
              <div>
                <div class="title">${escapeText(item.title || "未命名客户")}</div>
                <div class="status">${escapeText(labels[item.status] || item.status)} · #${item.id}</div>
              </div>
              <div class="meta">${escapeText(fmtTime(item.updated_at))}</div>
            </div>
            <div class="label">会话上下文</div>
            ${renderMessages(item.messages, item.latest_text || item.preview || "")}
            <div class="label">回复内容</div>
            ${item.status === "ready" ? `
              <textarea class="reply-editor" data-id="${item.id}" placeholder="可以修改 AI 生成内容，或直接写客服自己的回复">${escapeText(replyTextFor(item))}</textarea>
            ` : `<div class="box reply">${escapeText(item.reply_text || "")}</div>`}
            ${item.error ? `<div class="label">备注</div><div class="box error">${escapeText(item.error)}</div>` : ""}
            ${renderActions(item)}
          </article>
        `).join("");
      }
      function renderActions(item) {
        if (item.status === "ready") {
          return `
            <div class="actions">
              <button class="secondary" data-action="regenerate" data-id="${item.id}">重新生成</button>
              <button class="secondary" data-action="save" data-id="${item.id}">保存修改</button>
              <button class="danger" data-action="reject" data-id="${item.id}">拒绝</button>
              <button class="primary" data-action="approve" data-id="${item.id}">通过</button>
            </div>
          `;
        }
        if (item.status === "skipped" || item.status === "failed") {
          return `
            <div class="actions">
              <button class="secondary" data-action="regenerate" data-id="${item.id}">重新生成</button>
            </div>
          `;
        }
        return "";
      }
      function replyTextFor(item) {
        const key = String(item.id);
        return replyDrafts.has(key) ? replyDrafts.get(key) : (item.reply_text || "");
      }
      function isEditingReply() {
        const active = document.activeElement;
        return !!active && active.matches && active.matches("textarea.reply-editor");
      }
      async function refresh() {
        if (isEditingReply()) return;
        try {
          refreshBtn.disabled = true;
          const [counts, items] = await Promise.all([
            api("/api/review/counts"),
            api(`/api/review/items?status=${encodeURIComponent(selectedStatus)}`)
          ]);
          renderCounts(counts.counts || {});
          renderItems(items.items || []);
          setStatus(`已刷新 ${new Date().toLocaleTimeString()}，通过后后台会复核再发送。`);
        } catch (err) {
          setStatus(String(err.message || err), true);
        } finally {
          refreshBtn.disabled = false;
        }
      }
      async function act(id, action) {
        const buttons = document.querySelectorAll(`button[data-id="${id}"]`);
        const editor = document.querySelector(`textarea[data-id="${id}"]`);
        const body = {};
        if ((action === "approve" || action === "save") && editor) {
          const replyText = editor.value.trim();
          if (!replyText) {
            setStatus("回复内容不能为空。", true);
            return;
          }
          body.reply_text = editor.value;
          replyDrafts.set(String(id), editor.value);
        }
        buttons.forEach(btn => btn.disabled = true);
        try {
          await api(`/api/review/items/${id}/${action}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body)
          });
          replyDrafts.delete(String(id));
          await refresh();
        } catch (err) {
          setStatus(String(err.message || err), true);
          buttons.forEach(btn => btn.disabled = false);
        }
      }
      listEl.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-action]");
        if (!button) return;
        act(button.dataset.id, button.dataset.action);
      });
      listEl.addEventListener("input", (event) => {
        const editor = event.target.closest("textarea[data-id]");
        if (!editor) return;
        replyDrafts.set(String(editor.dataset.id), editor.value);
      });
      countsEl.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-status]");
        if (!button) return;
        selectedStatus = button.dataset.status || "ready";
        refresh();
      });
      refreshBtn.addEventListener("click", refresh);
      refresh();
      setInterval(refresh, 5000);
    </script>
  </body>
</html>
"""


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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


def _message_type(message: dict) -> str:
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


def _classified_messages(messages: list[dict]) -> list[dict]:
    return [{**message, "message_type": _message_type(message)} for message in messages]


def _review_item(item: dict) -> dict:
    latest_text = _context_latest_text(item)
    conversation_key = str(item.get("conversation_key") or "").strip()
    messages = state.list_conversation_messages(conversation_key=conversation_key) if conversation_key else []
    if not messages:
        messages = _fallback_messages(item, latest_text)
    messages = _classified_messages(messages)
    return {
        "id": item.get("id"),
        "conversation_key": conversation_key,
        "title": item.get("title") or "",
        "status": item.get("status") or "",
        "preview": item.get("preview") or "",
        "latest_text": latest_text,
        "messages": messages,
        "reply_text": item.get("reply_text") or "",
        "error": item.get("error") or "",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "time_text": item.get("time_text") or "",
    }


def list_review_items(*, status: str = "ready", limit: int = 100) -> list[dict]:
    allowed = {"pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"}
    selected_status = status if status in allowed else "ready"
    items = state.list_queue(status=selected_status, limit=limit)
    return [_review_item(item) for item in items]


def review_counts() -> dict[str, int]:
    counts = state.queue_counts()
    for status in ("pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"):
        counts.setdefault(status, 0)
    return counts


def approve_item(job_id: int, *, reply_text: str | None = None) -> dict:
    if not state.mark_approved(job_id, reply_text=reply_text):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event({"type": "review_approved", "job_id": job_id, "conversation": (item or {}).get("title")})
    return {"ok": True, "item": _review_item(item or {})}


def save_item(job_id: int, *, reply_text: str) -> dict:
    if not state.save_reply(job_id, reply_text=reply_text):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event({"type": "review_saved", "job_id": job_id, "conversation": (item or {}).get("title")})
    return {"ok": True, "item": _review_item(item or {})}


def regenerate_item(job_id: int, *, reason: str = "review_regenerate") -> dict:
    if not state.regenerate_reply(job_id, reason=reason):
        return {"ok": False, "error": "not_regeneratable", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_regenerate", "job_id": job_id, "conversation": (item or {}).get("title"), "reason": reason}
    )
    return {"ok": True, "item": _review_item(item or {})}


def reject_item(job_id: int, *, reason: str = "review_rejected") -> dict:
    if not state.reject_ready(job_id, reason=reason):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_rejected", "job_id": job_id, "conversation": (item or {}).get("title"), "reason": reason}
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
        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        actions = {"approve", "reject", "save", "regenerate"}
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
            result = approve_item(job_id, reply_text=str(reply_text) if reply_text is not None else None)
        elif parts[4] == "reject":
            reason = str(payload.get("reason") or "review_rejected").strip() or "review_rejected"
            result = reject_item(job_id, reason=reason)
        elif parts[4] == "save":
            reply_text = str(payload.get("reply_text") or "").strip()
            result = save_item(job_id, reply_text=reply_text)
        else:
            reason = str(payload.get("reason") or "review_regenerate").strip() or "review_regenerate"
            result = regenerate_item(job_id, reason=reason)
        _json_response(self, 200 if result.get("ok") else 409, result)


def serve_review(*, host: str | None = None, port: int | None = None) -> None:
    resolved_host = host or os.environ.get("WECOM_REVIEW_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    resolved_port = int(port or os.environ.get("WECOM_REVIEW_PORT", DEFAULT_PORT))
    httpd = ThreadingHTTPServer((resolved_host, resolved_port), ReviewHandler)
    display_host = _local_ip() if resolved_host in {"0.0.0.0", ""} else resolved_host
    print(f"wecom review: http://{display_host}:{resolved_port}/", flush=True)
    httpd.serve_forever()
