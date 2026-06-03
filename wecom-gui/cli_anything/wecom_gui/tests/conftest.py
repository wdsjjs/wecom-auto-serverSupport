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
