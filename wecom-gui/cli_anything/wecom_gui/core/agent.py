"""Fast AI customer-service agent.

The agent keeps GUI work short and serial while AI calls run concurrently:

1. Scan inbox rows into the queue.
2. Open pending chats briefly, read context, and start AI drafts in threads.
3. Continue scanning/reading while drafts are in flight.
4. Re-open ready chats and send replies.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor

from cli_anything.wecom_gui.core import chat, inbox, llm, reply, state, watcher, worker
from cli_anything.wecom_gui.utils import macos_backend


_DRAFT_STARTED_AT: dict[int, float] = {}
_DRAFT_POOL: dict[int, str] = {}
_DRAFT_MESSAGE_HASH: dict[int, str] = {}


def _message_image_paths(messages: list[dict]) -> list[str]:
    paths: list[str] = []
    for message in messages:
        for media in message.get("media") or []:
            if not isinstance(media, dict):
                continue
            path = str(media.get("capture_path") or "").strip()
            if path and media.get("capture_ok", True):
                paths.append(path)
    return paths


def _message_has_image(message: dict) -> bool:
    for media in message.get("media") or []:
        if isinstance(media, dict) and (media.get("type") or "image") == "image":
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


def _short(text: str | None, limit: int = 90) -> str:
    value = (text or "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _message_text(message: dict | None) -> str:
    if not message:
        return ""
    return str(message.get("content") or message.get("text") or "")


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

    if preview_text == "[图片]":
        for idx in range(len(messages) - 1, -1, -1):
            message = messages[idx]
            if _message_text(message).strip() != "[图片]" and not _message_has_image(message):
                continue
            patched = [dict(item) for item in messages]
            original_role = str(patched[idx].get("role") or "").strip()
            text = _message_text(patched[idx]).strip() or "[图片]"
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
    if not preview_text or preview_text == "[图片]":
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
        if latest is not None or _last_visible_text_matches(messages, expected_visible_text):
            return current
        if index < max_attempts - 1:
            time.sleep(sleep_delay)
    return current


def _new_message_debounce_seconds() -> float:
    return max(0.0, float(os.environ.get("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "3")))


def _resolve_latest_for_job(title: str, job: dict, current: dict) -> tuple[dict, dict | None, str | None]:
    latest = watcher.latest_user_message(current.get("messages", []))
    if latest is not None:
        return current, latest, None

    preview_latest = None
    patched_messages = current.get("messages", [])
    if not _preview_is_existing_reply(job, job.get("preview", "")):
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
    if not unread and not enqueued:
        return

    _log(
        "[AI客服] 扫描左侧会话："
        f"{_required_tag_text()}={visible}，页数={pages}，未读={unread}，新入队={enqueued}，忽略={ignored}"
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


def _draft_for_customer(messages: list[dict], customer_name: str, customer_uid: str = "") -> dict:
    return llm.draft_reply(messages, customer_name=customer_name, customer_uid=customer_uid)


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


def _read_one_pending(*, last: int, executor: ThreadPoolExecutor, futures: dict[int, Future], max_drafts: int) -> dict:
    job = state.claim_pending_for_read()
    if job is None:
        return {"ok": True, "read": 0, "reason": "queue_empty"}

    title = job["title"]
    try:
        _log(f"[AI客服] 打开会话：{title}，读取最近 {last} 条聊天记录")
        with state.gui_lock():
            inbox.open_row(job)
            time.sleep(float(os.environ.get("WECOM_AGENT_OPEN_READ_DELAY", "0.45")))
            current = _read_current_with_retry(
                last=last,
                expected_visible_text=job.get("preview", ""),
                existing_reply_text=job.get("reply_text"),
                capture_images=True,
            )
        _log(
            f"[AI客服] 聊天读取结果：{title}，source={current.get('source')}，"
            f"消息数={len(current.get('messages', []))}"
        )

        current, latest, reason = _resolve_latest_for_job(title, job, current)
        if latest is None:
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
            state.mark_pending(job["id"], reason)
            return {"ok": True, "read": 1, "drafting": 0, "conversation": title, "reason": reason}

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

        state.mark_drafting(
            job["id"],
            message_hash=current["hash"],
            messages=current["messages"],
            latest=latest,
        )
        customer_uid = _bind_visible_uid(title)
        if not customer_uid:
            binding = state.lookup_wecom_customer(customer_name=title) or {}
            customer_uid = str(binding.get("uid") or "").strip()
        _DRAFT_STARTED_AT[job["id"]] = time.perf_counter()
        _DRAFT_POOL[job["id"]] = pool
        _DRAFT_MESSAGE_HASH[job["id"]] = current["hash"]
        futures[job["id"]] = executor.submit(_draft_for_customer, current["messages"], title, customer_uid)
        state.append_event(
            {
                "type": "agent_drafting",
                "conversation": title,
                "hash": current["hash"],
                "latest": latest,
                "pool": pool,
                "image_paths": _latest_user_turn_image_paths(current["messages"]),
            }
        )
        _log(f"[AI客服] 最新客户消息：{title}｜{_short(_message_text(latest))}")
        uid_text = customer_uid if customer_uid else "未绑定"
        _log(f"[AI客服] 开始调用AI回复：{title}，UID={uid_text}，池={pool}，历史消息数={len(current['messages'])}")
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
                continue
            state.mark_ready(job_id, reply_text=draft["text"])
            state.append_event(
                {
                    "type": "agent_ready",
                    "job_id": job_id,
                    "reply": draft["text"],
                    "action": draft.get("action"),
                    "handoff": (draft.get("raw") or {}).get("handoff"),
                }
            )
            _log(f"[AI客服] AI回复已生成：{title}｜{elapsed_text}｜池={pool}｜{_short(draft['text'], 140)}")
            if draft.get("action") == "handoff":
                handoff = (draft.get("raw") or {}).get("handoff") or {}
                if handoff.get("notified"):
                    _log(f"[AI客服] 飞书转人工通知已发送：{title}")
                else:
                    _log(f"[AI客服] 飞书转人工通知未发送：{title}｜{handoff.get('reason') or handoff.get('error') or 'unknown'}")
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
                continue
            state.mark_failed(job_id, str(exc))
            state.append_event({"type": "agent_draft_failed", "job_id": job_id, "error": str(exc)})
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
    try:
        with state.gui_lock():
            inbox.open_row(job)
            time.sleep(0.25)
            current = _read_current_with_retry(
                last=last,
                expected_visible_text=expected_latest_text,
                capture_images=False,
            )
            latest = watcher.latest_user_message(current["messages"])
            latest_text = (latest or {}).get("content") or (latest or {}).get("text") or ""

            role_misread_but_same_text = (
                latest is None
                and _last_visible_text_matches(current.get("messages", []), expected_latest_text)
            )
            same_turn_contains_expected = _latest_user_turn_contains(
                current.get("messages", []),
                expected_latest_text,
            )
            if not current.get("messages"):
                reason = "send_recheck_empty_retry"
                if mode == "review":
                    state.mark_approved_retry(job["id"], reply_text=job["reply_text"], reason=reason)
                else:
                    state.mark_ready(job["id"], reply_text=job["reply_text"])
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
                (not latest or latest_text != expected_latest_text)
                and not role_misread_but_same_text
                and not same_turn_contains_expected
            ):
                reason = "stale_context"
                state.mark_skipped(job["id"], reason)
                state.append_event(
                    {
                        "type": "agent_stale",
                        "conversation": title,
                        "expected": expected_latest_text,
                        "actual": latest_text,
                    }
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

            if mode == "dry-run":
                state.mark_done(job["id"], message_hash=current["hash"], reply_text=job["reply_text"])
                _log(f"[AI客服] 演练模式，不发送：{title}｜{_short(job['reply_text'], 140)}")
                return {"ok": True, "sent": 0, "dry_run": True, "conversation": title}

            _log(f"[AI客服] 发送前复核：{title}，最新客户消息未变化，准备发送")
            reply.send_text(job["reply_text"], dry_run=False, submit=True)
            time.sleep(0.5)
            after_send = _read_current_with_retry(
                last=last,
                expected_visible_text=job["reply_text"],
                capture_images=False,
            )
            if not worker._messages_contain_text(after_send["messages"], job["reply_text"]):
                _log(f"[AI客服] 发送后暂未读到回复，继续复核：{title}")
                after_send = _read_current_with_retry(
                    last=last,
                    expected_visible_text=job["reply_text"],
                    capture_images=False,
                    attempts=max(2, int(os.environ.get("WECOM_AGENT_SEND_VERIFY_ATTEMPTS", "5"))),
                    delay=float(os.environ.get("WECOM_AGENT_SEND_VERIFY_DELAY", "0.45")),
                )
                if not worker._messages_contain_text(after_send["messages"], job["reply_text"]):
                    raise RuntimeError("sent_reply_not_visible")
            state.mark_done(job["id"], message_hash=after_send["hash"], reply_text=job["reply_text"])
            state.append_event(
                {"type": "agent_sent", "conversation": title, "hash": after_send["hash"], "reply": job["reply_text"]}
            )
            _log(f"[AI客服] 已发送给 {title}：{_short(job['reply_text'], 140)}")
            return {"ok": True, "sent": 1, "conversation": title}
    except Exception as exc:
        state.mark_failed(job["id"], str(exc))
        state.append_event({"type": "agent_send_failed", "conversation": title, "error": str(exc)})
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

    text_workers = _pool_limit("text", max_drafts)
    image_workers = _pool_limit("image", max_drafts)
    _log(
        "[AI客服] 启动："
        f"模式={_mode_text(mode)}，扫描间隔={scan_interval}s，轮询间隔={poll}s，"
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

            _drop_superseded_drafts(futures)
            finished = _finish_drafts(futures)
            ready += finished["ready"]

            send_result = _send_one_ready(last=last, mode=mode)
            sent += send_result.get("sent", 0)

            while len(futures) < max_drafts:
                intake = _read_one_pending(last=last, executor=executor, futures=futures, max_drafts=max_drafts)
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
