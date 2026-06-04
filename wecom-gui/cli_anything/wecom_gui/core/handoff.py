"""Handoff detection helpers for the WeCom review flow."""

from __future__ import annotations


DIRECT_HANDOFF_TERMS = ("转人工", "人工客服", "人工", "真人客服", "真人")
INDIRECT_HANDOFF_RULES = (
    (("投诉", "举报", "维权", "差评", "曝光", "12315", "消协", "工商", "黑猫"), "客户投诉/维权，需要人工处理"),
    (("退款", "退货", "退钱", "退单", "取消订单", "赔偿", "补偿", "仅退款"), "客户售后/退款诉求，需要人工处理"),
    (("生气", "气死", "气炸", "太差", "垃圾", "骗子", "骗我", "欺骗", "欺诈", "坑我", "不满意", "失望"), "客户情绪激动，需要人工安抚"),
    (("主管", "经理", "负责人", "领导", "上级"), "客户要求升级处理"),
    (("起诉", "律师", "法院", "报警", "警察", "法律", "法务"), "客户法律/风险诉求，需要人工处理"),
)
HANDOFF_REPLY_TEXT = "您好，这个问题我帮您转人工客服确认处理，请您稍等。"


def detect_direct_handoff(text: object) -> bool:
    body = str(text or "")
    return any(term in body for term in DIRECT_HANDOFF_TERMS)


def classify_handoff(text: object) -> dict:
    body = str(text or "").strip()
    if not body:
        return {"type": "", "reason": ""}
    if detect_direct_handoff(body):
        return {"type": "direct", "reason": direct_handoff_reason(body)}
    for terms, reason in INDIRECT_HANDOFF_RULES:
        if any(term in body for term in terms):
            return {"type": "indirect", "reason": reason}
    return {"type": "", "reason": ""}


def detect_handoff(text: object) -> bool:
    return bool(classify_handoff(text).get("type"))


def direct_handoff_reason(text: object) -> str:
    body = str(text or "").strip()
    for term in DIRECT_HANDOFF_TERMS:
        if term in body:
            return f"客户要求{term}"
    return "客户要求人工客服"
