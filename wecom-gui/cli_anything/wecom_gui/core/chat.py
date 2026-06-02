"""Read visible chat context from the WeCom desktop window."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cli_anything.wecom_gui.utils import macos_backend


NOISE_LINES = {
    "搜索",
    "通讯录",
    "工作台",
    "会议",
    "文档",
    "邮件",
    "日程",
}

@dataclass(frozen=True)
class Message:
    role: str
    text: str
    source: str = "accessibility"


def _dedupe_keep_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _message_hash(messages: list[Message]) -> str:
    joined = "\n".join(msg.text for msg in messages)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalized_role(value: object) -> str:
    role = str(value or "").strip()
    if not role or role == "unknown":
        return "用户"
    return role


def infer_roles(messages: list[dict]) -> list[dict]:
    """Infer WeCom message roles from horizontal bubble positions.

    WeCom does not expose sender labels in Accessibility for all message rows.
    In the desktop layout, outgoing/service bubbles are right-aligned. We split
    visible message right edges at the midpoint between min/max. This works
    better than left x-position because long right-aligned bubbles expand left.
    This is a heuristic, but it is deterministic and exposed through
    `role_confidence` so watch mode can stay conservative.
    """
    xs = [msg.get("x") for msg in messages if isinstance(msg.get("x"), int)]
    rights = [msg.get("right") for msg in messages if isinstance(msg.get("right"), int)]
    if len(set(xs)) < 2 and len(set(rights)) < 2:
        return [
            {
                **msg,
                "role": _normalized_role(msg.get("role")),
                "role_confidence": "low",
                "content": msg.get("text", ""),
            }
            for msg in messages
        ]

    left_threshold = (min(xs) + max(xs)) / 2 if len(set(xs)) >= 2 else None
    right_threshold = (min(rights) + max(rights)) / 2 if len(set(rights)) >= 2 else None
    enriched: list[dict] = []
    for msg in messages:
        x = msg.get("x")
        right = msg.get("right")
        if isinstance(x, int) and left_threshold is not None and x < left_threshold:
            role = "用户"
            confidence = "medium"
        elif isinstance(right, int) and right_threshold is not None:
            role = "客服" if right >= right_threshold else "用户"
            confidence = "medium"
        else:
            role = _normalized_role(msg.get("role"))
            confidence = "low"
        enriched.append(
            {
                **msg,
                "role": role,
                "role_confidence": confidence,
                "role_threshold": right_threshold,
                "role_left_threshold": left_threshold,
                "content": msg.get("text", ""),
            }
        )
    return enriched


def _message_has_media(message: dict) -> bool:
    return bool(message.get("media"))


def _last_user_turn_start(messages: list[dict]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "用户" or _message_has_media(messages[index]):
            start = index
            while start > 0 and (messages[start - 1].get("role") == "用户" or _message_has_media(messages[start - 1])):
                start -= 1
            return start
    return len(messages)


def _strip_old_media(messages: list[dict]) -> list[dict]:
    """Keep image capture work scoped to the latest user turn."""
    start = _last_user_turn_start(messages)
    stripped: list[dict] = []
    for index, message in enumerate(messages):
        if index >= start:
            stripped.append(message)
            continue
        copied = {key: value for key, value in message.items() if key != "media"}
        stripped.append(copied)
    return stripped


def _mark_latest_media_from_preview(messages: list[dict], preview: str | None) -> list[dict]:
    preview_text = str(preview or "").strip()
    if preview_text not in {"[动画表情]", "[表情]", "[动画]"}:
        return messages
    patched = [dict(message) for message in messages]
    for index in range(len(patched) - 1, -1, -1):
        media_items = patched[index].get("media")
        if not isinstance(media_items, list) or not media_items:
            continue
        patched[index]["text"] = preview_text
        patched[index]["content"] = preview_text
        patched[index]["media"] = [
            {
                **media,
                "type": "animated_sticker",
                "skip_capture": True,
                "error": "animated_sticker_not_captured",
            }
            for media in media_items
            if isinstance(media, dict)
        ]
        return patched
    return messages


def read_current(
    last: int = 10,
    app_name: str | None = None,
    *,
    capture_images: bool = True,
    media_preview: str | None = None,
) -> dict:
    """Read the latest visible text lines as chat context."""
    try:
        gui_messages = macos_backend.chat_messages(
            app_name,
            last=last,
            capture_images=False,
            include_image_media=True,
        )
    except TypeError:
        gui_messages = macos_backend.chat_messages(app_name, last=last, capture_images=False)
    if gui_messages:
        gui_messages = infer_roles(gui_messages)
        gui_messages = _mark_latest_media_from_preview(gui_messages, media_preview)
        if capture_images:
            gui_messages = macos_backend.capture_chat_images(_strip_old_media(gui_messages))
        return {
            "ok": True,
            "source": "accessibility-chat-table",
            "last": last,
            "capture_images": capture_images,
            "message_count": len(gui_messages),
            "hash": hashlib.sha256(
                "\n".join(msg["text"] for msg in gui_messages).encode("utf-8")
            ).hexdigest(),
            "messages": gui_messages,
        }

    raw_lines = macos_backend.visible_text(app_name)
    lines = [line for line in _dedupe_keep_order(raw_lines) if line not in NOISE_LINES]
    selected = lines[-last:] if last > 0 else lines
    messages = [Message(role="unknown", text=line) for line in selected]
    return {
        "ok": True,
        "source": "accessibility",
        "last": last,
        "capture_images": capture_images,
        "message_count": len(messages),
        "hash": _message_hash(messages),
        "messages": [msg.__dict__ for msg in messages],
        "raw_line_count": len(raw_lines),
    }
