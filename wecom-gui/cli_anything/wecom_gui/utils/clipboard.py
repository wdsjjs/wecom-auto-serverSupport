"""macOS clipboard helpers."""

from __future__ import annotations

import subprocess


def set_clipboard(text: str) -> None:
    """Set the macOS clipboard to text using pbcopy."""
    proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "pbcopy failed")


def get_clipboard() -> str:
    """Read the macOS clipboard using pbpaste."""
    proc = subprocess.run(["pbpaste"], text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "pbpaste failed")
    return proc.stdout
