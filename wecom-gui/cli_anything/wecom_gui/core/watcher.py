"""Polling loop for semi-automatic and automatic customer-service replies."""

from __future__ import annotations

import time

from cli_anything.wecom_gui.core import chat, inbox, llm, reply, state


def latest_user_message(messages: list[dict]) -> dict | None:
    """Return latest message only if it came from the customer/user."""
    if not messages:
        return None
    latest = messages[-1]
    if latest.get("role") != "用户":
        return None
    text = str(latest.get("content") or latest.get("text") or "").strip()
    if "以上是打招呼内容" in text or ("你已添加了" in text and "现在可以开始聊天了" in text):
        return None
    return latest


def watch_current(
    *,
    poll: float,
    last: int,
    mode: str,
    once: bool = False,
) -> dict:
    """Watch the currently open chat and reply when visible content changes.

    This MVP intentionally watches only the current chat. Inbox scanning is the
    next layer because it is much more dependent on WeCom layout and unread badges.
    """
    if mode not in {"dry-run", "approve", "auto"}:
        raise ValueError("mode must be dry-run, approve, or auto")

    seen_hash: str | None = None
    iterations = 0
    replies = 0
    while True:
        iterations += 1
        current = chat.read_current(last=last, capture_images=False)
        current_hash = current["hash"]
        if current_hash != seen_hash and current["message_count"]:
            seen_hash = current_hash
            latest = latest_user_message(current["messages"])
            if latest is None:
                state.append_event(
                    {
                        "type": "skip",
                        "reason": "latest_message_not_user",
                        "hash": current_hash,
                        "latest_role": current["messages"][-1].get("role"),
                        "mode": mode,
                    }
                )
                print(f"[watch] skip: latest role is {current['messages'][-1].get('role')}")
                if once:
                    break
                time.sleep(poll)
                continue
            draft = llm.draft_reply(current["messages"])
            event = {"type": "draft", "hash": current_hash, "draft": draft["text"], "mode": mode}
            state.append_event(event)
            if mode == "auto":
                reply.send_text(draft["text"], dry_run=False, submit=True)
                replies += 1
                state.append_event({"type": "sent", "hash": current_hash, "reply": draft["text"]})
                print(f"[watch] sent: {draft['text']}")
            elif mode == "dry-run":
                print(f"[watch] dry-run draft: {draft['text']}")
                replies += 0
            else:
                print("\nSuggested reply:\n" + draft["text"])
                answer = input("Send this reply? [y/N] ").strip().lower()
                if answer == "y":
                    reply.send_text(draft["text"], dry_run=False, submit=True)
                    replies += 1
        if once:
            break
        time.sleep(poll)
    return {"ok": True, "iterations": iterations, "replies": replies}


def _conversation_signature(row: dict) -> str:
    return "|".join(
        [
            str(row.get("title", "")),
            str(row.get("preview", "")),
            ",".join(row.get("tags", []) or []),
        ]
    )


def _should_consider(row: dict) -> bool:
    """Filter obvious non-customer/system rows from inbox scanning."""
    title = row.get("title", "")
    if title in {"企业微信团队", "行业资讯", "微信客服", "客户联系"}:
        return False
    preview = row.get("preview", "")
    if "聊天已结束" in preview:
        return False
    return True


def watch_inbox(
    *,
    poll: float,
    last: int,
    mode: str,
    inbox_limit: int,
    process_existing: bool = False,
    once: bool = False,
) -> dict:
    """Watch visible inbox rows, open changed conversations, and reply if needed."""
    if mode not in {"dry-run", "approve", "auto"}:
        raise ValueError("mode must be dry-run, approve, or auto")

    seen: dict[str, str] = {}
    processed_hashes: set[str] = set()
    iterations = 0
    replies = 0
    while True:
        iterations += 1
        scan = inbox.scan_visible(limit=inbox_limit)
        rows = [row for row in scan["conversations"] if _should_consider(row)]
        for row in rows:
            title = row["title"]
            signature = _conversation_signature(row)
            first_seen = title not in seen
            changed = seen.get(title) != signature
            seen[title] = signature
            if first_seen and not process_existing:
                continue
            if not changed and not first_seen:
                continue

            state.append_event({"type": "inbox_candidate", "conversation": row, "mode": mode})
            inbox.open_by_name(title)
            time.sleep(0.5)
            current = chat.read_current(last=last, capture_images=False)
            latest = latest_user_message(current["messages"])
            if latest is None:
                state.append_event(
                    {
                        "type": "skip",
                        "reason": "latest_message_not_user",
                        "conversation": title,
                        "latest_role": current["messages"][-1].get("role") if current["messages"] else None,
                        "mode": mode,
                    }
                )
                print(f"[watch-inbox] skip {title}: latest role is {current['messages'][-1].get('role') if current['messages'] else None}")
                continue

            current_hash = current["hash"]
            if current_hash in processed_hashes:
                continue
            processed_hashes.add(current_hash)
            draft = llm.draft_reply(current["messages"])
            state.append_event(
                {
                    "type": "draft",
                    "conversation": title,
                    "hash": current_hash,
                    "latest": latest,
                    "draft": draft["text"],
                    "mode": mode,
                }
            )
            if mode == "auto":
                reply.send_text(draft["text"], dry_run=False, submit=True)
                replies += 1
                state.append_event({"type": "sent", "conversation": title, "hash": current_hash, "reply": draft["text"]})
                print(f"[watch-inbox] sent to {title}: {draft['text']}")
            elif mode == "dry-run":
                print(f"[watch-inbox] dry-run {title}: {draft['text']}")
            else:
                print(f"\nConversation: {title}\nSuggested reply:\n{draft['text']}")
                answer = input("Send this reply? [y/N] ").strip().lower()
                if answer == "y":
                    reply.send_text(draft["text"], dry_run=False, submit=True)
                    replies += 1
        if once:
            break
        time.sleep(poll)
    return {"ok": True, "iterations": iterations, "replies": replies}
