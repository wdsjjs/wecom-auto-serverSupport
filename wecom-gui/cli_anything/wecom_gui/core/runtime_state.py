"""Local runtime telemetry shared by the Mac edge and AI processes.

This data is deliberately operational: it contains no customer message body,
credentials, or raw Accessibility payloads.  It is safe for the desktop client
to render and for the edge process to project to the central workbench.
"""

from __future__ import annotations

import json
import time
from typing import Any

from cli_anything.wecom_gui.core import state


PROCESS_EDGE = "edge_channel"
PROCESS_AI = "ai_reply"
PROCESSES = {PROCESS_EDGE, PROCESS_AI}
STATUSES = {"idle", "running", "waiting", "retrying", "failed", "stopped"}
RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_EVENTS = 20


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_process_state (
            process_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            conversation_key TEXT NOT NULL DEFAULT '',
            conversation_label TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT '',
            rationale TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_activity_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process_name TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            conversation_key TEXT NOT NULL DEFAULT '',
            conversation_label TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT '',
            rationale TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '',
            occurred_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_supervisor_service (
            service_name TEXT PRIMARY KEY,
            process_name TEXT NOT NULL,
            screen_session TEXT NOT NULL,
            pid INTEGER,
            command_line TEXT NOT NULL DEFAULT '',
            log_path TEXT NOT NULL DEFAULT '',
            last_action TEXT NOT NULL DEFAULT '',
            last_result TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_activity_event_recent
        ON runtime_activity_event (occurred_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_activity_event_expiry
        ON runtime_activity_event (expires_at)
        """
    )


def _clean_text(value: object, *, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _clean_metrics(value: object) -> dict[str, int | float | str | bool]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int | float | str | bool] = {}
    for key, item in value.items():
        name = _clean_text(key, limit=48)
        if not name or name.lower() in {"text", "content", "message", "token", "secret", "credential"}:
            continue
        if isinstance(item, bool):
            result[name] = item
        elif isinstance(item, (int, float)):
            result[name] = item
        elif isinstance(item, str):
            result[name] = _clean_text(item, limit=96)
    return result


def _row(row) -> dict[str, Any]:
    return {
        "process": row["process_name"],
        "status": row["status"],
        "phase": row["phase"],
        "conversation_key": row["conversation_key"],
        "conversation_label": row["conversation_label"],
        "direction": row["direction"],
        "rationale": row["rationale"],
        "metrics": json.loads(row["metrics_json"] or "{}"),
        "error_code": row["error_code"],
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else row["occurred_at"],
        "occurred_at": row["occurred_at"] if "occurred_at" in row.keys() else row["updated_at"],
    }


def report(
    process: str,
    *,
    status: str,
    phase: str,
    conversation_key: str = "",
    conversation_label: str = "",
    direction: str = "",
    rationale: str = "",
    metrics: object = None,
    error_code: str = "",
) -> dict[str, Any]:
    """Upsert an operational state and append one redacted activity event."""
    if process not in PROCESSES:
        raise ValueError("unsupported runtime process")
    if status not in STATUSES:
        raise ValueError("unsupported runtime status")
    now = time.time()
    phase_value = _clean_text(phase, limit=64) or "unknown"
    values = (
        _clean_text(conversation_key, limit=256),
        _clean_text(conversation_label, limit=128),
        _clean_text(direction, limit=32),
        _clean_text(rationale, limit=160),
        json.dumps(_clean_metrics(metrics), ensure_ascii=False, sort_keys=True),
        _clean_text(error_code, limit=96),
    )
    with state.connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO runtime_process_state
              (process_name, status, phase, conversation_key, conversation_label,
               direction, rationale, metrics_json, error_code, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(process_name) DO UPDATE SET
              status=excluded.status, phase=excluded.phase,
              conversation_key=excluded.conversation_key,
              conversation_label=excluded.conversation_label,
              direction=excluded.direction, rationale=excluded.rationale,
              metrics_json=excluded.metrics_json, error_code=excluded.error_code,
              updated_at=excluded.updated_at
            """,
            (process, status, phase_value, *values, now),
        )
        conn.execute(
            """
            INSERT INTO runtime_activity_event
              (process_name, status, phase, conversation_key, conversation_label,
               direction, rationale, metrics_json, error_code, occurred_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (process, status, phase_value, *values, now, now + RETENTION_SECONDS),
        )
        conn.execute(
            "DELETE FROM runtime_activity_event WHERE id IN (SELECT id FROM runtime_activity_event WHERE expires_at <= ? ORDER BY id LIMIT 200)",
            (now,),
        )
        row = conn.execute("SELECT * FROM runtime_process_state WHERE process_name = ?", (process,)).fetchone()
    return _row(row)


def snapshot(*, limit: int = MAX_EVENTS) -> dict[str, Any]:
    now = time.time()
    with state.connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            "DELETE FROM runtime_activity_event WHERE id IN (SELECT id FROM runtime_activity_event WHERE expires_at <= ? ORDER BY id LIMIT 200)",
            (now,),
        )
        states = [_row(row) for row in conn.execute("SELECT * FROM runtime_process_state ORDER BY process_name").fetchall()]
        events = [_row(row) for row in conn.execute(
            "SELECT * FROM runtime_activity_event WHERE expires_at > ? ORDER BY occurred_at DESC LIMIT ?",
            (now, max(1, min(MAX_EVENTS, int(limit)))),
        ).fetchall()]
    return {"ok": True, "now": now, "states": states, "events": events}


def record_supervisor(
    service: str,
    *,
    process: str,
    screen_session: str,
    pid: int | None,
    command_line: str,
    log_path: str,
    action: str,
    result: str,
) -> None:
    """Persist the supervisor's controlled-session metadata without secrets."""
    with state.connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO runtime_supervisor_service
              (service_name, process_name, screen_session, pid, command_line, log_path, last_action, last_result, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_name) DO UPDATE SET
              process_name=excluded.process_name, screen_session=excluded.screen_session,
              pid=excluded.pid, command_line=excluded.command_line, log_path=excluded.log_path,
              last_action=excluded.last_action, last_result=excluded.last_result,
              updated_at=excluded.updated_at
            """,
            (
                _clean_text(service, limit=32), _clean_text(process, limit=32), _clean_text(screen_session, limit=96), pid,
                _clean_text(command_line, limit=1024), _clean_text(log_path, limit=512), _clean_text(action, limit=32),
                _clean_text(result, limit=96), time.time(),
            ),
        )


def supervisor_snapshot() -> list[dict[str, Any]]:
    with state.connect() as conn:
        _ensure_schema(conn)
        return [dict(row) for row in conn.execute("SELECT * FROM runtime_supervisor_service ORDER BY service_name").fetchall()]
