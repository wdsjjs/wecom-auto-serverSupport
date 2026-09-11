from __future__ import annotations

import base64
import hashlib
import sqlite3
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

from cli_anything.wecom_gui.core import edge_message_ledger, edge_state, edge_worker, state
from cli_anything.wecom_gui.tests.test_edge_channel import FakeChannel


def message(text, *, direction="inbound", stamp=""):
    return {"text": text, "time": stamp,
            "role": {"inbound": "用户", "outbound": "客服", "unknown": "unknown"}[direction],
            "role_confidence": "low" if direction == "unknown" else "high",
            "direction_evidence": {"source": "screencapturekit", "status": "matched",
                                   "side": "right" if direction == "outbound" else "left"}}


@pytest.fixture
def capture(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    row = {"title": "客户A", "external_user_id": "customer-1"}
    monkeypatch.setattr(edge_worker, "_current_identity_for_row", lambda row: ("customer-1", ""))
    monkeypatch.setattr(edge_worker.macos_backend, "selected_conversation_row", lambda **kwargs: row)

    def run(messages, **kwargs):
        monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: {"messages": messages})
        return edge_worker.collect_visible_conversation_once(**kwargs)
    return run


def all_events():
    with edge_state._connect() as conn:
        return [edge_state._event_row(row) for row in conn.execute("SELECT * FROM edge_inbound_events ORDER BY id")]


def test_window_sliding_and_direction_resolution_keep_original_identity_and_time(capture):
    capture([])
    original = [message(f"row-{i}") for i in range(19)] + [message("nmn怎么吃", direction="unknown")]
    assert capture(original)["captured"] == 20
    first = all_events()[-1]
    edge_state.mark_inbound_delivered(first["client_event_id"])
    shifted = original[3:-1] + [message("nmn怎么吃", stamp="昨天 12:00")]
    shifted += [message("new-1"), message("new-2"), message("new-3")]
    assert capture(shifted)["captured"] == 4
    events = all_events()
    resolved = events[19]
    assert len(events) == 23
    assert resolved["payload"]["message"]["direction"] == "inbound"
    assert resolved["payload"]["message"]["id"] == first["payload"]["message"]["id"]
    assert resolved["client_event_id"] == first["client_event_id"]
    assert resolved["payload"]["occurred_at"] == first["payload"]["occurred_at"]
    assert capture(shifted)["captured"] == 0


def test_repeated_question_is_new_but_rescanning_it_is_not(capture):
    capture([])
    first = [message("nmn怎么吃"), message("每天一次", direction="outbound")]
    assert capture(first)["captured"] == 2
    second = first + [message("nmn怎么吃")]
    assert capture(second)["captured"] == 1
    events = all_events()
    assert events[0]["payload"]["message"]["id"] != events[2]["payload"]["message"]["id"]
    assert capture(second)["captured"] == 0
    # A new connection (as on process restart) uses the persisted sequence.
    assert capture(second)["captured"] == 0


def test_same_text_leaving_window_does_not_renumber_remaining_occurrence(capture):
    capture([])
    first = [message("重复"), message("锚点A"), message("锚点B"), message("重复", direction="unknown")]
    capture(first)
    old_id = all_events()[-1]["payload"]["message"]["id"]
    assert capture(first[1:])["captured"] == 0
    assert capture(first[1:3] + [message("重复")])["captured"] == 1
    assert len(all_events()) == 4
    assert all_events()[-1]["payload"]["message"]["id"] == old_id


def test_legacy_pending_history_is_not_replayed_and_new_suffix_is_kept(capture):
    old = [message("NMN怎么吃啊？", direction="unknown"), message("人呢人呢人呢？", direction="unknown"),
           message("在吗在吗？", direction="unknown")]
    row = {"title": "客户A"}
    original_ids = []
    for position, msg in enumerate(old, start=4):
        key, payload, media = edge_worker._event_for_row(row, {}, msg, "customer-1", position, direction="inbound")
        _, event = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
        edge_state.mark_inbound_delivered(event["client_event_id"])
        original_ids.append(payload["message"]["id"])
    edge_state.record_visible_chat_observations("uid:customer-1", [])
    fingerprints = [fp for fp, _, _ in edge_worker._visible_observation_fingerprints(old)]
    edge_state.record_visible_chat_observations("uid:customer-1", fingerprints)
    assert capture(old + [message("nmn怎么吃")])["captured"] == 1
    events = all_events()
    assert len(events) == 4
    assert [event["payload"]["message"]["id"] for event in events[:3]] == original_ids
    assert all(event["status"] == "delivered" for event in events[:3])
    assert events[-1]["payload"]["message"]["text"] == "nmn怎么吃"
    assert capture(old + [message("nmn怎么吃")])["captured"] == 0


def test_legacy_unknown_event_is_resolved_without_replacing_id(capture):
    unknown = message("待确认", direction="unknown")
    edge_state.record_visible_chat_observations("uid:customer-1", [])
    fingerprint = edge_worker._visible_observation_fingerprints([unknown])[0][0]
    edge_state.record_visible_chat_observations("uid:customer-1", [fingerprint])
    key, payload, media = edge_worker._event_for_row({}, {}, unknown, "customer-1", 1, direction="unknown")
    _, first = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
    edge_state.mark_inbound_delivered(first["client_event_id"])
    assert capture([message("待确认")])["captured"] == 1
    assert len(all_events()) == 1
    resolved = all_events()[0]
    assert resolved["client_event_id"] == first["client_event_id"]
    assert resolved["payload"]["occurred_at"] == first["payload"]["occurred_at"]
    assert resolved["payload"]["message"]["direction"] == "inbound"


def test_ambiguous_or_disjoint_snapshot_is_retained_without_advancing_tail(capture):
    capture([])
    capture([message("A"), message("B"), message("A")])
    for snapshot in [[message("A")], [message("new-X"), message("new-Y")]]:
        result = capture(snapshot)
        assert result["reason"] == "message_alignment_pending"
    assert len(all_events()) == 3
    assert edge_state.edge_status()["message_alignment_pending"] == 2
    assert capture([message("B"), message("A"), message("new-X"), message("new-Y")])["captured"] == 2
    assert capture([message("new-X"), message("new-Y")])["captured"] == 0
    assert edge_state.edge_status()["message_alignment_pending"] == 1


def test_queue_failure_keeps_reserved_ids_without_marking_captured(capture, monkeypatch):
    capture([])
    original_enqueue = edge_state.enqueue_inbound
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("simulated write failure")
        return original_enqueue(**kwargs)

    monkeypatch.setattr(edge_state, "enqueue_inbound", fail_second)
    assert not capture([message("A"), message("B")])["ok"]
    assert all_events() == []
    with edge_message_ledger.transaction() as conn:
        reserved = [dict(row) for row in conn.execute("SELECT * FROM edge_message_ledger ORDER BY sequence")]
        assert len(reserved) == 2
        assert all(row["capture_status"] == "pending_direction" for row in reserved)
    monkeypatch.setattr(edge_state, "enqueue_inbound", original_enqueue)
    assert capture([message("A"), message("B")])["captured"] == 2
    assert [event["payload"]["message"]["id"] for event in all_events()] == [row["event_id"] for row in reserved]


def test_scroll_back_to_unique_history_does_not_move_tail_or_resend(capture):
    capture([])
    history = [message(f"msg-{i}") for i in range(40)]
    capture(history[:20])
    capture(history[10:30])
    assert capture(history[:10])["captured"] == 0
    assert capture(history[20:40])["captured"] == 10
    assert len(all_events()) == 40


def test_empty_read_during_upgrade_does_not_turn_history_into_new_messages(capture):
    history = [message("old-A"), message("old-B")]
    fingerprints = [fp for fp, _, _ in edge_worker._visible_observation_fingerprints(history)]
    edge_state.record_visible_chat_observations("uid:customer-1", fingerprints)
    capture([])
    assert capture(history)["captured"] == 0
    assert all_events() == []


def test_concurrent_snapshot_commits_do_not_duplicate_messages(capture):
    capture([])
    messages = [message("first"), message("second")]
    candidates = edge_worker._visible_observation_fingerprints(messages)

    def collect(_):
        return edge_worker._capture_ordered_snapshot(
            {"title": "客户A"}, {}, "customer-1", "uid:customer-1", candidates,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(collect, range(2)))
    assert sorted(result["captured"] for result in results) == [0, 2]
    assert len(all_events()) == 2


def image_message(*, direction="unknown", count=1, row_id="7551:1000:row-1", fingerprint="rgb32-v1:image-1"):
    result = {**message("[图片]", direction=direction), "capture_row_id": row_id, "media": [
        {"type": "image", "rect": {"x": 10, "y": 20, "width": 80, "height": 80},
         "chat_viewport": {"x": 0, "y": 0, "width": 500, "height": 500}}
        for _ in range(count)]}
    if fingerprint:
        result["direction_evidence"]["imageFingerprint"] = fingerprint
    return result


def pixel_fingerprint(pixels):
    digest = hashlib.sha256(bytes(value & 0xf8 for value in pixels)).hexdigest()
    return "rgb32-v2:" + digest + ":" + base64.b64encode(bytes(pixels)).decode()


def test_pixel_matching_allows_only_small_render_drift():
    pixels = [80, 100, 140] * 1024
    original = pixel_fingerprint(pixels)
    drift = pixel_fingerprint([v + (8 if i % 3 == 0 else 0) for i, v in enumerate(pixels)])
    assert edge_message_ledger.media_fingerprints_match(original, drift)
    assert edge_message_ledger.media_fingerprints_match(original, pixel_fingerprint([v + 4 for v in pixels]))
    assert not edge_message_ledger.media_fingerprints_match(original, drift, allow_render_drift=False)
    assert not edge_message_ledger.media_fingerprints_match(original, pixel_fingerprint([v + 9 for v in pixels]))
    replaced = pixels.copy()
    replaced[12:15] = [0, 255, 0]
    assert not edge_message_ledger.media_fingerprints_match(original, pixel_fingerprint(replaced))
    assert not edge_message_ledger.media_fingerprints_match(original, "rgb32-v2:bad:not-base64")
    assert not edge_message_ledger.media_fingerprints_match(original, original.replace(original.split(":")[1], "0" * 64))
    resampled = pixels.copy()
    for x in range(32):
        for channel in range(3):
            resampled[(14 * 32 + x) * 3 + channel] += 40
            resampled[(15 * 32 + x) * 3 + channel] -= 40
    assert edge_message_ledger.media_fingerprints_match(original, pixel_fingerprint(resampled))
    replaced = pixels.copy()
    for y in range(12, 16):
        for x in range(12, 16):
            for channel in range(3):
                replaced[(y * 32 + x) * 3 + channel] += 50
    assert not edge_message_ledger.media_fingerprints_match(original, pixel_fingerprint(replaced))


def test_legacy_pixel_anchor_upgrades_only_on_exact_digest_and_does_not_drift(capture):
    capture([])
    pixels = [80, 100, 140] * 1024
    original = pixel_fingerprint(pixels)
    legacy = "rgb32-v1:" + original.split(":")[1]
    capture([image_message(fingerprint=legacy)], media_budget=edge_worker.MediaCaptureBudget(image_limit=0))
    with edge_message_ledger.transaction() as conn:
        entry = dict(conn.execute("SELECT * FROM edge_message_ledger").fetchone())
    drift = pixel_fingerprint([v + (8 if i % 3 == 0 else 0) for i, v in enumerate(pixels)])
    with pytest.raises(edge_message_ledger.MediaIdentityError, match="media_fingerprint_changed"):
        edge_message_ledger.verify_media_identity(entry, image_message(fingerprint=drift))
    edge_message_ledger.verify_media_identity(entry, image_message(fingerprint=original))
    edge_message_ledger.verify_media_identity(entry, image_message(fingerprint=drift))
    assert edge_message_ledger.media_state(entry)["image_fingerprint"] == original
    with pytest.raises(edge_message_ledger.MediaIdentityError, match="media_row_identity_changed"):
        edge_message_ledger.verify_media_identity(entry, image_message(row_id="7551:1000:another", fingerprint=drift))
    with pytest.raises(edge_message_ledger.MediaIdentityError, match="media_row_identity_changed"):
        edge_message_ledger.verify_media_identity(entry, image_message(row_id="9999:2000:another", fingerprint=drift))


@pytest.fixture
def grab_images(monkeypatch, tmp_path):
    def grab(row, candidates, index, missing, **kwargs):
        # Capturing must not hold a SQLite write lock.
        with edge_state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
        result = []
        for i in missing:
            path = tmp_path / f"image-{i}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage content")
            result.append({"type": "image", "capture_path": str(path), "capture_ok": True})
        return {"media": result, "direction_evidence": {}}
    mock = Mock(side_effect=grab)
    monkeypatch.setattr(edge_worker, "_capture_snapshot_image", mock)
    return mock


def test_three_new_images_align_after_old_images_scroll_offscreen(capture, grab_images, monkeypatch):
    viewport = {"x": 311, "y": 100, "width": 700, "height": 600}

    def read_snapshot(bodies, *, old_images_offscreen=False):
        rows = []
        image_number = 0
        for index, body in enumerate(bodies, start=1):
            is_image = body is None
            offscreen = is_image and old_images_offscreen and image_number < 2
            evidence = {"source": "screencapturekit", "status": "matched", "side": "left"}
            if offscreen:
                evidence.update(status="outside_viewport_or_unlaid_out", side="unknown")
            rows.append({
                "index": index, "texts": [] if is_image else [body],
                "captureRowId": f"7551:1000:row-{index}",
                "messageTexts": [] if is_image else [body],
                "bubbleImageSupported": is_image, "snapshotComplete": True,
                "x": 311, "y": -600 if offscreen else 200, "width": 700,
                "height": 290 if is_image else 84, "chatViewport": viewport,
                "directionEvidence": evidence,
            })
            image_number += int(is_image)
        monkeypatch.setattr(edge_worker.macos_backend, "_swift_ax", lambda command: rows)
        return edge_worker.chat.infer_roles(edge_worker.macos_backend._ax_chat_messages(
            last=20, include_hidden_images=True,
        ))

    capture([])
    original = [f"old-{i}" for i in range(17)] + [None, None, "last-text"]
    assert capture(read_snapshot(original))["captured"] == 20
    originals = all_events()
    for event in originals:
        edge_state.mark_inbound_delivered(event["client_event_id"])

    shifted = read_snapshot(original[3:] + [None, None, None], old_images_offscreen=True)
    assert len(shifted) == 20
    assert capture(shifted)["captured"] == 3
    events = all_events()
    assert len(events) == 23
    assert [event["client_event_id"] for event in events[:20]] == [
        event["client_event_id"] for event in originals
    ]
    assert all(event["status"] == "delivered" for event in events[:20])
    assert all(len(event["media"]) == 1 for event in events[-3:])
    assert capture(shifted)["captured"] == 0


def test_image_baseline_is_not_captured_or_uploaded(capture, grab_images):
    assert capture([image_message()])["captured"] == 0
    assert capture([image_message()])["captured"] == 0
    grab_images.assert_not_called()
    assert all_events() == []


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_capture_then_upload_retry_and_direction_resolution_reuse_files(capture, grab_images, direction):
    capture([])
    assert capture([image_message(count=2)])["captured"] == 1
    original = all_events()[0]
    assert len(original["media"]) == 2
    assert all(item["sha256"] for item in original["media"])
    assert original["media"][0]["media_id"] != original["media"][1]["media_id"]
    assert edge_worker.flush_inbound(FakeChannel(inbound_error=RuntimeError("offline")))["failed"] == 1
    assert capture([image_message(count=2)])["captured"] == 0
    edge_state.retry_inbound(original["client_event_id"], "retry", delay_seconds=0)
    assert edge_worker.flush_inbound(FakeChannel())["delivered"] == 1
    assert capture([image_message(direction=direction, count=2)])["captured"] == 1
    grab_images.assert_called_once()
    resolved = all_events()[0]
    assert len(all_events()) == 1
    assert resolved["client_event_id"] == original["client_event_id"]
    assert resolved["payload"]["message"]["id"] == original["payload"]["message"]["id"]
    assert resolved["payload"]["occurred_at"] == original["payload"]["occurred_at"]
    assert resolved["payload"]["message"]["source"] == original["payload"]["message"]["source"]
    assert resolved["payload"]["message"]["direction"] == direction
    assert resolved["media"] == original["media"]


def test_partial_capture_waits_and_recovers_same_message_after_backoff(capture, grab_images, monkeypatch):
    capture([])
    successful = grab_images.side_effect
    now = edge_worker.time.time()
    monkeypatch.setattr(edge_worker.time, "time", lambda: now)

    def partial(row, candidates, index, missing, **kwargs):
        result = successful(row, candidates, index, [0])
        result["media"].append({"type": "image", "error": "preview_not_found"})
        return result
    grab_images.side_effect = partial
    assert capture([image_message(count=2)])["pending_media"] == 1
    assert len(all_events()) == 1
    assert not edge_state.media_files_ready(all_events()[0]["media"])
    with edge_message_ledger.transaction() as conn:
        first = dict(conn.execute("SELECT * FROM edge_message_ledger").fetchone())
    assert first["capture_status"] == "pending_media"
    assert edge_state.edge_status()["media_capture_pending"] == 1
    assert capture([image_message(count=2)])["pending_media"] == 1
    assert grab_images.call_count == 1
    now += 3
    grab_images.side_effect = successful
    assert capture([image_message(count=2)])["captured"] == 0
    assert grab_images.call_args.args[-1] == [1]
    assert all_events()[0]["payload"]["message"]["id"] == first["event_id"]
    assert edge_state.edge_status()["media_capture_pending"] == 0


def test_missing_spool_file_waits_without_http_and_repairs_original_identity(capture, grab_images):
    capture([])
    capture([image_message(direction="inbound")])
    original = all_events()[0]
    from pathlib import Path
    Path(original["media"][0]["capture_path"]).unlink()
    channel = FakeChannel()
    edge_worker.flush_inbound(channel)
    edge_worker.flush_inbound(channel)
    assert channel.inbound_calls == []
    waiting = all_events()[0]
    assert waiting["status"] == "waiting_media"
    assert waiting["attempts"] == 0
    assert capture([image_message()])["captured"] == 0
    repaired = all_events()[0]
    assert repaired["client_event_id"] == original["client_event_id"]
    assert repaired["payload"]["message"]["id"] == original["payload"]["message"]["id"]
    assert repaired["payload"]["occurred_at"] == original["payload"]["occurred_at"]
    assert repaired["payload"]["message"]["direction"] == "inbound"
    assert edge_worker.flush_inbound(channel)["delivered"] == 1


def test_waiting_image_cannot_be_repaired_with_empty_attachments(capture):
    capture([])
    key, payload, media = edge_worker._event_for_row({}, {}, image_message(), "customer-1", 1, direction="unknown")
    _, original = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
    edge_state.wait_for_inbound_media(original["client_event_id"])
    changed = {**payload, "message": {**payload["message"], "direction": "inbound", "media": []}}
    inserted, event = edge_state.enqueue_inbound(dedupe_key=key, payload=changed, media=[])
    assert inserted  # Direction can resolve while attachments remain pending.
    assert event["status"] == "waiting_media"
    assert len(event["media"]) == 1


def test_legacy_image_placeholder_is_recaptured_with_original_event_id(capture, grab_images):
    image = image_message()
    edge_state.record_visible_chat_observations("uid:customer-1", [])
    fingerprint = edge_worker._visible_observation_fingerprints([image])[0][0]
    edge_state.record_visible_chat_observations("uid:customer-1", [fingerprint])
    key, payload, media = edge_worker._event_for_row({}, {}, image, "customer-1", 1, direction="unknown")
    assert media[0]["capture_path"] == "."
    _, original = edge_state.enqueue_inbound(dedupe_key=key, payload=payload, media=media)
    channel = FakeChannel()
    edge_worker.flush_inbound(channel)
    assert not channel.inbound_calls
    assert capture([image])["captured"] == 0
    repaired = all_events()[0]
    assert repaired["client_event_id"] == original["client_event_id"]
    assert repaired["payload"]["occurred_at"] == original["payload"]["occurred_at"]
    assert repaired["payload"]["message"]["id"] == payload["message"]["id"]
    assert repaired["media"][0]["capture_path"] != "."
    assert edge_worker.flush_inbound(channel)["delivered"] == 1


@pytest.mark.parametrize("failure", ["conversation", "snapshot", "offscreen"])
def test_capture_refuses_changed_or_offscreen_target(capture, monkeypatch, failure):
    image = image_message()
    capture([image])
    candidates = edge_worker._visible_observation_fingerprints([image])
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    monkeypatch.setattr(edge_worker, "_row_matches_opened", lambda *args: failure != "conversation")
    if failure == "snapshot":
        monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: {"messages": []})
    elif failure == "offscreen":
        image["media"][0]["rect"]["y"] = -100
    grab = Mock()
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)
    with pytest.raises(edge_worker.MediaCapturePending):
        edge_worker._capture_snapshot_image({}, candidates, 0, [0])
    grab.assert_not_called()


def test_visible_image_capture_uses_existing_preview_and_keeps_failures_retryable(capture, monkeypatch):
    image = image_message()
    capture([image])
    candidates = edge_worker._visible_observation_fingerprints([image])
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    monkeypatch.setattr(edge_worker, "_row_matches_opened", lambda *args: True)
    grab = Mock(return_value=[{"media": [{"type": "image", "capture_path": "test.png"}]}])
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)
    result = edge_worker._capture_snapshot_image({}, candidates, 0, [0])
    assert result["media"][0]["capture_path"] == "test.png"
    assert grab.call_args.kwargs == {"cache_preview_failures": False}


@pytest.mark.parametrize("whole_row_visible", [True, False])
def test_clipped_image_is_revealed_then_captured_with_fresh_direction(capture, monkeypatch, whole_row_visible):
    image = image_message()
    image["row"] = 18
    image["media"][0]["rect"]["y"] = -100
    capture([image])
    visible = image_message(direction="inbound")
    visible["row"] = 18
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    monkeypatch.setattr(edge_worker, "_row_matches_opened", lambda *args: True)
    monkeypatch.setattr(edge_worker.chat, "read_current", Mock(side_effect=[
        {"messages": [image]}, {"messages": [visible]}, {"messages": [visible]},
    ]))
    reveal = Mock(return_value={"ok": whole_row_visible})
    grab = Mock(return_value=[{"media": [{"type": "image", "capture_path": "test.png"}]}])
    monkeypatch.setattr(edge_worker.macos_backend, "reveal_chat_row", reveal)
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)

    result = edge_worker._capture_snapshot_image({}, edge_worker._visible_observation_fingerprints([image]), 0, [0])

    reveal.assert_called_once_with(18, last=20)
    assert grab.call_args.args[0][0]["media"] == visible["media"]
    assert result["direction_evidence"] == visible["direction_evidence"]


@pytest.mark.parametrize("failure", ["scroll", "snapshot", "conversation", "offscreen"])
def test_reveal_failure_or_changed_snapshot_never_captures_a_different_image(capture, monkeypatch, failure):
    image = image_message()
    image["row"] = 18
    image["media"][0]["rect"]["y"] = -100
    capture([image])
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    monkeypatch.setattr(edge_worker, "_row_matches_opened", Mock(
        side_effect=[True, True, False] if failure == "conversation" else None, return_value=True,
    ))
    monkeypatch.setattr(edge_worker.chat, "read_current", Mock(side_effect=[
        {"messages": [image]}, {"messages": [message("new arrival")] if failure == "snapshot" else [image]},
    ]))
    monkeypatch.setattr(edge_worker.macos_backend, "reveal_chat_row", Mock(return_value={"ok": failure != "scroll"}))
    grab = Mock()
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)

    with pytest.raises(edge_worker.MediaCapturePending):
        edge_worker._capture_snapshot_image({}, edge_worker._visible_observation_fingerprints([image]), 0, [0])
    grab.assert_not_called()


@pytest.mark.parametrize("initial_direction,expected", [("unknown", "inbound"), ("outbound", "outbound")])
def test_image_reveal_resolves_only_unknown_direction_without_replacing_identity(
    capture, grab_images, monkeypatch, initial_direction, expected,
):
    capture([])
    now = edge_worker.time.time()
    monkeypatch.setattr(edge_worker.time, "time", lambda: now)
    successful = grab_images.side_effect
    grab_images.side_effect = RuntimeError("temporarily offscreen")
    snapshot = [image_message(direction=initial_direction)]
    capture(snapshot)
    original = all_events()[0]

    def revealed(*args, **kwargs):
        result = successful(*args, **kwargs)
        result["direction_evidence"] = {"source": "screencapturekit", "status": "matched", "side": "left"}
        return result

    now += 3
    grab_images.side_effect = revealed
    capture(snapshot)
    event = all_events()[0]
    assert len(all_events()) == 1
    assert event["client_event_id"] == original["client_event_id"]
    assert event["payload"]["occurred_at"] == original["payload"]["occurred_at"]
    assert event["payload"]["message"]["direction"] == expected
    assert event["payload"]["event_type"] == expected + "_message"
    assert edge_state.media_files_ready(event["media"])


class DeferredChannel(FakeChannel):
    supports_deferred_media = True

    def __init__(self):
        super().__init__()
        self.registrations = []
        self.attachment_calls = []

    def register_message(self, event):
        self.registrations.append(event)
        return {"accepted": True, "message": {"mediaState": "pending" if event["message"]["media"] else "ready"}}

    def post_inbound(self, event, media, *, attachment_only=False):
        assert attachment_only
        self.attachment_calls.append((event, media))
        return {"accepted": True}


def test_image_registration_precedes_attachment_and_does_not_hold_later_text(capture, grab_images, monkeypatch):
    capture([])
    successful = grab_images.side_effect
    now = edge_worker.time.time()
    monkeypatch.setattr(edge_worker.time, "time", lambda: now)
    grab_images.side_effect = RuntimeError("preview unavailable")
    snapshot = [image_message(), message("what is this?")]
    capture(snapshot)
    events = all_events()
    original_id = events[0]["payload"]["message"]["id"]
    assert [e["payload"]["message"]["source"]["sequence"] for e in events] == [1, 2]
    channel = DeferredChannel()
    assert edge_worker.flush_registrations(channel)["registered"] == 2
    edge_worker.flush_inbound(channel)
    assert [event["message"]["id"] for event in channel.registrations] == [e["payload"]["message"]["id"] for e in events]
    assert not channel.attachment_calls
    assert all_events()[1]["status"] == "delivered"
    now += 3
    grab_images.side_effect = successful
    capture(snapshot)
    assert edge_worker.flush_registrations(channel)["registered"] == 0
    assert edge_worker.flush_inbound(channel)["delivered"] == 1
    assert channel.attachment_calls[0][0]["message"]["id"] == original_id
    assert len(all_events()) == 2


def test_failed_registration_blocks_only_attachment_upload_and_keeps_identity(capture, grab_images, monkeypatch):
    capture([])
    capture([image_message()])
    original = all_events()[0]
    channel = DeferredChannel()
    monkeypatch.setattr(channel, "register_message", Mock(side_effect=RuntimeError("offline")))
    assert edge_worker.flush_registrations(channel)["failed"] == 1
    edge_worker.flush_inbound(channel)
    assert not channel.attachment_calls
    assert edge_state.due_registrations() == []
    assert all_events()[0]["client_event_id"] == original["client_event_id"]
    legacy = FakeChannel()
    edge_worker.flush_inbound(legacy)
    assert not legacy.inbound_calls  # Even a lost registration ACK pins the new protocol.


def test_media_pending_survives_leaving_window_and_can_still_be_registered(capture, grab_images):
    capture([])
    grab_images.side_effect = RuntimeError("not visible")
    snapshot = [image_message()] + [message(f"text-{i}") for i in range(19)]
    capture(snapshot)
    first = all_events()[0]
    capture(snapshot[1:] + [message("new")])
    channel = DeferredChannel()
    edge_worker.flush_registrations(channel)
    assert channel.registrations[0]["message"]["id"] == first["payload"]["message"]["id"]
    assert channel.registrations[0]["message"]["media"]
    assert edge_state.edge_status()["media_capture_pending"] == 1


def test_identical_image_window_is_retained_as_uncertain_not_silently_skipped(capture, grab_images):
    capture([])
    snapshot = [image_message(), image_message()]
    capture(snapshot)
    ids = [event["client_event_id"] for event in all_events()]
    result = capture(snapshot + [image_message()])
    assert result["reason"] == "message_alignment_pending"
    assert edge_state.edge_status()["message_alignment_pending"] == 1
    assert [event["client_event_id"] for event in all_events()] == ids


@pytest.mark.parametrize("change", ["row", "pixels"])
@pytest.mark.parametrize("stage", ["before", "reveal", "after"])
def test_identical_placeholders_cannot_hide_a_changed_capture_target(capture, monkeypatch, tmp_path, change, stage):
    import copy
    image = image_message()
    image["row"] = 18
    capture([image])
    changed = copy.deepcopy(image)
    if change == "row":
        changed["capture_row_id"] = "7551:1000:replacement"
    else:
        changed["direction_evidence"]["imageFingerprint"] = "rgb32-v1:different-image"
    if stage == "reveal":
        image["media"][0]["rect"]["y"] = -100
    reads = [changed] if stage == "before" else [image, changed]
    if stage == "after" and change == "pixels":
        reads += [changed, changed]
    monkeypatch.setattr(edge_worker.macos_backend, "_capture_sleep", lambda seconds: None)
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    monkeypatch.setattr(edge_worker, "_row_matches_opened", lambda *args: True)
    monkeypatch.setattr(edge_worker.chat, "read_current", Mock(side_effect=[{"messages": [item]} for item in reads]))
    monkeypatch.setattr(edge_worker.macos_backend, "reveal_chat_row", Mock(return_value={"ok": True}))
    monkeypatch.setenv("WECOM_GUI_CAPTURE_IMAGE_DIR", str(tmp_path))
    path = tmp_path / "unverified.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\ntest")
    grab = Mock(return_value=[{"media": [{"type": "image", "capture_path": str(path)}]}])
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)
    with pytest.raises(edge_worker.MediaCapturePending, match="media_(row_identity|fingerprint)_changed"):
        edge_worker._capture_snapshot_image({}, edge_worker._visible_observation_fingerprints([image]), 0, [0])
    assert grab.call_count == int(stage == "after")
    assert path.exists() == (stage != "after")


def test_image_without_complete_pixel_evidence_stays_pending(capture, monkeypatch):
    image = image_message(fingerprint="")
    capture([image])
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    grab = Mock()
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)
    with pytest.raises(edge_worker.MediaCapturePending, match="media_fingerprint_unavailable"):
        edge_worker._capture_snapshot_image({"title": "客户A", "external_user_id": "customer-1"},
                                            edge_worker._visible_observation_fingerprints([image]), 0, [0])
    grab.assert_not_called()


@pytest.mark.parametrize("transient", ["unavailable", "changed"])
def test_preview_dismissal_allows_only_bounded_rechecks_of_same_image(capture, monkeypatch, transient):
    image = image_message()
    hidden = image_message(fingerprint="")
    hidden["direction_evidence"].update(status="window_not_on_screen", side="unknown")
    if transient == "changed":
        hidden = image_message(fingerprint="rgb32-v1:preview-transition")
    capture([image])
    monkeypatch.setattr(edge_worker.macos_backend, "activate_app", lambda: None)
    monkeypatch.setattr(edge_worker, "_row_matches_opened", lambda *args: True)
    monkeypatch.setattr(edge_worker.macos_backend, "_capture_sleep", lambda seconds: None)
    grab = Mock(return_value=[{"media": [{"type": "image", "capture_path": "test.png"}]}])
    monkeypatch.setattr(edge_worker.macos_backend, "capture_chat_images", grab)
    reads = Mock(side_effect=[{"messages": [item]} for item in (image, hidden, image)])
    monkeypatch.setattr(edge_worker.chat, "read_current", reads)
    result = edge_worker._capture_snapshot_image({}, edge_worker._visible_observation_fingerprints([image]), 0, [0])
    assert result["media"][0]["capture_path"] == "test.png"
    assert reads.call_count == 3
    reads = Mock(side_effect=[{"messages": [item]} for item in (image, hidden, hidden, hidden)])
    monkeypatch.setattr(edge_worker.chat, "read_current", reads)
    with pytest.raises(edge_worker.MediaCapturePending, match="media_fingerprint_" + transient):
        edge_worker._capture_snapshot_image({}, edge_worker._visible_observation_fingerprints([image]), 0, [0])
    assert reads.call_count == 4


def test_image_budget_is_shared_between_conversations_and_next_round_resumes(capture, grab_images, monkeypatch):
    capture([])
    budget = edge_worker.MediaCaptureBudget()
    first = [image_message(), message("anchor"), image_message(row_id="7551:1000:row-2")]
    capture(first, media_budget=budget)
    second = [image_message(row_id="7551:1000:row-3"), message("anchor2"),
              image_message(row_id="7551:1000:row-4"), message("later text")]
    monkeypatch.setattr(edge_worker, "_current_identity_for_row", lambda row: ("customer-2", ""))
    capture([], media_budget=budget)
    result = capture(second, media_budget=budget)
    assert result["captured"] == 4
    assert result["pending_media"] == 1
    assert grab_images.call_count == budget.images == 3
    ids = [event["client_event_id"] for event in all_events()]
    assert len(edge_state.due_registrations()) == 7
    result = capture(second)
    assert result["pending_media"] == 0
    assert grab_images.call_count == 4
    assert [event["client_event_id"] for event in all_events()] == ids


def test_failed_early_images_do_not_starve_later_images(capture, monkeypatch):
    capture([])
    clock = [1000.0]
    monkeypatch.setattr(edge_worker.time, "time", lambda: clock[0])
    attempted = []

    def fail_capture(row, candidates, index, missing, **kwargs):
        attempted.append(index)
        raise edge_worker.MediaCapturePending("media_fingerprint_changed")

    monkeypatch.setattr(edge_worker, "_capture_snapshot_image", fail_capture)
    snapshot = []
    for i in range(4):
        snapshot.extend([image_message(row_id=f"7551:1000:row-{i}"), message(f"anchor-{i}")])
    budget = edge_worker.MediaCaptureBudget()
    capture(snapshot, media_budget=budget)
    assert attempted == [0, 2, 4]
    ids = [event["client_event_id"] for event in all_events()]
    clock[0] += 100
    capture(snapshot)
    assert attempted[3] == 6
    assert [event["client_event_id"] for event in all_events()] == ids


def test_same_image_is_not_retried_twice_in_shared_tick(capture, monkeypatch):
    capture([])
    clock = [1000.0]
    monkeypatch.setattr(edge_worker.time, "time", lambda: clock[0])
    attempted = []

    def fail_capture(*args, **kwargs):
        attempted.append(True)
        raise edge_worker.MediaCapturePending("media_fingerprint_changed")

    monkeypatch.setattr(edge_worker, "_capture_snapshot_image", fail_capture)
    budget = edge_worker.MediaCaptureBudget()
    snapshot = [image_message()]
    capture(snapshot, media_budget=budget)
    clock[0] += 100
    capture(snapshot, media_budget=budget)
    assert len(attempted) == 1
    capture(snapshot)
    assert len(attempted) == 2


def test_partial_message_due_to_budget_keeps_files_and_does_not_recapture(capture, grab_images):
    capture([])
    snapshot = [image_message(count=4)]
    assert capture(snapshot)["pending_media"] == 1
    original = all_events()[0]
    assert grab_images.call_args.args[-1] == [0, 1, 2]
    assert capture(snapshot)["pending_media"] == 0
    assert grab_images.call_args.args[-1] == [3]
    assert all_events()[0]["client_event_id"] == original["client_event_id"]
    assert len(all_events()[0]["media"]) == 4
    assert edge_state.media_files_ready(all_events()[0]["media"])


def test_capture_deadline_defers_remaining_images_but_preserves_later_text(capture, grab_images, monkeypatch):
    capture([])
    now = 100.0
    monkeypatch.setattr(edge_worker.time, "monotonic", lambda: now)

    def slow_capture(*args, **kwargs):
        nonlocal now
        now += 15
        raise edge_worker.macos_backend.CaptureDeadlineExceeded("media_time_budget_exhausted")

    grab_images.side_effect = slow_capture
    snapshot = [image_message(), message("anchor"), image_message(row_id="7551:1000:row-2"), message("later text")]
    budget = edge_worker.MediaCaptureBudget()
    assert capture(snapshot, media_budget=budget)["captured"] == 4
    assert grab_images.call_count == 1
    assert budget.elapsed == 15
    with edge_message_ledger.transaction() as conn:
        rows = list(conn.execute("SELECT attempts FROM edge_media_capture_state ORDER BY rowid"))
    assert [row[0] for row in rows] == [1, 0]
    channel = DeferredChannel()
    assert edge_worker.flush_registrations(channel)["registered"] == 4
    assert channel.registrations[-1]["message"]["text"] == "later text"


def test_media_retry_limit_survives_restart_and_explicit_resume_preserves_event(capture, grab_images, monkeypatch):
    from click.testing import CliRunner
    from cli_anything.wecom_gui.wecom_gui_cli import cli

    capture([])
    now = edge_worker.time.time()
    monkeypatch.setattr(edge_worker.time, "time", lambda: now)
    successful = grab_images.side_effect
    grab_images.side_effect = RuntimeError("private detail must not escape")
    snapshot = [image_message(direction="inbound")]
    for _ in range(8):
        capture(snapshot)
        now += 65
    original = all_events()[0]
    assert grab_images.call_count == 5
    status = edge_state.edge_status()
    assert status["media_capture_pending"] == status["media_capture_paused"] == 1
    assert status["paused_media_tasks"] == [{"event_id": original["payload"]["message"]["id"],
                                             "attempts": 5, "last_error": "media_capture:RuntimeError"}]
    with edge_message_ledger.transaction() as conn:
        row = dict(conn.execute("SELECT * FROM edge_message_ledger").fetchone())
    assert row["direction"] == "inbound"  # Failure must not restore the reserved unknown direction.
    assert not edge_message_ledger.begin_media_attempt(row)
    result = CliRunner().invoke(cli, ["--json", "edge-channel", "resume-media", row["event_id"]])
    assert result.exit_code == 0, result.output
    assert "media_retry_resumed" in result.output
    grab_images.side_effect = successful
    capture(snapshot)
    repaired = all_events()[0]
    assert repaired["client_event_id"] == original["client_event_id"]
    assert repaired["payload"]["occurred_at"] == original["payload"]["occurred_at"]
    assert edge_state.media_files_ready(repaired["media"])
    assert edge_state.edge_status()["media_capture_paused"] == 0


def test_crashes_consume_attempts_before_gui_work(capture, monkeypatch):
    capture([])
    now = edge_worker.time.time()
    monkeypatch.setattr(edge_worker.time, "time", lambda: now)
    snapshot = [image_message()]
    # Reserve the event and image identity without doing GUI work.
    capture(snapshot, media_budget=edge_worker.MediaCaptureBudget(image_limit=0))
    with edge_message_ledger.transaction() as conn:
        entry = dict(conn.execute("SELECT * FROM edge_message_ledger").fetchone())
    for _ in range(5):
        assert edge_message_ledger.begin_media_attempt(entry)
        assert not edge_message_ledger.begin_media_attempt(entry)
        now += 65
        # No save_media call: simulate abrupt termination, then a new connection.
    assert not edge_message_ledger.begin_media_attempt(entry)
    assert edge_state.edge_status()["paused_media_tasks"][0]["last_error"] == "media_capture_interrupted"


@pytest.mark.parametrize("same_pixels", [True, False])
def test_restarted_wecom_can_only_rebind_an_image_with_pinned_pixels(capture, monkeypatch, same_pixels):
    capture([])
    original = image_message()
    capture([original], media_budget=edge_worker.MediaCaptureBudget(image_limit=0))
    with edge_message_ledger.transaction() as conn:
        entry = dict(conn.execute("SELECT * FROM edge_message_ledger").fetchone())
    restarted = image_message(row_id="8000:2000:new-row", fingerprint="rgb32-v1:image-1" if same_pixels else "rgb32-v1:new-image")
    if same_pixels:
        edge_message_ledger.verify_media_identity(entry, restarted, require_pixels=True)
        assert edge_message_ledger.media_state(entry)["capture_row_id"] == restarted["capture_row_id"]
    else:
        with pytest.raises(edge_message_ledger.MediaIdentityError, match="media_fingerprint_changed"):
            edge_message_ledger.verify_media_identity(entry, restarted, require_pixels=True)
        assert edge_message_ledger.media_state(entry)["capture_row_id"] == original["capture_row_id"]


def test_tick_keeps_command_polling_after_shared_image_budget_exhaustion(capture, grab_images, monkeypatch):
    capture([])
    snapshot = [image_message(), message("anchor"), image_message(row_id="7551:1000:row-2"),
                image_message(row_id="7551:1000:row-3"), image_message(row_id="7551:1000:row-4")]
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: {"messages": snapshot})
    row = {"title": "客户A", "external_user_id": "customer-1", "unread_count": 4}
    monkeypatch.setattr(edge_worker.inbox, "scan_visible", lambda **kwargs: {"conversations": [row]})
    monkeypatch.setattr(edge_worker.inbox, "is_customer_candidate", lambda row: True)
    monkeypatch.setattr(edge_worker.inbox, "open_row", lambda row: None)
    channel = DeferredChannel()
    channel.heartbeat = Mock()
    channel.pull_command = Mock(return_value=None)
    monkeypatch.setattr(edge_worker.edge_channel.ChannelConfig, "from_env", lambda: None)
    monkeypatch.setattr(edge_worker.edge_channel, "ChannelClient", lambda config: channel)
    reported = []
    monkeypatch.setattr(edge_worker.runtime_reporting, "publish", lambda *args, **kwargs: reported.append(kwargs))
    result = edge_worker.tick(pull_wait_seconds=0)
    assert result["ok"]
    assert grab_images.call_count == 3
    assert len(channel.registrations) == 5
    channel.pull_command.assert_called_once_with(wait_seconds=0)
    assert result["state"]["media_capture_pending"] == 1
    assert reported[-1]["metrics"]["capture_images"] == 3
