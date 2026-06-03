"""Fixed new-customer welcome routing helpers."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata

from cli_anything.wecom_gui.core import message_config


DEFAULT_WELCOME_MESSAGE = "{WELCOME_MESSAGE}"
NEW_CUSTOMER_PREFIX = "你已添加了"
NEW_CUSTOMER_SUFFIX = "现在可以开始聊天了"


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"\s+", "", text)


def is_new_customer_text(value: object) -> bool:
    """Return whether text is the WeCom new-customer system prompt."""
    text = normalize_text(value)
    if not text:
        return False
    return text.startswith(NEW_CUSTOMER_PREFIX) and (
        NEW_CUSTOMER_SUFFIX in text or text.endswith("...") or text.endswith("…") or len(text) >= len(NEW_CUSTOMER_PREFIX) + 1
    )


def system_text_from_messages(messages: list[dict]) -> str:
    for message in messages:
        text = str(message.get("content") or message.get("text") or "").strip()
        if is_new_customer_text(text):
            return text
    return ""


def system_text_from_row(row: dict) -> str:
    preview = str(row.get("preview") or "").strip()
    if is_new_customer_text(preview):
        return preview
    for value in row.get("raw") or []:
        text = str(value or "").strip()
        if is_new_customer_text(text):
            return text
    return ""


def is_new_customer_row(row: dict) -> bool:
    return bool(system_text_from_row(row))


def is_new_customer_context(row: dict, messages: list[dict]) -> bool:
    return bool(system_text_from_messages(messages) or system_text_from_row(row))


def welcome_message() -> str:
    """Return the configured fixed welcome message without calling an LLM."""
    configured = os.environ.get("WECOM_GUI_WELCOME_MESSAGE", "").strip()
    if configured:
        return configured
    return message_config.fixed_message("welcome", "wecom_fixed_welcome").strip() or DEFAULT_WELCOME_MESSAGE


def message_hash(conversation_key: str, system_text: str) -> str:
    seed = "|".join(["welcome", str(conversation_key or ""), str(system_text or "")])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
