"""Mac edge executor for centrally created WeCom channel commands."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

from cli_anything.wecom_gui.core import chat, edge_channel, edge_state, inbox, reply, state
from cli_anything.wecom_gui.utils import macos_backend


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _message_text(message: dict) -> str:
    return str(message.get("content") or message.get("text") or "").strip()


def _latest_customer_message(messages: list[dict]) -> dict | None:
    for message in reversed(messages):
        role = str(message.get("role") or "").strip().lower()
        if role in {"客服", "service", "assistant", "staff", "reply"}:
            continue
        if _message_text(message) or message.get("media"):
            return message
    return None


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


def _message_identity(conversation_key: str, message: dict) -> tuple[str, str]:
    media = _media_fingerprint(message.get("media") or [])
    raw = f"{conversation_key}|{_message_text(message)}|{','.join(item['sha256'] for item in media)}"
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
            return expected_uid == str(macos_backend.current_external_user_id() or "").strip()
        except Exception:
            return False
    return state.conversation_key_for_row(expected) == state.conversation_key_for_row(selected)


def _event_for_row(row: dict, current: dict, latest: dict, external_user_id: str) -> tuple[str, dict, list[dict]]:
    conversation_key = state.conversation_key_for_uid(external_user_id) or state.conversation_key_for_row(row)
    message_id, message_hash = _message_identity(conversation_key, latest)
    media = _media_fingerprint(latest.get("media") or [])
    for index, item in enumerate(media):
        item["media_id"] = f"edge-media-{message_hash[:16]}-{index}"
    payload = {
        "event_type": "inbound_message",
        "occurred_at": time.time(),
        "conversation": {
            "key": conversation_key,
            "external_user_id": external_user_id,
            "title": str(row.get("title") or ""),
        },
        "message": {
            "id": message_id,
            "hash": message_hash,
            "text": _message_text(latest),
            "media": [{key: value for key, value in item.items() if key != "capture_path"} for item in media],
            "visible_chat_hash": str(current.get("hash") or ""),
        },
    }
    return f"{conversation_key}:{message_hash}", payload, media


def collect_inbound_once(*, inbox_limit: int = 30, last: int = 20) -> dict:
    """Capture unread external direct chats into the durable edge spool."""
    rows = inbox.scan_visible(limit=inbox_limit).get("conversations", [])
    captured = skipped = 0
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
                current = chat.read_current(last=last, capture_images=True, media_preview=str(row.get("preview") or ""))
                latest = _latest_customer_message(current.get("messages") or [])
                if latest is None or _has_unsupported_media(latest):
                    skipped += 1
                    continue
                try:
                    external_user_id = macos_backend.current_external_user_id()
                except Exception:
                    external_user_id = ""
                external_user_id = external_user_id or str(
                    row.get("external_user_id") or row.get("external_userid") or ""
                )
                key, payload, media = _event_for_row(row, current, latest, external_user_id)
                inserted, _event = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
                captured += int(inserted)
        except Exception:
            skipped += 1
    return {"ok": True, "captured": captured, "skipped": skipped, "visible": len(rows)}


def _retry_delay(attempts: int) -> float:
    return min(300.0, 2.0 ** min(8, max(0, attempts)))


def flush_inbound(client: edge_channel.ChannelClient) -> dict:
    delivered = failed = 0
    for event in edge_state.due_inbound():
        try:
            client.post_inbound(dict(event["payload"]), list(event["media"]))
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


def _expected_matches(command: dict, row: dict, current: dict) -> bool:
    latest = _latest_customer_message(current.get("messages") or [])
    if latest is None:
        return False
    conversation_key = str(row.get("conversation_key") or "") or state.conversation_key_for_row(row)
    message_id, message_hash = _message_identity(conversation_key, latest)
    expected_id = str(command.get("expected_latest_message_id") or "")
    expected_hash = str(command.get("expected_latest_message_hash") or command.get("expected_latest_hash") or "")
    return bool(expected_id and expected_hash) and expected_id == message_id and expected_hash == message_hash


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
    return any("".join(_message_text(item).split()) == normalized for item in messages)


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
            time.sleep(0.2)
            if not _row_matches_opened(row, macos_backend.selected_conversation_row(limit=30)):
                return {"status": "precondition_failed", "reason": "opened_conversation_mismatch"}
            macos_backend.ensure_input_ready()
            before = chat.read_current(last=last, capture_images=False)
            if not _expected_matches(command, row, before):
                return {"status": "precondition_failed", "reason": "expected_latest_message_mismatch"}
            text = str(command.get("text") or command.get("message") or "")
            attachments = _download_attachments(client, command)
            before_outbound_images = _outbound_image_count(before.get("messages") or [])
            try:
                reply.send_message(text, attachments=attachments, dry_run=False, submit=True)
            except Exception as exc:
                after_error = chat.read_current(last=last, capture_images=False)
                if _reply_visible(after_error.get("messages") or [], text) or (
                    not text and attachments and _outbound_image_count(after_error.get("messages") or []) > before_outbound_images
                ):
                    return {"status": "succeeded", "verification": "visible_after_send_error"}
                return {"status": "needs_reconciliation", "reason": f"send_exception:{type(exc).__name__}"}
            time.sleep(0.5)
            after = chat.read_current(last=last, capture_images=False)
            if _reply_visible(after.get("messages") or [], text) or (
                not text and attachments and _outbound_image_count(after.get("messages") or []) > before_outbound_images
            ):
                return {"status": "succeeded", "verification": "reply_visible"}
            return {"status": "needs_reconciliation", "reason": "reply_not_visible_after_send"}
    except Exception as exc:
        return {"status": "needs_reconciliation", "reason": f"gui_exception:{type(exc).__name__}"}


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
    config = edge_channel.ChannelConfig.from_env()
    client = edge_channel.ChannelClient(config)
    heartbeat_error = ""
    try:
        client.heartbeat()
    except Exception as exc:
        heartbeat_error = str(exc)
    capture = collect_inbound_once(inbox_limit=inbox_limit, last=last)
    inbound = flush_inbound(client)
    results_before = flush_command_results(client)
    command_result: dict | None = None
    try:
        command = client.pull_command(wait_seconds=pull_wait_seconds)
        if command:
            inserted, receipt = edge_state.record_command(command)
            if inserted and edge_state.mark_command_executing(receipt["command_id"]):
                command_result = execute_command(client, command, last=last)
                edge_state.save_command_result(receipt["command_id"], command_result)
    except Exception as exc:
        command_result = {"status": "pull_failed", "reason": str(exc)}
    results_after = flush_command_results(client)
    return {
        "ok": True,
        "heartbeat_error": heartbeat_error,
        "capture": capture,
        "inbound": inbound,
        "command_result": command_result,
        "results": {"before": results_before, "after": results_after},
        "state": edge_state.edge_status(),
    }


def run_forever(*, poll_seconds: float = 1.0, inbox_limit: int = 30, last: int = 20) -> None:
    while True:
        tick(inbox_limit=inbox_limit, last=last, pull_wait_seconds=25)
        time.sleep(max(0.1, poll_seconds))
