"""Crash-safe local spool for the WeCom desktop channel edge client.

This module deliberately owns tables separate from the legacy reply_queue.  The
central channel service is the business source of truth; this SQLite database
only makes GUI capture, command execution, and result delivery durable across
network/process failures.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from cli_anything.wecom_gui.core import state


PENDING = "pending"
DELIVERED = "delivered"
RESULT_PENDING = "result_pending"
RESULT_REPORTED = "result_reported"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(state.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_inbound_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_event_id TEXT NOT NULL UNIQUE,
            dedupe_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            media_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            delivered_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_edge_inbound_events_pending
        ON edge_inbound_events(status, next_attempt_at, id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_command_receipts (
            command_id TEXT PRIMARY KEY,
            lease_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            execution_status TEXT NOT NULL DEFAULT 'received',
            result_json TEXT,
            result_status TEXT NOT NULL DEFAULT '',
            result_attempts INTEGER NOT NULL DEFAULT 0,
            result_next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            received_at REAL NOT NULL,
            executed_at REAL,
            result_reported_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_edge_command_results_pending
        ON edge_command_receipts(result_status, result_next_attempt_at, received_at)
        """
    )


def _event_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    item["media"] = json.loads(item.pop("media_json") or "[]")
    return item


def _command_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    item["result"] = json.loads(item.pop("result_json") or "null")
    return item


def enqueue_inbound(*, dedupe_key: str, payload: dict, media: list[dict]) -> tuple[bool, dict]:
    """Persist a captured inbound event before any network request.

    A stable dedupe key means a scan after a crash reuses the original event
    rather than making a new event id for the same visible customer message.
    """
    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM edge_inbound_events WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        if existing is not None:
            return False, _event_row(existing)
        client_event_id = str(payload.get("client_event_id") or uuid.uuid4())
        complete_payload = {**payload, "client_event_id": client_event_id}
        conn.execute(
            """
            INSERT INTO edge_inbound_events
                (client_event_id, dedupe_key, payload_json, media_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                client_event_id,
                dedupe_key,
                json.dumps(complete_payload, ensure_ascii=False),
                json.dumps(media, ensure_ascii=False),
                PENDING,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM edge_inbound_events WHERE client_event_id = ?", (client_event_id,)
        ).fetchone()
        return True, _event_row(row)


def due_inbound(limit: int = 20, *, now: float | None = None) -> list[dict]:
    current = time.time() if now is None else now
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM edge_inbound_events
            WHERE status = ? AND next_attempt_at <= ?
            ORDER BY id ASC LIMIT ?
            """,
            (PENDING, current, limit),
        ).fetchall()
    return [_event_row(row) for row in rows]


def mark_inbound_delivered(client_event_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE edge_inbound_events
               SET status = ?, delivered_at = ?, last_error = ''
               WHERE client_event_id = ?""",
            (DELIVERED, time.time(), client_event_id),
        )


def retry_inbound(client_event_id: str, error: str, *, delay_seconds: float) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE edge_inbound_events
               SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
               WHERE client_event_id = ? AND status = ?""",
            (time.time() + max(0.0, delay_seconds), str(error)[:1000], client_event_id, PENDING),
        )


def record_command(command: dict) -> tuple[bool, dict]:
    """Durably accept a command. A command id is executable once, ever."""
    command_id = str(command.get("command_id") or "").strip()
    if not command_id:
        raise ValueError("channel command missing command_id")
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM edge_command_receipts WHERE command_id = ?", (command_id,)
        ).fetchone()
        if existing is not None:
            return False, _command_row(existing)
        conn.execute(
            """
            INSERT INTO edge_command_receipts
                (command_id, lease_id, payload_json, execution_status, received_at)
            VALUES (?, ?, ?, 'received', ?)
            """,
            (
                command_id,
                str(command.get("lease_id") or ""),
                json.dumps(command, ensure_ascii=False),
                time.time(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM edge_command_receipts WHERE command_id = ?", (command_id,)
        ).fetchone()
        return True, _command_row(row)


def mark_command_executing(command_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE edge_command_receipts SET execution_status = 'executing'
               WHERE command_id = ? AND execution_status = 'received'""",
            (command_id,),
        )
        return cur.rowcount == 1


def save_command_result(command_id: str, result: dict) -> None:
    """Persist a result before reporting it, so reboot never repeats a send."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE edge_command_receipts
            SET execution_status = ?, result_json = ?, result_status = ?,
                executed_at = ?, last_error = ''
            WHERE command_id = ?
            """,
            (
                str(result.get("status") or "needs_reconciliation"),
                json.dumps(result, ensure_ascii=False),
                RESULT_PENDING,
                time.time(),
                command_id,
            ),
        )


def due_command_results(limit: int = 20, *, now: float | None = None) -> list[dict]:
    current = time.time() if now is None else now
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM edge_command_receipts
            WHERE result_status = ? AND result_next_attempt_at <= ?
            ORDER BY received_at ASC LIMIT ?
            """,
            (RESULT_PENDING, current, limit),
        ).fetchall()
    return [_command_row(row) for row in rows]


def mark_command_result_reported(command_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE edge_command_receipts
               SET result_status = ?, result_reported_at = ?, last_error = ''
               WHERE command_id = ?""",
            (RESULT_REPORTED, time.time(), command_id),
        )


def retry_command_result(command_id: str, error: str, *, delay_seconds: float) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE edge_command_receipts
               SET result_attempts = result_attempts + 1,
                   result_next_attempt_at = ?, last_error = ?
               WHERE command_id = ? AND result_status = ?""",
            (time.time() + max(0.0, delay_seconds), str(error)[:1000], command_id, RESULT_PENDING),
        )


def edge_status() -> dict:
    with _connect() as conn:
        inbound_pending = conn.execute(
            "SELECT COUNT(*) AS count FROM edge_inbound_events WHERE status = ?", (PENDING,)
        ).fetchone()["count"]
        result_pending = conn.execute(
            "SELECT COUNT(*) AS count FROM edge_command_receipts WHERE result_status = ?", (RESULT_PENDING,)
        ).fetchone()["count"]
        commands = conn.execute("SELECT COUNT(*) AS count FROM edge_command_receipts").fetchone()["count"]
    return {
        "ok": True,
        "inbound_pending": inbound_pending,
        "command_results_pending": result_pending,
        "commands_received": commands,
    }
