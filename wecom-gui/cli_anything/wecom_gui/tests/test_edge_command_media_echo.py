from __future__ import annotations

import sqlite3

import pytest

from cli_anything.wecom_gui.core import edge_state, edge_worker, state
from cli_anything.wecom_gui.tests.test_edge_channel import FakeChannel, _command


def bubble(row_id, *, text="[图片]", image=True, side="right"):
    return {
        "capture_row_id": row_id, "text": text, "role": "客服" if side == "right" else "用户",
        "role_confidence": "high", "media": [{"type": "image"}] if image else [],
        "direction_evidence": {"source": "screencapturekit", "status": "matched", "side": side},
    }


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_GUI_SEND_ECHO_ATTEMPTS", "2")
    monkeypatch.setenv("WECOM_GUI_SEND_ECHO_RETRY_DELAY", "0")
    row = {"title": "客户A", "external_user_id": "customer-1"}
    monkeypatch.setattr(edge_worker.inbox, "open_row", lambda target: None)
    monkeypatch.setattr(edge_worker.macos_backend, "selected_conversation_row", lambda **kwargs: row)
    monkeypatch.setattr(edge_worker, "_current_identity_for_row", lambda target: ("customer-1", ""))
    monkeypatch.setattr(edge_worker.macos_backend, "send_input_ready", lambda: {"ok": True, "input": {"valueLength": 0}})
    monkeypatch.setattr(edge_worker, "_download_attachments", lambda client, cmd: [{"type": "image", "path": "image.png"}])
    sends = []
    monkeypatch.setattr(edge_worker.reply, "send_message", lambda *args, **kwargs: sends.append((args, kwargs)))
    snapshot = {"messages": [bubble("old", text="question", image=False, side="left")]}
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: snapshot)
    edge_worker.collect_visible_conversation_once()
    return snapshot, sends


@pytest.mark.parametrize("text", ["", "reply"])
def test_complete_echo_is_persisted_and_not_recaptured_after_result_retry(runtime, monkeypatch, text):
    snapshot, sends = runtime
    command = _command()
    command.update(text=text, media=[{"id": "media-1"}])
    edge_state.record_command(command)
    edge_state.mark_command_executing(command["command_id"])
    before = list(snapshot["messages"])
    outgoing = ([bubble("sent-text", text=text, image=False)] if text else []) + [bubble("sent-image")]
    reads = iter([{"messages": before}, {"messages": before + outgoing}])
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: next(reads))
    result = edge_worker.execute_command(FakeChannel(), command)
    assert result["status"] == "succeeded"
    assert len(result["_echo_rows"]) == len(outgoing)
    assert len(sends) == 1
    edge_state.save_command_result(command["command_id"], result)
    # A delayed/lost central receipt does not cause local echo uploads.
    edge_state.retry_command_result(command["command_id"], "offline", delay_seconds=0)
    receipt = edge_state.due_command_results()[0]
    assert "_echo_rows" not in receipt["result"]
    assert receipt["result_attempts"] == 1
    # Reopen SQLite as on restart, past the old 30-second text suppression TTL.
    monkeypatch.setattr(edge_state.time, "time", lambda: receipt["executed_at"] + 60)
    snapshot["messages"] = before + outgoing
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: snapshot)
    captures = []
    monkeypatch.setattr(edge_worker, "_prepare_snapshot_media", lambda *args: captures.append(args))
    for _ in range(2):
        assert edge_worker.collect_visible_conversation_once()["captured"] == 0
    assert captures == []
    with edge_state._connect() as conn:
        assert conn.execute("SELECT count(*) FROM edge_inbound_events").fetchone()[0] == 0

    # A customer's image and a later manual resend have different AX identities.
    snapshot["messages"] += [bubble("customer-image", side="left"), bubble("manual-image")]
    assert edge_worker.collect_visible_conversation_once()["captured"] == 2
    with edge_state._connect() as conn:
        rows = [edge_state._event_row(row) for row in conn.execute("SELECT * FROM edge_inbound_events ORDER BY id")]
    assert [row["payload"]["message"]["direction"] for row in rows] == ["inbound", "outbound"]
    assert len(captures) == 2


def test_delayed_image_waits_for_complete_echo_and_sends_only_once(runtime, monkeypatch):
    snapshot, sends = runtime
    before = snapshot["messages"]
    command = _command()
    command.update(text="reply", media=[{"id": "media-1"}])
    text = bubble("sent-text", text="reply", image=False)
    reads = iter([{"messages": before}, {"messages": before + [text]},
                  {"messages": before + [text, bubble("sent-image")]}])
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: next(reads))
    result = edge_worker.execute_command(FakeChannel(), command)
    assert result["status"] == "succeeded"
    assert len(result["_echo_rows"]) == 2
    assert len(sends) == 1


def test_text_echo_alone_cannot_confirm_or_reconcile_a_media_command(runtime, monkeypatch):
    snapshot, sends = runtime
    before = snapshot["messages"]
    command = _command()
    command.update(text="reply", media=[{"id": "media-1"}])
    edge_state.record_command(command)
    text = bubble("sent-text", text="reply", image=False)
    reads = iter([{"messages": before}] + [{"messages": before + [text]}] * 2)
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: next(reads))
    result = edge_worker.execute_command(FakeChannel(), command)
    assert result == {"status": "needs_reconciliation", "reason": "complete_media_reply_not_visible_after_send"}
    assert len(sends) == 1
    edge_state.save_command_result(command["command_id"], result)
    assert not edge_worker.reconcile_command_echo({"command_id": command["command_id"], "payload": command})["confirmed"]
    with edge_state._connect() as conn:
        assert conn.execute("SELECT count(*) FROM edge_command_echo_rows").fetchone()[0] == 0


def test_observed_images_after_send_exception_are_bound_without_resending(runtime, monkeypatch):
    snapshot, sends = runtime
    before = snapshot["messages"]
    command = _command()
    command.update(text="", media=[{"id": "media-1"}])

    def send(*args, **kwargs):
        sends.append(args)
        raise RuntimeError("native error after Enter")

    monkeypatch.setattr(edge_worker.reply, "send_message", send)
    reads = iter([{"messages": before}, {"messages": before + [bubble("sent-image")]}])
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: next(reads))
    result = edge_worker.execute_command(FakeChannel(), command)
    assert result["verification"] == "visible_after_send_error"
    assert len(result["_echo_rows"]) == 1
    assert len(sends) == 1


@pytest.mark.parametrize("mutation", ["old_image", "no_id", "unknown", "extra_staff", "different_text", "broken_anchor", "duplicate_id"])
def test_media_echo_rejects_ambiguous_or_incomplete_suffix(mutation):
    before = [bubble("old", text="anchor", image=False), bubble("old-image")]
    after = before + [bubble("text", text="reply", image=False), bubble("new-image")]
    if mutation == "old_image":
        after.pop()
    elif mutation == "no_id":
        after[-1].pop("capture_row_id")
    elif mutation == "unknown":
        after[-1]["direction_evidence"] = {"status": "outside_viewport"}
    elif mutation == "extra_staff":
        after += [bubble("another-image")]
    elif mutation == "different_text":
        after[-2]["text"] = "different reply"
    elif mutation == "broken_anchor":
        after = after[2:]
    elif mutation == "duplicate_id":
        after[-1]["capture_row_id"] = "old-image"
    assert edge_worker._media_command_echo_rows(before, after, "reply", 1) == []


def test_multiple_images_and_interleaved_customer_message_keep_exact_rows():
    before = [bubble("old", image=False, text="anchor")]
    after = before + [bubble("text", image=False, text="reply"), bubble("one"),
                      bubble("customer", side="left"), bubble("two")]
    rows = edge_worker._media_command_echo_rows(before, after, "reply", 2)
    assert [row["capture_row_id"] for row in rows] == ["text", "one", "two"]


def test_same_image_can_belong_to_two_distinct_send_commands(runtime):
    first = bubble("first-image")
    second = bubble("second-image")
    before = runtime[0]["messages"]
    for command_id, previous, following in [("first", before, before + [first]),
                                           ("second", before + [first], before + [first, second])]:
        command = _command()
        command.update(command_id=command_id, text="", media=[{"id": "same-image"}])
        edge_state.record_command(command)
        echoes = edge_worker._media_command_echo_rows(previous, following, "", 1)
        assert len(echoes) == 1
        edge_state.save_command_result(command_id, {"status": "succeeded", "_echo_rows": echoes})
    with edge_state._connect() as conn:
        assert conn.execute("SELECT count(DISTINCT command_id) FROM edge_command_echo_rows").fetchone()[0] == 2


def test_echo_binding_is_conversation_scoped_and_commits_with_receipt(runtime):
    command = _command()
    edge_state.record_command(command)
    image = bubble("sent-image")
    signature = edge_worker._snapshot_match_key(image)
    result = {"status": "succeeded", "_echo_rows": [{"capture_row_id": "sent-image", "match_key": signature}]}
    with edge_state._connect() as conn:
        conn.execute("""CREATE TRIGGER fail_receipt BEFORE UPDATE ON edge_command_receipts
                     BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        edge_state.save_command_result(command["command_id"], result)
    with edge_state._connect() as conn:
        assert conn.execute("SELECT count(*) FROM edge_command_echo_rows").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_receipt")
    edge_state.save_command_result(command["command_id"], result)
    with edge_state._connect() as conn:
        assert edge_state.is_command_echo_row("uid:customer-1", "sent-image", signature, connection=conn)
        assert not edge_state.is_command_echo_row("uid:customer-2", "sent-image", signature, connection=conn)
        assert not edge_state.is_command_echo_row("uid:customer-1", "sent-image", "changed", connection=conn)
