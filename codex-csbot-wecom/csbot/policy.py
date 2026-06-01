from __future__ import annotations

HANDOFF_TERMS = (
    "投诉",
    "退款",
    "退货",
    "赔偿",
    "人工",
    "差评",
    "维权",
    "举报",
    "12315",
)

HANDOFF_REPLY = "您好，这个问题我帮您转人工客服确认处理，请您稍等。"


def should_handoff(text: str) -> bool:
    body = text or ""
    return any(term in body for term in HANDOFF_TERMS)


def handoff_reply() -> str:
    return HANDOFF_REPLY
