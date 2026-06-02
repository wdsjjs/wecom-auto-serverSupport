"""Local state for dedupe, queueing, and audit logs."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import hashlib
from contextlib import contextmanager
from pathlib import Path

import fcntl


ACTIVE_STATUSES = {"processing", "reading", "drafting", "ready", "approved", "sending"}
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
    if _reply_queue_needs_rebuild(conn):
        _rebuild_reply_queue(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
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
    _ensure_column(conn, "reply_queue", "conversation_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reply_queue", "context_json", "TEXT")
    _ensure_column(conn, "reply_queue", "click_x", "REAL")
    _ensure_column(conn, "reply_queue", "click_y", "REAL")
    _ensure_column(conn, "reply_queue", "source", "TEXT")
    _backfill_conversation_keys(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_queue_conversation_key
        ON reply_queue(conversation_key)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_key TEXT NOT NULL,
            job_id INTEGER,
            message_hash TEXT NOT NULL DEFAULT '',
            seq INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            time_text TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            role_confidence TEXT NOT NULL DEFAULT '',
            media_json TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_messages_key
        ON conversation_messages(conversation_key, created_at, seq)
        """
    )
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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _has_unique_title_constraint(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "reply_queue"):
        return False
    for index in conn.execute("PRAGMA index_list(reply_queue)").fetchall():
        if not index["unique"]:
            continue
        columns = [row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()]
        if columns == ["title"]:
            return True
    return False


def _reply_queue_needs_rebuild(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "reply_queue"):
        return False
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reply_queue)").fetchall()}
    return "conversation_key" not in columns or _has_unique_title_constraint(conn)


def _legacy_conversation_key(title: object) -> str:
    value = str(title or "").strip()
    return "legacy:" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _rebuild_reply_queue(conn: sqlite3.Connection) -> None:
    backup = f"reply_queue_old_{int(time.time())}"
    conn.execute(f"ALTER TABLE reply_queue RENAME TO {backup}")
    conn.execute(
        """
        CREATE TABLE reply_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
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
    old_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({backup})").fetchall()}
    select_context = "context_json" if "context_json" in old_columns else "NULL AS context_json"
    select_click_x = "click_x" if "click_x" in old_columns else "NULL AS click_x"
    select_click_y = "click_y" if "click_y" in old_columns else "NULL AS click_y"
    select_source = "source" if "source" in old_columns else "'' AS source"
    rows = conn.execute(
        f"""
        SELECT id, title, preview, time_text, tags_json, raw_json, signature, status,
               attempts, last_message_hash, {select_context}, {select_click_x},
               {select_click_y}, {select_source}, reply_text, error, locked_at,
               created_at, updated_at
        FROM {backup}
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO reply_queue
                (id, conversation_key, title, preview, time_text, tags_json, raw_json,
                 signature, status, attempts, last_message_hash, context_json,
                 click_x, click_y, source, reply_text, error, locked_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                _legacy_conversation_key(row["title"]),
                row["title"],
                row["preview"],
                row["time_text"],
                row["tags_json"],
                row["raw_json"],
                row["signature"],
                row["status"],
                row["attempts"],
                row["last_message_hash"],
                row["context_json"],
                row["click_x"],
                row["click_y"],
                row["source"],
                row["reply_text"],
                row["error"],
                row["locked_at"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    conn.execute(f"DROP TABLE {backup}")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _backfill_conversation_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, title
        FROM reply_queue
        WHERE COALESCE(conversation_key, '') = ''
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE reply_queue SET conversation_key = ? WHERE id = ?",
            (_legacy_conversation_key(row["title"]), row["id"]),
        )


def _clean_key_part(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:20]


def conversation_key_for_row(row: dict) -> str:
    """Return the queue isolation key for a visible WeCom conversation row."""
    for key in (
        "conversation_key",
        "external_userid",
        "external_user_id",
        "externalUserId",
        "uid",
        "user_id",
        "conversation_id",
        "chat_id",
    ):
        value = _clean_key_part(row.get(key))
        if value:
            if key == "conversation_key":
                return value
            prefix = "uid" if "user" in key.lower() or key == "uid" else "conversation"
            return f"{prefix}:{value}"

    title = _clean_key_part(row.get("title"))
    tags = ",".join(_clean_key_part(tag) for tag in row.get("tags", []) if _clean_key_part(tag))
    source = _clean_key_part(row.get("source"))
    slot = ""
    if row.get("click_y") is not None:
        try:
            slot = f"y{round(float(row.get('click_y')) / 12) * 12:.0f}"
        except (TypeError, ValueError):
            slot = ""
    if not slot and row.get("index") is not None:
        slot = f"i{_clean_key_part(row.get('index'))}"
    if slot:
        return "visible:" + _short_hash("|".join([title, tags, source, slot]))
    return _legacy_conversation_key(title)


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["raw"] = json.loads(item.pop("raw_json") or "[]")
    return item


def _update_visible_row(conn: sqlite3.Connection, row: dict, signature: str, now: float, conversation_key: str) -> None:
    conn.execute(
        """
        UPDATE reply_queue
        SET preview = ?, time_text = ?, tags_json = ?, raw_json = ?,
            click_x = ?, click_y = ?, source = ?,
            signature = ?, updated_at = ?
        WHERE conversation_key = ?
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
            conversation_key,
        ),
    )


def enqueue_conversation(row: dict, signature: str) -> tuple[bool, dict]:
    """Insert/update a conversation job when the visible row changed.

    Returns `(changed, item)`. Existing processing jobs are not overwritten,
    which avoids racing with an active worker.
    """
    now = time.time()
    row_has_unread = int(row.get("unread_count") or 0) > 0 or bool(row.get("unread"))
    conversation_key = conversation_key_for_row(row)
    done_reopen_cooldown = float(
        os.environ.get("WECOM_GUI_DONE_REOPEN_COOLDOWN_SECONDS", DONE_REOPEN_COOLDOWN_SECONDS)
    )
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM reply_queue WHERE conversation_key = ?",
            (conversation_key,),
        ).fetchone()
        if existing and existing["signature"] == signature:
            can_reopen_done = (
                existing["status"] == "done"
                and row_has_unread
                and now - float(existing["updated_at"] or 0) >= done_reopen_cooldown
            )
            can_reopen_inactive = existing["status"] in {"failed", "skipped"} and row_has_unread
            if can_reopen_done or can_reopen_inactive:
                _update_visible_row(conn, row, signature, now, conversation_key)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = NULL, locked_at = NULL,
                        context_json = NULL, last_message_hash = NULL, reply_text = NULL,
                        updated_at = ?
                    WHERE conversation_key = ?
                    """,
                    (now, conversation_key),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                return True, _row_to_dict(item)
            return False, _row_to_dict(existing)
        if existing and existing["status"] in ACTIVE_STATUSES:
            same_preview = str(existing["preview"] or "").strip() == str(row.get("preview", "")).strip()
            if existing["status"] in REPLACEABLE_ACTIVE_STATUSES and row_has_unread and not same_preview:
                _update_visible_row(conn, row, signature, now, conversation_key)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = NULL, locked_at = NULL,
                        context_json = NULL, last_message_hash = NULL, reply_text = NULL,
                        updated_at = ?
                    WHERE conversation_key = ?
                    """,
                    (now, conversation_key),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                return True, _row_to_dict(item)
            return False, _row_to_dict(existing)
        if (
            existing
            and existing["status"] == "done"
            and (existing["reply_text"] or "").strip()
            and (existing["reply_text"] or "").strip() == str(row.get("preview", "")).strip()
        ):
            _update_visible_row(conn, row, signature, now, conversation_key)
            item = conn.execute(
                "SELECT * FROM reply_queue WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            return False, _row_to_dict(item)

        payload = (
            conversation_key,
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
                WHERE conversation_key = ?
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
                    conversation_key,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO reply_queue
                    (conversation_key, title, preview, time_text, tags_json, raw_json,
                     click_x, click_y, source, signature, status, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        item = conn.execute(
            "SELECT * FROM reply_queue WHERE conversation_key = ?",
            (conversation_key,),
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
        row = conn.execute(
            """
            SELECT * FROM reply_queue
            WHERE title = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (title,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_job_by_conversation_key(conversation_key: str) -> dict | None:
    """Return a queued job by isolated conversation key."""
    conversation_key = str(conversation_key or "").strip()
    if not conversation_key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reply_queue WHERE conversation_key = ?",
            (conversation_key,),
        ).fetchone()
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


def claim_approved_to_send() -> dict | None:
    """Claim one web-approved job for final sending."""
    return _claim_status("approved", "sending")


def record_conversation_messages(
    *,
    conversation_key: str,
    job_id: int | None,
    message_hash: str,
    messages: list[dict],
) -> None:
    """Persist the visible chat context used for drafting/review."""
    if not conversation_key:
        return
    now = time.time()
    with connect() as conn:
        conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_key = ?",
            (conversation_key,),
        )
        for index, message in enumerate(messages):
            text = str(message.get("content") or message.get("text") or "").strip()
            media = message.get("media") if isinstance(message.get("media"), list) else []
            conn.execute(
                """
                INSERT INTO conversation_messages
                    (conversation_key, job_id, message_hash, seq, role, text,
                     time_text, source, role_confidence, media_json, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_key,
                    job_id,
                    message_hash,
                    index,
                    str(message.get("role") or ""),
                    text,
                    str(message.get("time") or ""),
                    str(message.get("source") or ""),
                    str(message.get("role_confidence") or ""),
                    json.dumps(media, ensure_ascii=False),
                    json.dumps(message, ensure_ascii=False),
                    now,
                ),
            )


def list_conversation_messages(*, conversation_key: str, limit: int = 20) -> list[dict]:
    """Return persisted chat context for one conversation."""
    conversation_key = str(conversation_key or "").strip()
    if not conversation_key:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM conversation_messages
            WHERE conversation_key = ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (conversation_key, limit),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["media"] = json.loads(item.pop("media_json") or "[]")
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        items.append(item)
    return items


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
        row = conn.execute("SELECT conversation_key FROM reply_queue WHERE id = ?", (job_id,)).fetchone()
        conversation_key = str(row["conversation_key"] if row else "")
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'drafting', last_message_hash = ?, context_json = ?,
                error = NULL, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_hash, json.dumps(context, ensure_ascii=False), now, job_id),
        )
    record_conversation_messages(
        conversation_key=conversation_key,
        job_id=job_id,
        message_hash=message_hash,
        messages=messages,
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


def mark_approved(job_id: int, *, reply_text: str | None = None) -> bool:
    """Approve a ready reply for agent-managed sending."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status, reply_text
            FROM reply_queue
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row["status"] != "ready":
            return False
        final_reply = str(reply_text if reply_text is not None else row["reply_text"] or "").strip()
        if not final_reply:
            return False
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'approved', reply_text = ?, error = NULL,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (final_reply, now, job_id),
        )
        return cur.rowcount == 1


def save_reply(job_id: int, *, reply_text: str) -> bool:
    """Save a reviewer-authored reply and keep it waiting for approval."""
    final_reply = str(reply_text or "").strip()
    if not final_reply:
        return False
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', reply_text = ?, error = NULL,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
              AND status IN ('ready', 'failed', 'skipped')
            """,
            (final_reply, now, job_id),
        )
        return cur.rowcount == 1


def regenerate_reply(job_id: int, reason: str = "review_regenerate") -> bool:
    """Return a completed draft/rejected/error job to pending for a fresh read."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT conversation_key
            FROM reply_queue
            WHERE id = ?
              AND status IN ('ready', 'failed', 'skipped')
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'pending', reply_text = NULL, context_json = NULL,
                last_message_hash = NULL, error = ?, locked_at = NULL,
                updated_at = ?
            WHERE id = ?
              AND status IN ('ready', 'failed', 'skipped')
            """,
            (reason or "review_regenerate", now, job_id),
        )
        conversation_key = str(row["conversation_key"] or "").strip()
        if cur.rowcount == 1 and conversation_key:
            conn.execute(
                "DELETE FROM conversation_messages WHERE conversation_key = ?",
                (conversation_key,),
            )
        return cur.rowcount == 1


def mark_approved_retry(job_id: int, *, reply_text: str, reason: str = "") -> None:
    """Return an approved reply to the approved send queue after a retryable send check."""
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'approved', reply_text = ?, error = ?,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (reply_text, reason or None, now, job_id),
        )


def reject_ready(job_id: int, reason: str = "review_rejected") -> bool:
    """Reject a ready reply from the review UI."""
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'skipped', error = ?,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
              AND status = 'ready'
            """,
            (reason or "review_rejected", now, job_id),
        )
        return cur.rowcount == 1


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
