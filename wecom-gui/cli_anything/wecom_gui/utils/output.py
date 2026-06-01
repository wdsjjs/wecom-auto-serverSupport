"""Output helpers for human and JSON modes."""

from __future__ import annotations

import json
from typing import Any

import click

USE_JSON = False


def emit(data: Any, message: str | None = None) -> None:
    """Emit data in JSON mode or a compact human-readable format."""
    if USE_JSON:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    if message:
        click.echo(message)
    if data is None:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            click.echo(f"{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            click.echo(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    else:
        click.echo(str(data))


def error(message: str, *, code: str = "error") -> None:
    """Emit an error respecting JSON mode."""
    if USE_JSON:
        click.echo(json.dumps({"ok": False, "error": message, "code": code}, ensure_ascii=False))
    else:
        click.echo(f"Error: {message}", err=True)
