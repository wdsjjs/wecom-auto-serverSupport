"""Deterministic fixed-reply agent helpers.

This module owns service phrases that do not require an LLM: new-customer
notices, supplement first prompts, and short supplement acknowledgement text.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from cli_anything.wecom_gui.core import message_config, state, welcome
from cli_anything.wecom_gui.core.text import clean_customer_reply_text, clean_history_message_text


WELCOME_MESSAGE = "{WELCOME_MESSAGE}"
WELCOME_REPLY_SOURCE = "welcome"
WELCOME_ADDED_PATTERN = re.compile(r"你已添加了\s*(?P<nickname>.+?)\s*，现在可以开始聊天了。")
WELCOME_GREETING_SYSTEM_TEXT = "以上是打招呼内容"
SUPPLEMENT_REPLY_SOURCE = state.SUPPLEMENT_REPLY_SOURCE

LogFn = Callable[[str], None]
SupplementLogFn = Callable[..., None]


def message_text(message: dict | None) -> str:
    if not message:
        return ""
    return str(message.get("content") or message.get("text") or "")


def short_text(value: object, limit: int = 140) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def match_text(value: str | None) -> str:
    return "".join(str(value or "").split())


def texts_match(a: str | None, b: str | None) -> bool:
    left = match_text(a)
    right = match_text(b)
    if not left or not right:
        return False
    return left == right or (len(left) >= 6 and len(right) >= 6 and (left in right or right in left))


def is_welcome_added_system_text(text: str | None) -> bool:
    return bool(WELCOME_ADDED_PATTERN.search(str(text or "").strip()))


def is_greeting_system_text(text: str | None) -> bool:
    return WELCOME_GREETING_SYSTEM_TEXT in str(text or "").strip()


def is_system_notice_message(message: dict | None) -> bool:
    text = message_text(message).strip()
    return is_welcome_added_system_text(text) or is_greeting_system_text(text)


def supplement_profile_prompt() -> str:
    return message_config.supplement_profile_prompt()


def supplement_need_choices_text() -> str:
    return message_config.supplement_need_choices_text()


def supplement_first_reply_with_profile() -> str:
    return message_config.supplement_first_reply_with_profile()


def supplement_first_reply_choices_only() -> str:
    return message_config.supplement_first_reply_choices_only()


def supplement_selection_ack() -> str:
    return message_config.fixed_message("supplement", "selection_ack")


def fixed_message_texts_for_matching() -> tuple[str, ...]:
    values = [
        welcome.welcome_message(),
        supplement_profile_prompt(),
        supplement_need_choices_text(),
        supplement_first_reply_with_profile(),
        supplement_first_reply_choices_only(),
        supplement_selection_ack(),
        message_config.fixed_message("welcome", "supplement_web_welcome_template"),
    ]
    return tuple(clean_history_message_text(value) for value in values if clean_history_message_text(value))


def is_card_or_link_message(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if body.startswith(("[小程序]", "[链接]")):
        return True
    return "WXMsg WeAppLogo 小程序" in body


def is_fixed_service_text(text: str | None) -> bool:
    body = clean_history_message_text(text or "")
    if not body:
        return False
    if is_welcome_added_system_text(body) or is_greeting_system_text(body):
        return True
    if is_card_or_link_message(body):
        return True
    for fixed in fixed_message_texts_for_matching():
        if texts_match(body, fixed):
            return True
    if "Luna 营养工厂健康顾问" in body and "领产品说明书" in body:
        return True
    if "请问您想改善哪方面呢" in body and "20.儿童成长" in body:
        return True
    return False


def supplement_wecom_welcome_text(customer_name: str) -> str:
    title = str(customer_name or "客户").strip() or "客户"
    template = message_config.fixed_message("welcome", "supplement_web_welcome_template")
    return clean_customer_reply_text(template.replace("{用户名}", title))


def supplement_welcome_send_parts(final_reply: str, context: dict) -> list[str]:
    """Return separate WeCom messages for the welcome follow-up flow."""
    body = clean_customer_reply_text(final_reply)
    if not body:
        return []
    welcome_text = clean_customer_reply_text(context.get("supplement_welcome_text") or "")
    followup_text = clean_customer_reply_text(context.get("supplement_followup_text") or "")
    if not welcome_text or not followup_text:
        return [body]
    if body == clean_customer_reply_text(f"{welcome_text}\n\n{followup_text}"):
        return [welcome_text, followup_text]
    if body.startswith(welcome_text):
        remainder = clean_customer_reply_text(body[len(welcome_text):])
        if remainder:
            return [welcome_text, remainder]
    return [body]


def supplement_first_reply_text(*, has_profile: bool, customer_name: str = "", from_welcome: bool = False) -> str:
    first_reply = supplement_first_reply_choices_only() if has_profile else supplement_first_reply_with_profile()
    if not from_welcome:
        return first_reply
    welcome_text = supplement_wecom_welcome_text(customer_name)
    return clean_customer_reply_text(f"{welcome_text}\n\n{first_reply}" if welcome_text else first_reply)


def mark_welcome_ready(
    job: dict,
    current: dict,
    trigger: dict,
    *,
    customer_key: str,
    log: LogFn,
) -> dict:
    welcome_text = clean_customer_reply_text(os.environ.get("WECOM_WELCOME_MESSAGE") or WELCOME_MESSAGE)
    trigger_context = {
        **trigger,
        "role": "系统",
        "message_type": "system",
        "role_confidence": str(trigger.get("role_confidence") or "system_notice"),
    }
    state.mark_drafting(
        job["id"],
        message_hash=str(current.get("hash") or ""),
        messages=current.get("messages", []),
        latest=trigger_context,
    )
    state.mark_welcome_status(
        customer_key,
        state.WELCOME_PENDING,
        conversation_key=str(job.get("conversation_key") or ""),
        conversation=str(job.get("title") or ""),
        job_id=job["id"],
        reason="welcome_draft_ready",
    )
    state.mark_ready(job["id"], reply_text=welcome_text, reply_source=WELCOME_REPLY_SOURCE)
    state.append_event(
        {
            "type": "agent_welcome_ready",
            "job_id": job["id"],
            "conversation": job.get("title") or "",
            "customer_key": customer_key,
            "trigger": message_text(trigger),
        }
    )
    log(f"[AI客服] 新用户欢迎草稿已生成：{job.get('title') or ''}｜customer_key={customer_key}")
    return {"ok": True, "read": 1, "drafting": 0, "welcome": 1, "conversation": job.get("title") or ""}


def mark_supplement_first_reply_ready(
    job: dict,
    current: dict,
    latest: dict,
    *,
    customer_key: str,
    trace_id: str,
    has_profile: bool,
    route: dict,
    selected_needs: list[str],
    known_profile: dict,
    log_supplement: SupplementLogFn,
    log: LogFn,
    from_welcome: bool = False,
) -> dict:
    stage = state.SUPPLEMENT_COLLECTING_PROFILE
    latest_text = message_text(latest)
    first_reply = supplement_first_reply_choices_only() if has_profile else supplement_first_reply_with_profile()
    welcome_text = supplement_wecom_welcome_text(str(job.get("title") or "")) if from_welcome else ""
    reply_text = supplement_first_reply_text(
        has_profile=has_profile,
        customer_name=str(job.get("title") or ""),
        from_welcome=from_welcome,
    )
    extra_context = {
        "agent_mode": SUPPLEMENT_REPLY_SOURCE,
        "supplement_trace_id": trace_id,
        "supplement_customer_key": customer_key,
        "supplement_stage": stage,
        "supplement_from_welcome": from_welcome,
    }
    if from_welcome:
        extra_context.update(
            {
                "supplement_welcome_text": welcome_text,
                "supplement_followup_text": first_reply,
            }
        )
    state.mark_drafting(
        job["id"],
        message_hash=str(current.get("hash") or ""),
        messages=current.get("messages", []),
        latest=latest,
        extra_context=extra_context,
    )
    if from_welcome:
        state.mark_welcome_status(
            customer_key,
            state.WELCOME_PENDING,
            conversation_key=str(job.get("conversation_key") or ""),
            conversation=str(job.get("title") or ""),
            job_id=job["id"],
            reason="supplement_welcome_ready",
        )
    elif route.get("detected_intent") in {"welcome_followup", "new_customer_first_contact"}:
        state.mark_welcome_status(
            customer_key,
            state.WELCOME_SKIPPED,
            conversation_key=str(job.get("conversation_key") or ""),
            conversation=str(job.get("title") or ""),
            job_id=job["id"],
            reason="local_welcome_disabled",
        )
    state.mark_supplement_state(
        customer_key,
        stage,
        conversation_key=str(job.get("conversation_key") or ""),
        conversation=str(job.get("title") or ""),
        job_id=job["id"],
        trace_id=trace_id,
        digging_count=0,
        selected_needs=selected_needs,
        known_profile=known_profile,
        message_hash=str(current.get("hash") or ""),
        pending_next_stage=state.SUPPLEMENT_DIGGING_NEED,
        reason="first_prompt_ready",
    )
    log_supplement(
        "supplement_route_evaluated",
        job=job,
        customer_key=customer_key,
        trace_id=trace_id,
        stage=stage,
        message_hash=str(current.get("hash") or ""),
        latest_text=latest_text,
        details={
            "triggered": True,
            "matched_terms": route.get("matched_terms") or [],
            "active_state_exists": bool(route.get("active_state_exists")),
            "detected_intent": route.get("detected_intent") or "",
            "has_profile": has_profile,
            "trigger_source": "welcome" if from_welcome else "customer",
        },
    )
    state.mark_ready(
        job["id"],
        reply_text=reply_text,
        reply_source=SUPPLEMENT_REPLY_SOURCE,
        action="clarify",
    )
    auto_send_first_prompt = route.get("detected_intent") in {"welcome_followup", "new_customer_first_contact"}
    if auto_send_first_prompt:
        state.mark_approved(job["id"])
    state.append_event(
        {
            "type": "agent_supplement_first_prompt_ready",
            "job_id": job["id"],
            "conversation": job.get("title") or "",
            "customer_key": customer_key,
            "trace_id": trace_id,
            "has_profile": has_profile,
            "auto_send": auto_send_first_prompt,
        }
    )
    suffix = "，已进入发送队列" if auto_send_first_prompt else ""
    log(f"[AI客服] 补剂首轮话术已生成：{job.get('title') or ''}｜customer_key={customer_key}{suffix}")
    return {"ok": True, "read": 1, "drafting": 0, "supplement": 1, "conversation": job.get("title") or ""}


def mark_supplement_selection_ack_ready(
    job: dict,
    current: dict,
    latest: dict,
    *,
    customer_key: str,
    trace_id: str,
    state_row: dict,
    selected_needs: list[str],
    need_numbers: list[int],
    known_profile: dict,
    profile_opt_out: bool,
    log_supplement: SupplementLogFn,
    log: LogFn,
) -> dict:
    latest_text = message_text(latest)
    state.mark_drafting(
        job["id"],
        message_hash=str(current.get("hash") or ""),
        messages=current.get("messages", []),
        latest=latest,
        extra_context={
            "agent_mode": SUPPLEMENT_REPLY_SOURCE,
            "supplement_trace_id": trace_id,
            "supplement_customer_key": customer_key,
            "supplement_stage": state.SUPPLEMENT_DIGGING_NEED,
            "supplement_next_action": "wait_for_digging_detail",
        },
    )
    state.mark_supplement_state(
        customer_key,
        state.SUPPLEMENT_DIGGING_NEED,
        conversation_key=str(job.get("conversation_key") or ""),
        conversation=str(job.get("title") or ""),
        job_id=job["id"],
        trace_id=trace_id,
        digging_count=int(state_row.get("digging_count") or 0),
        selected_needs=selected_needs or state_row.get("selected_needs") or [],
        known_profile=known_profile,
        message_hash=str(current.get("hash") or ""),
        pending_next_stage=state.SUPPLEMENT_DIGGING_NEED,
        reason="need_selection_ack",
    )
    log_supplement(
        "supplement_need_parsed",
        job=job,
        customer_key=customer_key,
        trace_id=trace_id,
        stage=state.SUPPLEMENT_DIGGING_NEED,
        message_hash=str(current.get("hash") or ""),
        latest_text=latest_text,
        details={
            "need_numbers": need_numbers,
            "need_texts": selected_needs,
            "profile_fields_detected": ["basic_profile"] if known_profile.get("has_basic_profile") else [],
            "profile_opt_out": profile_opt_out,
        },
    )
    state.mark_ready(
        job["id"],
        reply_text=supplement_selection_ack(),
        reply_source=SUPPLEMENT_REPLY_SOURCE,
        action="clarify",
    )
    log(f"[AI客服] 补剂需求选择已确认：{job.get('title') or ''}｜{selected_needs or short_text(latest_text)}")
    return {"ok": True, "read": 1, "drafting": 0, "supplement": 1, "conversation": job.get("title") or ""}
