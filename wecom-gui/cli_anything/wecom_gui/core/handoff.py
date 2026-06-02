"""Handoff detection helpers for the WeCom review flow."""

from __future__ import annotations


DIRECT_HANDOFF_TERMS = ("转人工", "人工客服", "人工")
HANDOFF_REPLY_TEXT = "您好，这个问题我帮您转人工客服确认处理，请您稍等。"


def detect_direct_handoff(text: object) -> bool:
    body = str(text or "")
    return any(term in body for term in DIRECT_HANDOFF_TERMS)


def direct_handoff_reason(text: object) -> str:
    body = str(text or "").strip()
    for term in DIRECT_HANDOFF_TERMS:
        if term in body:
            return f"客户要求{term}"
    return "客户要求人工客服"
