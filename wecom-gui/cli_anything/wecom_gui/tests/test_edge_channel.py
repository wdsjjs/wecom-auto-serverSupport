from __future__ import annotations

import pytest

from cli_anything.wecom_gui.core import edge_channel, edge_state, edge_worker, state


class FakeChannel:
    def __init__(self, *, inbound_error: Exception | None = None):
        self.inbound_error = inbound_error
        self.inbound_calls: list[tuple[dict, list[dict]]] = []
        self.results: list[tuple[str, str, dict]] = []

    def post_inbound(self, event: dict, media: list[dict]) -> dict:
        self.inbound_calls.append((event, media))
        if self.inbound_error:
            raise self.inbound_error
        return {"accepted": True}

    def post_command_result(self, command_id: str, lease_id: str, result: dict) -> dict:
        self.results.append((command_id, lease_id, result))
        return {"accepted": True}


def test_inbound_png_uses_png_multipart_type_and_closes_file(tmp_path, monkeypatch):
    path = tmp_path / "capture.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage content")
    client = edge_channel.ChannelClient(edge_channel.ChannelConfig("https://example.test", "device", "test", False, 5))
    handles = []

    def request(method, endpoint, **kwargs):
        field, (name, handle, mime) = kwargs["files"][0]
        assert field == "media"
        assert name == "capture.png"
        assert mime == "image/png"
        assert handle.read() == path.read_bytes()
        handles.append(handle)
        return {"accepted": True}
    monkeypatch.setattr(client, "_json_request", request)
    payload = _inbound_payload()
    payload["message"]["media"] = [{"type": "image", "media_id": "media-1"}]
    client.post_inbound(payload, [{"capture_path": str(path)}])
    assert handles[0].closed


def test_registration_capability_is_explicit_and_registration_requires_ack(monkeypatch):
    client = edge_channel.ChannelClient(edge_channel.ChannelConfig("https://example.test", "device", "test", False, 5))
    monkeypatch.setattr(client, "_json_request", lambda *args, **kwargs: {"capabilities": {"deferredMediaV1": True}})
    client.heartbeat()
    assert client.supports_deferred_media
    monkeypatch.setattr(client, "_json_request", lambda *args, **kwargs: {"status": "active"})
    client.heartbeat()
    assert not client.supports_deferred_media
    with pytest.raises(edge_channel.ChannelError, match="not acknowledged"):
        client.register_message(_inbound_payload())


def test_attachment_only_request_requires_ready_ack_and_uses_original_message_id(tmp_path, monkeypatch):
    client = edge_channel.ChannelClient(edge_channel.ChannelConfig("https://example.test", "device", "test", False, 5))
    path = tmp_path / "test.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage bytes")
    event = _inbound_payload()
    event["message"]["media"] = [{"media_id": "media-1"}]
    state_response = "pending"
    def request(method, endpoint, **kwargs):
        assert endpoint == "/api/wecom-channel/edge/messages/edge-msg-1/media"
        assert kwargs["files"][0][0] == "media"
        return {"accepted": True, "message": {"mediaState": state_response}}
    monkeypatch.setattr(client, "_json_request", request)
    with pytest.raises(edge_channel.ChannelError, match="not acknowledged"):
        client.post_inbound(event, [{"capture_path": str(path)}], attachment_only=True)
    state_response = "ready"
    assert client.post_inbound(event, [{"capture_path": str(path)}], attachment_only=True)["accepted"]


def _inbound_payload() -> dict:
    return {
        "event_type": "inbound_message",
        "occurred_at": 1_788_282_000.0,
        "conversation": {"key": "uid:customer-1"},
        "message": {"id": "edge-msg-1", "hash": "message-hash", "text": "你好", "media": []},
    }


def _command(*, expires_at=None) -> dict:
    value = {
        "command_id": "cmd-1",
        "lease_id": "lease-1",
        "conversation": {"key": "uid:customer-1", "external_user_id": "customer-1", "title": "客户A"},
        "text": "您好，请问有什么可以帮您？",
    }
    if expires_at is not None:
        value["expires_at"] = expires_at
    return value


def _match_sidebar_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.edge_worker._current_identity_for_row",
        lambda row: (str(row.get("external_user_id") or "customer-1"), ""),
    )


@pytest.mark.parametrize("submitted,status", [
    (False, "precondition_failed"), (None, "needs_reconciliation"), (True, "needs_reconciliation"),
])
def test_text_submit_failure_keeps_specific_reason_and_never_retries(monkeypatch, tmp_path, submitted, status):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(edge_worker.inbox, "open_row", lambda row: None)
    monkeypatch.setattr(edge_worker, "_wait_for_opened_conversation", lambda row: True)
    monkeypatch.setattr(edge_worker.macos_backend, "send_input_ready", lambda: {"ok": True, "input": {"valueLength": 0}})
    reads = []
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: reads.append(kwargs) or {"messages": []})
    sends = []

    def fail_send(*args, **kwargs):
        sends.append(args)
        raise edge_worker.macos_backend.TextSendError("chat_input_focus_not_confirmed", submitted=submitted)

    monkeypatch.setattr(edge_worker.reply, "send_message", fail_send)
    result = edge_worker.execute_command(FakeChannel(), _command())
    assert result == {"status": status, "reason": "text_send:chat_input_focus_not_confirmed"}
    assert len(sends) == 1
    assert len(reads) == (1 if submitted is False else 2)


def test_unreadable_input_is_not_treated_as_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(edge_worker.inbox, "open_row", lambda row: None)
    monkeypatch.setattr(edge_worker, "_wait_for_opened_conversation", lambda row: True)
    monkeypatch.setattr(edge_worker.macos_backend, "send_input_ready", lambda: {
        "ok": False, "error": "chat_input_value_unavailable", "input": {},
    })
    monkeypatch.setattr(edge_worker.reply, "send_message", lambda *args, **kwargs: pytest.fail("must not send"))
    assert edge_worker.execute_command(FakeChannel(), _command()) == {
        "status": "precondition_failed", "reason": "chat_input_value_unavailable",
    }


def test_inbound_spool_reuses_persisted_event_id_and_retries(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    inserted, first = edge_state.enqueue_inbound(
        dedupe_key="uid:customer-1:message-hash", payload=_inbound_payload(), media=[]
    )
    inserted_again, duplicate = edge_state.enqueue_inbound(
        dedupe_key="uid:customer-1:message-hash", payload=_inbound_payload(), media=[]
    )

    assert inserted is True
    assert inserted_again is False
    assert duplicate["client_event_id"] == first["client_event_id"]

    failing = FakeChannel(inbound_error=RuntimeError("offline"))
    assert edge_worker.flush_inbound(failing) == {"delivered": 0, "failed": 1}
    assert edge_state.due_inbound() == []

    edge_state.retry_inbound(first["client_event_id"], "retry-now", delay_seconds=0)
    healthy = FakeChannel()
    assert edge_worker.flush_inbound(healthy) == {"delivered": 1, "failed": 0}
    assert healthy.inbound_calls[0][0]["client_event_id"] == first["client_event_id"]
    assert healthy.inbound_calls[0][0]["occurred_at"] == "2026-09-01T17:00:00+00:00"
    assert edge_state.edge_status()["inbound_pending"] == 0


def test_capture_unread_text_and_image_to_durable_spool(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    image_path = tmp_path / "captured.jpg"
    image_path.write_bytes(b"image-data")
    row = {
        "title": "客户A",
        "preview": "请看图片",
        "unread": True,
        "unread_count": 1,
        "external_user_id": "customer-1",
        "tags": ["@微信"],
    }
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.inbox.scan_visible", lambda limit: {"conversations": [row]}
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda target: target == row)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda **kwargs: {
            "hash": "visible-chat-hash",
            "messages": [{"role": "用户", "text": "请看图片", "media": [{"type": "image", "capture_path": str(image_path)}]}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.collect_inbound_once()
    event = edge_state.due_inbound()[0]

    assert result["captured"] == 1
    assert event["payload"]["conversation"]["external_user_id"] == "customer-1"
    assert event["payload"]["message"]["text"] == "请看图片"
    assert event["media"][0]["capture_path"] == str(image_path)
    assert event["payload"]["message"]["media"][0]["sha256"]


def test_capture_all_customer_messages_in_one_unresolved_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    row = {
        "title": "客户A",
        "preview": "第三句",
        "unread": True,
        "unread_count": 3,
        "external_user_id": "customer-1",
        "tags": ["@微信"],
    }
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.scan_visible", lambda limit: {"conversations": [row]})
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda target: target == row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda **kwargs: {
            "hash": "visible-chat-hash",
            "messages": [
                {"role": "客服", "text": "您好", "media": []},
                {"role": "用户", "text": "第一句", "media": []},
                {"role": "用户", "text": "第一句", "media": []},
                {"role": "用户", "text": "第三句", "media": []},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.collect_inbound_once()
    events = edge_state.due_inbound(limit=10)

    assert result["captured"] == 3
    assert [event["payload"]["message"]["text"] for event in events] == ["第一句", "第一句", "第三句"]
    assert len({event["client_event_id"] for event in events}) == 3


def test_visible_staff_message_remains_unknown_without_a_central_delivery_echo(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    monkeypatch.setenv("WECOM_GUI_CAPTURE_OUTBOUND", "1")
    row = {"title": "客户A", "external_user_id": "customer-1"}
    reads = iter([
        {"hash": "baseline", "messages": []},
        {"hash": "new", "messages": [
            {"role": "客服", "text": "人工新回复"},
        ]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))

    first = edge_worker.collect_visible_outbound_once()
    second = edge_worker.collect_visible_outbound_once()
    events = edge_state.due_inbound(limit=10)

    assert first["captured"] == 0
    assert second["captured"] == 1
    assert events[0]["payload"]["event_type"] == "unknown_message"
    assert events[0]["payload"]["message"]["direction"] == "unknown"
    assert events[0]["payload"]["message"]["text"] == "人工新回复"


def test_visual_staff_direction_does_not_resolve_without_a_central_delivery_echo(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    monkeypatch.setenv("WECOM_GUI_CAPTURE_OUTBOUND", "1")
    row = {"title": "客户A", "external_user_id": "customer-1"}
    reads = iter([
        {"hash": "baseline", "messages": []},
        {"hash": "unknown", "messages": [
            {"role": "用户", "role_confidence": "low", "text": "等待方向确认", "time": "10:00"},
        ]},
        {"hash": "resolved", "messages": [
            {"role": "客服", "role_confidence": "high", "text": "等待方向确认", "time": "10:00"},
        ]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))

    edge_worker.collect_visible_conversation_once()
    uncertain = edge_worker.collect_visible_conversation_once()
    unknown_event = edge_state.due_inbound(limit=10)[0]

    assert uncertain["captured"] == 1
    assert uncertain["pending_direction"] == 1
    assert unknown_event["payload"]["message"]["direction"] == "unknown"
    edge_state.mark_inbound_delivered(unknown_event["client_event_id"])

    resolved = edge_worker.collect_visible_conversation_once()
    events = edge_state.due_inbound(limit=10)

    assert resolved["captured"] == 0
    assert resolved["pending_direction"] == 1
    assert events == []


@pytest.mark.parametrize("side,direction", [("left", "inbound"), ("right", "outbound")])
def test_pixel_verified_direction_resolves_same_event_without_a_new_message(monkeypatch, tmp_path, side, direction):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    row = {"title": "客户A", "external_user_id": "customer-1"}
    monkeypatch.setattr(edge_worker.macos_backend, "selected_conversation_row", lambda limit=30: row)
    unknown = {"role": "unknown", "role_confidence": "low", "text": "待核对消息", "time": "10:00"}
    verified = {**unknown, "role": "用户" if side == "left" else "客服", "role_confidence": "high",
                "direction_evidence": {"source": "screencapturekit", "status": "matched", "side": side}}
    reads = iter([
        {"hash": "baseline", "messages": []},
        {"hash": "unchanged-text", "messages": [unknown]},
        {"hash": "unchanged-text", "messages": [verified]},
        {"hash": "unchanged-text", "messages": [verified]},
    ])
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: next(reads))
    edge_worker.collect_visible_conversation_once()
    edge_worker.collect_visible_conversation_once()
    first = edge_state.due_inbound()[0]
    assert first["payload"]["message"]["direction"] == "unknown"
    edge_state.mark_inbound_delivered(first["client_event_id"])
    result = edge_worker.collect_visible_conversation_once()
    resolved = edge_state.due_inbound()[0]
    assert result["pending_direction"] == 0
    assert resolved["client_event_id"] == first["client_event_id"]
    assert resolved["payload"]["message"]["id"] == first["payload"]["message"]["id"]
    assert resolved["payload"]["message"]["direction"] == direction
    edge_state.mark_inbound_delivered(resolved["client_event_id"])
    assert edge_worker.collect_visible_conversation_once()["captured"] == 0
    assert edge_state.due_inbound() == []


@pytest.mark.parametrize("times", [("", ""), ("10:00", "10:01")])
def test_identical_customer_and_staff_bubbles_are_both_kept(monkeypatch, tmp_path, times):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    row = {"title": "客户A", "external_user_id": "customer-1"}
    monkeypatch.setattr(edge_worker.macos_backend, "selected_conversation_row", lambda limit=30: row)
    messages = [
        {"text": "一样的正文", "time": stamp, "role": role, "role_confidence": "high",
         "direction_evidence": {"source": "screencapturekit", "status": "matched", "side": side}}
        for (role, side), stamp in zip([("用户", "left"), ("客服", "right")], times)
    ]
    reads = iter([{"messages": []}, {"messages": messages}, {"messages": messages}])
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: next(reads))
    edge_worker.collect_visible_conversation_once()
    assert edge_worker.collect_visible_conversation_once()["captured"] == 2
    events = edge_state.due_inbound()
    assert [event["payload"]["message"]["direction"] for event in events] == ["inbound", "outbound"]
    assert len({event["payload"]["message"]["id"] for event in events}) == 2
    assert edge_worker.collect_visible_conversation_once()["captured"] == 0


def test_verified_staff_history_only_establishes_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    row = {"title": "客户A", "external_user_id": "customer-1"}
    monkeypatch.setattr(edge_worker.macos_backend, "selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr(edge_worker.chat, "read_current", lambda **kwargs: {"messages": [
        {"text": "历史人工回复", "role": "客服", "role_confidence": "high",
         "direction_evidence": {"source": "screencapturekit", "status": "matched", "side": "right"}},
    ]})
    assert edge_worker.collect_visible_conversation_once()["captured"] == 0
    assert edge_state.due_inbound() == []


def test_visible_customer_message_is_uploaded_when_the_open_chat_is_not_unread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    row = {"title": "客户A", "external_user_id": "customer-1", "unread": False}
    reads = iter([
        {"hash": "baseline", "messages": [{"role": "客服", "text": "历史回复"}]},
        {"hash": "new", "messages": [
            {"role": "客服", "text": "历史回复"},
            {"role": "用户", "text": "当前打开会话的新消息"},
        ]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))

    edge_worker.collect_visible_conversation_once()
    result = edge_worker.collect_visible_conversation_once()
    events = edge_state.due_inbound(limit=10)

    assert result["captured"] == 1
    assert events[0]["payload"]["event_type"] == "inbound_message"
    assert events[0]["payload"]["message"]["direction"] == "inbound"
    assert events[0]["payload"]["message"]["text"] == "当前打开会话的新消息"


def test_visible_capture_retries_selected_row_after_activating_background_wecom(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    _match_sidebar_identity(monkeypatch)
    row = {"title": "客户A", "external_user_id": "customer-1"}
    selected_rows = iter([None, row])
    activated = []
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: next(selected_rows),
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.activate_app", lambda: activated.append(True))
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: {"messages": []})

    result = edge_worker.collect_visible_conversation_once()

    assert result["captured"] == 0
    assert activated == [True]


def test_visible_capture_refuses_sidebar_identity_that_does_not_match_selected_row(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "external_user_id": "customer-a"}
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.edge_worker._current_identity_for_row",
        lambda _row: ("", "sidebar_identity_title_mismatch"),
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("chat must not be read for an identity mismatch")),
    )

    result = edge_worker.collect_visible_conversation_once()

    assert result == {"ok": True, "captured": 0, "reason": "sidebar_identity_title_mismatch"}
    assert edge_state.due_inbound(limit=10) == []


def test_visible_capture_uses_an_explicitly_verified_local_uid_binding(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    state.bind_wecom_customer(
        uid="customer-1",
        customer_name="客户A",
        source="wecom-sidebar-jsapi",
    )
    row = {"title": "客户A"}
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.sidebar_identity", lambda: {"external_user_id": ""})
    reads = iter([
        {"messages": []},
        {"messages": [{"role": "用户", "text": "来自已绑定客户的新消息"}]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))

    edge_worker.collect_visible_conversation_once()
    result = edge_worker.collect_visible_conversation_once()

    assert result["captured"] == 1
    event = edge_state.due_inbound(limit=10)[0]
    assert event["payload"]["conversation"]["external_user_id"] == "customer-1"


def test_untrusted_name_binding_does_not_replace_sidebar_identity(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    state.bind_wecom_customer(uid="customer-1", customer_name="客户A", source="imported-unknown")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.sidebar_identity", lambda: {"external_user_id": ""})

    uid, error = edge_worker._current_identity_for_row({"title": "客户A"})

    assert uid == ""
    assert error == "sidebar_external_user_id_missing"


def test_command_echo_is_not_uploaded_twice_as_manual_staff_message(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_GUI_CAPTURE_OUTBOUND", "1")
    row = {"title": "客户A", "external_user_id": "customer-1"}
    reads = iter([
        {"hash": "baseline", "messages": []},
        {"hash": "echo", "messages": [{"role": "客服", "text": "中台发送"}]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))

    edge_worker.collect_visible_outbound_once()
    edge_state.register_outbound_echo_suppression("cmd-1", "uid:customer-1", "中台发送")
    result = edge_worker.collect_visible_outbound_once()

    assert result["captured"] == 0
    assert edge_state.due_inbound(limit=10) == []


def test_uncertain_command_is_reconciled_without_a_second_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    command = _command()
    edge_state.record_command(command)
    assert edge_state.mark_command_executing(command["command_id"])
    edge_state.save_command_result(command["command_id"], {"status": "needs_reconciliation", "reason": "reply_not_visible_after_send"})
    edge_state.mark_command_result_reported(command["command_id"])
    row = {"title": "客户A", "external_user_id": "customer-1"}
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda target: target == row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row", lambda limit=30: row)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "customer-1")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda **kwargs: {"messages": [{"role": "客服", "text": command["text"]}]},
    )

    result = edge_worker.reconcile_pending_command_echoes()

    assert result == {"checked": 1, "confirmed": 1}
    receipt = edge_state.due_command_results()[0]
    assert receipt["result"] == {"status": "succeeded", "verification": "reconciled_reply_visible"}


def test_unconfirmed_command_stops_background_reconciliation_after_the_limit(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    command = _command()
    edge_state.record_command(command)
    assert edge_state.mark_command_executing(command["command_id"])
    edge_state.save_command_result(command["command_id"], {"status": "needs_reconciliation"})
    edge_state.mark_command_result_reported(command["command_id"])
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.edge_worker.reconcile_command_echo",
        lambda receipt, **kwargs: {"confirmed": False, "reason": "reply_not_visible"},
    )
    monkeypatch.setenv("WECOM_GUI_RECONCILIATION_POLL_SECONDS", "0")
    monkeypatch.setenv("WECOM_GUI_RECONCILIATION_MAX_ATTEMPTS", "2")

    assert edge_worker.reconcile_pending_command_echoes() == {"checked": 1, "confirmed": 0}
    assert edge_worker.reconcile_pending_command_echoes() == {"checked": 1, "confirmed": 0}
    assert edge_worker.reconcile_pending_command_echoes() == {"checked": 0, "confirmed": 0}




def test_command_refuses_same_name_when_current_sidebar_uid_cannot_be_verified(monkeypatch):
    command = _command()
    command["expected_latest_message_id"] = "unused"
    command["expected_latest_message_hash"] = "unused"
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda row: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "客户A"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.execute_command(FakeChannel(), command)

    assert result == {"status": "precondition_failed", "reason": "opened_conversation_mismatch"}


def test_command_waits_for_target_conversation_selection(monkeypatch):
    command = _command()
    row = {"title": "客户A", "external_user_id": "customer-1", "conversation_key": "uid:customer-1"}
    selected_rows = iter([
        {"title": "其他客户", "external_user_id": "customer-2"},
        {"title": "客户A", "external_user_id": "customer-1"},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda target: target == row)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: next(selected_rows),
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.send_input_ready", lambda: {"ok": True, "input": {"valueLength": 0}})
    reads = iter([
        {"messages": [{"role": "用户", "text": "最新问题"}]},
        {"messages": [{"role": "用户", "text": "最新问题"}, {"role": "客服", "text": command["text"]}]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))
    monkeypatch.setattr("cli_anything.wecom_gui.core.reply.send_message", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.execute_command(FakeChannel(), command)

    assert result == {"status": "succeeded", "verification": "reply_visible"}


def test_duplicate_command_is_durable_but_never_becomes_executable_twice(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    command = _command()
    inserted, receipt = edge_state.record_command(command)
    inserted_again, duplicate = edge_state.record_command(command)

    assert inserted is True
    assert inserted_again is False
    assert duplicate["command_id"] == receipt["command_id"]
    assert edge_state.mark_command_executing("cmd-1") is True
    assert edge_state.mark_command_executing("cmd-1") is False


def test_expired_command_never_touches_gui(monkeypatch):
    command = _command(expires_at=1)
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda row: (_ for _ in ()).throw(AssertionError("must not open")))

    result = edge_worker.execute_command(FakeChannel(), command)

    assert result == {"status": "precondition_failed", "reason": "command_expired"}


def test_send_without_visible_confirmation_needs_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_GUI_SEND_ECHO_ATTEMPTS", "1")
    command = _command()
    row = {"title": "客户A", "external_user_id": "customer-1", "conversation_key": "uid:customer-1"}
    customer_message = {"role": "用户", "text": "物流到哪里了"}
    message_id, message_hash = edge_worker._message_identity("uid:customer-1", customer_message)
    command["expected_latest_message_id"] = message_id
    command["expected_latest_message_hash"] = message_hash

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda target: target == row)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "客户A", "external_user_id": "customer-1"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.send_input_ready", lambda: {"ok": True, "input": {"valueLength": 0}})
    reads = iter([
        {"hash": "before", "messages": [customer_message]},
        {"hash": "after", "messages": [customer_message]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))
    sends: list[str] = []
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, **kwargs: sends.append(text) or {"ok": True},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.execute_command(FakeChannel(), command)

    assert sends == [command["text"]]
    assert result == {"status": "needs_reconciliation", "reason": "reply_not_visible_after_send"}


def test_command_with_different_message_identifiers_still_sends_after_conversation_check(monkeypatch):
    command = _command()
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda row: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "客户A", "external_user_id": "customer-1"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.send_input_ready", lambda: {"ok": True, "input": {"valueLength": 0}})
    reads = iter([
        {"messages": [{"role": "用户", "text": "最新问题"}]},
        {"messages": [{"role": "用户", "text": "最新问题"}, {"role": "客服", "text": command["text"]}]},
    ])
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda **kwargs: next(reads))
    monkeypatch.setattr("cli_anything.wecom_gui.core.reply.send_message", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.execute_command(FakeChannel(), command)

    assert result == {"status": "succeeded", "verification": "reply_visible"}


def test_command_never_overwrites_an_existing_chat_draft(monkeypatch):
    command = _command()
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda row: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "客户A", "external_user_id": "customer-1"},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.send_input_ready",
        lambda: {"ok": True, "input": {"valueLength": 12}},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not overwrite draft")),
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.execute_command(FakeChannel(), command)

    assert result == {"status": "precondition_failed", "reason": "chat_input_not_empty"}


def test_command_result_is_persisted_before_report_and_retries(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    edge_state.record_command(_command())
    assert edge_state.mark_command_executing("cmd-1") is True
    edge_state.save_command_result("cmd-1", {"status": "succeeded", "verification": "reply_visible"})

    client = FakeChannel()
    assert edge_worker.flush_command_results(client) == {"reported": 1, "failed": 0}
    assert client.results == [("cmd-1", "lease-1", {"status": "succeeded", "verification": "reply_visible"})]
    assert edge_state.edge_status()["command_results_pending"] == 0


def test_channel_config_rejects_non_https(monkeypatch):
    monkeypatch.setenv("WECOM_CHANNEL_BASE_URL", "http://localhost:3000")
    monkeypatch.setenv("WECOM_CHANNEL_DEVICE_ID", "mac-1")
    monkeypatch.setenv("WECOM_CHANNEL_DEVICE_TOKEN", "token")

    with pytest.raises(edge_channel.ChannelError, match="https"):
        edge_channel.ChannelConfig.from_env()
