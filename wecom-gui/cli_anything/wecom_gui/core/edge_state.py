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
from contextlib import contextmanager, nullcontext
from typing import Iterator

from cli_anything.wecom_gui.core import state


PENDING = "pending"
DELIVERED = "delivered"
WAITING_MEDIA = "waiting_media"
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
    for column, definition in (
        ("registered_direction", "TEXT NOT NULL DEFAULT ''"),
        ("registration_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("registration_next_attempt_at", "REAL NOT NULL DEFAULT 0"),
        ("registration_error", "TEXT NOT NULL DEFAULT ''"),
        ("registration_started", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _ensure_column(conn, "edge_inbound_events", column, definition)
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
            reconciliation_next_attempt_at REAL NOT NULL DEFAULT 0,
            reconciliation_attempts INTEGER NOT NULL DEFAULT 0,
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
    _ensure_column(conn, "edge_command_receipts", "reconciliation_next_attempt_at", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "edge_command_receipts", "reconciliation_attempts", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_chat_observations (
            conversation_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            observed_at REAL NOT NULL,
            last_observed_at REAL NOT NULL,
            capture_status TEXT NOT NULL DEFAULT 'captured',
            direction_confidence TEXT NOT NULL DEFAULT '',
            direction_attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (conversation_key, fingerprint)
        )
        """
    )
    _ensure_column(conn, "edge_chat_observations", "last_observed_at", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "edge_chat_observations", "capture_status", "TEXT NOT NULL DEFAULT 'captured'")
    _ensure_column(conn, "edge_chat_observations", "direction_confidence", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "edge_chat_observations", "direction_attempts", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_chat_observation_baselines (
            conversation_key TEXT PRIMARY KEY,
            initialized_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_outbound_echo_suppressions (
            command_id TEXT PRIMARY KEY,
            conversation_key TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_edge_outbound_echo_suppressions_due
        ON edge_outbound_echo_suppressions(conversation_key, normalized_text, expires_at)
        """
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS edge_command_echo_rows (
            command_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            capture_row_id TEXT NOT NULL,
            match_key TEXT NOT NULL,
            PRIMARY KEY (conversation_key, capture_row_id)
        )"""
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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def enqueue_inbound(*, dedupe_key: str, payload: dict, media: list[dict], connection=None) -> tuple[bool, dict]:
    """Persist a captured inbound event before any network request.

    A stable dedupe key means a scan after a crash reuses the original event
    rather than making a new event id for the same visible customer message.
    """
    now = time.time()
    client_event_id = str(payload.get("client_event_id") or uuid.uuid4())
    complete_payload = {**payload, "client_event_id": client_event_id}
    with (nullcontext(connection) if connection is not None else _connect()) as conn:
        existing = conn.execute(
            "SELECT * FROM edge_inbound_events WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        if existing is not None:
            existing_payload = json.loads(existing["payload_json"])
            if not existing_payload["message"].get("source") and complete_payload["message"].get("source"):
                existing_payload["message"]["source"] = complete_payload["message"]["source"]
                conn.execute("UPDATE edge_inbound_events SET payload_json = ? WHERE dedupe_key = ?",
                             (json.dumps(existing_payload, ensure_ascii=False), dedupe_key))
                existing = conn.execute("SELECT * FROM edge_inbound_events WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            existing_direction = str((existing_payload.get("message") or {}).get("direction") or "")
            next_direction = str((complete_payload.get("message") or {}).get("direction") or "")
            previous_media = json.loads(existing["media_json"])
            media_repaired = (existing["status"] in {PENDING, WAITING_MEDIA} and bool(media)
                              and (existing["status"] == WAITING_MEDIA or not media_files_ready(previous_media))
                              and len(media) == len(previous_media) and media_files_ready(media))
            if media_repaired or (existing_direction == "unknown" and next_direction in {"inbound", "outbound"}):
                complete_payload["client_event_id"] = str(existing["client_event_id"])
                complete_payload["occurred_at"] = existing_payload["occurred_at"]
                if existing_direction in {"inbound", "outbound"}:
                    complete_payload["message"]["direction"] = existing_direction
                    complete_payload["event_type"] = f"{existing_direction}_message"
                if previous_media and not media_repaired:
                    media = previous_media
                    complete_payload["message"]["media"] = existing_payload["message"]["media"]
                conn.execute(
                    """
                    UPDATE edge_inbound_events
                    SET payload_json = ?, media_json = ?, status = ?, attempts = 0,
                        next_attempt_at = 0, last_error = '', delivered_at = NULL
                    WHERE dedupe_key = ?
                    """,
                    (
                        json.dumps(complete_payload, ensure_ascii=False),
                        json.dumps(media, ensure_ascii=False),
                        WAITING_MEDIA if media and not media_files_ready(media) else PENDING,
                        dedupe_key,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM edge_inbound_events WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                return True, _event_row(updated)
            return False, _event_row(existing)
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


def media_files_ready(media: list[dict]) -> bool:
    from pathlib import Path
    try:
        return all(bool(item.get("capture_path")) and Path(item["capture_path"]).is_file()
                   and Path(item["capture_path"]).stat().st_size > 0 for item in media)
    except OSError:
        return False


def inbound_by_key(dedupe_key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM edge_inbound_events WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        return _event_row(row) if row else None


def wait_for_inbound_media(client_event_id: str) -> None:
    with _connect() as conn:
        conn.execute("""UPDATE edge_inbound_events SET status = ?, last_error = 'awaiting_media_capture'
            WHERE client_event_id = ? AND status = ?""", (WAITING_MEDIA, client_event_id, PENDING))


def due_registrations(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("""SELECT * FROM edge_inbound_events
            WHERE json_type(payload_json, '$.message.source') = 'object'
              AND registered_direction != json_extract(payload_json, '$.message.direction')
              AND registration_next_attempt_at <= ? ORDER BY id LIMIT ?""", (time.time(), limit)).fetchall()
        return [_event_row(row) for row in rows]


def mark_registered(client_event_id: str, direction: str) -> None:
    with _connect() as conn:
        conn.execute("""UPDATE edge_inbound_events SET registered_direction = ?, registration_attempts = 0,
            registration_next_attempt_at = 0, registration_error = '' WHERE client_event_id = ?""", (direction, client_event_id))


def start_registration(client_event_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE edge_inbound_events SET registration_started = 1 WHERE client_event_id = ?", (client_event_id,))


def retry_registration(client_event_id: str, error: str, delay: float) -> None:
    with _connect() as conn:
        conn.execute("""UPDATE edge_inbound_events SET registration_attempts = registration_attempts + 1,
            registration_next_attempt_at = ?, registration_error = ? WHERE client_event_id = ?""",
            (time.time() + delay, error[:300], client_event_id))


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
    public_result = {key: value for key, value in result.items() if key != "_echo_rows"}
    with _connect() as conn:
        # The receipt and exact GUI echo identities commit together. No customer
        # content or image pixels are needed to recognize these rows on a rescan.
        if result.get("status") == "succeeded" and result.get("_echo_rows"):
            receipt = conn.execute("SELECT payload_json FROM edge_command_receipts WHERE command_id = ?",
                                   (command_id,)).fetchone()
            if receipt is None:
                raise ValueError("command receipt missing for verified echoes")
            conversation_key = str((json.loads(receipt[0]).get("conversation") or {}).get("key") or "")
            if not conversation_key:
                raise ValueError("command conversation missing for verified echoes")
            for echo in result["_echo_rows"]:
                conn.execute("""INSERT INTO edge_command_echo_rows
                    (command_id, conversation_key, capture_row_id, match_key) VALUES (?, ?, ?, ?)
                    ON CONFLICT(conversation_key, capture_row_id) DO NOTHING""",
                    (command_id, conversation_key, echo["capture_row_id"], echo["match_key"]))
        conn.execute(
            """
            UPDATE edge_command_receipts
            SET execution_status = ?, result_json = ?, result_status = ?,
                executed_at = ?, last_error = ''
            WHERE command_id = ?
            """,
            (
                str(result.get("status") or "needs_reconciliation"),
                json.dumps(public_result, ensure_ascii=False),
                RESULT_PENDING,
                time.time(),
                command_id,
            ),
        )


def is_command_echo_row(conversation_key: str, capture_row_id: str, match_key: str, *, connection) -> bool:
    if not capture_row_id:
        return False
    return connection.execute("""SELECT 1 FROM edge_command_echo_rows e
        JOIN edge_command_receipts r ON r.command_id = e.command_id
        WHERE e.conversation_key = ? AND e.capture_row_id = ? AND e.match_key = ?
          AND r.execution_status = 'succeeded'""", (conversation_key, capture_row_id, match_key)).fetchone() is not None


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


def due_reconciliation_checks(
    limit: int = 3,
    *,
    max_attempts: int = 3,
    now: float | None = None,
) -> list[dict]:
    """Return uncertain sends which can be verified without resending them."""
    current = time.time() if now is None else now
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM edge_command_receipts
            WHERE execution_status = 'needs_reconciliation'
              AND result_status = ?
              AND reconciliation_next_attempt_at <= ?
              AND reconciliation_attempts < ?
            ORDER BY executed_at ASC, received_at ASC LIMIT ?
            """,
            (RESULT_REPORTED, current, max(1, max_attempts), limit),
        ).fetchall()
    return [_command_row(row) for row in rows]


def schedule_reconciliation_check(command_id: str, *, delay_seconds: float) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE edge_command_receipts
            SET reconciliation_next_attempt_at = ?,
                reconciliation_attempts = reconciliation_attempts + 1
            WHERE command_id = ? AND execution_status = 'needs_reconciliation'
            """,
            (time.time() + max(0.0, delay_seconds), command_id),
        )


def record_visible_chat_observations(
    conversation_key: str,
    fingerprints: list[str],
    *,
    bootstrap_recent_count: int = 0,
) -> list[str]:
    """Return visible messages still awaiting a direction-safe capture.

    The first observation establishes a baseline so history is never replayed.
    Later observations remain ``pending_direction`` until the caller captures
    them or explicitly ignores them. For a newly discovered unread chat, the
    unread badge bounds the trailing messages that can be treated as new while
    the visible history still establishes the baseline.
    """
    now = time.time()
    unique = list(dict.fromkeys(item for item in fingerprints if item))
    if not conversation_key:
        return []
    with _connect() as conn:
        baseline = conn.execute(
            "SELECT 1 FROM edge_chat_observation_baselines WHERE conversation_key = ?",
            (conversation_key,),
        ).fetchone()
        bootstrap_count = min(max(0, int(bootstrap_recent_count)), len(unique))
        bootstrap_start = len(unique) - bootstrap_count
        for index, fingerprint in enumerate(unique):
            if baseline is None:
                capture_status = "pending_direction" if index >= bootstrap_start else "baseline"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO edge_chat_observations
                        (conversation_key, fingerprint, observed_at, last_observed_at, capture_status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (conversation_key, fingerprint, now, now, capture_status),
                )
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO edge_chat_observations
                    (conversation_key, fingerprint, observed_at, last_observed_at, capture_status)
                VALUES (?, ?, ?, ?, 'pending_direction')
                """,
                (conversation_key, fingerprint, now, now),
            )
            conn.execute(
                """
                UPDATE edge_chat_observations
                SET last_observed_at = ?
                WHERE conversation_key = ? AND fingerprint = ?
                  AND capture_status = 'pending_direction'
                """,
                (now, conversation_key, fingerprint),
            )
        if baseline is None:
            conn.execute(
                """
                INSERT INTO edge_chat_observation_baselines(conversation_key, initialized_at)
                VALUES (?, ?)
                """,
                (conversation_key, now),
            )
            if not bootstrap_count:
                return []
        if not unique:
            return []
        placeholders = ", ".join("?" for _ in unique)
        rows = conn.execute(
            f"""
            SELECT fingerprint FROM edge_chat_observations
            WHERE conversation_key = ? AND capture_status = 'pending_direction'
              AND fingerprint IN ({placeholders})
            """,
            (conversation_key, *unique),
        ).fetchall()
        return [str(row["fingerprint"]) for row in rows]


def mark_chat_observation_pending_direction(
    conversation_key: str,
    fingerprint: str,
    *,
    confidence: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE edge_chat_observations
            SET direction_confidence = ?, direction_attempts = direction_attempts + 1,
                last_observed_at = ?
            WHERE conversation_key = ? AND fingerprint = ?
              AND capture_status = 'pending_direction'
            """,
            (str(confidence)[:32], time.time(), conversation_key, fingerprint),
        )


def mark_chat_observation_captured(conversation_key: str, fingerprint: str, *, confidence: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE edge_chat_observations
            SET capture_status = 'captured', direction_confidence = ?, last_observed_at = ?
            WHERE conversation_key = ? AND fingerprint = ?
              AND capture_status = 'pending_direction'
            """,
            (str(confidence)[:32], time.time(), conversation_key, fingerprint),
        )


def ignore_chat_observation(conversation_key: str, fingerprint: str, *, reason: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE edge_chat_observations
            SET capture_status = ?, last_observed_at = ?
            WHERE conversation_key = ? AND fingerprint = ?
              AND capture_status = 'pending_direction'
            """,
            (f"ignored:{str(reason)[:64]}", time.time(), conversation_key, fingerprint),
        )


def register_outbound_echo_suppression(command_id: str, conversation_key: str, text: str, *, ttl_seconds: float = 30) -> None:
    normalized = "".join(str(text).split())
    if not command_id or not conversation_key or not normalized:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO edge_outbound_echo_suppressions(command_id, conversation_key, normalized_text, expires_at, consumed_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(command_id) DO UPDATE SET
                conversation_key = excluded.conversation_key,
                normalized_text = excluded.normalized_text,
                expires_at = excluded.expires_at,
                consumed_at = NULL
            """,
            (command_id, conversation_key, normalized, time.time() + max(1, ttl_seconds)),
        )


def consume_outbound_echo_suppression(conversation_key: str, text: str, *, connection=None) -> bool:
    normalized = "".join(str(text).split())
    if not conversation_key or not normalized:
        return False
    now = time.time()
    with (nullcontext(connection) if connection is not None else _connect()) as conn:
        conn.execute("DELETE FROM edge_outbound_echo_suppressions WHERE expires_at < ?", (now,))
        row = conn.execute(
            """
            SELECT command_id FROM edge_outbound_echo_suppressions
            WHERE conversation_key = ? AND normalized_text = ? AND consumed_at IS NULL AND expires_at >= ?
            ORDER BY expires_at ASC LIMIT 1
            """,
            (conversation_key, normalized, now),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE edge_outbound_echo_suppressions SET consumed_at = ? WHERE command_id = ?",
            (now, row["command_id"]),
        )
        return True


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
    from cli_anything.wecom_gui.core.edge_message_ledger import MEDIA_ATTEMPT_LIMIT

    with _connect() as conn:
        inbound_pending = conn.execute(
            "SELECT COUNT(*) AS count FROM edge_inbound_events WHERE status = ?", (PENDING,)
        ).fetchone()["count"]
        result_pending = conn.execute(
            "SELECT COUNT(*) AS count FROM edge_command_receipts WHERE result_status = ?", (RESULT_PENDING,)
        ).fetchone()["count"]
        commands = conn.execute("SELECT COUNT(*) AS count FROM edge_command_receipts").fetchone()["count"]
        alignment_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'edge_message_alignment_pending'").fetchone()
        pending_alignment = conn.execute("SELECT COUNT(*) FROM edge_message_alignment_pending").fetchone()[0] if alignment_table else 0
        ledger_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'edge_message_ledger'").fetchone()
        pending_media = conn.execute("""SELECT COUNT(*) FROM (
            SELECT dedupe_key FROM edge_inbound_events WHERE status = 'waiting_media'
            UNION SELECT conversation_key || ':' || event_hash FROM edge_message_ledger WHERE capture_status = 'pending_media'
        )""").fetchone()[0] if ledger_table else conn.execute(
            "SELECT COUNT(*) FROM edge_inbound_events WHERE status = ?", (WAITING_MEDIA,)
        ).fetchone()[0]
        media_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'edge_media_capture_state'").fetchone()
        paused_media = 0
        paused_tasks = []
        if ledger_table and media_table:
            paused_media = conn.execute("""SELECT COUNT(*) FROM edge_media_capture_state s
                JOIN edge_message_ledger l ON l.event_hash = s.event_hash
                WHERE l.capture_status = 'pending_media' AND s.attempts >= ?""", (MEDIA_ATTEMPT_LIMIT,)).fetchone()[0]
            paused_tasks = [dict(row) for row in conn.execute("""SELECT l.event_id, s.attempts, s.last_error
                FROM edge_media_capture_state s JOIN edge_message_ledger l ON l.event_hash = s.event_hash
                WHERE l.capture_status = 'pending_media' AND s.attempts >= ?
                ORDER BY l.occurred_at DESC, l.event_id LIMIT 20""", (MEDIA_ATTEMPT_LIMIT,))]
    return {
        "ok": True,
        "inbound_pending": inbound_pending,
        "command_results_pending": result_pending,
        "commands_received": commands,
        "message_alignment_pending": pending_alignment,
        "media_capture_pending": pending_media,
        "media_capture_paused": paused_media,
        "paused_media_tasks": paused_tasks,
    }
