"""Application-level WeCom desktop controls."""

from __future__ import annotations

from dataclasses import asdict

from cli_anything.wecom_gui.utils import macos_backend


def doctor() -> dict:
    """Return environment status for GUI automation."""
    return asdict(macos_backend.doctor_status())


def focus(app_name: str | None = None) -> dict:
    """Focus the WeCom desktop app."""
    return macos_backend.activate_app(app_name)
