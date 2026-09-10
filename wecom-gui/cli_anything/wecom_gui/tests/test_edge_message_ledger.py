from __future__ import annotations

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


def image_message(*, direction="unknown", count=1):
    return {**message("[图片]", direction=direction), "media": [
        {"type": "image", "rect": {"x": 10, "y": 20, "width": 80, "height": 80},
         "chat_viewport": {"x": 0, "y": 0, "width": 500, "height": 500}}
        for _ in range(count)]}


@pytest.fixture
def grab_images(monkeypatch, tmp_path):
    def grab(row, candidates, index, missing):
        # Capturing must not hold a SQLite write lock.
        with edge_state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
        result = []
        for i in missing:
            path = tmp_path / f"image-{i}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage content")
            result.append({"type": "image", "capture_path": str(path), "capture_ok": True})
        return result
    mock = Mock(side_effect=grab)
    monkeypatch.setattr(edge_worker, "_capture_snapshot_image", mock)
    return mock


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

    def partial(row, candidates, index, missing):
        return [successful(row, candidates, index, [0])[0],
                {"type": "image", "error": "preview_not_found"}]
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
    assert result[0]["capture_path"] == "test.png"
    assert grab.call_args.kwargs == {"cache_preview_failures": False}


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
