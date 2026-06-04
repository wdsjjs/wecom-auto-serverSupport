"""Fast AI customer-service agent.

The agent keeps GUI work short and serial while AI calls run concurrently:

1. Scan inbox rows into the queue.
2. Open pending chats briefly, read context, and start AI drafts in threads.
3. Continue scanning/reading while drafts are in flight.
4. Re-open ready chats and send replies.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor

from cli_anything.wecom_gui.core import chat, handoff, inbox, llm, message_config, reply, state, watcher, worker, welcome
from cli_anything.wecom_gui.core.text import clean_customer_reply_text
from cli_anything.wecom_gui.utils import macos_backend


_DRAFT_STARTED_AT: dict[int, float] = {}
_DRAFT_POOL: dict[int, str] = {}
_DRAFT_MESSAGE_HASH: dict[int, str] = {}
_DRAFT_AGENT_MODE: dict[int, str] = {}
_DRAFT_TRACE_ID: dict[int, str] = {}
_DRAFT_CUSTOMER_KEY: dict[int, str] = {}
WELCOME_MESSAGE = "{WELCOME_MESSAGE}"
WELCOME_REPLY_SOURCE = "welcome"
WELCOME_ADDED_PATTERN = re.compile(r"你已添加了\s*(?P<nickname>.+?)\s*，现在可以开始聊天了。")
WELCOME_GREETING_SYSTEM_TEXT = "以上是打招呼内容"
SUPPLEMENT_REPLY_SOURCE = "supplement"
SUPPLEMENT_TRIGGER_TERMS = (
    "补剂推荐",
    "推荐补剂",
    "营养补剂",
    "搭配补剂",
    "补剂搭配",
    "补剂怎么搭配",
    "推荐搭配",
    "怎么搭配",
    "需要补什么",
    "需要补点什么",
    "吃什么补剂",
    "适合什么补剂",
)
SUPPLEMENT_NEED_CHOICES_TEXT = message_config.supplement_need_choices_text()
SUPPLEMENT_PROFILE_PROMPT = message_config.supplement_profile_prompt()
SUPPLEMENT_FIRST_REPLY_WITH_PROFILE = message_config.supplement_first_reply_with_profile()
SUPPLEMENT_FIRST_REPLY_CHOICES_ONLY = message_config.supplement_first_reply_choices_only()
SUPPLEMENT_SELECTION_ACK = message_config.fixed_message("supplement", "selection_ack")
SUPPLEMENT_PROFILE_OPT_OUT_TERMS = (
    "不想提供",
    "不愿意提供",
    "不方便提供",
    "不提供",
    "不想说",
    "不愿意说",
    "不方便说",
    "不透露",
    "不想透露",
    "不方便透露",
    "不填",
    "先不填",
    "隐私",
    "个人信息",
    "别问",
)
SUPPLEMENT_NEED_LABELS = {
    1: "抗衰/身体机能下降",
    2: "睡眠质量差",
    3: "身体代谢差/免疫力",
    4: "白发、脱发、头皮活力",
    5: "减脂减重/改善体型",
    6: "皮肤松弛暗沉/长痘痘",
    7: "晨起疲惫，精神不振",
    8: "每天用脑超八小时，注意力难集中",
    9: "抑郁焦虑，情绪差",
    10: "肝脏排毒功能差，熬夜伤肝",
    11: "办公室久坐不动人群",
    12: "用眼过度，眼疲劳",
    13: "女性保养",
    14: "肠胃不好，便秘或菌群失调",
    15: "男性性功能问题",
    16: "备孕支持",
    17: "运动健身人群",
    18: "需促进骨骼健康，强健骨质",
    19: "经常抽烟，烟瘾重",
    20: "儿童成长，助力身体发育",
}
SUPPLEMENT_SKIP_TERMS = (
    "价格",
    "多少钱",
    "起拍",
    "优惠",
    "链接",
    "发货",
    "物流",
    "订单",
    "改地址",
    "退款",
    "投诉",
)


def _message_image_paths(messages: list[dict]) -> list[str]:
    paths: list[str] = []
    for message in messages:
        for media in message.get("media") or []:
            if not isinstance(media, dict):
                continue
            path = llm.resolve_capture_path(media.get("capture_path"))
            if path and media.get("capture_ok", True):
                paths.append(path)
    return paths


def _message_has_image(message: dict) -> bool:
    for media in message.get("media") or []:
        if not isinstance(media, dict):
            continue
        media_type = str(media.get("type") or "image").strip()
        if media_type == "image" and not media.get("skip_capture"):
            return True
    return False


def _is_preview_turn_anchor(message: dict) -> bool:
    return str(message.get("role_confidence") or "") in {"preview_fallback", "unread_preview_after_image"}


def _latest_user_turn_messages(messages: list[dict]) -> list[dict]:
    for index in range(len(messages) - 1, -1, -1):
        if not _is_preview_turn_anchor(messages[index]):
            continue
        start = index
        while start > 0:
            previous = messages[start - 1]
            if _is_preview_turn_anchor(previous) or _message_has_image(previous):
                start -= 1
                continue
            break
        return messages[start : index + 1]

    turn: list[dict] = []
    for message in reversed(messages):
        text = _message_text(message).strip()
        has_media = bool(message.get("media"))
        if not text and not has_media:
            continue
        if message.get("role") == "用户" or (turn and has_media):
            turn.append(message)
            continue
        if turn:
            break
    turn.reverse()
    return turn


def _latest_user_turn_image_paths(messages: list[dict]) -> list[str]:
    return _message_image_paths(_latest_user_turn_messages(messages))


def _latest_user_turn_has_images(messages: list[dict]) -> bool:
    return any(_message_has_image(message) for message in _latest_user_turn_messages(messages))


def _draft_pool_for_messages(messages: list[dict]) -> str:
    return "image" if _latest_user_turn_has_images(messages) else "text"


def _pool_limit(pool: str, max_drafts: int) -> int:
    if pool == "image":
        return max(0, int(os.environ.get("WECOM_AGENT_IMAGE_WORKERS", "3")))
    configured = int(os.environ.get("WECOM_AGENT_TEXT_WORKERS", "0") or "0")
    if configured > 0:
        return configured
    image_workers = max(0, int(os.environ.get("WECOM_AGENT_IMAGE_WORKERS", "3")))
    return max(1, max_drafts - image_workers)


def _stale_active_seconds() -> float:
    configured = os.environ.get("WECOM_AGENT_STALE_ACTIVE_SECONDS", "").strip()
    if configured:
        return max(1.0, float(configured))
    timeouts = [180.0]
    for key in ("WECOM_GUI_CODEX_TIMEOUT", "WECOM_GUI_PI_TIMEOUT"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            timeouts.append(float(raw) + 60.0)
        except ValueError:
            continue
    return max(timeouts)


def _pool_counts(futures: dict[int, Future]) -> dict[str, int]:
    counts = {"text": 0, "image": 0}
    for job_id in futures:
        pool = _DRAFT_POOL.get(job_id, "text")
        counts[pool] = counts.get(pool, 0) + 1
    return counts


def _has_pool_capacity(pool: str, futures: dict[int, Future], max_drafts: int) -> bool:
    if len(futures) >= max_drafts:
        return False
    counts = _pool_counts(futures)
    return counts.get(pool, 0) < _pool_limit(pool, max_drafts)


def _log(message: str) -> None:
    print(message, flush=True)


def _read_only_enabled() -> bool:
    value = str(os.environ.get("WECOM_AGENT_READ_ONLY", "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _short(text: str | None, limit: int = 90) -> str:
    value = (text or "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _message_text(message: dict | None) -> str:
    if not message:
        return ""
    return str(message.get("content") or message.get("text") or "")


def _is_welcome_added_system_text(text: str | None) -> bool:
    return bool(WELCOME_ADDED_PATTERN.search(str(text or "").strip()))


def _is_greeting_system_text(text: str | None) -> bool:
    return WELCOME_GREETING_SYSTEM_TEXT in str(text or "").strip()


def _is_system_notice_message(message: dict | None) -> bool:
    if not message:
        return False
    text = _message_text(message).strip()
    return _is_welcome_added_system_text(text) or _is_greeting_system_text(text)


def _welcome_trigger_message(messages: list[dict]) -> dict | None:
    for message in reversed(messages):
        if _is_welcome_added_system_text(_message_text(message)):
            return message
    return None


def _welcome_customer_key(job: dict, visible_uid: str = "") -> str:
    uid = str(visible_uid or "").strip()
    if uid:
        return state.conversation_key_for_uid(uid)
    return str(job.get("conversation_key") or "").strip()


def _job_is_welcome_pending(job: dict) -> bool:
    customer_key = str(job.get("conversation_key") or "").strip()
    welcome = state.get_welcome_state(customer_key)
    return bool(
        welcome
        and int(welcome.get("job_id") or 0) == int(job.get("id") or 0)
        and str(welcome.get("status") or "") == state.WELCOME_PENDING
    )


def _supplement_customer_key(job: dict, visible_uid: str = "") -> str:
    uid = str(visible_uid or "").strip()
    if uid:
        return state.conversation_key_for_uid(uid)
    return str(job.get("conversation_key") or "").strip()


def _external_user_id_from_key(customer_key: str) -> str:
    key = str(customer_key or "").strip()
    return key.split(":", 1)[1] if key.startswith("uid:") else ""


def _new_trace_id(job_id: int, customer_key: str) -> str:
    return f"supp-{job_id}-{abs(hash(customer_key)) % 1_000_000}-{int(time.time() * 1000)}"


def _has_supplement_profile(text: str) -> bool:
    body = str(text or "")
    has_age = bool(re.search(r"\b\d{1,2}\s*岁\b|\b\d{1,2}\s*(?:year|years|y/o)\b", body, flags=re.I))
    has_gender = any(term in body for term in ("男", "女", "男性", "女性", "女士", "男士", "宝妈", "孕", "备孕"))
    has_standalone_age = bool(re.search(r"(?<!\d)(?:1[2-9]|[2-7]\d|80)(?!\d)", body))
    has_height_or_weight = bool(re.search(r"\b\d{2,3}\s*(?:cm|CM|厘米|斤|kg|KG|公斤)\b", body))
    return (has_gender and (has_age or has_standalone_age)) or has_height_or_weight


def _declines_supplement_profile(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    return any(term in body for term in SUPPLEMENT_PROFILE_OPT_OUT_TERMS)


def _merge_supplement_known_profile(existing: dict | None, latest_text: str) -> dict:
    known_profile = dict(existing or {})
    if _has_supplement_profile(latest_text):
        known_profile["has_basic_profile"] = True
    if _declines_supplement_profile(latest_text):
        known_profile["profile_opt_out"] = True
        known_profile["has_basic_profile"] = False
    return known_profile


def _selected_supplement_need_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for raw in re.findall(r"(?<!\d)(?:[1-9]|1\d|20)(?!\d)", str(text or "")):
        value = int(raw)
        if value not in numbers:
            numbers.append(value)
    return numbers


def _supplement_selected_needs(text: str) -> list[str]:
    return [SUPPLEMENT_NEED_LABELS[value] for value in _selected_supplement_need_numbers(text)]


def _is_supplement_need_selection(text: str) -> bool:
    if _selected_supplement_need_numbers(text):
        return True
    body = str(text or "").strip()
    if not body:
        return False
    return _has_explicit_supplement_recommendation_intent(body)


def supplement_first_reply_with_profile() -> str:
    return message_config.supplement_first_reply_with_profile()


def supplement_first_reply_choices_only() -> str:
    return message_config.supplement_first_reply_choices_only()


def supplement_selection_ack() -> str:
    return message_config.fixed_message("supplement", "selection_ack")


def _supplement_wecom_welcome_text(customer_name: str) -> str:
    title = str(customer_name or "客户").strip() or "客户"
    template = message_config.fixed_message("welcome", "supplement_web_welcome_template")
    return clean_customer_reply_text(template.replace("{用户名}", title))


def _supplement_welcome_send_parts(final_reply: str, context: dict) -> list[str]:
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


def _supplement_first_reply_text(*, has_profile: bool, customer_name: str = "", from_welcome: bool = False) -> str:
    first_reply = supplement_first_reply_choices_only() if has_profile else supplement_first_reply_with_profile()
    if not from_welcome:
        return first_reply
    welcome_text = _supplement_wecom_welcome_text(customer_name)
    return clean_customer_reply_text(f"{welcome_text}\n\n{first_reply}" if welcome_text else first_reply)


def _has_explicit_supplement_recommendation_intent(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if any(term in body for term in SUPPLEMENT_TRIGGER_TERMS):
        return True
    has_supplement = "补剂" in body or "营养品" in body
    if has_supplement and any(term in body for term in ("推荐", "搭配", "适合", "吃什么", "补什么", "改善")):
        return True
    return False


def _non_supplement_reason(text: str) -> str:
    body = str(text or "").strip()
    if not body:
        return "empty_message"
    if any(term in body for term in ("价格", "多少钱", "优惠", "链接", "起拍")):
        return "price_intent"
    if any(term in body for term in ("发货", "物流")):
        return "shipping_intent"
    if "订单" in body:
        return "order_intent"
    if "改地址" in body:
        return "address_change_intent"
    if body in {"你好", "您好", "在吗", "在不在", "hello", "hi"}:
        return "ordinary_greeting"
    return ""


def _supplement_route(latest_text: str, *, active_state: dict | None = None) -> dict:
    text = str(latest_text or "").strip()
    active_exists = bool(active_state and str(active_state.get("stage") or "") in state.SUPPLEMENT_ACTIVE_STAGES)
    matched_terms = [term for term in SUPPLEMENT_TRIGGER_TERMS if term in text]
    skip_terms = [term for term in SUPPLEMENT_SKIP_TERMS if term in text]
    non_supplement_reason = _non_supplement_reason(text)
    if non_supplement_reason and not (
        _is_supplement_need_selection(text)
        or _has_supplement_profile(text)
        or _declines_supplement_profile(text)
    ):
        return {
            "triggered": False,
            "matched_terms": matched_terms,
            "active_state_exists": active_exists,
            "detected_intent": "non_supplement",
            "reason": non_supplement_reason,
        }
    if active_exists:
        return {
            "triggered": True,
            "matched_terms": matched_terms,
            "active_state_exists": True,
            "detected_intent": "active_supplement_flow",
            "reason": "",
        }
    if _has_explicit_supplement_recommendation_intent(text) and not skip_terms:
        return {
            "triggered": True,
            "matched_terms": matched_terms,
            "active_state_exists": False,
            "detected_intent": "supplement_recommendation",
            "reason": "",
        }
    reason = non_supplement_reason or "no_recommendation_intent"
    return {
        "triggered": False,
        "matched_terms": matched_terms,
        "active_state_exists": False,
        "detected_intent": "non_supplement",
        "reason": reason,
    }


def _log_supplement(
    event_type: str,
    *,
    job: dict,
    customer_key: str,
    trace_id: str,
    stage: str,
    message_hash: str = "",
    latest_text: str = "",
    details: dict | None = None,
) -> None:
    state.log_supplement_event(
        event_type,
        trace_id=trace_id,
        job_id=job.get("id"),
        conversation_key=str(job.get("conversation_key") or ""),
        external_user_id=_external_user_id_from_key(customer_key),
        customer_id=customer_key,
        conversation=str(job.get("title") or ""),
        stage=stage,
        message_hash=message_hash,
        latest_text_preview=_short(latest_text, 120),
        details=details or {},
    )


def _supplement_agent_context(
    *,
    state_row: dict | None,
    route: dict,
    customer_key: str,
    trace_id: str,
    stage: str,
) -> dict:
    return {
        "reply_source": SUPPLEMENT_REPLY_SOURCE,
        "trace_id": trace_id,
        "customer_key": customer_key,
        "current_stage": stage,
        "digging_count": int((state_row or {}).get("digging_count") or 0),
        "selected_needs": (state_row or {}).get("selected_needs") or [],
        "known_profile": (state_row or {}).get("known_profile") or {},
        "profile_opt_out": bool(((state_row or {}).get("known_profile") or {}).get("profile_opt_out")),
        "digging_question_policy": {
            "max_questions_per_reply": 1,
            "direct_question_only": True,
            "avoid_preface_phrases": ["从...方面看", "从...角度看", "考虑到", "结合您的情况"],
            "stop_profile_questions_if_opted_out": True,
        },
        "route": {
            "matched_terms": route.get("matched_terms") or [],
            "detected_intent": route.get("detected_intent") or "",
            "active_state_exists": bool(route.get("active_state_exists")),
        },
        "knowledge_sources_required": [
            "10 补剂推荐 / recommendation_rule",
            "5 产品常规信息 / product_profile",
            "7 L0级注意事项 / safety_policy",
            "Mem0 customer profile and purchase history",
        ],
        "max_digging_rounds": 2,
    }


def _match_text(value: str | None) -> str:
    return "".join(str(value or "").split())


def _texts_match(a: str | None, b: str | None) -> bool:
    left = _match_text(a)
    right = _match_text(b)
    if not left or not right:
        return False
    return left == right or (len(left) >= 6 and len(right) >= 6 and (left in right or right in left))


def _preview_matches_suffix(suffix: str, preview: str) -> bool:
    if not suffix or not preview:
        return False
    if suffix == preview:
        return True
    if len(suffix) < 6 or len(preview) < 6:
        return False
    return suffix in preview or suffix.startswith(preview) or preview.startswith(suffix)


def _latest_from_unread_preview(messages: list[dict], preview: str | None) -> tuple[list[dict], dict | None]:
    """Repair AX role inference when the unread preview matches the newest chat rows."""
    preview_text = str(preview or "").strip()
    if not preview_text:
        return messages, None

    if preview_text in {"[图片]", "[动画表情]"}:
        for idx in range(len(messages) - 1, -1, -1):
            message = messages[idx]
            if _message_text(message).strip() not in {"[图片]", "[动画表情]"} and not _message_has_image(message):
                continue
            patched = [dict(item) for item in messages]
            original_role = str(patched[idx].get("role") or "").strip()
            text = preview_text if preview_text == "[动画表情]" else (_message_text(patched[idx]).strip() or "[图片]")
            patched[idx] = {
                **patched[idx],
                "role": "用户",
                "role_confidence": "preview_fallback",
                "original_role": original_role,
                "content": text,
                "text": text,
            }
            return patched, patched[idx]

    indices = [idx for idx, message in enumerate(messages) if _message_text(message).strip()]
    if not indices:
        return messages, None

    preview_key = _match_text(preview_text)
    max_suffix = min(len(indices), int(os.environ.get("WECOM_AGENT_PREVIEW_FALLBACK_MAX_SUFFIX", "6")))
    for count in range(max_suffix, 0, -1):
        suffix_indices = indices[-count:]
        suffix_key = "".join(_match_text(_message_text(messages[idx])) for idx in suffix_indices)
        if not _preview_matches_suffix(suffix_key, preview_key):
            continue

        patched = [dict(message) for message in messages]
        patch_indices = list(suffix_indices)
        first_idx = suffix_indices[0]
        while first_idx > 0 and _message_has_image(messages[first_idx - 1]):
            first_idx -= 1
            patch_indices.insert(0, first_idx)
        for idx in patch_indices:
            text = _message_text(patched[idx]).strip()
            original_role = str(patched[idx].get("role") or "").strip()
            patched[idx] = {
                **patched[idx],
                "role": "用户",
                "role_confidence": "preview_fallback",
                "original_role": original_role,
                "content": text,
                "text": text,
            }
        return patched, patched[suffix_indices[-1]]

    return messages, None


def _preview_is_existing_reply(job: dict, preview: str | None) -> bool:
    preview_text = str(preview or "").strip()
    if not preview_text:
        return False
    reply_text = str(job.get("reply_text") or "").strip()
    return bool(reply_text and _texts_match(reply_text, preview_text))


def _append_preview_to_user_turn(
    messages: list[dict],
    preview: str | None,
    *,
    existing_reply: str | None = None,
) -> tuple[list[dict], dict | None]:
    """Add real unread preview text when AX only exposes the latest image row."""
    preview_text = str(preview or "").strip()
    if not preview_text or preview_text in {"[图片]", "[动画表情]"}:
        return messages, None
    reply_text = str(existing_reply or "").strip()
    if reply_text and _texts_match(reply_text, preview_text):
        return messages, None
    latest = watcher.latest_user_message(messages)
    if latest is None or _message_text(latest).strip() != "[图片]":
        return messages, None
    if any(_texts_match(_message_text(message), preview_text) for message in messages):
        return messages, None
    appended = {
        "role": "用户",
        "role_confidence": "unread_preview_after_image",
        "content": preview_text,
        "text": preview_text,
        "source": "unread-preview",
    }
    return [*messages, appended], appended


def _last_visible_text_matches(messages: list[dict], expected: str | None) -> bool:
    expected_text = str(expected or "").strip()
    if not expected_text:
        return False
    for message in reversed(messages):
        text = _message_text(message).strip()
        if text:
            return _texts_match(text, expected_text)
    return False


def _reply_visible_enough(messages: list[dict], reply_text: str | None) -> bool:
    final_reply = clean_customer_reply_text(reply_text or "")
    if not final_reply:
        return False
    if worker._messages_contain_text(messages, final_reply):
        return True
    expected_key = worker._reply_match_key(final_reply)
    if len(expected_key) < 80:
        return False
    head = expected_key[:80]
    tail = expected_key[-80:]
    for message in reversed(messages):
        actual_key = worker._reply_match_key(_message_text(message))
        if not actual_key:
            continue
        if expected_key in actual_key or actual_key in expected_key:
            return True
        if head in actual_key and tail in actual_key:
            return True
    return False


def _latest_user_turn_texts(messages: list[dict]) -> list[str]:
    texts: list[str] = []
    for message in reversed(messages):
        text = _message_text(message).strip()
        if not text:
            continue
        if message.get("role") == "用户":
            texts.append(text)
            continue
        if texts:
            break
    texts.reverse()
    return texts


def _latest_user_turn_contains(messages: list[dict], expected: str | None) -> bool:
    expected_text = str(expected or "").strip()
    if not expected_text:
        return False
    return any(_texts_match(text, expected_text) for text in _latest_user_turn_texts(messages))


def _welcome_recheck_still_current(messages: list[dict], expected: str | None) -> bool:
    expected_text = str(expected or "").strip()
    if not expected_text:
        return False
    trigger_index: int | None = None
    for index, message in enumerate(messages):
        text = _message_text(message).strip()
        if _is_welcome_added_system_text(text) and _texts_match(text, expected_text):
            trigger_index = index
    if trigger_index is None:
        return False
    for message in messages[trigger_index + 1 :]:
        if message.get("role") == "用户" and not _is_system_notice_message(message):
            return False
    return True


def _reply_already_visible(messages: list[dict], reply_text: str | None) -> bool:
    final_reply = clean_customer_reply_text(reply_text or "")
    return bool(final_reply and _reply_visible_enough(messages, final_reply))


def _latest_non_welcome_customer_message(messages: list[dict]) -> dict | None:
    for message in reversed(messages):
        text = _message_text(message).strip()
        if not text:
            continue
        if welcome.is_new_customer_text(text) or text == "以上是打招呼内容":
            continue
        if message.get("role") == "用户":
            return message
    return None


def _handoff_waiting_same_hash(job: dict, current: dict) -> bool:
    return (
        bool(str(job.get("handoff_type") or "").strip())
        and bool(str(job.get("last_message_hash") or "").strip())
        and str(current.get("hash") or "").strip() == str(job.get("last_message_hash") or "").strip()
        and _reply_already_visible(current.get("messages", []), job.get("reply_text"))
    )


def _keep_handoff_open(job: dict, current: dict, *, reason: str) -> dict:
    reply_visible = _reply_already_visible(current.get("messages", []), job.get("reply_text"))
    final_reason = (
        "handoff_waiting_same_hash"
        if _handoff_waiting_same_hash(job, current) or (reply_visible and reason.startswith("latest_message_not_user:"))
        else reason
    )
    state.mark_handoff_waiting(
        job["id"],
        message_hash=current.get("hash") or job.get("last_message_hash"),
        reply_text=job.get("reply_text"),
        reply_source=str(job.get("reply_source") or "") or "human",
        attachments=job.get("reply_attachments") or [],
    )
    return {
        "ok": True,
        "read": 1,
        "drafting": 0,
        "handoff": 0,
        "conversation": job.get("title") or "",
        "reason": final_reason,
    }


def _read_current_with_retry(
    last: int,
    *,
    attempts: int | None = None,
    delay: float | None = None,
    expected_visible_text: str | None = None,
    existing_reply_text: str | None = None,
    capture_images: bool = False,
) -> dict:
    max_attempts = attempts or int(os.environ.get("WECOM_AGENT_READ_ATTEMPTS", "4"))
    sleep_delay = delay if delay is not None else float(os.environ.get("WECOM_AGENT_READ_RETRY_DELAY", "0.35"))
    current: dict = {"ok": True, "source": "unread", "message_count": 0, "hash": "", "messages": []}
    for index in range(max(1, max_attempts)):
        try:
            current = chat.read_current(
                last=last,
                capture_images=capture_images,
                media_preview=expected_visible_text,
            )
        except TypeError as exc:
            if "media_preview" not in str(exc):
                raise
            current = chat.read_current(last=last, capture_images=capture_images)
        if capture_images:
            messages, appended = _append_preview_to_user_turn(
                current.get("messages", []),
                expected_visible_text,
                existing_reply=existing_reply_text,
            )
            if appended is not None:
                current = {**current, "messages": messages}
        messages = current.get("messages", [])
        latest = watcher.latest_user_message(messages)
        if latest is not None or _last_visible_text_matches(messages, expected_visible_text) or _welcome_trigger_message(messages):
            return current
        if index < max_attempts - 1:
            time.sleep(sleep_delay)
    return current


def _new_message_debounce_seconds() -> float:
    return max(0.0, float(os.environ.get("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "3")))


def _resolve_latest_for_job(title: str, job: dict, current: dict) -> tuple[dict, dict | None, str | None]:
    latest = watcher.latest_user_message(current.get("messages", []))
    if latest is not None and _is_system_notice_message(latest):
        latest = None
    if latest is not None:
        return current, latest, None

    preview_latest = None
    patched_messages = current.get("messages", [])
    if (
        not _is_system_notice_message(current.get("messages", [])[-1] if current.get("messages") else None)
        and not _is_system_notice_message({"text": job.get("preview", ""), "content": job.get("preview", "")})
        and not _preview_is_existing_reply(job, job.get("preview", ""))
    ):
        patched_messages, preview_latest = _latest_from_unread_preview(
            current.get("messages", []),
            job.get("preview", ""),
        )
    if preview_latest is not None:
        current = {**current, "messages": patched_messages}
        state.append_event(
            {
                "type": "agent_preview_role_fallback",
                "conversation": title,
                "preview": job.get("preview", ""),
                "latest": preview_latest,
            }
        )
        _log(
            f"[AI客服] 使用未读预览修正最新客户消息角色：{title}｜"
            f"原角色={preview_latest.get('original_role') or 'unknown'}｜预览={_short(job.get('preview'))}"
        )
        return current, preview_latest, None

    reason = (
        "read_empty_retry_exhausted"
        if not current.get("messages")
        else f"latest_message_not_user:{current['messages'][-1].get('role')}"
    )
    return current, None, reason


def _saved_supplement_context_after_ack(job: dict) -> tuple[dict, dict | None]:
    if str(job.get("error") or "") != "supplement_continue_after_ack":
        return {}, None
    conversation_key = str(job.get("conversation_key") or "").strip()
    if not conversation_key:
        return {}, None
    messages = state.list_conversation_messages(conversation_key=conversation_key, limit=50)
    if not messages:
        return {}, None
    latest = None
    for message in reversed(messages):
        if message.get("role") != "用户":
            continue
        if not str(message.get("content") or message.get("text") or "").strip():
            continue
        latest = message
        break
    if latest is None:
        return {}, None
    saved_hash = str(job.get("last_message_hash") or "").strip()
    if not saved_hash:
        saved_hash = hashlib.sha1(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "source": "saved-supplement-context-after-ack",
        "message_count": len(messages),
        "hash": saved_hash,
        "messages": messages,
        "capture_images": False,
    }, latest


def _debounce_latest_message(
    title: str,
    job: dict,
    current: dict,
    latest: dict,
    *,
    last: int,
) -> tuple[dict, dict | None, str | None]:
    delay = _new_message_debounce_seconds()
    if delay <= 0:
        return current, latest, None

    original_hash = str(current.get("hash") or "")
    original_text = _message_text(latest)
    time.sleep(delay)
    with state.gui_lock():
        inbox.open_row(job)
        settle_delay = float(os.environ.get("WECOM_AGENT_DEBOUNCE_READ_DELAY", "0.15"))
        if settle_delay > 0:
            time.sleep(settle_delay)
        refreshed = _read_current_with_retry(
            last=last,
            expected_visible_text=job.get("preview", ""),
            existing_reply_text=job.get("reply_text"),
            capture_images=True,
        )

    refreshed, refreshed_latest, reason = _resolve_latest_for_job(title, job, refreshed)
    if refreshed_latest is None:
        if not refreshed.get("messages"):
            state.append_event(
                {
                    "type": "agent_debounce_empty_recheck_kept_initial",
                    "conversation": title,
                    "old_hash": original_hash,
                    "reason": reason,
                }
            )
            _log(f"[AI客服] 新消息复查读空：{title}，保留首次读取；原因={_reason_text(reason or '')}")
            return current, latest, None
        if str(refreshed.get("hash") or "") != original_hash:
            state.append_event(
                {
                    "type": "agent_debounce_changed_unresolved",
                    "conversation": title,
                    "old_hash": original_hash,
                    "new_hash": refreshed.get("hash"),
                    "reason": reason,
                }
            )
            _log(f"[AI客服] 新消息复查发现聊天已变化但未确认客户消息：{title}，暂停旧消息AI；原因={_reason_text(reason or '')}")
            return refreshed, None, "debounce_changed_unresolved"
        _log(f"[AI客服] 新消息复查未取到客户消息：{title}，保留首次读取；原因={_reason_text(reason or '')}")
        return current, latest, None

    refreshed_hash = str(refreshed.get("hash") or "")
    refreshed_text = _message_text(refreshed_latest)
    if refreshed_hash != original_hash or not _texts_match(refreshed_text, original_text):
        state.append_event(
            {
                "type": "agent_debounce_replaced_message",
                "conversation": title,
                "old_hash": original_hash,
                "new_hash": refreshed_hash,
                "old_latest": original_text,
                "new_latest": refreshed_text,
            }
        )
        _log(
            f"[AI客服] 新消息复查：{title}，发现更新，改用最新消息｜"
            f"旧={_short(original_text)}｜新={_short(refreshed_text)}"
        )
        return refreshed, refreshed_latest, None

    return current, latest, None


def _reason_text(reason: str) -> str:
    if reason == "read_empty_retry_exhausted":
        return "聊天区读取为空，重试后仍没有消息"
    if reason.startswith("latest_message_not_user:"):
        role = reason.split(":", 1)[1] or "未知"
        return f"最新一条不是客户消息，角色={role}"
    if reason == "stale_context":
        return "客户又发了新消息，旧回复作废"
    if reason == "queue_empty":
        return "当前没有待处理会话"
    return reason


def _log_read_context(title: str, job: dict, current: dict) -> None:
    messages = current.get("messages", [])
    media_count = sum(len(message.get("media") or []) for message in messages if isinstance(message, dict))
    try:
        log_limit = int(os.environ.get("WECOM_AGENT_READ_CONTEXT_LOG_LIMIT", "220") or "220")
    except ValueError:
        log_limit = 220
    state.append_event(
        {
            "type": "agent_read_context",
            "job_id": job.get("id"),
            "conversation": title,
            "source": current.get("source"),
            "hash": current.get("hash"),
            "message_count": len(messages),
            "media_count": media_count,
            "capture_images": current.get("capture_images"),
            "preview": job.get("preview", ""),
            "messages": messages,
        }
    )
    _log(
        f"[AI客服] 读取明细：{title}，hash={_short(str(current.get('hash') or ''), 16)}，"
        f"消息数={len(messages)}，媒体数={media_count}｜"
        f"{_short(json.dumps(messages, ensure_ascii=False), max(80, log_limit))}"
    )


def _selected_conversation_after_open(limit: int = 30) -> dict | None:
    try:
        return macos_backend.selected_conversation_row(limit=limit)
    except TypeError:
        return macos_backend.selected_conversation_row()
    except Exception as exc:
        state.append_event({"type": "agent_selected_conversation_check_failed", "error": str(exc)})
        _log(f"[AI客服] 当前选中会话校验失败：{exc}")
        return None


def _conversation_title_matches(expected: str | None, actual: str | None) -> bool:
    left = str(expected or "").strip()
    right = str(actual or "").strip()
    return bool(left and right and (left == right or left.startswith(right) or right.startswith(left)))


def _ensure_opened_conversation_matches(job: dict, *, stage: str) -> bool:
    expected = str(job.get("title") or "").strip()
    if not expected:
        return True
    selected = _selected_conversation_after_open()
    if not selected:
        state.append_event(
            {
                "type": "agent_selected_conversation_unavailable",
                "job_id": job.get("id"),
                "conversation": expected,
                "stage": stage,
            }
        )
        _log(f"[AI客服] 打开后未能确认当前选中会话，继续读取：{expected}")
        return True
    actual = str(selected.get("title") or "").strip()
    if _conversation_title_matches(expected, actual):
        state.append_event(
            {
                "type": "agent_selected_conversation_confirmed",
                "job_id": job.get("id"),
                "conversation": expected,
                "selected": actual,
                "stage": stage,
            }
        )
        _log(f"[AI客服] 已确认打开目标会话：{expected}")
        return True
    state.append_event(
        {
            "type": "agent_selected_conversation_mismatch",
            "job_id": job.get("id"),
            "conversation": expected,
            "selected": actual,
            "stage": stage,
            "selected_preview": selected.get("preview", ""),
        }
    )
    _log(f"[AI客服] 打开会话不一致，暂停读取：目标={expected}，当前={actual or '未知'}")
    return False


def _ensure_chat_input_ready_for_job(job: dict, *, stage: str) -> dict:
    title = str(job.get("title") or "").strip()
    result = macos_backend.ensure_input_ready()
    ok = bool(result.get("ok"))
    state.append_event(
        {
            "type": "agent_input_ready",
            "job_id": job.get("id"),
            "conversation": title,
            "stage": stage,
            "ok": ok,
            "error": result.get("error") or "",
            "sidebar": result.get("sidebar") if isinstance(result.get("sidebar"), dict) else {},
            "input": result.get("input") if isinstance(result.get("input"), dict) else {},
        }
    )
    _log(
        f"[AI客服] 输入框/侧边栏检查：{title or '当前会话'}，"
        f"阶段={stage}，结果={'ok' if ok else result.get('error') or '失败'}"
    )
    if not ok:
        raise RuntimeError(f"chat input not ready: {result}")
    return result


def _mode_text(mode: str) -> str:
    if mode == "auto":
        return "真实发送"
    if mode == "review":
        return "网页审核后发送"
    return "演练模式"


def _format_counts(counts: dict[str, int]) -> str:
    labels = {
        "pending": "待读取",
        "processing": "处理中",
        "reading": "读取中",
        "drafting": "AI处理中",
        "ready": "待审核",
        "approved": "已审核待发送",
        "sending": "发送中",
        "done": "已完成",
        "failed": "失败",
        "skipped": "已跳过",
    }
    parts = [f"{labels.get(status, status)}={count}" for status, count in sorted(counts.items()) if count]
    return "、".join(parts) if parts else "队列为空"


def _required_tag_text() -> str:
    return (
        os.environ.get("WECOM_GUI_REQUIRED_TAGS", "").strip()
        or os.environ.get("WECOM_GUI_REQUIRED_TAG", "@微信").strip()
        or "@微信"
    )


def _log_scan(scan: dict) -> None:
    visible = scan.get("visible", scan.get("scanned", 0))
    pages = int(scan.get("pages_scanned") or 1)
    unread = scan.get("unread", 0)
    enqueued = scan.get("enqueued", 0)
    ignored = scan.get("ignored", 0)
    cached = scan.get("ignored_cached_preview", 0)
    welcome_count = len(scan.get("welcome_items", []) or [])
    bounded = bool(scan.get("bounded_scan"))
    log_empty = str(os.environ.get("WECOM_AGENT_LOG_EMPTY_SCANS", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not unread and not enqueued and not welcome_count and not cached and not bounded and not log_empty:
        return

    _log(
        "[AI客服] 扫描左侧会话："
        f"模式={'有界单聊' if bounded else '旧链路'}，"
        f"{_required_tag_text()}={visible}，页数={pages}，未读={unread}，"
        f"新客户={welcome_count}，新入队={enqueued}，缓存跳过={cached}，忽略={ignored}"
    )
    for row in scan.get("unread_items", [])[:5]:
        unread_count = int(row.get("unread_count") or 0)
        badge = f"未读{unread_count}" if unread_count else "未读"
        _log(f"[AI客服] 检测到未读消息：{row.get('title', '')}（{badge}）预览：{_short(row.get('preview'))}")
    for item in scan.get("items", []):
        _log(f"[AI客服] 已加入处理队列：{item.get('title', '')}")


def _log_current_chat(result: dict) -> None:
    if not result.get("enqueued"):
        reason = str(result.get("reason") or "").strip()
        quiet_reasons = {
            "selected_conversation_not_found",
            "same_current_message",
            "latest_message_not_user",
            "selected_conversation_not_customer",
            "selected_conversation_low_role_confidence",
        }
        if reason in quiet_reasons:
            return
        if reason and reason != "not_run":
            _log(f"[AI客服] 当前会话未入队：原因={reason}")
        return
    _log(
        "[AI客服] 当前会话检测到客户新消息："
        f"{result.get('conversation', '')}｜{_short(result.get('latest'))}"
    )


def _log_heartbeat(
    *,
    futures: dict[int, Future],
    last_scan: dict | None,
    scanned: int,
    read: int,
    ready: int,
    sent: int,
) -> None:
    counts = state.queue_counts()
    scan_part = "尚未扫描"
    if last_scan is not None:
        pages = int(last_scan.get("pages_scanned") or 1)
        scan_part = (
            f"最近扫描 {_required_tag_text()}={last_scan.get('visible', last_scan.get('scanned', 0))}，"
            f"页数={pages}，"
            f"未读={last_scan.get('unread', 0)}，新入队={last_scan.get('enqueued', 0)}，"
            f"无未读忽略={last_scan.get('ignored_no_unread', 0)}，重复忽略={last_scan.get('ignored_existing', 0)}"
        )
    _log(
        "[AI客服] 状态："
        f"AI请求中={len(futures)}；{_format_counts(counts)}；"
        f"{scan_part}；累计入队={scanned}，已读取={read}，AI完成={ready}，已发送={sent}"
    )


def _normalize_wecom_window() -> None:
    if os.environ.get("WECOM_GUI_NORMALIZE_FULLSCREEN", "0") != "1":
        geometry = _window_geometry_with_retry()
        if geometry.get("ok"):
            _log(
                "[AI客服] 企业微信窗口："
                f"fullscreen=unknown frame={geometry.get('window')} screen={geometry.get('screen')}"
            )
            _log(
                "[AI客服] 左侧栏定位："
                f"source={geometry.get('source')} frame={geometry.get('sidebar')} "
                f"scroll_point={geometry.get('scrollPoint')}"
            )
        return

    result: dict = {"ok": False, "fullscreen": False, "fallbackMaximized": False, "error": "ax_normalize_failed"}
    geometry: dict = {"ok": False, "source": "unavailable"}
    attempts = max(1, int(os.environ.get("WECOM_GUI_GEOMETRY_ATTEMPTS", "4")))
    delay = float(os.environ.get("WECOM_GUI_GEOMETRY_RETRY_DELAY", "0.35"))
    for index in range(attempts):
        result = macos_backend.normalize_window()
        geometry = _window_geometry_with_retry(attempts=1)
        if geometry.get("ok"):
            break
        if index < attempts - 1:
            time.sleep(delay)
    window_frame = geometry.get("window") or result.get("window")
    screen_frame = geometry.get("screen") or result.get("screen")
    fullscreen = bool(result.get("fullscreen"))
    fallback = bool(result.get("fallbackMaximized"))
    _log(
        "[AI客服] 企业微信窗口："
        f"fullscreen={str(fullscreen).lower()} fallback_maximized={str(fallback).lower()} "
        f"frame={window_frame} screen={screen_frame}"
    )
    if geometry.get("ok"):
        _log(
            "[AI客服] 左侧栏定位："
            f"source={geometry.get('source')} frame={geometry.get('sidebar')} "
            f"scroll_point={geometry.get('scrollPoint')}"
        )
    elif result.get("error"):
        _log(f"[AI客服] 左侧栏定位失败：{result.get('error')}")


def _window_geometry_with_retry(*, attempts: int | None = None, delay: float | None = None) -> dict:
    max_attempts = attempts or int(os.environ.get("WECOM_GUI_GEOMETRY_ATTEMPTS", "4"))
    sleep_delay = delay if delay is not None else float(os.environ.get("WECOM_GUI_GEOMETRY_RETRY_DELAY", "0.35"))
    geometry: dict = {"ok": False, "source": "unavailable"}
    for index in range(max(1, max_attempts)):
        geometry = macos_backend.window_geometry()
        if geometry.get("ok"):
            return geometry
        if index < max_attempts - 1:
            time.sleep(sleep_delay)
    return geometry


def _draft(messages: list[dict]) -> dict:
    return llm.draft_reply(messages)


def _draft_for_customer(
    messages: list[dict],
    customer_name: str,
    customer_uid: str = "",
    *,
    agent_mode: str = "",
    agent_context: dict | None = None,
) -> dict:
    return llm.draft_reply(
        messages,
        provider="pi" if agent_mode == SUPPLEMENT_REPLY_SOURCE else None,
        customer_name=customer_name,
        customer_uid=customer_uid,
        agent_mode=agent_mode,
        agent_context=agent_context,
    )


def _mark_welcome_ready(job: dict, current: dict, *, system_text: str) -> dict:
    message_hash = str(current.get("hash") or "").strip() or welcome.message_hash(
        str(job.get("conversation_key") or ""),
        system_text,
    )
    message = {
        "role": "system",
        "content": system_text,
        "text": system_text,
        "source": "wecom-new-customer-system",
        "role_confidence": "system",
    }
    messages = current.get("messages", []) or [message]
    latest = message
    state.mark_drafting(
        job["id"],
        message_hash=message_hash,
        messages=messages,
        latest=latest,
    )
    state.mark_ready(
        job["id"],
        reply_text=welcome.welcome_message(),
        reply_source="welcome",
        action="welcome",
    )
    state.append_event(
        {
            "type": "welcome_draft_ready",
            "job_id": job["id"],
            "conversation": job.get("title") or "",
            "conversation_key": job.get("conversation_key") or "",
            "system_text": system_text,
            "reply_source": "welcome",
        }
    )
    _log(f"[AI客服] 新客户欢迎草稿已生成：{job.get('title') or ''}｜{_short(system_text)}")
    return {"ok": True, "read": 1, "drafting": 0, "ready": 1, "conversation": job.get("title") or "", "welcome": 1}


def _bind_visible_uid(title: str) -> str:
    try:
        uid = macos_backend.current_external_user_id()
    except Exception as exc:
        _log(f"[AI客服] 读取侧边栏UID失败：{title}｜{exc}")
        return ""
    if not uid:
        return ""
    binding = state.bind_wecom_customer(
        uid=uid,
        customer_name=title,
        display_name=title,
        source="accessibility-sidebar-debug",
        raw={"uid": uid, "customer_name": title, "source": "accessibility-sidebar-debug"},
    )
    _log(f"[AI客服] 已绑定侧边栏UID：{title}｜{binding['uid']}")
    return str(binding.get("uid") or "")


def _mark_welcome_ready_if_needed(job: dict, current: dict, trigger: dict, *, visible_uid: str) -> dict | None:
    customer_key = _welcome_customer_key(job, visible_uid)
    if not customer_key:
        return None
    existing = state.get_welcome_state(customer_key)
    if existing and str(existing.get("status") or "") in {state.WELCOME_PENDING, state.WELCOME_SENT}:
        reason = str(existing.get("status") or "welcome_duplicate")
        state.mark_skipped(job["id"], reason)
        return {
            "ok": True,
            "read": 1,
            "drafting": 0,
            "welcome": 0,
            "conversation": job.get("title") or "",
            "reason": reason,
        }

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
            "trigger": _message_text(trigger),
        }
    )
    _log(f"[AI客服] 新用户欢迎草稿已生成：{job.get('title') or ''}｜customer_key={customer_key}")
    return {"ok": True, "read": 1, "drafting": 0, "welcome": 1, "conversation": job.get("title") or ""}


def _mark_supplement_first_reply_ready(
    job: dict,
    current: dict,
    latest: dict,
    *,
    customer_key: str,
    trace_id: str,
    has_profile: bool,
    route: dict,
    from_welcome: bool = False,
) -> dict:
    stage = state.SUPPLEMENT_COLLECTING_PROFILE
    latest_text = _message_text(latest)
    first_reply = supplement_first_reply_choices_only() if has_profile else supplement_first_reply_with_profile()
    welcome_text = _supplement_wecom_welcome_text(str(job.get("title") or "")) if from_welcome else ""
    reply_text = _supplement_first_reply_text(
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
    state.mark_supplement_state(
        customer_key,
        stage,
        conversation_key=str(job.get("conversation_key") or ""),
        conversation=str(job.get("title") or ""),
        job_id=job["id"],
        trace_id=trace_id,
        digging_count=0,
        selected_needs=_supplement_selected_needs(latest_text),
        known_profile=_merge_supplement_known_profile({"has_basic_profile": has_profile}, latest_text),
        message_hash=str(current.get("hash") or ""),
        pending_next_stage=state.SUPPLEMENT_DIGGING_NEED,
        reason="first_prompt_ready",
    )
    _log_supplement(
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
    state.append_event(
        {
            "type": "agent_supplement_first_prompt_ready",
            "job_id": job["id"],
            "conversation": job.get("title") or "",
            "customer_key": customer_key,
            "trace_id": trace_id,
            "has_profile": has_profile,
        }
    )
    _log(f"[AI客服] 补剂首轮话术已生成：{job.get('title') or ''}｜customer_key={customer_key}")
    return {"ok": True, "read": 1, "drafting": 0, "supplement": 1, "conversation": job.get("title") or ""}


def _mark_supplement_selection_ack_ready(
    job: dict,
    current: dict,
    latest: dict,
    *,
    customer_key: str,
    trace_id: str,
    state_row: dict,
    selected_needs: list[str],
) -> dict:
    latest_text = _message_text(latest)
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
        known_profile=_merge_supplement_known_profile(state_row.get("known_profile") or {}, latest_text),
        message_hash=str(current.get("hash") or ""),
        pending_next_stage=state.SUPPLEMENT_DIGGING_NEED,
        reason="need_selection_ack",
    )
    _log_supplement(
        "supplement_need_parsed",
        job=job,
        customer_key=customer_key,
        trace_id=trace_id,
        stage=state.SUPPLEMENT_DIGGING_NEED,
        message_hash=str(current.get("hash") or ""),
        latest_text=latest_text,
        details={
            "need_numbers": _selected_supplement_need_numbers(latest_text),
            "need_texts": selected_needs,
            "profile_fields_detected": ["basic_profile"] if _has_supplement_profile(latest_text) else [],
            "profile_opt_out": _declines_supplement_profile(latest_text),
        },
    )
    state.mark_ready(
        job["id"],
        reply_text=supplement_selection_ack(),
        reply_source=SUPPLEMENT_REPLY_SOURCE,
        action="clarify",
    )
    _log(f"[AI客服] 补剂需求选择已确认：{job.get('title') or ''}｜{selected_needs or _short(latest_text)}")
    return {"ok": True, "read": 1, "drafting": 0, "supplement": 1, "conversation": job.get("title") or ""}


def _read_one_pending(
    *,
    last: int,
    executor: ThreadPoolExecutor,
    futures: dict[int, Future],
    max_drafts: int,
    read_only: bool = False,
) -> dict:
    job = state.claim_pending_for_read()
    if job is None:
        return {"ok": True, "read": 0, "reason": "queue_empty"}

    title = job["title"]
    visible_uid = ""
    try:
        _log(f"[AI客服] 打开会话：{title}，读取最近 {last} 条聊天记录")
        with state.gui_lock():
            inbox.open_row(job)
            time.sleep(float(os.environ.get("WECOM_AGENT_OPEN_READ_DELAY", "0.45")))
            if not _ensure_opened_conversation_matches(job, stage="read"):
                state.mark_pending(job["id"], "opened_conversation_mismatch", preserve_context=True)
                return {
                    "ok": True,
                    "read": 0,
                    "retry": 1,
                    "conversation": title,
                    "reason": "opened_conversation_mismatch",
                }
            _ensure_chat_input_ready_for_job(job, stage="read")
            current = _read_current_with_retry(
                last=last,
                expected_visible_text=job.get("preview", ""),
                existing_reply_text=job.get("reply_text"),
                capture_images=True,
            )
            visible_uid = _bind_visible_uid(title)
            if visible_uid:
                upgraded = state.upgrade_job_conversation_key_to_uid(job["id"], visible_uid)
                if upgraded:
                    if upgraded["id"] != job["id"]:
                        _log(f"[AI客服] 合并同一UID会话：{title}｜{job['id']} -> {upgraded['id']}")
                    job = upgraded
        _log(
            f"[AI客服] 聊天读取结果：{title}，source={current.get('source')}，"
            f"消息数={len(current.get('messages', []))}"
        )
        _log_read_context(title, job, current)
        if read_only or _read_only_enabled():
            state.mark_read_logged(
                job["id"],
                message_hash=str(current.get("hash") or ""),
                messages=current.get("messages", []),
                reason="read_only",
            )
            state.append_event(
                {
                    "type": "agent_read_only_completed",
                    "job_id": job["id"],
                    "conversation": title,
                    "hash": current.get("hash"),
                    "message_count": len(current.get("messages", [])),
                }
            )
            _log(f"[AI客服] 只读模式：已记录读取结果，不调用AI：{title}")
            return {"ok": True, "read": 1, "drafting": 0, "read_only": True, "conversation": title}

        if _handoff_waiting_same_hash(job, current):
            state.mark_handoff_waiting(
                job["id"],
                message_hash=current.get("hash"),
                reply_text=job.get("reply_text"),
                reply_source=str(job.get("reply_source") or "") or "human",
                attachments=job.get("reply_attachments") or [],
            )
            _log(f"[AI客服] 转人工会话无新增聊天内容，继续等待：{title}")
            return {
                "ok": True,
                "read": 1,
                "drafting": 0,
                "handoff": 0,
                "conversation": title,
                "reason": "handoff_waiting_same_hash",
            }

        saved_current, saved_latest = _saved_supplement_context_after_ack(job)
        if saved_current and saved_latest is not None:
            current = saved_current
            latest = saved_latest
            reason = None
            _log(f"[AI客服] 补剂稍等后继续调用AI：{title}｜复用客户上下文")
        else:
            current, latest, reason = _resolve_latest_for_job(title, job, current)
        if latest is None:
            welcome_trigger = _welcome_trigger_message(current.get("messages", []))
            if (
                welcome_trigger is not None
                and not str(job.get("handoff_type") or "").strip()
                and _welcome_recheck_still_current(current.get("messages", []), _message_text(welcome_trigger))
            ):
                customer_key = _supplement_customer_key(job, visible_uid)
                if customer_key:
                    existing_supplement = state.get_supplement_state(customer_key)
                    if existing_supplement and str(existing_supplement.get("stage") or "") in state.SUPPLEMENT_ACTIVE_STAGES | {state.SUPPLEMENT_RECOMMENDED}:
                        state.mark_skipped(job["id"], "supplement_welcome_duplicate")
                        _log(f"[AI客服] 跳过重复新用户补剂话术：{title}｜customer_key={customer_key}")
                        return {
                            "ok": True,
                            "read": 1,
                            "drafting": 0,
                            "supplement": 0,
                            "conversation": title,
                            "reason": "supplement_welcome_duplicate",
                        }
                    trace_id = _new_trace_id(job["id"], customer_key)
                    return _mark_supplement_first_reply_ready(
                        job,
                        current,
                        welcome_trigger,
                        customer_key=customer_key,
                        trace_id=trace_id,
                        has_profile=False,
                        route={
                            "triggered": True,
                            "matched_terms": [],
                            "active_state_exists": False,
                            "detected_intent": "welcome_followup",
                        },
                        from_welcome=True,
                    )
                welcome_result = _mark_welcome_ready_if_needed(job, current, welcome_trigger, visible_uid=visible_uid)
                if welcome_result is not None:
                    return welcome_result
            if str(job.get("handoff_type") or "").strip():
                _log(f"[AI客服] 转人工会话未读到新的客户消息，继续人工接管：{title}｜原因={_reason_text(reason or '')}")
                return _keep_handoff_open(job, current, reason=reason or "handoff_no_customer_message")
            reason = reason or "latest_message_not_user:unknown"
            if reason == "read_empty_retry_exhausted":
                state.mark_pending(job["id"], reason)
                state.append_event(
                    {
                        "type": "agent_read_empty_retry",
                        "conversation": title,
                        "reason": reason,
                        "preview": job.get("preview", ""),
                    }
                )
                _log(
                    f"[AI客服] 暂缓会话：{title}，原因：{_reason_text(reason)}；"
                    f"读取消息数={len(current.get('messages', []))}，未读预览={_short(job.get('preview'))}"
                )
                return {"ok": True, "read": 1, "drafting": 0, "conversation": title, "reason": reason}
            state.mark_skipped(job["id"], reason)
            state.append_event(
                {
                    "type": "agent_skipped",
                    "conversation": title,
                    "reason": reason,
                    "message_count": len(current.get("messages", [])),
                    "preview": job.get("preview", ""),
                }
            )
            _log(
                f"[AI客服] 跳过会话：{title}，原因：{_reason_text(reason)}；"
                f"读取消息数={len(current.get('messages', []))}，未读预览={_short(job.get('preview'))}"
            )
            return {"ok": True, "read": 1, "drafting": 0, "conversation": title, "reason": reason}

        current, latest, debounce_reason = _debounce_latest_message(title, job, current, latest, last=last)
        if latest is None:
            reason = debounce_reason or "debounce_latest_missing"
            if str(job.get("handoff_type") or "").strip():
                _log(f"[AI客服] 转人工会话复查后无新客户消息，继续人工接管：{title}")
                return _keep_handoff_open(job, current, reason=reason)
            state.mark_pending(job["id"], reason)
            return {"ok": True, "read": 1, "drafting": 0, "conversation": title, "reason": reason}

        latest_turn_text = llm.latest_user_turn_text(current["messages"]) or _message_text(latest)
        if str(job.get("handoff_type") or "").strip():
            reason = str(job.get("handoff_reason") or "人工接管会话收到客户新消息").strip()
            state.mark_drafting(
                job["id"],
                message_hash=current["hash"],
                messages=current["messages"],
                latest=latest,
            )
            state.mark_handoff_attention(job["id"])
            state.append_event(
                {
                    "type": "agent_handoff_attention",
                    "job_id": job["id"],
                    "conversation": title,
                    "latest": latest_turn_text,
                }
            )
            _log(f"[AI客服] 转人工会话有新消息：{title}｜{_short(latest_turn_text)}")
            return {"ok": True, "read": 1, "drafting": 0, "handoff": 1, "conversation": title}
        handoff_route = handoff.classify_handoff(latest_turn_text)
        if handoff_route.get("type"):
            handoff_type = str(handoff_route.get("type") or "indirect")
            reason = str(handoff_route.get("reason") or "需要人工处理")
            state.mark_drafting(
                job["id"],
                message_hash=current["hash"],
                messages=current["messages"],
                latest=latest,
            )
            state.mark_handoff_pending(
                job["id"],
                handoff_type=handoff_type,
                handoff_reason=reason,
                reply_text="",
            )
            state.append_event(
                {
                    "type": "agent_handoff_pre_ai",
                    "job_id": job["id"],
                    "conversation": title,
                    "handoff_type": handoff_type,
                    "reason": reason,
                    "latest": latest_turn_text,
                }
            )
            _log(f"[AI客服] 前置转人工：{title}｜类型={handoff_type}｜原因={reason}｜{_short(latest_turn_text)}")
            return {"ok": True, "read": 1, "drafting": 0, "handoff": 1, "conversation": title, "reason": reason}

        customer_key = _supplement_customer_key(job, visible_uid)
        supplement_state = state.get_supplement_state(customer_key) if customer_key else None
        supplement_route = _supplement_route(latest_turn_text, active_state=supplement_state)
        if supplement_route.get("triggered") and customer_key:
            trace_id = str((supplement_state or {}).get("trace_id") or "") or _new_trace_id(job["id"], customer_key)
            active_stage = str((supplement_state or {}).get("stage") or "")
            selected_needs = _supplement_selected_needs(latest_turn_text)
            if not supplement_route.get("active_state_exists"):
                return _mark_supplement_first_reply_ready(
                    job,
                    current,
                    latest,
                    customer_key=customer_key,
                    trace_id=trace_id,
                    has_profile=_has_supplement_profile(latest_turn_text),
                    route=supplement_route,
                )
            if (
                active_stage == state.SUPPLEMENT_COLLECTING_PROFILE
                and _is_supplement_need_selection(latest_turn_text)
                and str((supplement_state or {}).get("reason") or "") != "need_selection_ack"
            ):
                return _mark_supplement_selection_ack_ready(
                    job,
                    current,
                    latest,
                    customer_key=customer_key,
                    trace_id=trace_id,
                    state_row=supplement_state or {},
                    selected_needs=selected_needs,
                )

        pool = _draft_pool_for_messages(current["messages"])
        if not _has_pool_capacity(pool, futures, max_drafts):
            state.mark_pending(job["id"], f"{pool}_pool_full")
            state.append_event(
                {
                    "type": "agent_pool_wait",
                    "conversation": title,
                    "pool": pool,
                    "pool_counts": _pool_counts(futures),
                }
            )
            return {"ok": True, "read": 1, "drafting": 0, "conversation": title, "reason": f"{pool}_pool_full"}
        agent_mode = ""
        agent_context: dict | None = None
        if supplement_route.get("triggered") and customer_key:
            trace_id = str((supplement_state or {}).get("trace_id") or "") or _new_trace_id(job["id"], customer_key)
            current_stage = str((supplement_state or {}).get("stage") or "") or state.SUPPLEMENT_DIGGING_NEED
            digging_count = min(2, int((supplement_state or {}).get("digging_count") or 0))
            known_profile = _merge_supplement_known_profile((supplement_state or {}).get("known_profile") or {}, latest_turn_text)
            stage_for_drafting = (
                state.SUPPLEMENT_READY_TO_RECOMMEND
                if current_stage == state.SUPPLEMENT_READY_TO_RECOMMEND or digging_count >= 2
                else state.SUPPLEMENT_DIGGING_NEED
            )
            pending_next_stage = (
                state.SUPPLEMENT_RECOMMENDED
                if stage_for_drafting == state.SUPPLEMENT_READY_TO_RECOMMEND
                else state.SUPPLEMENT_DIGGING_NEED
            )
            agent_mode = SUPPLEMENT_REPLY_SOURCE
            agent_context = _supplement_agent_context(
                state_row={**(supplement_state or {}), "digging_count": digging_count, "known_profile": known_profile},
                route=supplement_route,
                customer_key=customer_key,
                trace_id=trace_id,
                stage=stage_for_drafting,
            )
            state.mark_supplement_state(
                customer_key,
                stage_for_drafting,
                conversation_key=str(job.get("conversation_key") or ""),
                conversation=str(job.get("title") or ""),
                job_id=job["id"],
                trace_id=trace_id,
                digging_count=digging_count,
                selected_needs=selected_needs or (supplement_state or {}).get("selected_needs") or [],
                known_profile=known_profile,
                message_hash=str(current.get("hash") or ""),
                pending_next_stage=pending_next_stage,
                reason="supplement_agent_drafting",
            )
            _log_supplement(
                "supplement_state_loaded",
                job=job,
                customer_key=customer_key,
                trace_id=trace_id,
                stage=stage_for_drafting,
                message_hash=str(current.get("hash") or ""),
                latest_text=latest_turn_text,
                details={
                    "current_stage": stage_for_drafting,
                    "digging_count": digging_count,
                    "selected_needs": selected_needs or (supplement_state or {}).get("selected_needs") or [],
                    "profile_opt_out": bool(known_profile.get("profile_opt_out")),
                },
            )
        state.mark_drafting(
            job["id"],
            message_hash=current["hash"],
            messages=current["messages"],
            latest=latest,
            extra_context={
                "agent_mode": agent_mode,
                "agent_context": agent_context or {},
                "supplement_trace_id": (agent_context or {}).get("trace_id", ""),
                "supplement_customer_key": customer_key if agent_mode == SUPPLEMENT_REPLY_SOURCE else "",
            }
            if agent_mode == SUPPLEMENT_REPLY_SOURCE
            else None,
        )
        customer_uid = visible_uid
        if not customer_uid:
            binding = state.lookup_wecom_customer(customer_name=title) or {}
            customer_uid = str(binding.get("uid") or "").strip()
        _DRAFT_STARTED_AT[job["id"]] = time.perf_counter()
        _DRAFT_POOL[job["id"]] = pool
        _DRAFT_MESSAGE_HASH[job["id"]] = current["hash"]
        if agent_mode == SUPPLEMENT_REPLY_SOURCE:
            _DRAFT_AGENT_MODE[job["id"]] = SUPPLEMENT_REPLY_SOURCE
            _DRAFT_TRACE_ID[job["id"]] = str((agent_context or {}).get("trace_id") or "")
            _DRAFT_CUSTOMER_KEY[job["id"]] = customer_key
        futures[job["id"]] = executor.submit(
            _draft_for_customer,
            current["messages"],
            title,
            customer_uid,
            agent_mode=agent_mode,
            agent_context=agent_context,
        )
        state.append_event(
            {
                "type": "agent_drafting",
                "conversation": title,
                "hash": current["hash"],
                "latest": latest,
                "pool": pool,
                "agent_mode": agent_mode,
                "image_paths": _latest_user_turn_image_paths(current["messages"]),
            }
        )
        _log(f"[AI客服] 最新客户消息：{title}｜{_short(_message_text(latest))}")
        uid_text = customer_uid if customer_uid else "未绑定"
        mode_text = f"，Agent={agent_mode}" if agent_mode else ""
        _log(f"[AI客服] 开始调用AI回复：{title}，UID={uid_text}，池={pool}{mode_text}，历史消息数={len(current['messages'])}")
        return {"ok": True, "read": 1, "drafting": 1, "conversation": title}
    except Exception as exc:
        state.mark_failed(job["id"], str(exc))
        state.append_event({"type": "agent_read_failed", "conversation": title, "error": str(exc)})
        _log(f"[AI客服] 读取失败：{title}，错误：{exc}")
        return {"ok": False, "read": 1, "failed": 1, "conversation": title, "error": str(exc)}


def _finish_drafts(futures: dict[int, Future]) -> dict:
    ready = 0
    failed = 0
    for job_id, future in list(futures.items()):
        if not future.done():
            continue
        futures.pop(job_id, None)
        pool = _DRAFT_POOL.pop(job_id, "text")
        agent_mode = _DRAFT_AGENT_MODE.pop(job_id, "")
        trace_id = _DRAFT_TRACE_ID.pop(job_id, "")
        customer_key = _DRAFT_CUSTOMER_KEY.pop(job_id, "")
        expected_hash = _DRAFT_MESSAGE_HASH.pop(job_id, "")
        job = state.get_job(job_id)
        title = (job or {}).get("title") or f"job#{job_id}"
        started_at = _DRAFT_STARTED_AT.pop(job_id, None)
        elapsed_ms = round((time.perf_counter() - started_at) * 1000) if started_at is not None else None
        elapsed_text = (
            f"耗时={elapsed_ms}ms/{elapsed_ms / 1000:.2f}s"
            if elapsed_ms is not None
            else "耗时=unknown"
        )
        try:
            draft = future.result()
            current_job = state.get_job(job_id)
            if (
                not current_job
                or current_job.get("status") != "drafting"
                or str(current_job.get("last_message_hash") or "") != expected_hash
            ):
                state.append_event(
                    {
                        "type": "agent_stale_draft_discarded",
                        "job_id": job_id,
                        "conversation": title,
                        "expected_hash": expected_hash,
                        "actual_hash": (current_job or {}).get("last_message_hash"),
                        "actual_status": (current_job or {}).get("status"),
                    }
                )
                _log(f"[AI客服] AI回复已丢弃：{title}，原因=旧消息已被新消息覆盖，{elapsed_text}，池={pool}")
                if agent_mode == SUPPLEMENT_REPLY_SOURCE and job and customer_key:
                    _log_supplement(
                        "supplement_skipped",
                        job=job,
                        customer_key=customer_key,
                        trace_id=trace_id,
                        stage=state.SUPPLEMENT_SKIPPED,
                        message_hash=expected_hash,
                        details={"reason": "stale_draft_discarded"},
                    )
                continue
            if draft.get("action") == "handoff":
                raw = draft.get("raw") if isinstance(draft.get("raw"), dict) else {}
                codex = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
                reply_body = codex.get("reply") if isinstance(codex.get("reply"), dict) else {}
                handoff_result = codex.get("handoff") if isinstance(codex.get("handoff"), dict) else {}
                reason = str(
                    reply_body.get("decision_basis")
                    or handoff_result.get("reason")
                    or handoff_result.get("error")
                    or draft.get("text")
                    or "AI 判断需要人工处理"
                ).strip()
                state.mark_handoff_pending(
                    job_id,
                    handoff_type="indirect",
                    handoff_reason=reason,
                    reply_text="",
                    duration_ms=elapsed_ms,
                )
            else:
                reply_source = SUPPLEMENT_REPLY_SOURCE if agent_mode == SUPPLEMENT_REPLY_SOURCE else "ai"
                state.mark_ready(
                    job_id,
                    reply_text=draft["text"],
                    reply_source=reply_source,
                    duration_ms=elapsed_ms,
                    action=str(draft.get("action") or ""),
                )
                if agent_mode == SUPPLEMENT_REPLY_SOURCE and job and customer_key:
                    action_value = str(draft.get("action") or "")
                    current_supplement_state = state.get_supplement_state(customer_key) or {}
                    current_digging_count = min(2, int(current_supplement_state.get("digging_count") or 0))
                    next_digging_count = min(2, current_digging_count + 1) if action_value == "clarify" else current_digging_count
                    next_stage = (
                        state.SUPPLEMENT_DIGGING_NEED
                        if action_value == "clarify"
                        else state.SUPPLEMENT_READY_TO_RECOMMEND
                    )
                    pending_next_stage = (
                        (state.SUPPLEMENT_READY_TO_RECOMMEND if next_digging_count >= 2 else state.SUPPLEMENT_DIGGING_NEED)
                        if action_value == "clarify"
                        else state.SUPPLEMENT_RECOMMENDED
                    )
                    state.mark_supplement_state(
                        customer_key,
                        next_stage,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job_id,
                        trace_id=trace_id,
                        digging_count=next_digging_count,
                        message_hash=expected_hash,
                        pending_next_stage=pending_next_stage,
                        reason="supplement_draft_ready",
                    )
                    _log_supplement(
                        "supplement_draft_ready",
                        job=job,
                        customer_key=customer_key,
                        trace_id=trace_id,
                        stage=next_stage,
                        message_hash=expected_hash,
                        details={
                            "action": str(draft.get("action") or ""),
                            "duration_ms": elapsed_ms,
                            "digging_count": next_digging_count,
                            "pending_next_stage": pending_next_stage,
                            "reply_preview": str(draft.get("text") or "")[:160],
                        },
                    )
            state.append_event(
                {
                    "type": "agent_ready",
                    "job_id": job_id,
                    "reply": draft["text"],
                    "action": draft.get("action"),
                    "duration_ms": elapsed_ms,
                    "agent_mode": agent_mode,
                    "handoff": (draft.get("raw") or {}).get("handoff"),
                }
            )
            _log(f"[AI客服] AI回复已生成：{title}｜{elapsed_text}｜池={pool}｜{_short(draft['text'], 140)}")
            ready += 1
        except Exception as exc:
            current_job = state.get_job(job_id)
            if (
                not current_job
                or current_job.get("status") != "drafting"
                or str(current_job.get("last_message_hash") or "") != expected_hash
            ):
                state.append_event(
                    {
                        "type": "agent_stale_draft_error_discarded",
                        "job_id": job_id,
                        "conversation": title,
                        "expected_hash": expected_hash,
                        "actual_hash": (current_job or {}).get("last_message_hash"),
                        "actual_status": (current_job or {}).get("status"),
                        "error": str(exc),
                    }
                )
                _log(f"[AI客服] AI失败结果已丢弃：{title}，原因=旧消息已被新消息覆盖，{elapsed_text}，池={pool}，错误：{exc}")
                if agent_mode == SUPPLEMENT_REPLY_SOURCE and job and customer_key:
                    _log_supplement(
                        "supplement_skipped",
                        job=job,
                        customer_key=customer_key,
                        trace_id=trace_id,
                        stage=state.SUPPLEMENT_SKIPPED,
                        message_hash=expected_hash,
                        details={"reason": "stale_draft_error_discarded", "error": str(exc)},
                    )
                continue
            state.mark_failed(job_id, str(exc))
            state.append_event({"type": "agent_draft_failed", "job_id": job_id, "error": str(exc)})
            if agent_mode == SUPPLEMENT_REPLY_SOURCE and job and customer_key:
                _log_supplement(
                    "supplement_llm_failed",
                    job=job,
                    customer_key=customer_key,
                    trace_id=trace_id,
                    stage=state.SUPPLEMENT_DIGGING_NEED,
                    message_hash=expected_hash,
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                )
            _log(f"[AI客服] AI回复失败：{title}，池={pool}，{elapsed_text}，错误：{exc}")
            failed += 1
    return {"ready": ready, "failed": failed}


def _drop_superseded_drafts(futures: dict[int, Future]) -> dict:
    discarded = 0
    for job_id, future in list(futures.items()):
        expected_hash = _DRAFT_MESSAGE_HASH.get(job_id, "")
        current_job = state.get_job(job_id)
        if (
            current_job
            and current_job.get("status") == "drafting"
            and str(current_job.get("last_message_hash") or "") == expected_hash
        ):
            continue

        futures.pop(job_id, None)
        pool = _DRAFT_POOL.pop(job_id, "text")
        _DRAFT_MESSAGE_HASH.pop(job_id, None)
        _DRAFT_STARTED_AT.pop(job_id, None)
        _DRAFT_AGENT_MODE.pop(job_id, None)
        _DRAFT_TRACE_ID.pop(job_id, None)
        _DRAFT_CUSTOMER_KEY.pop(job_id, None)
        future.cancel()
        discarded += 1
        title = (current_job or {}).get("title") or f"job#{job_id}"
        state.append_event(
            {
                "type": "agent_superseded_draft_untracked",
                "job_id": job_id,
                "conversation": title,
                "expected_hash": expected_hash,
                "actual_hash": (current_job or {}).get("last_message_hash"),
                "actual_status": (current_job or {}).get("status"),
                "pool": pool,
            }
        )
        _log(f"[AI客服] AI旧请求已让位：{title}，原因=新消息覆盖旧消息，池={pool}")
    return {"discarded": discarded}




def _send_one_ready(*, last: int, mode: str) -> dict:
    if mode == "review":
        job = state.claim_approved_to_send()
    else:
        job = state.claim_ready_to_send()
    if job is None:
        return {"ok": True, "sent": 0, "reason": "queue_empty"}

    title = job["title"]
    context = json.loads(job.get("context_json") or "{}")
    expected_latest = context.get("latest") or {}
    expected_latest_text = expected_latest.get("content") or expected_latest.get("text") or ""
    is_welcome_reply = str(job.get("reply_source") or "").strip() == WELCOME_REPLY_SOURCE or _job_is_welcome_pending(job)
    is_supplement_reply = str(job.get("reply_source") or "").strip() == SUPPLEMENT_REPLY_SOURCE
    supplement_from_welcome = is_supplement_reply and bool(context.get("supplement_from_welcome"))
    supplement_trace_id = str(context.get("supplement_trace_id") or "")
    supplement_customer_key = str(context.get("supplement_customer_key") or job.get("conversation_key") or "")
    try:
        with state.gui_lock():
            inbox.open_row(job)
            time.sleep(0.25)
            if not _ensure_opened_conversation_matches(job, stage="send"):
                state.mark_pending(job["id"], "send_opened_conversation_mismatch", preserve_context=True)
                return {
                    "ok": True,
                    "sent": 0,
                    "retry": 1,
                    "conversation": title,
                    "reason": "send_opened_conversation_mismatch",
                }
            _ensure_chat_input_ready_for_job(job, stage="send")
            current = _read_current_with_retry(
                last=last,
                expected_visible_text=expected_latest_text,
                capture_images=False,
            )
            final_reply = clean_customer_reply_text(job["reply_text"])
            is_handoff = bool(str(job.get("handoff_type") or "").strip())
            if is_handoff and str(job.get("error") or "").strip() == state.HANDOFF_WAITING_ERROR:
                state.mark_handoff_waiting(
                    job["id"],
                    message_hash=current.get("hash") or job.get("last_message_hash"),
                    reply_text=final_reply,
                    reply_source=str(job.get("reply_source") or "") or "human",
                    attachments=job.get("reply_attachments") or [],
                )
                _log(f"[AI客服] 转人工会话仍在等待客户新消息，跳过重复发送：{title}")
                return {"ok": True, "sent": 0, "waiting": 1, "conversation": title, "reason": "handoff_waiting"}
            if is_handoff and _reply_already_visible(current.get("messages", []), final_reply):
                state.mark_handoff_waiting(
                    job["id"],
                    message_hash=current.get("hash") or job.get("last_message_hash"),
                    reply_text=final_reply,
                    reply_source=str(job.get("reply_source") or "") or "human",
                    attachments=job.get("reply_attachments") or [],
                )
                state.append_event(
                    {
                        "type": "agent_handoff_duplicate_send_skipped",
                        "job_id": job["id"],
                        "conversation": title,
                        "reply": final_reply,
                    }
                )
                _log(f"[AI客服] 转人工回复已在会话中可见，跳过重复发送：{title}｜{_short(final_reply, 140)}")
                return {
                    "ok": True,
                    "sent": 0,
                    "waiting": 1,
                    "conversation": title,
                    "reason": "handoff_reply_already_visible",
                }
            latest = watcher.latest_user_message(current["messages"])
            latest_text = (latest or {}).get("content") or (latest or {}).get("text") or ""

            if is_welcome_reply:
                customer_latest = _latest_non_welcome_customer_message(current.get("messages", []))
                context_changed = (
                    bool(str(job.get("last_message_hash") or "").strip())
                    and bool(str(current.get("hash") or "").strip())
                    and str(current.get("hash") or "").strip() != str(job.get("last_message_hash") or "").strip()
                )
                if context_changed and customer_latest is not None:
                    reason = "welcome_context_changed"
                    state.mark_skipped(job["id"], reason)
                    state.mark_welcome_status(
                        str(job.get("conversation_key") or ""),
                        state.WELCOME_SKIPPED,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        reason=reason,
                    )
                    state.append_event(
                        {
                            "type": "welcome_send_recheck_stale",
                            "conversation": title,
                            "expected": expected_latest_text,
                            "actual": _message_text(customer_latest),
                        }
                    )
                    _log(
                        f"[AI客服] 跳过欢迎发送：{title}，原因=客户已发新消息；"
                        f"最新消息：{_short(_message_text(customer_latest))}"
                    )
                    return {"ok": True, "sent": 0, "skipped": 1, "conversation": title, "reason": reason}
                _log(f"[AI客服] 发送前复核：{title}，新客户欢迎场景仍有效，准备发送")

            role_misread_but_same_text = (
                latest is None
                and _last_visible_text_matches(current.get("messages", []), expected_latest_text)
            )
            same_turn_contains_expected = _latest_user_turn_contains(
                current.get("messages", []),
                expected_latest_text,
            )
            welcome_context_ok = (
                is_welcome_reply
                and _welcome_recheck_still_current(current.get("messages", []), expected_latest_text)
            )
            supplement_welcome_context_ok = (
                supplement_from_welcome
                and _welcome_recheck_still_current(current.get("messages", []), expected_latest_text)
            )
            if not current.get("messages"):
                reason = "send_recheck_empty_retry"
                if mode == "review":
                    state.mark_approved_retry(job["id"], reply_text=job["reply_text"], reason=reason)
                else:
                    state.mark_ready(
                        job["id"],
                        reply_text=job["reply_text"],
                        reply_source=str(job.get("reply_source") or "") or "ai",
                    )
                state.append_event(
                    {
                        "type": "agent_send_recheck_empty",
                        "conversation": title,
                        "expected": expected_latest_text,
                    }
                )
                _log(f"[AI客服] 发送前复核暂未读到聊天记录：{title}，延后重试发送")
                return {"ok": True, "sent": 0, "retry": 1, "conversation": title, "reason": reason}
            if (
                not is_welcome_reply
                and
                (not latest or latest_text != expected_latest_text)
                and not role_misread_but_same_text
                and not same_turn_contains_expected
                and not welcome_context_ok
                and not supplement_welcome_context_ok
            ):
                reason = "stale_context"
                state.mark_skipped(job["id"], reason)
                if is_welcome_reply:
                    state.mark_welcome_status(
                        str(job.get("conversation_key") or ""),
                        state.WELCOME_SKIPPED,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        reason=reason,
                    )
                if is_supplement_reply and supplement_customer_key:
                    _log_supplement(
                        "supplement_send_recheck_stale",
                        job=job,
                        customer_key=supplement_customer_key,
                        trace_id=supplement_trace_id,
                        stage=state.SUPPLEMENT_SKIPPED,
                        message_hash=str(job.get("last_message_hash") or ""),
                        latest_text=latest_text,
                        details={"expected": expected_latest_text, "actual": latest_text},
                    )
                    state.mark_supplement_state(
                        supplement_customer_key,
                        state.SUPPLEMENT_SKIPPED,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        trace_id=supplement_trace_id,
                        message_hash=str(job.get("last_message_hash") or ""),
                        reason=reason,
                    )
                state.append_event(
                    {
                        "type": "agent_stale",
                        "conversation": title,
                        "expected": expected_latest_text,
                        "actual": latest_text,
                    }
                )
                state.record_metric(
                    "stale_context_skipped",
                    conversation_key=str(job.get("conversation_key") or ""),
                    conversation=title,
                    job_id=job["id"],
                    reply_source=str(job.get("reply_source") or ""),
                    details={"expected": expected_latest_text, "actual": latest_text},
                )
                _log(
                    f"[AI客服] 跳过发送：{title}，原因：{_reason_text(reason)}；"
                    f"最新消息：{_short(latest_text)}"
                )
                return {"ok": True, "sent": 0, "skipped": 1, "conversation": title, "reason": reason}

            if role_misread_but_same_text:
                _log(f"[AI客服] 发送前复核：{title}，最新文本未变化但角色误判，准备发送")
            elif same_turn_contains_expected and latest_text != expected_latest_text:
                _log(f"[AI客服] 发送前复核：{title}，同一轮客户消息仍包含原问题，准备发送")
            elif welcome_context_ok:
                _log(f"[AI客服] 发送前复核：{title}，欢迎系统文案仍在当前会话，准备发送")
            elif supplement_welcome_context_ok:
                _log(f"[AI客服] 发送前复核：{title}，欢迎后补剂首段话术仍在当前会话，准备发送")

            send_parts = _supplement_welcome_send_parts(final_reply, context) if supplement_from_welcome else [final_reply]
            text_send_parts = [part for part in send_parts if clean_customer_reply_text(part)]
            visible_parts = [
                part for part in send_parts if part and _reply_visible_enough(current.get("messages", []), part)
            ]
            if text_send_parts and len(visible_parts) == len(text_send_parts):
                state.mark_done(
                    job["id"],
                    message_hash=current["hash"],
                    reply_text=final_reply,
                    reply_source=str(job.get("reply_source") or "") or None,
                    attachments=job.get("reply_attachments") or [],
                    reply_parts=send_parts,
                )
                state.append_event(
                    {
                        "type": "agent_send_already_visible",
                        "conversation": title,
                        "hash": current["hash"],
                        "reply": final_reply,
                    }
                )
                if is_welcome_reply:
                    state.mark_welcome_status(
                        str(job.get("conversation_key") or ""),
                        state.WELCOME_SENT,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        reason="already_visible",
                    )
                if supplement_from_welcome and supplement_customer_key:
                    state.mark_welcome_status(
                        supplement_customer_key,
                        state.WELCOME_SENT,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        reason="already_visible_with_supplement_first_prompt",
                    )
                if is_supplement_reply and supplement_customer_key:
                    current_state = state.get_supplement_state(supplement_customer_key) or {}
                    stage_after_send = (
                        state.SUPPLEMENT_RECOMMENDED
                        if str(current_state.get("pending_next_stage") or "") == state.SUPPLEMENT_RECOMMENDED
                        else state.SUPPLEMENT_READY_TO_RECOMMEND
                        if str(current_state.get("pending_next_stage") or "") == state.SUPPLEMENT_READY_TO_RECOMMEND
                        else str(current_state.get("stage") or state.SUPPLEMENT_DIGGING_NEED)
                    )
                    state.mark_supplement_state(
                        supplement_customer_key,
                        stage_after_send,
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        trace_id=supplement_trace_id or str(current_state.get("trace_id") or ""),
                        message_hash=current["hash"],
                        reason="already_visible",
                    )
                _log(f"[AI客服] 回复已在会话中可见，记录完成并跳过重复发送：{title}｜{_short(final_reply, 140)}")
                return {"ok": True, "sent": 0, "already_visible": 1, "conversation": title}

            if mode == "dry-run":
                state.mark_done(
                    job["id"],
                    message_hash=current["hash"],
                    reply_text=final_reply,
                    reply_source=str(job.get("reply_source") or "") or None,
                    attachments=job.get("reply_attachments") or [],
                    reply_parts=send_parts,
                )
                _log(f"[AI客服] 演练模式，不发送：{title}｜{_short(final_reply, 140)}")
                return {"ok": True, "sent": 0, "dry_run": True, "conversation": title}

            _log(f"[AI客服] 发送前复核：{title}，最新客户消息未变化，准备发送")
            attachments = job.get("reply_attachments") or []
            for index, part in enumerate(send_parts):
                reply.send_message(
                    part,
                    attachments=attachments if index == len(send_parts) - 1 else [],
                    dry_run=False,
                    submit=True,
                )
                if index < len(send_parts) - 1:
                    time.sleep(float(os.environ.get("WECOM_AGENT_SPLIT_SEND_DELAY", "0.35")))
            time.sleep(0.5)
            after_send = _read_current_with_retry(
                last=last,
                expected_visible_text=send_parts[-1] if send_parts else final_reply,
                capture_images=False,
                attempts=1,
            )
            missing_parts = [part for part in text_send_parts if not _reply_visible_enough(after_send["messages"], part)]
            if missing_parts:
                _log(f"[AI客服] 发送后暂未读到回复，继续复核：{title}")
                after_send = _read_current_with_retry(
                    last=last,
                    expected_visible_text=send_parts[-1] if send_parts else final_reply,
                    capture_images=False,
                    attempts=max(1, int(os.environ.get("WECOM_AGENT_SEND_VERIFY_ATTEMPTS", "2"))),
                    delay=float(os.environ.get("WECOM_AGENT_SEND_VERIFY_DELAY", "0.45")),
                )
                missing_parts = [part for part in text_send_parts if not _reply_visible_enough(after_send["messages"], part)]
                if missing_parts:
                    state.mark_send_verification_failed(
                        job["id"],
                        reply_text=final_reply,
                        reason="sent_reply_not_visible",
                    )
                    state.append_event(
                        {
                            "type": "agent_send_failed",
                            "conversation": title,
                            "error": "sent_reply_not_visible",
                            "missing_parts": missing_parts,
                        }
                    )
                    state.record_metric(
                        "agent_send_failed",
                        conversation_key=str(job.get("conversation_key") or ""),
                        conversation=title,
                        job_id=job["id"],
                        reply_source=str(job.get("reply_source") or ""),
                        details={"error": "sent_reply_not_visible", "missing_parts": missing_parts},
                    )
                    _log(
                        f"[AI客服] 发送后未确认回复可见，已转入人工复核，避免重复粘贴："
                        f"{title}｜缺失={len(missing_parts)}"
                    )
                    return {
                        "ok": False,
                        "sent": 0,
                        "failed": 1,
                        "conversation": title,
                        "error": "sent_reply_not_visible",
                    }
            state.mark_done(
                job["id"],
                message_hash=after_send["hash"],
                reply_text=final_reply,
                reply_source=str(job.get("reply_source") or "") or None,
                attachments=job.get("reply_attachments") or [],
                reply_parts=send_parts,
            )
            if is_welcome_reply:
                state.mark_welcome_status(
                    str(job.get("conversation_key") or ""),
                    state.WELCOME_SENT,
                    conversation_key=str(job.get("conversation_key") or ""),
                    conversation=title,
                    job_id=job["id"],
                    reason="sent",
                )
            if supplement_from_welcome and supplement_customer_key:
                state.mark_welcome_status(
                    supplement_customer_key,
                    state.WELCOME_SENT,
                    conversation_key=str(job.get("conversation_key") or ""),
                    conversation=title,
                    job_id=job["id"],
                    reason="sent_with_supplement_first_prompt",
                )
            if is_supplement_reply and supplement_customer_key:
                current_state = state.get_supplement_state(supplement_customer_key) or {}
                stage_after_send = (
                    state.SUPPLEMENT_RECOMMENDED
                    if str(current_state.get("pending_next_stage") or "") == state.SUPPLEMENT_RECOMMENDED
                    else state.SUPPLEMENT_READY_TO_RECOMMEND
                    if str(current_state.get("pending_next_stage") or "") == state.SUPPLEMENT_READY_TO_RECOMMEND
                    else str(current_state.get("stage") or state.SUPPLEMENT_DIGGING_NEED)
                )
                state.mark_supplement_state(
                    supplement_customer_key,
                    stage_after_send,
                    conversation_key=str(job.get("conversation_key") or ""),
                    conversation=title,
                    job_id=job["id"],
                    trace_id=supplement_trace_id or str(current_state.get("trace_id") or ""),
                    message_hash=after_send["hash"],
                    reason="sent",
                )
                _log_supplement(
                        "supplement_sent",
                        job=job,
                        customer_key=supplement_customer_key,
                        trace_id=supplement_trace_id or str(current_state.get("trace_id") or ""),
                        stage=stage_after_send,
                        message_hash=after_send["hash"],
                        details={"reply_preview": final_reply[:160]},
                    )
                if (
                    final_reply == supplement_selection_ack()
                    and str((state.get_supplement_state(supplement_customer_key) or {}).get("stage") or "")
                    in state.SUPPLEMENT_ACTIVE_STAGES
                ):
                    state.mark_pending(job["id"], "supplement_continue_after_ack", preserve_context=True)
                    _log(f"[AI客服] 补剂稍等话术已发送，继续进入补剂追问：{title}")
            if str(job.get("handoff_type") or "").strip():
                state.mark_handoff_waiting(
                    job["id"],
                    message_hash=after_send["hash"],
                    reply_text=final_reply,
                    reply_source=str(job.get("reply_source") or "") or "human",
                    attachments=job.get("reply_attachments") or [],
                )
            state.append_event(
                {"type": "agent_sent", "conversation": title, "hash": after_send["hash"], "reply": final_reply}
            )
            state.record_metric(
                "agent_sent",
                conversation_key=str(job.get("conversation_key") or ""),
                conversation=title,
                job_id=job["id"],
                reply_source=str(job.get("reply_source") or ""),
                details={"reply_preview": final_reply[:240]},
            )
            _log(f"[AI客服] 已发送给 {title}：{_short(final_reply, 140)}")
            return {"ok": True, "sent": 1, "conversation": title}
    except Exception as exc:
        if str(job.get("handoff_type") or "").strip():
            state.mark_send_verification_failed(job["id"], reply_text=job.get("reply_text") or "", reason=str(exc))
        else:
            state.mark_failed(job["id"], str(exc))
        state.append_event({"type": "agent_send_failed", "conversation": title, "error": str(exc)})
        state.record_metric(
            "agent_send_failed",
            conversation_key=str(job.get("conversation_key") or ""),
            conversation=title,
            job_id=job["id"],
            reply_source=str(job.get("reply_source") or ""),
            details={"error": str(exc)},
        )
        _log(f"[AI客服] 发送失败：{title}，错误：{exc}")
        return {"ok": False, "sent": 0, "failed": 1, "conversation": title, "error": str(exc)}


def agent_loop(
    *,
    poll: float,
    scan_interval: float,
    inbox_limit: int,
    last: int,
    mode: str,
    max_drafts: int,
    scan_pages: int = 1,
    scroll_ticks: int = 6,
    deep_scan_interval: float = 30.0,
    log_interval: float = 5.0,
    once: bool = False,
    read_only: bool = False,
) -> dict:
    """Run the fast AI customer-service loop."""
    if mode not in {"dry-run", "auto", "review"}:
        raise ValueError("mode must be dry-run, auto, or review")

    iterations = 0
    scanned = 0
    read = 0
    ready = 0
    sent = 0
    next_scan_at = 0.0
    next_deep_scan_at = 0.0
    next_log_at = 0.0
    futures: dict[int, Future] = {}
    last_scan: dict | None = None
    read_only_mode = read_only or _read_only_enabled()

    text_workers = _pool_limit("text", max_drafts)
    image_workers = _pool_limit("image", max_drafts)
    _log(
        "[AI客服] 启动："
        f"模式={_mode_text(mode)}，读取模式={'只读不调AI' if read_only_mode else '正常调用AI'}，"
        f"扫描间隔={scan_interval}s，轮询间隔={poll}s，"
        f"左侧每页扫描前{inbox_limit}条，滚动扫描页数={scan_pages}，"
        f"滚动深扫间隔={deep_scan_interval}s，读取最近{last}条聊天记录，"
        f"最多并发AI={max_drafts}，文本池={text_workers}，图片池={image_workers}"
    )
    _normalize_wecom_window()
    with ThreadPoolExecutor(max_workers=max_drafts) as executor:
        while True:
            iterations += 1
            now = time.time()
            state.reset_stale_active(older_than_seconds=_stale_active_seconds())

            if now >= next_scan_at:
                effective_scan_pages = max(1, scan_pages if now >= next_deep_scan_at else 1)
                current_result: dict = {"ok": False, "enqueued": 0, "reason": "not_run"}
                scan = {
                    "ok": False,
                    "visible": 0,
                    "unread": 0,
                    "enqueued": 0,
                    "ignored_no_unread": 0,
                    "ignored_existing": 0,
                    "items": [],
                    "unread_items": [],
                    "pages_scanned": effective_scan_pages,
                }
                try:
                    with state.gui_lock():
                        try:
                            current_result = worker.enqueue_current_chat_if_changed(last=last, inbox_limit=inbox_limit)
                        except Exception as exc:
                            current_result = {"ok": False, "enqueued": 0, "reason": str(exc)}
                            _log(f"[AI客服] 当前会话检测失败：{exc}")
                        if current_result.get("enqueued"):
                            scan["reason"] = "skip_sidebar_scan_current_chat_enqueued"
                            effective_scan_pages = 1
                        else:
                            try:
                                scan = worker.scan_once(
                                    inbox_limit=inbox_limit,
                                    scan_pages=effective_scan_pages,
                                    scroll_ticks=scroll_ticks,
                                )
                            except Exception as exc:
                                scan["error"] = str(exc)
                                _log(f"[AI客服] 左侧会话扫描失败：{exc}")
                except Exception as exc:
                    _log(f"[AI客服] GUI 操作失败，本轮跳过：{exc}")
                if effective_scan_pages > 1:
                    next_deep_scan_at = now + deep_scan_interval
                _log_current_chat(current_result)
                last_scan = scan
                scanned += scan["enqueued"] + current_result.get("enqueued", 0)
                _log_scan(scan)
                next_scan_at = now + scan_interval

            if not read_only_mode:
                _drop_superseded_drafts(futures)
                finished = _finish_drafts(futures)
                ready += finished["ready"]

                send_result = _send_one_ready(last=last, mode=mode)
                sent += send_result.get("sent", 0)

            while len(futures) < max_drafts:
                intake = _read_one_pending(
                    last=last,
                    executor=executor,
                    futures=futures,
                    max_drafts=max_drafts,
                    read_only=read_only_mode,
                )
                read += intake.get("read", 0)
                if intake.get("reason") == "queue_empty" or str(intake.get("reason") or "").endswith("_pool_full"):
                    break

            if now >= next_log_at:
                _log_heartbeat(
                    futures=futures,
                    last_scan=last_scan,
                    scanned=scanned,
                    read=read,
                    ready=ready,
                    sent=sent,
                )
                next_log_at = now + log_interval

            if once:
                break
            time.sleep(poll)

    return {"ok": True, "iterations": iterations, "scanned": scanned, "read": read, "ready": ready, "sent": sent}
