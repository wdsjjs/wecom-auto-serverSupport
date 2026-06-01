"""Reply helpers for WeCom GUI automation."""

from __future__ import annotations

from cli_anything.wecom_gui.utils import macos_backend


def send_text(text: str, *, dry_run: bool = False, submit: bool = True) -> dict:
    """Paste and optionally submit a reply to the currently focused chat."""
    if dry_run:
        return {"ok": True, "dry_run": True, "submitted": False, "chars": len(text), "text": text}
    try:
        result = macos_backend.send_via_ax_text_input(text, submit=submit)
    except RuntimeError as exc:
        result = macos_backend.paste_and_enter(text, submit=submit)
        result["method"] = "clipboard_fallback"
        result["fallback_reason"] = str(exc)
    result["dry_run"] = False
    return result
