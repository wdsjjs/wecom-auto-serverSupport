"""Queue scanner and worker for multi-conversation auto replies."""

from __future__ import annotations

import os
import time
import unicodedata

from cli_anything.wecom_gui.core import chat, inbox, llm, reply, state, watcher
from cli_anything.wecom_gui.utils import macos_backend


def _reply_match_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(normalized.split())


def _messages_contain_text(messages: list[dict], text: str) -> bool:
    expected = text.strip()
    if not expected:
        return False
    expected_key = _reply_match_key(expected)
    for message in messages:
        actual = str(message.get("content") or message.get("text") or "").strip()
        if actual == expected or _reply_match_key(actual) == expected_key:
            return True
    return False


def _message_text(message: dict) -> str:
    return str(message.get("content") or message.get("text") or "").strip()


def _text_matches(left: object, right: object) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    left_key = _reply_match_key(left_text)
    right_key = _reply_match_key(right_text)
    return left_key == right_key or (
        len(left_key) >= 6 and len(right_key) >= 6 and (left_key in right_key or right_key in left_key)
    )


def _latest_message_matching_preview(messages: list[dict], preview: str) -> dict | None:
    preview_text = str(preview or "").strip()
    if not preview_text:
        return None
    for message in reversed(messages):
        text = _message_text(message)
        if _text_matches(text, preview_text):
            patched = {**message}
            patched["role"] = "用户"
            patched["role_confidence"] = "selected_preview_fallback"
            patched["original_role"] = str(message.get("role") or "").strip()
            patched["content"] = text
            patched["text"] = text
            return patched
    return None


def _latest_reply_for_row(row: dict) -> str:
    conversation_key = state.conversation_key_for_row(row)
    if not conversation_key:
        return ""
    item = state.get_job_by_conversation_key(conversation_key) or {}
    reply_text = str(item.get("reply_text") or "").strip()
    if reply_text:
        return reply_text
    return ""


def _requires_unread() -> bool:
    return os.environ.get("WECOM_GUI_REQUIRE_UNREAD", "1") != "0"


def _has_unread(row: dict) -> bool:
    return int(row.get("unread_count") or 0) > 0 or bool(row.get("unread"))


def _same_current_message(existing: dict | None, *, signature: str, message_hash: str) -> bool:
    if not existing:
        return False
    status = str(existing.get("status") or "")
    if status in state.ACTIVE_STATUSES or status in {"done", "skipped", "failed", "pending"}:
        if message_hash and str(existing.get("last_message_hash") or "") == message_hash:
            return True
        if signature and str(existing.get("signature") or "") == signature:
            return True
    return False


def _row_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("title") or "").strip(),
        str(row.get("preview") or "").strip(),
        str(row.get("time") or "").strip(),
    )


def _scan_visible_pages(*, inbox_limit: int, scan_pages: int = 1, scroll_ticks: int = 6) -> dict:
    pages = max(1, int(scan_pages or 1))
    if pages == 1:
        scan = inbox.scan_visible(limit=inbox_limit)
        scan["pages_scanned"] = 1
        return scan

    rows_by_key: dict[tuple[str, str, str], dict] = {}
    pages_seen = 0
    for page_index in range(pages):
        scan = inbox.scan_visible(limit=inbox_limit)
        pages_seen += 1
        for row in scan["conversations"]:
            rows_by_key.setdefault(_row_key(row), row)
        if page_index < pages - 1:
            try:
                macos_backend.scroll_sidebar("down", ticks=scroll_ticks)
            except Exception as exc:
                scan["scroll_error"] = str(exc)
                break
            time.sleep(0.25)
    for _ in range(pages - 1):
        try:
            macos_backend.scroll_sidebar("up", ticks=scroll_ticks)
        except Exception:
            break
        time.sleep(0.1)
    rows = list(rows_by_key.values())
    return {
        "ok": True,
        "source": "accessibility",
        "heuristic": True,
        "conversation_count": len(rows),
        "conversations": rows,
        "pages_scanned": pages_seen,
    }


def scan_once(*, inbox_limit: int, process_existing: bool = True, scan_pages: int = 1, scroll_ticks: int = 6) -> dict:
    """Scan visible inbox rows and enqueue changed conversations without clicking."""
    scan = _scan_visible_pages(inbox_limit=inbox_limit, scan_pages=scan_pages, scroll_ticks=scroll_ticks)
    candidates = [row for row in scan["conversations"] if watcher._should_consider(row)]
    unread_rows = [row for row in candidates if _has_unread(row)]
    require_unread = _requires_unread()
    rows = unread_rows if require_unread else candidates
    enqueued = 0
    ignored_no_unread = len(candidates) - len(unread_rows) if require_unread else 0
    ignored_existing = 0
    items: list[dict] = []
    for row in rows:
        signature = watcher._conversation_signature(row)
        changed, item = state.enqueue_conversation(row, signature)
        if changed:
            enqueued += 1
            items.append(item)
            state.append_event({"type": "queue_enqueued", "conversation": row})
        else:
            ignored_existing += 1
            if process_existing and item.get("status") in {"done", "skipped", "failed"}:
                # enqueue_conversation already reopens changed completed rows;
                # unchanged existing rows stay ignored.
                pass
    return {
        "ok": True,
        "visible": len(candidates),
        "unread": len(unread_rows),
        "scanned": len(rows),
        "enqueued": enqueued,
        "ignored": ignored_no_unread + ignored_existing,
        "ignored_no_unread": ignored_no_unread,
        "ignored_existing": ignored_existing,
        "items": items,
        "candidates": candidates,
        "unread_items": unread_rows,
        "pages_scanned": scan.get("pages_scanned", 1),
    }


def enqueue_current_chat_if_changed(*, last: int, inbox_limit: int = 30) -> dict:
    """Enqueue the currently open chat when its latest visible user message changed.

    This catches the active conversation case where WeCom clears the sidebar red
    badge as soon as focus moves, so the unread-only sidebar scan would miss it.
    """
    current = chat.read_current(last=last, capture_images=False)
    latest = watcher.latest_user_message(current.get("messages", []))
    selected = macos_backend.selected_conversation_row(limit=inbox_limit)
    if not selected:
        return {"ok": True, "enqueued": 0, "reason": "selected_conversation_not_found"}
    if not watcher._should_consider(selected):
        return {"ok": True, "enqueued": 0, "reason": "selected_conversation_not_customer"}
    if latest is None:
        latest = _latest_message_matching_preview(current.get("messages", []), str(selected.get("preview") or ""))
    if latest is None:
        return {"ok": True, "enqueued": 0, "reason": "latest_message_not_user"}
    if latest.get("role_confidence") == "low":
        return {"ok": True, "enqueued": 0, "reason": "selected_conversation_low_role_confidence"}
    latest_text = str(latest.get("content") or latest.get("text") or "").strip()
    if latest_text and _reply_match_key(latest_text) == _reply_match_key(_latest_reply_for_row(selected)):
        return {"ok": True, "enqueued": 0, "reason": "latest_visible_message_is_own_reply"}
    row = {
        **selected,
        "preview": latest_text or selected.get("preview", ""),
        "unread": True,
        "unread_count": max(1, int(selected.get("unread_count") or 0)),
        "source": selected.get("source") or "current-chat",
    }
    signature = f"current|{row.get('title', '')}|{latest_text}"
    existing = state.get_job_by_conversation_key(state.conversation_key_for_row(row))
    if _same_current_message(existing, signature=signature, message_hash=str(current.get("hash") or "")):
        return {
            "ok": True,
            "enqueued": 0,
            "item": existing,
            "conversation": row.get("title", ""),
            "latest": latest_text,
            "message_hash": current.get("hash", ""),
            "reason": "same_current_message",
        }
    changed, item = state.enqueue_conversation(row, signature)
    if changed:
        state.append_event({"type": "current_chat_enqueued", "conversation": row})
    return {
        "ok": True,
        "enqueued": 1 if changed else 0,
        "item": item,
        "conversation": row.get("title", ""),
        "latest": latest_text,
        "message_hash": current.get("hash", ""),
        "reason": "" if changed else "unchanged",
    }


def scan_loop(*, poll: float, inbox_limit: int, once: bool = False) -> dict:
    """Continuously scan inbox into the queue without touching chat panes."""
    iterations = 0
    enqueued = 0
    while True:
        iterations += 1
        result = scan_once(inbox_limit=inbox_limit)
        enqueued += result["enqueued"]
        if result["enqueued"]:
            print(f"[scan] enqueued {result['enqueued']} item(s)")
        if once:
            break
        time.sleep(poll)
    return {"ok": True, "iterations": iterations, "enqueued": enqueued}


def process_one(*, last: int, mode: str) -> dict:
    """Claim and process one queued conversation."""
    if mode not in {"dry-run", "approve", "auto"}:
        raise ValueError("mode must be dry-run, approve, or auto")

    with state.gui_lock():
        job = state.claim_next()
        if job is None:
            return {"ok": True, "processed": 0, "reason": "queue_empty"}

        title = job["title"]
        try:
            inbox.open_row(job)
            time.sleep(0.5)
            current = chat.read_current(last=last, capture_images=True)
            latest = watcher.latest_user_message(current["messages"])
            if latest is None:
                reason = f"latest_message_not_user:{current['messages'][-1].get('role') if current['messages'] else None}"
                state.mark_skipped(job["id"], reason)
                state.append_event({"type": "queue_skipped", "conversation": title, "reason": reason})
                print(f"[worker] skip {title}: {reason}")
                return {"ok": True, "processed": 1, "sent": 0, "skipped": 1, "conversation": title, "reason": reason}

            binding = state.lookup_wecom_customer(customer_name=title) or {}
            draft = llm.draft_reply(
                current["messages"],
                customer_name=title,
                customer_uid=str(binding.get("uid") or ""),
            )
            state.append_event(
                {
                    "type": "queue_draft",
                    "conversation": title,
                    "hash": current["hash"],
                    "latest": latest,
                    "draft": draft["text"],
                    "mode": mode,
                }
            )
            if mode == "dry-run":
                state.mark_done(job["id"], message_hash=current["hash"], reply_text=draft["text"])
                print(f"[worker] dry-run {title}: {draft['text']}")
                return {"ok": True, "processed": 1, "sent": 0, "conversation": title, "reply": draft["text"]}
            if mode == "approve":
                print(f"\nConversation: {title}\nSuggested reply:\n{draft['text']}")
                answer = input("Send this reply? [y/N] ").strip().lower()
                if answer != "y":
                    state.mark_skipped(job["id"], "not_approved")
                    return {"ok": True, "processed": 1, "sent": 0, "conversation": title, "reason": "not_approved"}

            reply.send_text(draft["text"], dry_run=False, submit=True)
            time.sleep(0.5)
            after_send = chat.read_current(last=last, capture_images=False)
            if not _messages_contain_text(after_send["messages"], draft["text"]):
                raise RuntimeError("sent_reply_not_visible")
            state.mark_done(job["id"], message_hash=after_send["hash"], reply_text=draft["text"])
            state.append_event(
                {"type": "queue_sent", "conversation": title, "hash": after_send["hash"], "reply": draft["text"]}
            )
            print(f"[worker] sent to {title}: {draft['text']}")
            return {"ok": True, "processed": 1, "sent": 1, "conversation": title, "reply": draft["text"]}
        except Exception as exc:
            state.mark_failed(job["id"], str(exc))
            state.append_event({"type": "queue_failed", "conversation": title, "error": str(exc)})
            print(f"[worker] failed {title}: {exc}")
            return {"ok": False, "processed": 1, "sent": 0, "failed": 1, "conversation": title, "error": str(exc)}


def worker_loop(*, poll: float, last: int, mode: str, once: bool = False) -> dict:
    """Continuously process queued conversations one at a time."""
    iterations = 0
    processed = 0
    sent = 0
    while True:
        iterations += 1
        result = process_one(last=last, mode=mode)
        processed += result.get("processed", 0)
        sent += result.get("sent", 0)
        if once:
            break
        if result.get("reason") == "queue_empty":
            time.sleep(poll)
    return {"ok": True, "iterations": iterations, "processed": processed, "sent": sent}
