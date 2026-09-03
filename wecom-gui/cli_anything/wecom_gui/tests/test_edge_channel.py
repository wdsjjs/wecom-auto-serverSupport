from __future__ import annotations

import pytest

from cli_anything.wecom_gui.core import edge_channel, edge_state, edge_worker


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


def _inbound_payload() -> dict:
    return {
        "event_type": "inbound_message",
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
    assert edge_state.edge_status()["inbound_pending"] == 0


def test_capture_unread_text_and_image_to_durable_spool(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
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
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.ensure_input_ready", lambda: {"ok": True})
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


def test_command_without_expected_message_identifiers_is_precondition_failure(monkeypatch):
    command = _command()
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda row: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "客户A", "external_user_id": "customer-1"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.ensure_input_ready", lambda: {"ok": True})
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda **kwargs: {"messages": [{"role": "用户", "text": "最新问题"}]},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.edge_worker.time.sleep", lambda _: None)

    result = edge_worker.execute_command(FakeChannel(), command)

    assert result == {"status": "precondition_failed", "reason": "expected_latest_message_mismatch"}


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
    monkeypatch.setenv("WECOM_CHANNEL_ACCOUNT_ID", "account-1")

    with pytest.raises(edge_channel.ChannelError, match="https"):
        edge_channel.ChannelConfig.from_env()
