"""Reply helpers for WeCom GUI automation."""

from __future__ import annotations

from pathlib import Path

from cli_anything.wecom_gui.utils import macos_backend


def send_text(
    text: str,
    *,
    dry_run: bool = False,
    submit: bool = True,
    allow_clipboard_fallback: bool = True,
) -> dict:
    """Paste and optionally submit a reply to the currently focused chat."""
    if dry_run:
        return {"ok": True, "dry_run": True, "submitted": False, "chars": len(text), "text": text}
    try:
        result = macos_backend.send_via_ax_text_input(text, submit=submit)
    except RuntimeError as exc:
        if not allow_clipboard_fallback:
            raise
        result = macos_backend.paste_and_enter(text, submit=submit)
        result["method"] = "clipboard_fallback"
        result["fallback_reason"] = str(exc)
    result["dry_run"] = False
    return result


def send_message(
    text: str,
    *,
    attachments: list[dict] | None = None,
    dry_run: bool = False,
    submit: bool = True,
    allow_clipboard_fallback: bool = True,
) -> dict:
    """Send text plus optional local image attachments to the focused chat."""
    files = [item for item in (attachments or []) if str(item.get("type") or "image") == "image"]
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "submitted": False,
            "chars": len(text),
            "attachment_count": len(files),
            "text": text,
        }
    if not files:
        result = send_text(
            text,
            dry_run=False,
            submit=submit,
            allow_clipboard_fallback=allow_clipboard_fallback,
        )
        if result is None:
            result = {"ok": True, "submitted": submit, "chars": len(text)}
        result["attachment_count"] = 0
        return result

    text_result = send_text(text, dry_run=False, submit=False,
                            allow_clipboard_fallback=allow_clipboard_fallback) if text.strip() else {"ok": True, "chars": 0}
    if text_result is None:
        text_result = {"ok": True, "submitted": False, "chars": len(text)}
    file_results = []
    for index, item in enumerate(files):
        path = Path(str(item.get("path") or "")).expanduser()
        file_results.append(macos_backend.paste_file_and_enter(path, submit=submit and index == len(files) - 1))
    return {
        "ok": all(result.get("ok") for result in file_results) and bool(text_result.get("ok", True)),
        "dry_run": False,
        "submitted": submit,
        "chars": len(text),
        "attachment_count": len(files),
        "text_result": text_result,
        "file_results": file_results,
    }
