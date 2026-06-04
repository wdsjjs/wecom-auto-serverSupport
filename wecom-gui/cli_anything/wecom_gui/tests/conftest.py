from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_message_debounce_by_default(monkeypatch):
    monkeypatch.setenv("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "0")


@pytest.fixture(autouse=True)
def _disable_real_wecom_uid_lookup(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.current_external_user_id",
        lambda: "",
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.agent._selected_conversation_after_open",
        lambda limit=30: None,
    )


@pytest.fixture(autouse=True)
def _disable_real_input_ready_lookup(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.agent._ensure_chat_input_ready_for_job",
        lambda job, stage: {"ok": True, "input": {"x": 1}, "sidebar": {"ok": True}},
    )
