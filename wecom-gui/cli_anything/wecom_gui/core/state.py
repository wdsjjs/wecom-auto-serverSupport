"""Local state for dedupe, queueing, and audit logs."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import fcntl


ACTIVE_STATUSES = {"processing", "reading", "drafting", "ready", "sending"}
REPLACEABLE_ACTIVE_STATUSES = {"processing", "reading", "drafting"}
DONE_REOPEN_COOLDOWN_SECONDS = 60.0


def state_dir() -> Path:
    path = Path.home() / ".cli-anything-wecom-gui"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_event(event: dict) -> Path:
    """Append a JSONL audit event and return the log path."""
    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "events.jsonl"
    payload = {"ts": time.time(), **event}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def db_path() -> Path:
    """Return the SQLite state database path."""
    return state_dir() / "state.sqlite"


@contextmanager
def gui_lock():
    """Serialize GUI operations across scanner/worker processes."""
    path = state_dir() / "gui.lock"
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def connect() -> sqlite3.Connection:
    """Open the state database and ensure schema exists."""
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create queue tables if needed."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL UNIQUE,
            preview TEXT NOT NULL DEFAULT '',
            time_text TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL DEFAULT '[]',
            signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_message_hash TEXT,
            context_json TEXT,
            click_x REAL,
            click_y REAL,
            source TEXT,
            reply_text TEXT,
            error TEXT,
            locked_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    _ensure_column(conn, "reply_queue", "context_json", "TEXT")
    _ensure_column(conn, "reply_queue", "click_x", "REAL")
    _ensure_column(conn, "reply_queue", "click_y", "REAL")
    _ensure_column(conn, "reply_queue", "source", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wecom_customer_bindings (
            uid TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'sidebar',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wecom_customer_bindings_name
        ON wecom_customer_bindings(customer_name)
        """
    )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["raw"] = json.loads(item.pop("raw_json") or "[]")
    return item


def _update_visible_row(conn: sqlite3.Connection, row: dict, signature: str, now: float) -> None:
    conn.execute(
        """
        UPDATE reply_queue
        SET preview = ?, time_text = ?, tags_json = ?, raw_json = ?,
            click_x = ?, click_y = ?, source = ?,
            signature = ?, updated_at = ?
        WHERE title = ?
        """,
        (
            row.get("preview", ""),
            row.get("time", ""),
            json.dumps(row.get("tags", []), ensure_ascii=False),
            json.dumps(row.get("raw", []), ensure_ascii=False),
            row.get("click_x"),
            row.get("click_y"),
            row.get("source", ""),
            signature,
            now,
            row.get("title", ""),
        ),
    )


def enqueue_conversation(row: dict, signature: str) -> tuple[bool, dict]:
    """Insert/update a conversation job when the visible row changed.

    Returns `(changed, item)`. Existing processing jobs are not overwritten,
    which avoids racing with an active worker.
    """
    now = time.time()
    row_has_unread = int(row.get("unread_count") or 0) > 0 or bool(row.get("unread"))
    done_reopen_cooldown = float(
        os.environ.get("WECOM_GUI_DONE_REOPEN_COOLDOWN_SECONDS", DONE_REOPEN_COOLDOWN_SECONDS)
    )
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM reply_queue WHERE title = ?",
            (row.get("title", ""),),
        ).fetchone()
        if existing and existing["signature"] == signature:
            can_reopen_done = (
                existing["status"] == "done"
                and row_has_unread
                and now - float(existing["updated_at"] or 0) >= done_reopen_cooldown
            )
            can_reopen_inactive = existing["status"] in {"failed", "skipped"} and row_has_unread
            if can_reopen_done or can_reopen_inactive:
                _update_visible_row(conn, row, signature, now)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = NULL, locked_at = NULL,
                        context_json = NULL, last_message_hash = NULL, reply_text = NULL,
                        updated_at = ?
                    WHERE title = ?
                    """,
                    (now, row.get("title", "")),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE title = ?",
                    (row.get("title", ""),),
                ).fetchone()
                return True, _row_to_dict(item)
            return False, _row_to_dict(existing)
        if existing and existing["status"] in ACTIVE_STATUSES:
            same_preview = str(existing["preview"] or "").strip() == str(row.get("preview", "")).strip()
            if existing["status"] in REPLACEABLE_ACTIVE_STATUSES and row_has_unread and not same_preview:
                _update_visible_row(conn, row, signature, now)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = NULL, locked_at = NULL,
                        context_json = NULL, last_message_hash = NULL, reply_text = NULL,
                        updated_at = ?
                    WHERE title = ?
                    """,
                    (now, row.get("title", "")),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE title = ?",
                    (row.get("title", ""),),
                ).fetchone()
                return True, _row_to_dict(item)
            return False, _row_to_dict(existing)
        if (
            existing
            and existing["status"] == "done"
            and (existing["reply_text"] or "").strip()
            and (existing["reply_text"] or "").strip() == str(row.get("preview", "")).strip()
        ):
            _update_visible_row(conn, row, signature, now)
            item = conn.execute(
                "SELECT * FROM reply_queue WHERE title = ?",
                (row.get("title", ""),),
            ).fetchone()
            return False, _row_to_dict(item)

        payload = (
            row.get("title", ""),
            row.get("preview", ""),
            row.get("time", ""),
            json.dumps(row.get("tags", []), ensure_ascii=False),
            json.dumps(row.get("raw", []), ensure_ascii=False),
            row.get("click_x"),
            row.get("click_y"),
            row.get("source", ""),
            signature,
            "pending",
            None,
            now,
            now,
        )
        if existing:
            conn.execute(
                """
                UPDATE reply_queue
                SET preview = ?, time_text = ?, tags_json = ?, raw_json = ?,
                    click_x = ?, click_y = ?, source = ?,
                    signature = ?, status = ?, error = ?, updated_at = ?
                WHERE title = ?
                """,
                (
                    row.get("preview", ""),
                    row.get("time", ""),
                    json.dumps(row.get("tags", []), ensure_ascii=False),
                    json.dumps(row.get("raw", []), ensure_ascii=False),
                    row.get("click_x"),
                    row.get("click_y"),
                    row.get("source", ""),
                    signature,
                    "pending",
                    None,
                    now,
                    row.get("title", ""),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO reply_queue
                    (title, preview, time_text, tags_json, raw_json,
                     click_x, click_y, source, signature, status, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        item = conn.execute(
            "SELECT * FROM reply_queue WHERE title = ?",
            (row.get("title", ""),),
        ).fetchone()
        return True, _row_to_dict(item)


def list_queue(status: str | None = None, limit: int = 50) -> list[dict]:
    """List queued jobs."""
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM reply_queue WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reply_queue ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_job(job_id: int) -> dict | None:
    """Return one queued job by id."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM reply_queue WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_job_by_title(title: str) -> dict | None:
    """Return the current queued job for one conversation title."""
    title = str(title or "").strip()
    if not title:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM reply_queue WHERE title = ?", (title,)).fetchone()
    return _row_to_dict(row) if row else None


def queue_counts() -> dict[str, int]:
    """Return queue counts grouped by status."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM reply_queue GROUP BY status ORDER BY status"
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def clear_queue(status: str | None = None) -> int:
    """Clear queued jobs and return deleted count."""
    with connect() as conn:
        if status:
            cur = conn.execute("DELETE FROM reply_queue WHERE status = ?", (status,))
        else:
            cur = conn.execute("DELETE FROM reply_queue")
        return cur.rowcount


def _binding_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["raw"] = json.loads(item.pop("raw_json") or "{}")
    return item


def bind_wecom_customer(
    *,
    uid: str,
    customer_name: str,
    display_name: str = "",
    source: str = "sidebar",
    raw: dict | None = None,
) -> dict:
    """Bind a WeCom external user id to the visible conversation/customer name."""
    uid = str(uid or "").strip()
    customer_name = str(customer_name or "").strip()
    display_name = str(display_name or "").strip()
    source = str(source or "sidebar").strip() or "sidebar"
    if not uid:
        raise ValueError("uid is required")
    if not customer_name:
        raise ValueError("customer_name is required")
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO wecom_customer_bindings
                (uid, customer_name, display_name, source, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                customer_name = excluded.customer_name,
                display_name = excluded.display_name,
                source = excluded.source,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (
                uid,
                customer_name,
                display_name,
                source,
                json.dumps(raw or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM wecom_customer_bindings WHERE uid = ?", (uid,)).fetchone()
    return _binding_to_dict(row)


def lookup_wecom_customer(*, uid: str = "", customer_name: str = "") -> dict | None:
    """Find a WeCom customer binding by uid or visible customer name."""
    uid = str(uid or "").strip()
    customer_name = str(customer_name or "").strip()
    with connect() as conn:
        row = None
        if uid:
            row = conn.execute("SELECT * FROM wecom_customer_bindings WHERE uid = ?", (uid,)).fetchone()
        if row is None and customer_name:
            row = conn.execute(
                """
                SELECT * FROM wecom_customer_bindings
                WHERE customer_name = ? OR display_name = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (customer_name, customer_name),
            ).fetchone()
    return _binding_to_dict(row) if row else None


def list_wecom_customer_bindings(limit: int = 100) -> list[dict]:
    """List recent WeCom customer uid bindings."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM wecom_customer_bindings
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_binding_to_dict(row) for row in rows]


def reset_stale_active(*, older_than_seconds: float = 120.0) -> int:
    """Return abandoned active jobs to pending after a crashed worker."""
    cutoff = time.time() - older_than_seconds
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'pending', locked_at = NULL,
                error = 'stale_active_reset', updated_at = ?
            WHERE status IN ('processing', 'reading', 'drafting', 'sending')
              AND updated_at < ?
            """,
            (time.time(), cutoff),
        )
        return cur.rowcount


def _claim_status(from_status: str, to_status: str) -> dict | None:
    """Atomically claim one job from one status into another."""
    now = time.time()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM reply_queue
            WHERE status = ?
            ORDER BY updated_at ASC
            LIMIT 1
            """,
            (from_status,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE reply_queue
            SET status = ?, attempts = attempts + 1,
                locked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (to_status, now, now, row["id"]),
        )
        conn.commit()
        claimed = conn.execute("SELECT * FROM reply_queue WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_dict(claimed)
    finally:
        conn.close()


def claim_next() -> dict | None:
    """Claim one pending job for the legacy synchronous worker."""
    return _claim_status("pending", "processing")


def claim_pending_for_read() -> dict | None:
    """Claim one pending job for fast context intake."""
    return _claim_status("pending", "reading")


def claim_ready_to_send() -> dict | None:
    """Claim one AI-ready job for final sending."""
    return _claim_status("ready", "sending")


def mark_drafting(job_id: int, *, message_hash: str, messages: list[dict], latest: dict) -> None:
    now = time.time()
    latest_text = str(latest.get("content") or latest.get("text") or "").strip()
    context = {
        "latest": {
            "role": latest.get("role") or "用户",
            "text": latest_text,
            "content": latest_text,
            "source": latest.get("source") or "",
            "role_confidence": latest.get("role_confidence") or "",
        },
        "message_count": len(messages),
    }
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'drafting', last_message_hash = ?, context_json = ?,
                error = NULL, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_hash, json.dumps(context, ensure_ascii=False), now, job_id),
        )


def mark_ready(job_id: int, *, reply_text: str) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', reply_text = ?, error = NULL,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (reply_text, now, job_id),
        )


def mark_pending(job_id: int, reason: str = "") -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'pending', error = ?, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (reason or None, now, job_id),
        )


def mark_done(job_id: int, *, message_hash: str | None = None, reply_text: str | None = None) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'done', last_message_hash = ?, reply_text = ?,
                error = NULL, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_hash, reply_text, now, job_id),
        )


def mark_skipped(job_id: int, reason: str) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'skipped', error = ?, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (reason, now, job_id),
        )


def mark_failed(job_id: int, error: str) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'failed', error = ?, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (error, now, job_id),
        )
