"""Mac edge executor for centrally created WeCom channel commands."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from cli_anything.wecom_gui.core import chat, edge_channel, edge_state, inbox, reply, runtime_reporting, state
from cli_anything.wecom_gui.utils import macos_backend


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _message_text(message: dict) -> str:
    return str(message.get("content") or message.get("text") or "").strip()


def _customer_messages_since_last_staff(messages: list[dict]) -> list[dict]:
    """Return every customer bubble in the current unresolved customer turn."""
    result: list[dict] = []
    for message in reversed(messages):
        role = str(message.get("role") or "").strip().lower()
        if role in {"客服", "service", "assistant", "staff", "reply"}:
            break
        if _message_text(message) or message.get("media"):
            result.append(message)
    return list(reversed(result))


def _latest_customer_message(messages: list[dict]) -> dict | None:
    customer_messages = _customer_messages_since_last_staff(messages)
    return customer_messages[-1] if customer_messages else None


def _has_unsupported_media(message: dict) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("type") or "image") != "image"
        for item in message.get("media") or []
    )


def _media_fingerprint(media: list[dict]) -> list[dict]:
    result = []
    for item in media:
        if not isinstance(item, dict) or str(item.get("type") or "image") != "image":
            continue
        path = Path(str(item.get("capture_path") or "")).expanduser()
        digest = ""
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.append({"type": "image", "sha256": digest, "capture_path": str(path)})
    return result


def _message_identity(
    conversation_key: str,
    message: dict,
    turn_position: int = 1,
    *,
    direction: str = "inbound",
) -> tuple[str, str]:
    media = _media_fingerprint(message.get("media") or [])
    raw = (
        f"{conversation_key}|{turn_position}|{_message_text(message)}|"
        f"{','.join(item['sha256'] for item in media)}"
    )
    fingerprint = _hash(raw)
    return f"edge-msg-{fingerprint[:32]}", fingerprint


def _row_matches_opened(expected: dict, selected: dict | None) -> bool:
    if not selected:
        return False
    expected_uid = str(expected.get("external_user_id") or expected.get("external_userid") or "").strip()
    selected_uid = str(selected.get("external_user_id") or selected.get("external_userid") or "").strip()
    if expected_uid:
        if selected_uid:
            return expected_uid == selected_uid
        try:
            current_uid = str(macos_backend.current_external_user_id() or "").strip()
        except Exception:
            current_uid = ""
        if current_uid:
            return expected_uid == current_uid
        bound_uid = _trusted_bound_uid_for_row(selected)
        return bool(bound_uid and expected_uid == bound_uid)
    return state.conversation_key_for_row(expected) == state.conversation_key_for_row(selected)


def _normalized_conversation_title(value: object) -> str:
    return "".join(str(value or "").split()).casefold()


_TRUSTED_BINDING_SOURCES = {
    "accessibility-sidebar-debug",
    "wecom-sidebar-jsapi",
    "wecom-sidebar-manual",
    "central-command-verified",
}


def _trusted_bound_uid_for_row(row: dict | None) -> str:
    """Use a local UID binding only when it came from an explicit verification step."""
    title = str((row or {}).get("title") or "").strip()
    if not title:
        return ""
    binding = state.lookup_wecom_customer(customer_name=title) or {}
    if str(binding.get("source") or "") not in _TRUSTED_BINDING_SOURCES:
        return ""
    bound_name = str(binding.get("customer_name") or binding.get("display_name") or "")
    if _normalized_conversation_title(bound_name) != _normalized_conversation_title(title):
        return ""
    return str(binding.get("uid") or "").strip()


def _current_identity_for_row(row: dict) -> tuple[str, str]:
    """Return the sidebar external ID only when its visible customer name matches the row."""
    try:
        identity = macos_backend.sidebar_identity()
    except Exception:
        identity = {}
    external_user_id = str(identity.get("external_user_id") or identity.get("external_userid") or "").strip()
    display_name = str(identity.get("display_name") or "").strip()
    row_title = str(row.get("title") or "").strip()
    if display_name and row_title and _normalized_conversation_title(display_name) != _normalized_conversation_title(row_title):
        return "", "sidebar_identity_title_mismatch"
    if external_user_id:
        return external_user_id, ""
    bound_uid = _trusted_bound_uid_for_row(row)
    if bound_uid:
        return bound_uid, ""
    return "", "sidebar_external_user_id_missing"


def _selected_conversation_row_for_capture(*, limit: int = 30) -> dict | None:
    """Read the selected row, activating WeCom only when macOS hides a background AX tree."""
    row = macos_backend.selected_conversation_row(limit=limit)
    if row:
        return row
    macos_backend.activate_app()
    time.sleep(0.15)
    return macos_backend.selected_conversation_row(limit=limit)


def _event_for_row(
    row: dict,
    current: dict,
    message: dict,
    external_user_id: str,
    turn_position: int,
    direction: str = "inbound",
) -> tuple[str, dict, list[dict]]:
    conversation_key = state.conversation_key_for_uid(external_user_id) or state.conversation_key_for_row(row)
    message_id, message_hash = _message_identity(
        conversation_key,
        message,
        turn_position,
        direction=direction,
    )
    media = _media_fingerprint(message.get("media") or [])
    for index, item in enumerate(media):
        item["media_id"] = f"edge-media-{message_hash[:16]}-{index}"
    payload = {
        "event_type": f"{direction}_message",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "conversation": {
            "key": conversation_key,
            "external_user_id": external_user_id,
            "title": str(row.get("title") or ""),
        },
        "message": {
            "id": message_id,
            "hash": message_hash,
            "direction": direction,
            "text": _message_text(message),
            "media": [{key: value for key, value in item.items() if key != "capture_path"} for item in media],
            "visible_chat_hash": str(current.get("hash") or ""),
        },
    }
    return f"{conversation_key}:{message_hash}", payload, media


def collect_inbound_once(*, inbox_limit: int = 30, last: int = 20) -> dict:
    """Open unread chats and capture only their baseline-safe increments."""
    try:
        rows = inbox.scan_visible(limit=inbox_limit).get("conversations", [])
    except Exception as exc:
        return {
            "ok": False,
            "captured": 0,
            "skipped": 0,
            "visible": 0,
            "pending_direction": 0,
            "reason": f"inbox_scan_exception:{type(exc).__name__}",
        }

    captured = skipped = pending_direction = 0
    for row in rows:
        if not inbox.is_customer_candidate(row):
            continue
        if not (bool(row.get("unread")) or int(row.get("unread_count") or 0) > 0):
            continue
        try:
            with state.gui_lock():
                inbox.open_row(row)
                time.sleep(0.2)
                selected = macos_backend.selected_conversation_row(limit=inbox_limit)
                if not _row_matches_opened(row, selected):
                    skipped += 1
                    continue
            visible = collect_visible_conversation_once(
                last=last,
                expected_row=row,
                bootstrap_recent_count=int(row.get("unread_count") or 1),
            )
            captured += int(visible.get("captured") or 0)
            pending_direction += int(visible.get("pending_direction") or 0)
            if not visible.get("ok"):
                skipped += 1
        except Exception:
            skipped += 1
    return {
        "ok": True,
        "captured": captured,
        "skipped": skipped,
        "visible": len(rows),
        "pending_direction": pending_direction,
    }


def _visible_observation_fingerprints(messages: list[dict]) -> list[tuple[str, dict, int]]:
    """Assign stable identities before any sender-direction inference."""
    occurrences: dict[str, int] = {}
    observed: list[tuple[str, dict, int]] = []
    for message in messages:
        text = _message_text(message)
        if not text and not message.get("media"):
            continue
        base = "|".join([
            text,
            str(message.get("time") or ""),
            json.dumps(_media_fingerprint(message.get("media") or []), ensure_ascii=False),
        ])
        position = occurrences.get(base, 0) + 1
        occurrences[base] = position
        fingerprint = _hash(f"visible|{base}|{position}")
        observed.append((fingerprint, message, position))
    return observed


def collect_visible_conversation_once(
    *,
    last: int = 100,
    expected_row: dict | None = None,
    bootstrap_recent_count: int = 0,
) -> dict:
    """Synchronize new customer and staff bubbles from the currently open conversation."""
    try:
        with state.gui_lock():
            row = _selected_conversation_row_for_capture(limit=30)
            if not row or not str(row.get("title") or "").strip():
                return {"ok": True, "captured": 0, "reason": "no_selected_conversation"}
            if expected_row and not _row_matches_opened(expected_row, row):
                return {"ok": True, "captured": 0, "reason": "selected_conversation_mismatch"}
            if expected_row:
                row = expected_row
            external_user_id, identity_error = _current_identity_for_row(row)
            if identity_error:
                return {"ok": True, "captured": 0, "reason": identity_error}
            current = chat.read_current(last=last, capture_images=False)
            conversation_key = state.conversation_key_for_uid(external_user_id) or state.conversation_key_for_row(row)
            messages = current.get("messages") or []
            candidates = _visible_observation_fingerprints(messages)
            pending_fingerprints = set(edge_state.record_visible_chat_observations(
                conversation_key,
                [fingerprint for fingerprint, _message, _position in candidates],
                bootstrap_recent_count=bootstrap_recent_count,
            ))
            unresolved_user_positions = {
                id(message): position
                for position, message in enumerate(_customer_messages_since_last_staff(messages), start=1)
            }
            captured = 0
            pending_direction = 0
            for fingerprint, message, position in candidates:
                if fingerprint not in pending_fingerprints:
                    continue
                text = _message_text(message)
                role = str(message.get("role") or "").strip()
                confidence = str(message.get("role_confidence") or "").strip().lower()
                if not confidence and role in {"用户", "客服"}:
                    confidence = "high"
                if role == "用户" and confidence in {"high", "medium"}:
                    event_position = unresolved_user_positions.get(id(message))
                    if event_position is None:
                        continue
                    direction = "inbound"
                elif role == "客服" and confidence in {"high", "medium"}:
                    # A visual right-side bubble is not sufficient evidence of who sent it.
                    # Only the locally recorded echo of a central delivery confirms outbound.
                    if text and edge_state.consume_outbound_echo_suppression(conversation_key, text):
                        edge_state.mark_chat_observation_captured(conversation_key, fingerprint, confidence=confidence)
                        continue
                    direction = "unknown"
                    event_position = position
                else:
                    key, payload, media = _event_for_row(
                        row,
                        current,
                        message,
                        external_user_id,
                        position,
                        direction="unknown",
                    )
                    inserted, _event = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
                    edge_state.mark_chat_observation_pending_direction(
                        conversation_key,
                        fingerprint,
                        confidence=confidence or "unknown",
                    )
                    captured += int(inserted)
                    pending_direction += 1
                    continue
                if _has_unsupported_media(message):
                    edge_state.ignore_chat_observation(conversation_key, fingerprint, reason="unsupported_media")
                    continue
                if direction == "unknown":
                    key, payload, media = _event_for_row(
                        row,
                        current,
                        message,
                        external_user_id,
                        event_position,
                        direction="unknown",
                    )
                    inserted, _event = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
                    edge_state.mark_chat_observation_pending_direction(
                        conversation_key,
                        fingerprint,
                        confidence=confidence or "unknown",
                    )
                    captured += int(inserted)
                    pending_direction += 1
                    continue
                key, payload, media = _event_for_row(
                    row,
                    current,
                    message,
                    external_user_id,
                    event_position,
                    direction=direction,
                )
                inserted, _event = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
                edge_state.mark_chat_observation_captured(conversation_key, fingerprint, confidence=confidence)
                captured += int(inserted)
            return {
                    "ok": True,
                    "captured": captured,
                    "pending_direction": pending_direction,
                    "baseline": not pending_fingerprints,
                }
    except Exception as exc:
        return {"ok": False, "captured": 0, "reason": f"visible_capture_exception:{type(exc).__name__}"}


def collect_visible_outbound_once(*, last: int = 100) -> dict:
    """Compatibility wrapper for callers that only reported staff capture."""
    return collect_visible_conversation_once(last=last)


def _retry_delay(attempts: int) -> float:
    return min(300.0, 2.0 ** min(8, max(0, attempts)))


def _event_for_upload(payload: dict) -> dict:
    """Make legacy spooled epoch timestamps acceptable to the central API."""
    event = dict(payload)
    occurred_at = event.get("occurred_at")
    if isinstance(occurred_at, (int, float)):
        event["occurred_at"] = datetime.fromtimestamp(occurred_at, timezone.utc).isoformat()
    return event


def flush_inbound(client: edge_channel.ChannelClient) -> dict:
    delivered = failed = 0
    for event in edge_state.due_inbound():
        try:
            client.post_inbound(_event_for_upload(dict(event["payload"])), list(event["media"]))
            edge_state.mark_inbound_delivered(event["client_event_id"])
            delivered += 1
        except Exception as exc:
            edge_state.retry_inbound(event["client_event_id"], str(exc), delay_seconds=_retry_delay(event["attempts"]))
            failed += 1
    return {"delivered": delivered, "failed": failed}


def _command_expired(command: dict) -> bool:
    value = command.get("expires_at")
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)):
        return float(value) <= time.time()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() <= time.time()
    except ValueError:
        return True


def _command_row(command: dict) -> dict:
    conversation = command.get("conversation") if isinstance(command.get("conversation"), dict) else {}
    external_user_id = str(conversation.get("external_user_id") or command.get("external_user_id") or "")
    title = str(conversation.get("title") or command.get("conversation_title") or command.get("customer_name") or "")
    if not title and external_user_id:
        binding = state.lookup_wecom_customer(uid=external_user_id) or {}
        title = str(binding.get("customer_name") or "")
    return {"title": title, "external_user_id": external_user_id, "conversation_key": str(conversation.get("key") or "")}


def _wait_for_opened_conversation(row: dict, *, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _row_matches_opened(row, macos_backend.selected_conversation_row(limit=30)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _download_attachments(client: edge_channel.ChannelClient, command: dict) -> list[dict]:
    command_id = str(command["command_id"])
    attachments = command.get("media") or command.get("attachments") or []
    local: list[dict] = []
    for index, item in enumerate(attachments):
        if not isinstance(item, dict) or str(item.get("type") or "image") != "image":
            raise ValueError("only image command media is supported")
        media_id = str(item.get("media_id") or item.get("id") or "").strip()
        if not media_id:
            raise ValueError("command image missing media_id")
        suffix = Path(str(item.get("filename") or "image.jpg")).suffix or ".jpg"
        destination = state.state_dir() / "edge-command-media" / command_id / f"{index}{suffix}"
        client.download_command_media(command_id, media_id, destination)
        local.append({"type": "image", "path": str(destination)})
    return local


def _reply_visible(messages: list[dict], text: str) -> bool:
    normalized = "".join(text.split())
    if not normalized:
        return False
    return any(
        str(item.get("role") or "").strip() == "客服"
        and "".join(_message_text(item).split()) == normalized
        for item in messages
    )


def _visible_outbound_reply_count(messages: list[dict], text: str) -> int:
    normalized = "".join(text.split())
    if not normalized:
        return 0
    return sum(
        1
        for item in messages
        if str(item.get("role") or "").strip() == "客服"
        and "".join(_message_text(item).split()) == normalized
    )


def _wait_for_visible_outbound_reply(
    text: str,
    *,
    before_count: int,
    last: int,
) -> bool:
    """Confirm a newly rendered outgoing bubble without resending the command."""
    attempts = max(1, int(os.environ.get("WECOM_GUI_SEND_ECHO_ATTEMPTS", "20")))
    delay = max(0.0, float(os.environ.get("WECOM_GUI_SEND_ECHO_RETRY_DELAY", "0.25")))
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        observed = chat.read_current(last=last, capture_images=False)
        messages = observed.get("messages") or []
        if _visible_outbound_reply_count(messages, text) > before_count:
            return True
    return False


def _outbound_image_count(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role not in {"客服", "service", "assistant", "staff", "reply"}:
            continue
        total += sum(1 for item in message.get("media") or [] if isinstance(item, dict))
    return total


def execute_command(client: edge_channel.ChannelClient, command: dict, *, last: int = 20) -> dict:
    """Execute one command at most once; uncertain sends become reconciliation."""
    command_id = str(command["command_id"])
    if _command_expired(command):
        return {"status": "precondition_failed", "reason": "command_expired"}
    row = _command_row(command)
    if not str(row.get("title") or ""):
        return {"status": "precondition_failed", "reason": "conversation_not_resolvable"}
    try:
        with state.gui_lock():
            inbox.open_row(row)
            if not _wait_for_opened_conversation(row):
                return {"status": "precondition_failed", "reason": "opened_conversation_mismatch"}
            input_state = macos_backend.send_input_ready()
            if int((input_state.get("input") or {}).get("valueLength") or 0) > 0:
                return {"status": "precondition_failed", "reason": "chat_input_not_empty"}
            before = chat.read_current(last=last, capture_images=False)
            text = str(command.get("text") or command.get("message") or "")
            attachments = _download_attachments(client, command)
            before_outbound_replies = _visible_outbound_reply_count(before.get("messages") or [], text)
            before_outbound_images = _outbound_image_count(before.get("messages") or [])
            try:
                reply.send_message(
                    text,
                    attachments=attachments,
                    dry_run=False,
                    submit=True,
                    allow_clipboard_fallback=False,
                )
            except Exception as exc:
                after_error = chat.read_current(last=last, capture_images=False)
                if _visible_outbound_reply_count(after_error.get("messages") or [], text) > before_outbound_replies or (
                    not text and attachments and _outbound_image_count(after_error.get("messages") or []) > before_outbound_images
                ):
                    return {"status": "succeeded", "verification": "visible_after_send_error"}
                return {"status": "needs_reconciliation", "reason": f"send_exception:{type(exc).__name__}"}
            if text and _wait_for_visible_outbound_reply(text, before_count=before_outbound_replies, last=last):
                return {"status": "succeeded", "verification": "reply_visible"}
            if not text and attachments:
                time.sleep(0.5)
                after = chat.read_current(last=last, capture_images=False)
                if _outbound_image_count(after.get("messages") or []) > before_outbound_images:
                    return {"status": "succeeded", "verification": "reply_visible"}
            return {"status": "needs_reconciliation", "reason": "reply_not_visible_after_send"}
    except Exception as exc:
        return {"status": "needs_reconciliation", "reason": f"gui_exception:{type(exc).__name__}"}


def reconcile_command_echo(receipt: dict, *, last: int = 100) -> dict:
    """Re-read an uncertain send without ever issuing a second send action."""
    command = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
    command_id = str(receipt.get("command_id") or command.get("command_id") or "")
    row = _command_row(command)
    text = str(command.get("text") or command.get("message") or "")
    if not command_id or not str(row.get("title") or ""):
        return {"confirmed": False, "reason": "conversation_not_resolvable"}
    if not text:
        return {"confirmed": False, "reason": "automatic_media_reconciliation_not_supported"}
    try:
        with state.gui_lock():
            inbox.open_row(row)
            if not _wait_for_opened_conversation(row):
                return {"confirmed": False, "reason": "opened_conversation_mismatch"}
            current = chat.read_current(last=last, capture_images=False)
            if not _reply_visible(current.get("messages") or [], text):
                return {"confirmed": False, "reason": "reply_not_visible"}
    except Exception as exc:
        return {"confirmed": False, "reason": f"gui_exception:{type(exc).__name__}"}

    edge_state.save_command_result(
        command_id,
        {"status": "succeeded", "verification": "reconciled_reply_visible"},
    )
    conversation = command.get("conversation") if isinstance(command.get("conversation"), dict) else {}
    edge_state.register_outbound_echo_suppression(command_id, str(conversation.get("key") or ""), text)
    return {"confirmed": True, "command_id": command_id}


def reconcile_pending_command_echoes(*, last: int = 100) -> dict:
    """Periodically resolve ambiguous sends; do not retry delivery itself."""
    checked = confirmed = 0
    max_attempts = max(1, int(os.environ.get("WECOM_GUI_RECONCILIATION_MAX_ATTEMPTS", "3")))
    for receipt in edge_state.due_reconciliation_checks(max_attempts=max_attempts):
        checked += 1
        result = reconcile_command_echo(receipt, last=last)
        if result.get("confirmed"):
            confirmed += 1
        else:
            edge_state.schedule_reconciliation_check(
                str(receipt.get("command_id") or ""),
                delay_seconds=float(os.environ.get("WECOM_GUI_RECONCILIATION_POLL_SECONDS", "5")),
            )
    return {"checked": checked, "confirmed": confirmed}


def flush_command_results(client: edge_channel.ChannelClient) -> dict:
    reported = failed = 0
    for receipt in edge_state.due_command_results():
        try:
            client.post_command_result(receipt["command_id"], receipt["lease_id"], receipt["result"])
            edge_state.mark_command_result_reported(receipt["command_id"])
            reported += 1
        except Exception as exc:
            edge_state.retry_command_result(
                receipt["command_id"], str(exc), delay_seconds=_retry_delay(receipt["result_attempts"])
            )
            failed += 1
    return {"reported": reported, "failed": failed}


def tick(*, inbox_limit: int = 30, last: int = 20, pull_wait_seconds: int = 25) -> dict:
    runtime_reporting.publish(
        "edge_channel",
        status="running",
        phase="scanning_inbox",
        metrics={"inbox_limit": inbox_limit, "read_window": last},
    )
    config = edge_channel.ChannelConfig.from_env()
    client = edge_channel.ChannelClient(config)
    heartbeat_error = ""
    try:
        client.heartbeat()
    except Exception as exc:
        heartbeat_error = str(exc)
    capture = collect_inbound_once(inbox_limit=inbox_limit, last=last)
    visible_capture = collect_visible_conversation_once(last=last)
    pending_direction = int(capture.get("pending_direction") or 0) + int(visible_capture.get("pending_direction") or 0)
    if pending_direction > 0:
        runtime_reporting.publish(
            "edge_channel",
            status="waiting",
            phase="direction_confirmation",
            direction="unknown",
            rationale="bubble_direction_not_confirmed",
            metrics={"pending_direction": pending_direction},
        )
    else:
        runtime_reporting.publish(
            "edge_channel",
            status="running",
            phase="uploading_events",
            metrics={"captured": int(capture.get("captured") or 0) + int(visible_capture.get("captured") or 0)},
        )
    inbound = flush_inbound(client)
    results_before = flush_command_results(client)
    reconciliation = reconcile_pending_command_echoes(last=last)
    command_result: dict | None = None
    runtime_reporting.publish("edge_channel", status="waiting", phase="waiting_for_command")
    try:
        command = client.pull_command(wait_seconds=pull_wait_seconds)
        if command:
            runtime_reporting.publish(
                "edge_channel",
                status="running",
                phase="executing_delivery_command",
                conversation_key=str((command.get("conversation") or {}).get("key") or ""),
                conversation_label=str((command.get("conversation") or {}).get("title") or ""),
            )
            inserted, receipt = edge_state.record_command(command)
            if inserted and edge_state.mark_command_executing(receipt["command_id"]):
                command_result = execute_command(client, command, last=last)
                edge_state.save_command_result(receipt["command_id"], command_result)
                if command_result.get("status") == "succeeded":
                    conversation = command.get("conversation") if isinstance(command.get("conversation"), dict) else {}
                    conversation_key = str(conversation.get("key") or "")
                    text = str(command.get("text") or command.get("message") or "")
                    edge_state.register_outbound_echo_suppression(receipt["command_id"], conversation_key, text)
    except Exception as exc:
        command_result = {"status": "pull_failed", "reason": str(exc)}
        runtime_reporting.publish("edge_channel", status="retrying", phase="command_pull_failed", error_code=type(exc).__name__)
    results_after = flush_command_results(client)
    final_status = "retrying" if heartbeat_error or inbound.get("failed") else "idle"
    runtime_reporting.publish(
        "edge_channel",
        status=final_status,
        phase="idle" if final_status == "idle" else "network_retry",
        metrics={"pending_uploads": edge_state.edge_status().get("inbound_pending", 0)},
        error_code="heartbeat_failed" if heartbeat_error else "",
    )
    return {
        "ok": True,
        "heartbeat_error": heartbeat_error,
        "capture": capture,
        "visible_capture": visible_capture,
        "inbound": inbound,
        "command_result": command_result,
        "results": {"before": results_before, "after": results_after},
        "reconciliation": reconciliation,
        "state": edge_state.edge_status(),
    }


def run_forever(*, poll_seconds: float = 1.0, inbox_limit: int = 30, last: int = 20) -> None:
    while True:
        tick(inbox_limit=inbox_limit, last=last, pull_wait_seconds=25)
        time.sleep(max(0.1, poll_seconds))
