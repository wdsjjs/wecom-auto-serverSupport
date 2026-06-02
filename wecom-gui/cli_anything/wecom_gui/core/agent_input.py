"""Build offline-test payloads from the current WeCom chat window."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cli_anything.wecom_gui.core import chat, llm, state
from cli_anything.wecom_gui.utils import macos_backend


def _message_text(message: dict) -> str:
    return str(message.get("content") or message.get("text") or "")


def _dump_message(message: dict) -> dict:
    media_items = []
    for media in message.get("media") or []:
        if not isinstance(media, dict):
            continue
        media_items.append(
            {
                "type": str(media.get("type") or "image"),
                "capture_path": str(media.get("capture_path") or ""),
                "capture_ok": bool(media.get("capture_ok")),
                "error": str(media.get("error") or ""),
                "source": str(media.get("source") or ""),
            }
        )
    text = _message_text(message)
    return {
        "role": str(message.get("role") or ""),
        "content": text,
        "text": text,
        "time": str(message.get("time") or ""),
        "source": str(message.get("source") or ""),
        "role_confidence": str(message.get("role_confidence") or ""),
        "media": media_items,
    }


def _customer_uid() -> str:
    try:
        return macos_backend.current_external_user_id()
    except Exception:
        return ""


def _selected_conversation_title(inbox_limit: int) -> str:
    try:
        row = macos_backend.selected_conversation_row(limit=inbox_limit)
    except Exception:
        row = None
    return str((row or {}).get("title") or "").strip()


def _csbot_customer_id(customer_uid: str) -> str:
    return (
        customer_uid.strip()
        or os.environ.get("WECOM_GUI_CSBOT_CUSTOMER_ID", "wecom-customer").strip()
        or "wecom-customer"
    )


def build_current_agent_input(
    *,
    last: int = 12,
    capture_images: bool = True,
    customer_name: str = "",
    customer_uid: str = "",
    inbox_limit: int = 30,
) -> dict:
    """Read the current WeCom chat and return the payload passed to the Agent."""
    current = chat.read_current(last=last, capture_images=capture_images)
    messages = [message for message in current.get("messages", []) if isinstance(message, dict)]
    if not customer_name:
        customer_name = _selected_conversation_title(inbox_limit)
    if not customer_uid:
        customer_uid = _customer_uid()

    latest_user_text = llm.latest_user_text(messages)
    latest_user_turn_text = llm.latest_user_turn_text(messages)
    context = llm.build_csbot_context(messages, customer_name=customer_name)
    if customer_uid:
        context["external_user_id"] = customer_uid
        context["wecom_uid"] = customer_uid

    return {
        "ok": True,
        "created_at": time.time(),
        "source": "wecom-gui-current-chat",
        "read": {
            "hash": str(current.get("hash") or ""),
            "source": str(current.get("source") or ""),
            "message_count": len(messages),
            "capture_images": bool(current.get("capture_images")),
            "messages": [_dump_message(message) for message in messages],
        },
        "agent_input": {
            "customer_name": customer_name,
            "customer_uid": customer_uid,
            "messages": [_dump_message(message) for message in messages],
        },
        "csbot_input": {
            "customer_id": _csbot_customer_id(customer_uid),
            "query": latest_user_turn_text or latest_user_text,
            "context": context,
        },
        "latest_user_text": latest_user_text,
        "latest_user_turn_text": latest_user_turn_text,
    }


def write_agent_input_file(payload: dict, output_path: str | Path) -> Path:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def default_output_path() -> Path:
    return state.state_dir() / "agent-input-current.json"
