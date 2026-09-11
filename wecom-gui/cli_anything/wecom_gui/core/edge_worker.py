"""Mac edge executor for centrally created WeCom channel commands."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cli_anything.wecom_gui.core import chat, edge_channel, edge_message_ledger, edge_state, inbox, reply, runtime_reporting, state
from cli_anything.wecom_gui.utils import macos_backend


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _message_text(message: dict) -> str:
    return str(message.get("content") or message.get("text") or "").strip()


def _identity_text(message: dict) -> str:
    return str(message.get("identity_text") or _message_text(message)).strip()


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
        f"{conversation_key}|{turn_position}|{_identity_text(message)}|"
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
    ledger_entry: dict | None = None,
) -> tuple[str, dict, list[dict]]:
    conversation_key = state.conversation_key_for_uid(external_user_id) or state.conversation_key_for_row(row)
    message_id, message_hash = _message_identity(
        conversation_key,
        message,
        turn_position,
        direction=direction,
    )
    if ledger_entry is not None:
        message_id, message_hash = ledger_entry["event_id"], ledger_entry["event_hash"]
    media = _media_fingerprint(message.get("media") or [])
    for index, item in enumerate(media):
        item["media_id"] = f"edge-media-{message_hash[:16]}-{index}"
    payload = {
        "event_type": f"{direction}_message",
        "occurred_at": (datetime.fromtimestamp(ledger_entry["occurred_at"], timezone.utc)
                        if ledger_entry is not None else datetime.now(timezone.utc)).isoformat(),
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
            **({"source": {"stream_id": ledger_entry["stream_id"], "sequence": ledger_entry["sequence"]}}
               if ledger_entry and ledger_entry.get("stream_id") else {}),
        },
    }
    return f"{conversation_key}:{message_hash}", payload, media


def collect_inbound_once(*, inbox_limit: int = 30, last: int = 20, media_budget=None) -> dict:
    """Open unread chats and capture only their baseline-safe increments."""
    media_budget = media_budget or MediaCaptureBudget()
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
                media_budget=media_budget,
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
    identity_occurrences: dict[str, int] = {}
    observed: list[tuple[str, dict, int]] = []
    for message in messages:
        text = _message_text(message)
        if not text and not message.get("media"):
            continue
        base = "|".join([
            _identity_text(message),
            str(message.get("identity_time", message.get("time")) or ""),
            json.dumps(_media_fingerprint(message.get("media") or []), ensure_ascii=False),
        ])
        position = occurrences.get(base, 0) + 1
        occurrences[base] = position
        fingerprint = _hash(f"visible|{base}|{position}")
        # Event IDs do not include time labels; repeated text at different times still needs separate IDs.
        identity_base = json.dumps([_identity_text(message), _media_fingerprint(message.get("media") or [])], ensure_ascii=False)
        identity_position = identity_occurrences.get(identity_base, 0) + 1
        identity_occurrences[identity_base] = identity_position
        observed.append((fingerprint, message, identity_position))
    return observed


def collect_visible_conversation_once(
    *,
    last: int = 20,
    expected_row: dict | None = None,
    bootstrap_recent_count: int = 0,
    media_budget=None,
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
            return _capture_ordered_snapshot(
                row, current, external_user_id, conversation_key, candidates,
                bootstrap_recent_count=bootstrap_recent_count,
                media_budget=media_budget,
            )
    except Exception as exc:
        return {"ok": False, "captured": 0, "reason": f"visible_capture_exception:{type(exc).__name__}"}


def _snapshot_match_key(message: dict) -> str:
    # A screenshot becomes available later; file digests must not change alignment.
    return _hash(json.dumps([_message_text(message),
                            [str(item.get("type") or "image") for item in message.get("media") or []]],
                           ensure_ascii=False))


class MediaCapturePending(ValueError):
    """A bounded, non-sensitive reason to retry media collection."""


@dataclass
class MediaCaptureBudget:
    image_limit: int = 3
    seconds_limit: float = 15.0
    images: int = 0
    elapsed: float = 0.0
    deferred: int = 0
    attempted_events: set[str] = field(default_factory=set)

    def available(self, requested: int) -> int:
        count = min(requested, max(0, self.image_limit - self.images)) if self.elapsed < self.seconds_limit else 0
        self.deferred += requested - count
        return count

    @contextmanager
    def capture(self, count: int):
        started = time.monotonic()
        self.images += count
        try:
            with macos_backend.capture_deadline(started + self.seconds_limit - self.elapsed):
                yield
        finally:
            self.elapsed += time.monotonic() - started


def _capture_snapshot_image(row, candidates, index, missing_indices, *, entry=None):
    macos_backend.activate_app()
    last = max(20, len(candidates))
    expected = candidates[index][1]
    expected_row_id = str(expected.get("capture_row_id") or "")
    fingerprint = str((expected.get("direction_evidence") or {}).get("imageFingerprint") or "")

    def checked_snapshot(*, require_pixels=False):
        nonlocal fingerprint
        if not _row_matches_opened(row, macos_backend.selected_conversation_row(limit=30)):
            raise MediaCapturePending("media_conversation_changed")
        fresh = chat.read_current(last=last, capture_images=False)
        messages = [msg for _, msg, _ in _visible_observation_fingerprints(fresh.get("messages") or [])]
        if [_snapshot_match_key(msg) for msg in messages] != [_snapshot_match_key(msg) for _, msg, _ in candidates]:
            raise MediaCapturePending("media_snapshot_changed")
        if not _row_matches_opened(row, macos_backend.selected_conversation_row(limit=30)):
            raise MediaCapturePending("media_conversation_changed")
        target = messages[index]
        if not expected_row_id or target.get("capture_row_id") != expected_row_id:
            raise MediaCapturePending("media_row_identity_changed")
        fresh_fingerprint = str((target.get("direction_evidence") or {}).get("imageFingerprint") or "")
        if fingerprint and fresh_fingerprint and not edge_message_ledger.media_fingerprints_match(fingerprint, fresh_fingerprint):
            raise MediaCapturePending("media_fingerprint_changed")
        if require_pixels and not fresh_fingerprint:
            raise MediaCapturePending("media_fingerprint_unavailable")
        if not fingerprint or (fingerprint.startswith("rgb32-v1:") and fresh_fingerprint.startswith("rgb32-v2:")):
            fingerprint = fresh_fingerprint
        if entry:
            edge_message_ledger.verify_media_identity(entry, target, require_pixels=require_pixels)
        return messages

    def visible(item):
        rect, viewport = item.get("rect") or {}, item.get("chat_viewport") or {}
        x, y, w, h = (float(rect.get(k) or 0) for k in ("x", "y", "width", "height"))
        vx, vy, vw, vh = (float(viewport.get(k) or 0) for k in ("x", "y", "width", "height"))
        return (w > 0 and h > 0 and vw > 0 and vh > 0
                and vx <= x and vy <= y and x + w <= vx + vw and y + h <= vy + vh)

    messages = checked_snapshot()
    missing = [(messages[index].get("media") or [])[i] for i in missing_indices]
    native_row = int(messages[index].get("row") or 0)
    has_pixels = bool((messages[index].get("direction_evidence") or {}).get("imageFingerprint"))
    if not all(visible(item) for item in missing) or (native_row and not has_pixels):
        if not native_row:
            raise MediaCapturePending("media_not_visible")
        macos_backend.reveal_chat_row(native_row, last=last)
        # Scrolling can load history or coincide with new arrivals. Revalidate the
        # entire ordered snapshot before using any new click coordinates.
        messages = checked_snapshot()
        missing = [(messages[index].get("media") or [])[i] for i in missing_indices]
        if not all(visible(item) for item in missing):
            raise MediaCapturePending("media_not_visible")
    if not fingerprint or not (messages[index].get("direction_evidence") or {}).get("imageFingerprint"):
        raise MediaCapturePending("media_fingerprint_unavailable")
    # A transient preview failure must stay retryable, not become a sticker.
    captured = macos_backend.capture_chat_images(
        [{**messages[index], "media": missing}], cache_preview_failures=False,
    )[0]["media"]
    try:
        for attempt in range(3):
            try:
                checked_snapshot(require_pixels=True)
                break
            except (MediaCapturePending, edge_message_ledger.MediaIdentityError) as exc:
                if str(exc) not in {"media_fingerprint_unavailable", "media_fingerprint_changed"} or attempt == 2:
                    raise
                # Preview dismissal can animate or briefly hide the chat. Keep
                # files quarantined until the original pixels return; changed
                # conversation, sequence or AX row still aborts immediately.
                macos_backend._capture_sleep(0.2)
    except Exception:
        # Files obtained while the target changed must never enter the upload spool.
        for item in captured:
            path = Path(str(item.get("capture_path") or "")).expanduser()
            if path.is_file() and path.resolve().is_relative_to(macos_backend._image_capture_dir().resolve()):
                path.unlink(missing_ok=True)
        raise
    return {"media": captured, "direction_evidence": messages[index].get("direction_evidence") or {}}


def _prepare_snapshot_media(row, candidates, index, entry, existing, budget):
    message = candidates[index][1]
    media = [dict(item) for item in message.get("media") or []]
    direction_evidence = {}
    cached = edge_message_ledger.media_state(entry)
    for saved in [existing.get("media") if existing else [], json.loads(cached["files_json"])]:
        if len(saved) != len(media):
            continue
        for i, item in enumerate(saved):
            if edge_state.media_files_ready([item]):
                media[i] = {**media[i], **item}
    missing = [i for i, item in enumerate(media) if not edge_state.media_files_ready([item])]
    if missing:
        if entry["event_hash"] in budget.attempted_events:
            return None
        if cached["paused_reason"] or cached["attempts"] >= edge_message_ledger.MEDIA_ATTEMPT_LIMIT or cached["next_attempt_at"] > time.time():
            return None
        selected = missing[:budget.available(len(missing))]
        if not selected or not edge_message_ledger.begin_media_attempt(entry):
            return None
        budget.attempted_events.add(entry["event_hash"])
        try:
            with budget.capture(len(selected)):
                edge_message_ledger.verify_media_identity(entry, message)
                capture = _capture_snapshot_image(row, candidates, index, selected, entry=entry)
            captured = capture["media"]
            direction_evidence = capture["direction_evidence"]
            if len(captured) != len(selected):
                raise ValueError("media_capture_count_mismatch")
            for i, item in zip(selected, captured):
                media[i] = item
            if not edge_state.media_files_ready(media):
                errors = {str(item.get("error") or "media_file_missing") for item in captured
                          if not edge_state.media_files_ready([item])}
                edge_message_ledger.save_media(entry, media, error=",".join(sorted(errors)), complete=False)
                return None
        except Exception as exc:
            edge_message_ledger.save_media(entry, media, error=(str(exc) if isinstance(exc, (
                MediaCapturePending, edge_message_ledger.MediaIdentityError, macos_backend.CaptureDeadlineExceeded))
                                                              else f"media_capture:{type(exc).__name__}"))
            return None
    edge_message_ledger.save_media(entry, media)
    return {**message, "media": media, "direction_evidence": direction_evidence}


def _capture_ordered_snapshot(row, current, external_user_id, conversation_key, candidates, *, bootstrap_recent_count=0, media_budget=None):
    media_budget = media_budget or MediaCaptureBudget()
    descriptors = []
    for fingerprint, message, position in candidates:
        media = [{"type": item["type"], "sha256": item["sha256"]}
                 for item in _media_fingerprint(message.get("media") or [])]
        descriptors.append({
            "match_key": _hash(json.dumps([_message_text(message), media], ensure_ascii=False)),
            "legacy_fingerprint": fingerprint,
            "legacy_hash": _message_identity(conversation_key, message, position)[1],
            "message": message,
            "media_only": bool(media) and _message_text(message) in {"[图片]", ""},
        })
    captured = pending_direction = pending_media = 0
    # Reserve durable identities first; slow GUI capture must not hold a DB lock.
    with edge_message_ledger.transaction() as conn:
        entries, reason = edge_message_ledger.prepare(
            conn, conversation_key, descriptors, bootstrap_recent_count=bootstrap_recent_count,
        )
        if reason == "message_alignment_pending":
            return {"ok": False, "captured": 0, "reason": reason, "pending_alignment": 1}
        for entry, (_, message, _) in zip(entries, candidates):
            if message.get("media") and entry["capture_status"] not in {"baseline", "ignored", "captured"}:
                edge_message_ledger.remember_media_identity(conn, entry, message)
    prepared = []
    for index, (entry, (_fingerprint, message, position)) in enumerate(zip(entries, candidates)):
        if entry["capture_status"] in {"baseline", "ignored"}:
            continue
        existing = edge_state.inbound_by_key(f'{conversation_key}:{entry["event_hash"]}')
        repair_media = bool(existing and existing["media"] and (
            existing["status"] == edge_state.WAITING_MEDIA or
            (existing["status"] == edge_state.PENDING and not edge_state.media_files_ready(existing["media"]))))
        if entry["capture_status"] == "captured" and not repair_media:
            continue
        if repair_media:
            edge_state.wait_for_inbound_media(existing["client_event_id"])
        if not message.get("media") and (repair_media or entry["capture_status"] == "pending_media"):
            pending_media += 1
            continue
        prepared.append((entry, message, position, index, existing))
    # Enqueue and captured status still commit atomically. A failed commit leaves
    # reserved pending rows (and cached files) available with the same IDs.
    suppressed = set()
    with edge_message_ledger.transaction() as conn:
        for entry, message, position, _index, _existing in prepared:
            text = _message_text(message)
            role = str(message.get("role") or "").strip()
            confidence = str(message.get("role_confidence") or "").strip().lower()
            if not confidence and role in {"用户", "客服"}:
                confidence = "high"
            evidence = message.get("direction_evidence") or {}
            if role != "用户" and evidence.get("side") != "left" and edge_state.is_command_echo_row(
                    conversation_key, str(message.get("capture_row_id") or ""),
                    _snapshot_match_key(message), connection=conn):
                edge_message_ledger.mark(conn, entry, status="captured", direction="outbound")
                suppressed.add(entry["event_hash"])
                continue
            direction = "unknown"
            if role == "用户" and confidence in {"high", "medium"}:
                direction = "inbound"
            elif role == "客服" and confidence in {"high", "medium"}:
                if text and not message.get("media") and edge_state.consume_outbound_echo_suppression(conversation_key, text, connection=conn):
                    edge_message_ledger.mark(conn, entry, status="captured", direction="outbound")
                    continue
                evidence = message.get("direction_evidence") or {}
                if (evidence.get("source") == "screencapturekit" and evidence.get("status") == "matched"
                        and evidence.get("side") == "right"):
                    direction = "outbound"
            if _has_unsupported_media(message):
                edge_message_ledger.mark(conn, entry, status="ignored")
                continue
            key, payload, media = _event_for_row(
                row, current, message, external_user_id, position, direction=direction, ledger_entry=entry,
            )
            inserted, event = edge_state.enqueue_inbound(
                dedupe_key=key, payload=payload, media=media, connection=conn,
            )
            direction = event["payload"]["message"]["direction"]
            needs_media = bool(media) and not edge_state.media_files_ready(event["media"])
            edge_message_ledger.mark(conn, entry, direction=direction,
                status="pending_media" if needs_media else "pending_direction" if direction == "unknown" else "captured")
            captured += int(inserted)
            pending_direction += int(direction == "unknown")
    # Registration is now durable even when image capture is unavailable.
    # Only reorder capture work; registration and message identities stay chronological.
    # Unattempted work is due at zero; retries rotate by their persisted due time.
    media_work = [item for item in prepared if item[0]["event_hash"] not in suppressed
                  and item[1].get("media") and not _has_unsupported_media(item[1])]
    media_work.sort(key=lambda item: edge_message_ledger.media_state(item[0])["next_attempt_at"])
    for entry, message, position, index, existing in media_work:
        if not message.get("media") or _has_unsupported_media(message):
            continue
        enriched = _prepare_snapshot_media(row, candidates, index, entry, existing, media_budget)
        if enriched is None:
            pending_media += 1
            continue
        with edge_message_ledger.transaction() as conn:
            key, payload, media = _event_for_row(row, current, enriched, external_user_id, position, ledger_entry=entry)
            # Fresh pixels may resolve an unknown sender while revealing an image;
            # attachment completion must never overwrite an already known sender.
            saved = conn.execute("SELECT payload_json FROM edge_inbound_events WHERE dedupe_key = ?", (key,)).fetchone()
            if not saved:
                continue
            payload = json.loads(saved[0])
            evidence = enriched.get("direction_evidence") or {}
            if (payload["message"]["direction"] == "unknown"
                    and evidence.get("source") == "screencapturekit" and evidence.get("status") == "matched"
                    and evidence.get("side") in {"left", "right"}):
                payload["message"]["direction"] = "inbound" if evidence["side"] == "left" else "outbound"
                payload["event_type"] = payload["message"]["direction"] + "_message"
                pending_direction = max(0, pending_direction - 1)
            payload["message"]["media"] = [{k: v for k, v in item.items() if k != "capture_path"} for item in media]
            _, event = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media, connection=conn)
            direction = event["payload"]["message"]["direction"]
            edge_message_ledger.mark(conn, entry, direction=direction,
                                     status="pending_direction" if direction == "unknown" else "captured")
    return {"ok": True, "captured": captured, "pending_direction": pending_direction,
            "pending_media": pending_media,
            "baseline": all(entry["capture_status"] == "baseline" for entry in entries)}


def collect_visible_outbound_once(*, last: int = 20) -> dict:
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
        # A server rollback or lost heartbeat must not send a registered placeholder
        # through the old duplicate path, which cannot attach its missing files.
        if event["registration_started"] and not getattr(client, "supports_deferred_media", False):
            continue
        deferred = bool(getattr(client, "supports_deferred_media", False) and event["payload"]["message"].get("source"))
        if deferred and event["registered_direction"] != event["payload"]["message"]["direction"]:
            continue
        if not edge_state.media_files_ready(event["media"]):
            edge_state.wait_for_inbound_media(event["client_event_id"])
            continue
        try:
            client.post_inbound(_event_for_upload(dict(event["payload"])), list(event["media"]),
                                **({"attachment_only": True} if deferred else {}))
            edge_state.mark_inbound_delivered(event["client_event_id"])
            delivered += 1
        except Exception as exc:
            edge_state.retry_inbound(event["client_event_id"], str(exc), delay_seconds=_retry_delay(event["attempts"]))
            failed += 1
    return {"delivered": delivered, "failed": failed}


def flush_registrations(client: edge_channel.ChannelClient) -> dict:
    registered = failed = 0
    if not getattr(client, "supports_deferred_media", False):
        return {"registered": 0, "failed": 0}
    for event in edge_state.due_registrations():
        try:
            edge_state.start_registration(event["client_event_id"])
            result = client.register_message(_event_for_upload(event["payload"]))
            edge_state.mark_registered(event["client_event_id"], event["payload"]["message"]["direction"])
            if not event["media"] or result.get("message", {}).get("mediaState") == "ready":
                edge_state.mark_inbound_delivered(event["client_event_id"])
            registered += 1
        except Exception as exc:
            edge_state.retry_registration(event["client_event_id"], type(exc).__name__,
                                          _retry_delay(event["registration_attempts"]))
            failed += 1
    return {"registered": registered, "failed": failed}


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


def _media_command_echo_rows(before: list[dict], after: list[dict], text: str, image_count: int) -> list[dict]:
    """Require a continuous AX suffix containing the complete outgoing payload."""
    before = [item for item in before if _message_text(item) or item.get("media")]
    after = [item for item in after if _message_text(item) or item.get("media")]
    before_ids = [str(item.get("capture_row_id") or "") for item in before]
    after_ids = [str(item.get("capture_row_id") or "") for item in after]
    if (not after_ids or any(not value for value in before_ids + after_ids)
            or len(set(before_ids)) != len(before_ids) or len(set(after_ids)) != len(after_ids)):
        return []
    start = 0
    if before_ids:
        if before_ids[-1] not in after_ids:
            return []
        start = after_ids.index(before_ids[-1]) + 1
        if start > len(before_ids) or after_ids[:start] != before_ids[-start:]:
            return []
    suffix = after[start:]
    if any(item["capture_row_id"] in before_ids for item in suffix):
        return []
    outgoing = []
    for item in suffix:
        evidence = item.get("direction_evidence") or {}
        if (evidence.get("source") == "screencapturekit" and evidence.get("status") == "matched"
                and evidence.get("side") == "left"):
            continue
        if (item.get("role") != "客服" or evidence.get("source") != "screencapturekit"
                or evidence.get("status") != "matched" or evidence.get("side") != "right"
                or _has_unsupported_media(item)):
            return []
        outgoing.append(item)
    texts = ["".join(_message_text(item).split()) for item in outgoing
             if not item.get("media") or _message_text(item) not in {"", "[图片]"}]
    normalized = "".join(text.split())
    if texts != ([normalized] if normalized else []):
        return []
    if sum(len(item.get("media") or []) for item in outgoing) != image_count:
        return []
    return [{"capture_row_id": item["capture_row_id"], "match_key": _snapshot_match_key(item)} for item in outgoing]


def _wait_for_media_command_echoes(row: dict, before: list[dict], text: str, image_count: int, *, last: int) -> list[dict]:
    attempts = max(1, int(os.environ.get("WECOM_GUI_SEND_ECHO_ATTEMPTS", "20")))
    delay = max(0.0, float(os.environ.get("WECOM_GUI_SEND_ECHO_RETRY_DELAY", "0.25")))
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        if not _row_matches_opened(row, macos_backend.selected_conversation_row(limit=30)):
            return []
        observed = chat.read_current(last=last, capture_images=False)
        if not _row_matches_opened(row, macos_backend.selected_conversation_row(limit=30)):
            return []
        echoes = _media_command_echo_rows(before, observed.get("messages") or [], text, image_count)
        if echoes:
            return echoes
    return []


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
            if not input_state.get("ok"):
                return {"status": "precondition_failed", "reason": str(input_state.get("error") or "chat_input_not_ready")}
            if int((input_state.get("input") or {}).get("valueLength") or 0) > 0:
                return {"status": "precondition_failed", "reason": "chat_input_not_empty"}
            before = chat.read_current(last=last, capture_images=False)
            text = str(command.get("text") or command.get("message") or "")
            try:
                attachments = _download_attachments(client, command)
            except Exception as exc:
                # No send has started, even if an earlier attachment downloaded.
                return {"status": "precondition_failed", "reason": f"command_media_download_failed:{type(exc).__name__}"}
            before_outbound_replies = _visible_outbound_reply_count(before.get("messages") or [], text)
            try:
                reply.send_message(
                    text,
                    attachments=attachments,
                    dry_run=False,
                    submit=True,
                    allow_clipboard_fallback=False,
                )
            except Exception as exc:
                if isinstance(exc, macos_backend.TextSendError) and exc.submitted is False:
                    return {"status": "precondition_failed", "reason": f"text_send:{exc.reason_code}"}
                if attachments:
                    echoes = _wait_for_media_command_echoes(row, before.get("messages") or [], text, len(attachments), last=last)
                    if echoes:
                        return {"status": "succeeded", "verification": "visible_after_send_error", "_echo_rows": echoes}
                    return {"status": "needs_reconciliation", "reason": f"send_exception:{type(exc).__name__}"}
                after_error = chat.read_current(last=last, capture_images=False)
                if _visible_outbound_reply_count(after_error.get("messages") or [], text) > before_outbound_replies:
                    return {"status": "succeeded", "verification": "visible_after_send_error"}
                reason = (f"text_send:{exc.reason_code}" if isinstance(exc, macos_backend.TextSendError)
                          else f"send_exception:{type(exc).__name__}")
                return {"status": "needs_reconciliation", "reason": reason}
            if attachments:
                echoes = _wait_for_media_command_echoes(row, before.get("messages") or [], text, len(attachments), last=last)
                if echoes:
                    return {"status": "succeeded", "verification": "reply_visible", "_echo_rows": echoes}
                return {"status": "needs_reconciliation", "reason": "complete_media_reply_not_visible_after_send"}
            if text and _wait_for_visible_outbound_reply(text, before_count=before_outbound_replies, last=last):
                return {"status": "succeeded", "verification": "reply_visible"}
            return {"status": "needs_reconciliation", "reason": "reply_not_visible_after_send"}
    except Exception as exc:
        return {"status": "needs_reconciliation", "reason": f"gui_exception:{type(exc).__name__}"}


def reconcile_command_echo(receipt: dict, *, last: int = 20) -> dict:
    """Re-read an uncertain send without ever issuing a second send action."""
    command = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
    command_id = str(receipt.get("command_id") or command.get("command_id") or "")
    row = _command_row(command)
    text = str(command.get("text") or command.get("message") or "")
    if not command_id or not str(row.get("title") or ""):
        return {"confirmed": False, "reason": "conversation_not_resolvable"}
    if not text or command.get("media") or command.get("attachments"):
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


def reconcile_pending_command_echoes(*, last: int = 20) -> dict:
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
    media_budget = MediaCaptureBudget()
    capture = collect_inbound_once(inbox_limit=inbox_limit, last=last, media_budget=media_budget)
    visible_capture = collect_visible_conversation_once(last=last, media_budget=media_budget)
    pending_direction = int(capture.get("pending_direction") or 0) + int(visible_capture.get("pending_direction") or 0)
    pending_alignment = edge_state.edge_status()["message_alignment_pending"]
    if pending_alignment:
        runtime_reporting.publish(
            "edge_channel", status="waiting", phase="message_alignment",
            rationale="message_sequence_overlap_not_unique", error_code="message_alignment_pending",
            metrics={"pending_alignment": pending_alignment},
        )
    elif pending_direction > 0:
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
    registrations = flush_registrations(client)
    inbound = flush_inbound(client)
    results_before = flush_command_results(client)
    reconciliation = reconcile_pending_command_echoes(last=last)
    command_result: dict | None = None
    runtime_reporting.publish("edge_channel", status="waiting", phase="waiting_for_command",
                              metrics={"pending_alignment": pending_alignment},
                              error_code="message_alignment_pending" if pending_alignment else "")
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
                    if not command_result.get("_echo_rows"):
                        edge_state.register_outbound_echo_suppression(receipt["command_id"], conversation_key, text)
                command_result = {key: value for key, value in command_result.items() if key != "_echo_rows"}
    except Exception as exc:
        command_result = {"status": "pull_failed", "reason": str(exc)}
        runtime_reporting.publish("edge_channel", status="retrying", phase="command_pull_failed", error_code=type(exc).__name__)
    results_after = flush_command_results(client)
    queue_status = edge_state.edge_status()
    pending_media = queue_status["media_capture_pending"]
    paused_media = queue_status["media_capture_paused"]
    if paused_media:
        runtime_reporting.publish(
            "edge_channel", status="waiting", phase="media_capture_paused",
            rationale="图片自动重试已达上限，消息记录保留，等待手动恢复",
            error_code=queue_status["paused_media_tasks"][0]["last_error"] or "media_retry_limit",
            metrics={"paused_media": paused_media, "attempt_limit": edge_message_ledger.MEDIA_ATTEMPT_LIMIT},
        )
    final_status = "retrying" if heartbeat_error or inbound.get("failed") or registrations["failed"] else ("waiting" if pending_alignment or pending_media else "idle")
    runtime_reporting.publish(
        "edge_channel",
        status=final_status,
        phase="network_retry" if final_status == "retrying" else ("media_capture_paused" if paused_media else
                                                                "message_alignment" if pending_alignment else
                                                                "media_capture" if pending_media else "idle"),
        metrics={"pending_uploads": queue_status.get("inbound_pending", 0),
                 "pending_alignment": pending_alignment, "pending_media": pending_media, "paused_media": paused_media,
                 "capture_images": media_budget.images, "capture_seconds": round(media_budget.elapsed, 2),
                 "deferred_images": media_budget.deferred},
        error_code="heartbeat_failed" if heartbeat_error else ("media_retry_limit" if paused_media else
                                                              "message_alignment_pending" if pending_alignment else
                                                              "media_capture_pending" if pending_media else ""),
    )
    return {
        "ok": True,
        "heartbeat_error": heartbeat_error,
        "capture": capture,
        "visible_capture": visible_capture,
        "inbound": inbound,
        "registrations": registrations,
        "command_result": command_result,
        "results": {"before": results_before, "after": results_after},
        "reconciliation": reconciliation,
        "state": edge_state.edge_status(),
    }


def run_forever(*, poll_seconds: float = 1.0, inbox_limit: int = 30, last: int = 20) -> None:
    while True:
        tick(inbox_limit=inbox_limit, last=last, pull_wait_seconds=25)
        time.sleep(max(0.1, poll_seconds))
