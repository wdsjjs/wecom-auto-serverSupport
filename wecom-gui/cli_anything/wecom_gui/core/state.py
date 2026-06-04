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

from cli_anything.wecom_gui.core.text import clean_customer_reply_text, clean_history_message_text


ACTIVE_STATUSES = {"processing", "reading", "drafting", "ready", "approved", "sending"}
REPLACEABLE_ACTIVE_STATUSES = {"processing", "reading", "drafting"}
REPLY_PREVIEW_PROTECTED_STATUSES = {"ready", "approved", "sending", "done", "failed"}
DONE_REOPEN_COOLDOWN_SECONDS = 60.0
HANDOFF_TERMS = ("人工", "转人工", "人工客服")
HANDOFF_WAITING_ERROR = "handoff_waiting"
HANDOFF_NEW_MESSAGE_ERROR = "handoff_new_message"
HANDOFF_SESSION_SECONDS = 600.0
HANDOFF_VISIBLE_STATUSES = {"pending", "processing", "reading", "drafting", "ready", "approved", "sending", "failed"}
LATENCY_BUCKETS = (
    (5_000, "0-5s"),
    (15_000, "5-15s"),
    (30_000, "15-30s"),
    (60_000, "30-60s"),
)
WELCOME_PENDING = "welcome_pending"
WELCOME_SENT = "welcome_sent"
WELCOME_SKIPPED = "welcome_skipped"
WELCOME_STATUSES = {WELCOME_PENDING, WELCOME_SENT, WELCOME_SKIPPED}
SUPPLEMENT_COLLECTING_PROFILE = "collecting_profile"
SUPPLEMENT_DIGGING_NEED = "digging_need"
SUPPLEMENT_READY_TO_RECOMMEND = "ready_to_recommend"
SUPPLEMENT_RECOMMENDED = "recommended"
SUPPLEMENT_CLOSED = "closed"
SUPPLEMENT_SKIPPED = "skipped"
SUPPLEMENT_ACTIVE_STAGES = {
    SUPPLEMENT_COLLECTING_PROFILE,
    SUPPLEMENT_DIGGING_NEED,
    SUPPLEMENT_READY_TO_RECOMMEND,
}
SUPPLEMENT_STAGES = {
    *SUPPLEMENT_ACTIVE_STAGES,
    SUPPLEMENT_RECOMMENDED,
    SUPPLEMENT_CLOSED,
    SUPPLEMENT_SKIPPED,
}
SUPPLEMENT_REPLY_SOURCE = "supplement"


def state_dir() -> Path:
    configured = os.environ.get("WECOM_GUI_STATE_DIR", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".cli-anything-wecom-gui"
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


def latency_bucket(duration_ms: object) -> str:
    """Return a stable response-time bucket label."""
    try:
        value = float(duration_ms)
    except (TypeError, ValueError):
        return ""
    for upper_bound, label in LATENCY_BUCKETS:
        if value < upper_bound:
            return label
    return "60s+"


def handoff_type_for_text(text: object) -> str:
    """Classify explicit customer handoff requests."""
    body = str(text or "").strip()
    if not body:
        return ""
    return "direct" if any(term in body for term in HANDOFF_TERMS) else ""


def handoff_session_seconds() -> float:
    """Return how long a handoff conversation stays open after a reply."""
    configured = os.environ.get("WECOM_HANDOFF_SESSION_SECONDS", "").strip()
    if not configured:
        configured = os.environ.get("WECOM_GUI_HANDOFF_SESSION_SECONDS", "").strip()
    if not configured:
        return HANDOFF_SESSION_SECONDS
    try:
        return max(0.0, float(configured))
    except ValueError:
        return HANDOFF_SESSION_SECONDS


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
            handoff_type TEXT NOT NULL DEFAULT '',
            handoff_reason TEXT NOT NULL DEFAULT '',
            reply_source TEXT NOT NULL DEFAULT '',
            reply_attachments_json TEXT NOT NULL DEFAULT '[]',
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
    _ensure_column(conn, "reply_queue", "handoff_type", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reply_queue", "handoff_reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reply_queue", "reply_source", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "reply_queue", "reply_attachments_json", "TEXT NOT NULL DEFAULT '[]'")
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
            message_type TEXT NOT NULL DEFAULT '',
            media_json TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    _ensure_column(conn, "conversation_messages", "message_type", "TEXT NOT NULL DEFAULT ''")
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            conversation_key TEXT NOT NULL DEFAULT '',
            conversation TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            reply_source TEXT NOT NULL DEFAULT '',
            handoff_type TEXT NOT NULL DEFAULT '',
            duration_ms REAL,
            duration_bucket TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_metric_events_type_created
        ON metric_events(event_type, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_metric_events_conversation_created
        ON metric_events(conversation_key, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_metric_state (
            conversation_key TEXT PRIMARY KEY,
            conversation TEXT NOT NULL DEFAULT '',
            last_reply_source TEXT NOT NULL DEFAULT '',
            last_reply_at REAL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_issue_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            conversation_key TEXT NOT NULL DEFAULT '',
            conversation TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            original_reply TEXT NOT NULL DEFAULT '',
            final_reply TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            issue_text TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS welcome_states (
            customer_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            conversation_key TEXT NOT NULL DEFAULT '',
            conversation TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supplement_states (
            customer_key TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            conversation_key TEXT NOT NULL DEFAULT '',
            conversation TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            trace_id TEXT NOT NULL DEFAULT '',
            digging_count INTEGER NOT NULL DEFAULT 0,
            selected_needs_json TEXT NOT NULL DEFAULT '[]',
            known_profile_json TEXT NOT NULL DEFAULT '{}',
            last_message_hash TEXT NOT NULL DEFAULT '',
            pending_next_stage TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supplement_agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            trace_id TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            conversation_key TEXT NOT NULL DEFAULT '',
            external_user_id TEXT NOT NULL DEFAULT '',
            customer_id TEXT NOT NULL DEFAULT '',
            conversation TEXT NOT NULL DEFAULT '',
            reply_source TEXT NOT NULL DEFAULT 'supplement',
            stage TEXT NOT NULL DEFAULT '',
            message_hash TEXT NOT NULL DEFAULT '',
            latest_text_preview TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_supplement_agent_logs_trace_created
        ON supplement_agent_logs(trace_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supplement_full_test_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            trace_id TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            conversation_key TEXT NOT NULL DEFAULT '',
            external_user_id TEXT NOT NULL DEFAULT '',
            customer_id TEXT NOT NULL DEFAULT '',
            conversation TEXT NOT NULL DEFAULT '',
            reply_source TEXT NOT NULL DEFAULT 'supplement',
            stage TEXT NOT NULL DEFAULT '',
            message_hash TEXT NOT NULL DEFAULT '',
            latest_text_preview TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_supplement_full_test_logs_trace_created
        ON supplement_full_test_logs(trace_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reply_issue_tasks_status_updated
        ON reply_issue_tasks(status, updated_at)
        """
    )
    _collapse_visible_conversation_keys_by_title(conn)
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
            handoff_type TEXT NOT NULL DEFAULT '',
            handoff_reason TEXT NOT NULL DEFAULT '',
            reply_source TEXT NOT NULL DEFAULT '',
            reply_attachments_json TEXT NOT NULL DEFAULT '[]',
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
    select_handoff_type = "handoff_type" if "handoff_type" in old_columns else "'' AS handoff_type"
    select_handoff_reason = "handoff_reason" if "handoff_reason" in old_columns else "'' AS handoff_reason"
    select_reply_source = "reply_source" if "reply_source" in old_columns else "'' AS reply_source"
    select_reply_attachments = (
        "reply_attachments_json" if "reply_attachments_json" in old_columns else "'[]' AS reply_attachments_json"
    )
    rows = conn.execute(
        f"""
        SELECT id, title, preview, time_text, tags_json, raw_json, signature, status,
               attempts, last_message_hash, {select_context}, {select_click_x},
               {select_click_y}, {select_source}, reply_text, {select_handoff_type},
               {select_handoff_reason}, {select_reply_source}, {select_reply_attachments},
               error, locked_at,
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
                 click_x, click_y, source, reply_text, handoff_type, handoff_reason,
                 reply_source, reply_attachments_json, error, locked_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                row["handoff_type"],
                row["handoff_reason"],
                row["reply_source"],
                row["reply_attachments_json"],
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
    explicit_key = _clean_key_part(row.get("conversation_key"))
    if explicit_key:
        return explicit_key
    for key in ("external_userid", "external_user_id", "externalUserId", "uid", "user_id"):
        value = _clean_key_part(row.get(key))
        if value:
            return f"uid:{value}"

    title = _clean_key_part(row.get("title"))
    if title:
        return _legacy_conversation_key(title)

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


def conversation_key_for_uid(uid: object) -> str:
    """Return the stable queue key for one real WeCom external user id."""
    value = _clean_key_part(uid)
    return f"uid:{value}" if value else ""


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["raw"] = json.loads(item.pop("raw_json") or "[]")
    item["reply_attachments"] = json.loads(item.pop("reply_attachments_json", "[]") or "[]")
    return item


def _issue_row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _welcome_row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _supplement_row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["selected_needs"] = json.loads(item.pop("selected_needs_json") or "[]")
    item["known_profile"] = json.loads(item.pop("known_profile_json") or "{}")
    return item


def _record_metric_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    conversation_key: str = "",
    conversation: str = "",
    job_id: int | None = None,
    reply_source: str = "",
    handoff_type: str = "",
    duration_ms: float | int | None = None,
    details: dict | None = None,
    created_at: float | None = None,
) -> None:
    event_type = str(event_type or "").strip()
    if not event_type:
        return
    duration_value = None if duration_ms is None else float(duration_ms)
    now = time.time() if created_at is None else float(created_at)
    conn.execute(
        """
        INSERT INTO metric_events
            (event_type, conversation_key, conversation, job_id, reply_source,
             handoff_type, duration_ms, duration_bucket, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            str(conversation_key or ""),
            str(conversation or ""),
            job_id,
            str(reply_source or ""),
            str(handoff_type or ""),
            duration_value,
            latency_bucket(duration_value),
            json.dumps(details or {}, ensure_ascii=False),
            now,
        ),
    )


def record_metric(
    event_type: str,
    *,
    conversation_key: str = "",
    conversation: str = "",
    job_id: int | None = None,
    reply_source: str = "",
    handoff_type: str = "",
    duration_ms: float | int | None = None,
    details: dict | None = None,
) -> None:
    """Persist one structured metrics event."""
    with connect() as conn:
        _record_metric_event(
            conn,
            event_type=event_type,
            conversation_key=conversation_key,
            conversation=conversation,
            job_id=job_id,
            reply_source=reply_source,
            handoff_type=handoff_type,
            duration_ms=duration_ms,
            details=details,
        )


def _last_reply_source(conn: sqlite3.Connection, conversation_key: str) -> str:
    row = conn.execute(
        """
        SELECT last_reply_source
        FROM conversation_metric_state
        WHERE conversation_key = ?
        """,
        (conversation_key,),
    ).fetchone()
    return str(row["last_reply_source"] if row else "").strip()


def _set_last_reply_source(
    conn: sqlite3.Connection,
    *,
    conversation_key: str,
    conversation: str,
    reply_source: str,
    now: float,
) -> None:
    conversation_key = str(conversation_key or "").strip()
    reply_source = str(reply_source or "").strip()
    if not conversation_key or not reply_source:
        return
    conn.execute(
        """
        INSERT INTO conversation_metric_state
            (conversation_key, conversation, last_reply_source, last_reply_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(conversation_key) DO UPDATE SET
            conversation = excluded.conversation,
            last_reply_source = excluded.last_reply_source,
            last_reply_at = excluded.last_reply_at,
            updated_at = excluded.updated_at
        """,
        (conversation_key, str(conversation or ""), reply_source, now, now),
    )


def _issue_reply_summary(reply_text: str, attachments: list[dict] | None = None) -> str:
    final_reply = clean_customer_reply_text(reply_text or "")
    if final_reply:
        return final_reply
    image_count = len([item for item in attachments or [] if isinstance(item, dict)])
    return f"[图片] x{image_count}" if image_count else ""


def create_reply_issue_task(
    *,
    job_id: int,
    conversation_key: str,
    conversation: str,
    source: str,
    original_reply: str,
    final_reply: str,
    attachments: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict | None:
    """Create a pending follow-up task for later reply-quality notes."""
    source_value = str(source or "").strip() or "reply"
    original = clean_customer_reply_text(original_reply or "")
    final = _issue_reply_summary(final_reply or "", attachments)
    if not final:
        return None
    now = time.time()

    def _create(active_conn: sqlite3.Connection) -> dict | None:
        active_conn.execute(
            """
            INSERT INTO reply_issue_tasks
                (job_id, conversation_key, conversation, source, original_reply,
                 final_reply, status, issue_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)
            """,
            (
                int(job_id or 0),
                str(conversation_key or ""),
                str(conversation or ""),
                source_value,
                original,
                final,
                now,
                now,
            ),
        )
        task_id = active_conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        _record_metric_event(
            active_conn,
            event_type="reply_issue_task_created",
            conversation_key=str(conversation_key or ""),
            conversation=str(conversation or ""),
            job_id=int(job_id or 0),
            details={"source": source_value, "final_reply": final[:240]},
            created_at=now,
        )
        row = active_conn.execute("SELECT * FROM reply_issue_tasks WHERE id = ?", (task_id,)).fetchone()
        return _issue_row_to_dict(row) if row is not None else None

    if conn is not None:
        return _create(conn)
    with connect() as owned_conn:
        return _create(owned_conn)


def list_reply_issue_tasks(*, status: str = "pending", limit: int = 100) -> list[dict]:
    """List pending/completed reply-quality tasks."""
    status_value = str(status or "pending").strip()
    limit_value = max(1, int(limit or 100))
    with connect() as conn:
        if status_value:
            rows = conn.execute(
                """
                SELECT *
                FROM reply_issue_tasks
                WHERE status = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (status_value, limit_value),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM reply_issue_tasks
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit_value,),
            ).fetchall()
    return [_issue_row_to_dict(row) for row in rows]


def reply_issue_pending_count() -> int:
    """Return pending reply-quality task count."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM reply_issue_tasks WHERE status = 'pending'"
        ).fetchone()
    return int(row["count"] if row else 0)


def complete_reply_issue_task(task_id: int, *, issue_text: str) -> dict | None:
    """Complete one reply-quality task with a reviewer-entered note."""
    issue = clean_history_message_text(issue_text)
    if not issue:
        return None
    now = time.time()
    with connect() as conn:
        row = conn.execute("SELECT * FROM reply_issue_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or str(row["status"] or "") != "pending":
            return None
        conn.execute(
            """
            UPDATE reply_issue_tasks
            SET status = 'completed', issue_text = ?, updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (issue, now, task_id),
        )
        _record_metric_event(
            conn,
            event_type="review_reply_issue",
            conversation_key=str(row["conversation_key"] or ""),
            conversation=str(row["conversation"] or ""),
            job_id=row["job_id"],
            details={"source": row["source"], "issue": issue},
            created_at=now,
        )
        updated = conn.execute("SELECT * FROM reply_issue_tasks WHERE id = ?", (task_id,)).fetchone()
    return _issue_row_to_dict(updated) if updated is not None else None


def _handoff_session_active(row: sqlite3.Row | dict, *, now: float) -> bool:
    if not str(row["handoff_type"] or "").strip():
        return False
    session_seconds = handoff_session_seconds()
    if session_seconds <= 0:
        return True
    return now - float(row["updated_at"] or 0) < session_seconds


def metrics_summary(*, since_hours: float = 24.0) -> dict:
    """Return SQLite-backed review/agent metrics for a recent time window."""
    try:
        hours = max(0.0, float(since_hours))
    except (TypeError, ValueError):
        hours = 24.0
    now = time.time()
    since = now - hours * 3600
    with connect() as conn:
        event_counts = {
            row["event_type"]: row["count"]
            for row in conn.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM metric_events
                WHERE created_at >= ?
                GROUP BY event_type
                """,
                (since,),
            ).fetchall()
        }
        served_users = conn.execute(
            """
            SELECT COUNT(DISTINCT conversation_key) AS count
            FROM metric_events
            WHERE event_type = 'customer_message_received'
              AND created_at >= ?
              AND conversation_key != ''
            """,
            (since,),
        ).fetchone()["count"]
        source_counts = {
            (row["reply_source"] or "unknown"): row["count"]
            for row in conn.execute(
                """
                SELECT COALESCE(NULLIF(reply_source, ''), 'unknown') AS reply_source, COUNT(*) AS count
                FROM metric_events
                WHERE event_type = 'customer_message_received'
                  AND created_at >= ?
                GROUP BY COALESCE(NULLIF(reply_source, ''), 'unknown')
                """,
                (since,),
            ).fetchall()
        }
        handoff_counts = {
            (row["handoff_type"] or "unknown"): row["count"]
            for row in conn.execute(
                """
                SELECT COALESCE(NULLIF(handoff_type, ''), 'unknown') AS handoff_type, COUNT(*) AS count
                FROM metric_events
                WHERE event_type = 'handoff'
                  AND created_at >= ?
                GROUP BY COALESCE(NULLIF(handoff_type, ''), 'unknown')
                """,
                (since,),
            ).fetchall()
        }
        response_time_buckets = {label: 0 for _upper, label in LATENCY_BUCKETS}
        response_time_buckets["60s+"] = 0
        for row in conn.execute(
            """
            SELECT duration_bucket, COUNT(*) AS count
            FROM metric_events
            WHERE created_at >= ?
              AND duration_bucket != ''
              AND event_type IN ('ai_draft_ready', 'agent_sent')
            GROUP BY duration_bucket
            """,
            (since,),
        ).fetchall():
            response_time_buckets[row["duration_bucket"]] = row["count"]
        pending_handoff = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM reply_queue
            WHERE status = 'ready'
              AND COALESCE(handoff_type, '') != ''
            """
        ).fetchone()["count"]
    return {
        "since": since,
        "until": now,
        "since_hours": hours,
        "served_users": int(served_users or 0),
        "customer_messages": {
            "after_ai_reply": int(source_counts.get("ai", 0)),
            "after_human_reply": int(source_counts.get("human", 0)),
            "unknown": int(source_counts.get("unknown", 0)),
            "total": int(event_counts.get("customer_message_received", 0)),
        },
        "handoffs": {
            "direct": int(handoff_counts.get("direct", 0)),
            "indirect": int(handoff_counts.get("indirect", 0)),
            "unknown": int(handoff_counts.get("unknown", 0)),
            "pending": int(pending_handoff or 0),
        },
        "response_time_buckets": response_time_buckets,
        "diagnostics": {
            "review_approved": int(event_counts.get("review_approved", 0)),
            "review_rejected": int(event_counts.get("review_rejected", 0)),
            "review_saved": int(event_counts.get("review_saved", 0)),
            "review_regenerate": int(event_counts.get("review_regenerate", 0)),
            "review_reply_issue": int(event_counts.get("review_reply_issue", 0)),
            "human_edited_reply": int(event_counts.get("human_edited_reply", 0)),
            "agent_sent": int(event_counts.get("agent_sent", 0)),
            "agent_send_failed": int(event_counts.get("agent_send_failed", 0)),
            "stale_context_skipped": int(event_counts.get("stale_context_skipped", 0)),
            "image_message": int(event_counts.get("image_message", 0)),
            "image_capture_failed": int(event_counts.get("image_capture_failed", 0)),
            "ai_draft_ready": int(event_counts.get("ai_draft_ready", 0)),
        },
    }


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


def _append_preview_customer_message(
    conn: sqlite3.Connection,
    *,
    row: dict,
    conversation_key: str,
    job_id: int | None,
    message_hash: str,
    now: float,
) -> None:
    preview = clean_history_message_text(row.get("preview") or "")
    conversation_key = str(conversation_key or "").strip()
    if not conversation_key or not preview:
        return
    last = conn.execute(
        """
        SELECT text, message_type
        FROM conversation_messages
        WHERE conversation_key = ?
        ORDER BY seq DESC, id DESC
        LIMIT 1
        """,
        (conversation_key,),
    ).fetchone()
    if (
        last is not None
        and str(last["message_type"] or "") == "customer"
        and str(last["text"] or "") == preview
    ):
        return
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM conversation_messages WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()
    seq = int(seq_row["next_seq"] if seq_row else 0)
    raw = {"source": "unread-preview", "row": row}
    conn.execute(
        """
        INSERT INTO conversation_messages
            (conversation_key, job_id, message_hash, seq, role, text,
             time_text, source, role_confidence, message_type, media_json,
             raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_key,
            job_id,
            message_hash,
            seq,
            "用户",
            preview,
            str(row.get("time") or ""),
            "unread-preview",
            "low",
            "customer",
            "[]",
            json.dumps(raw, ensure_ascii=False),
            now,
        ),
    )


def _move_conversation_related_records(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    target_key: str,
    source_job_id: int | None,
    target_job_id: int | None,
) -> None:
    source_value = str(source_key or "").strip()
    target_value = str(target_key or "").strip()
    if not source_value or not target_value or source_value == target_value:
        return
    conn.execute(
        "UPDATE conversation_messages SET conversation_key = ?, job_id = ? WHERE conversation_key = ?",
        (target_value, target_job_id, source_value),
    )
    conn.execute(
        """
        UPDATE metric_events
        SET conversation_key = ?,
            job_id = CASE WHEN job_id IS NULL OR job_id = ? THEN ? ELSE job_id END
        WHERE conversation_key = ?
        """,
        (target_value, source_job_id, target_job_id, source_value),
    )
    conn.execute(
        "UPDATE reply_issue_tasks SET conversation_key = ?, job_id = ? WHERE conversation_key = ?",
        (target_value, target_job_id, source_value),
    )
    target_metric = conn.execute(
        "SELECT 1 FROM conversation_metric_state WHERE conversation_key = ?",
        (target_value,),
    ).fetchone()
    if target_metric is None:
        conn.execute(
            "UPDATE conversation_metric_state SET conversation_key = ? WHERE conversation_key = ?",
            (target_value, source_value),
        )
    else:
        conn.execute("DELETE FROM conversation_metric_state WHERE conversation_key = ?", (source_value,))
    conn.execute(
        "UPDATE supplement_agent_logs SET conversation_key = ?, job_id = ? WHERE conversation_key = ?",
        (target_value, target_job_id, source_value),
    )
    conn.execute(
        "UPDATE supplement_full_test_logs SET conversation_key = ?, job_id = ? WHERE conversation_key = ?",
        (target_value, target_job_id, source_value),
    )
    _move_welcome_state_key(conn, source_key=source_value, target_key=target_value, job_id=target_job_id)
    _move_supplement_state_key(conn, source_key=source_value, target_key=target_value, job_id=target_job_id)


def _merge_reply_queue_row(
    conn: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    target: sqlite3.Row,
    target_key: str,
    now: float,
) -> sqlite3.Row | None:
    source_key = str(source["conversation_key"] or "")
    target_id = int(target["id"])
    source_is_newer = float(source["updated_at"] or 0) >= float(target["updated_at"] or 0)
    if source_is_newer:
        target_is_handoff = bool(str(target["handoff_type"] or "").strip())
        merged_handoff_type = str(source["handoff_type"] or target["handoff_type"] or "")
        merged_handoff_reason = str(source["handoff_reason"] or target["handoff_reason"] or "")
        merged_reply_text = source["reply_text"]
        merged_reply_attachments = source["reply_attachments_json"]
        if target_is_handoff and not str(source["reply_text"] or "").strip():
            merged_reply_text = target["reply_text"]
            if str(source["reply_attachments_json"] or "[]") == "[]":
                merged_reply_attachments = target["reply_attachments_json"]
        conn.execute(
            """
            UPDATE reply_queue
            SET title = ?, preview = ?, time_text = ?, tags_json = ?, raw_json = ?,
                signature = ?, status = ?, attempts = ?, last_message_hash = ?,
                context_json = ?, click_x = ?, click_y = ?, source = ?,
                reply_text = ?, handoff_type = ?, handoff_reason = ?,
                reply_source = ?, reply_attachments_json = ?, error = ?,
                locked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                source["title"],
                source["preview"],
                source["time_text"],
                source["tags_json"],
                source["raw_json"],
                source["signature"],
                source["status"],
                source["attempts"],
                source["last_message_hash"],
                source["context_json"],
                source["click_x"],
                source["click_y"],
                source["source"],
                merged_reply_text,
                merged_handoff_type,
                merged_handoff_reason,
                source["reply_source"],
                merged_reply_attachments,
                source["error"],
                source["locked_at"],
                now,
                target_id,
            ),
        )
    _move_conversation_related_records(
        conn,
        source_key=source_key,
        target_key=target_key,
        source_job_id=int(source["id"]),
        target_job_id=target_id,
    )
    conn.execute("DELETE FROM reply_queue WHERE id = ?", (source["id"],))
    return conn.execute("SELECT * FROM reply_queue WHERE id = ?", (target_id,)).fetchone()


def _collapse_visible_conversation_keys_by_title(conn: sqlite3.Connection) -> None:
    """Move old visible-position rows to the current title fallback key."""
    rows = conn.execute(
        """
        SELECT *
        FROM reply_queue
        WHERE conversation_key LIKE 'visible:%'
          AND TRIM(title) != ''
        ORDER BY title ASC, updated_at DESC, id DESC
        """
    ).fetchall()
    now = time.time()
    for source in rows:
        source_key = str(source["conversation_key"] or "")
        target_key = _legacy_conversation_key(source["title"])
        if not target_key or source_key == target_key:
            continue
        target = conn.execute("SELECT * FROM reply_queue WHERE conversation_key = ?", (target_key,)).fetchone()
        if target is None:
            conn.execute(
                "UPDATE reply_queue SET conversation_key = ?, updated_at = ? WHERE id = ?",
                (target_key, now, source["id"]),
            )
            _move_conversation_related_records(
                conn,
                source_key=source_key,
                target_key=target_key,
                source_job_id=int(source["id"]),
                target_job_id=int(source["id"]),
            )
            continue
        if int(target["id"]) == int(source["id"]):
            continue
        _merge_reply_queue_row(conn, source=source, target=target, target_key=target_key, now=now)


def _merge_title_fallback_into_uid(
    conn: sqlite3.Connection,
    *,
    row: dict,
    uid_key: str,
    existing: sqlite3.Row | None,
    now: float,
) -> sqlite3.Row | None:
    if not uid_key.startswith("uid:"):
        return existing
    title = _clean_key_part(row.get("title"))
    if not title:
        return existing
    source_key = _legacy_conversation_key(title)
    if source_key == uid_key:
        return existing
    source = conn.execute("SELECT * FROM reply_queue WHERE conversation_key = ?", (source_key,)).fetchone()
    if source is None or (existing is not None and int(source["id"]) == int(existing["id"])):
        return existing
    if existing is None:
        conn.execute(
            "UPDATE reply_queue SET conversation_key = ?, updated_at = ? WHERE id = ?",
            (uid_key, now, source["id"]),
        )
        _move_conversation_related_records(
            conn,
            source_key=source_key,
            target_key=uid_key,
            source_job_id=int(source["id"]),
            target_job_id=int(source["id"]),
        )
        return conn.execute("SELECT * FROM reply_queue WHERE id = ?", (source["id"],)).fetchone()
    return _merge_reply_queue_row(conn, source=source, target=existing, target_key=uid_key, now=now)


def _record_customer_message_metric(
    conn: sqlite3.Connection,
    *,
    row: dict,
    conversation_key: str,
    job_id: int | None,
    now: float,
) -> None:
    reply_source = _last_reply_source(conn, conversation_key) or "unknown"
    preview = str(row.get("preview") or "").strip()
    handoff_type = handoff_type_for_text(preview)
    _record_metric_event(
        conn,
        event_type="customer_message_received",
        conversation_key=conversation_key,
        conversation=str(row.get("title") or ""),
        job_id=job_id,
        reply_source=reply_source,
        handoff_type=handoff_type,
        details={
            "preview": preview,
            "source": row.get("source") or "",
            "unread_count": int(row.get("unread_count") or 0),
        },
        created_at=now,
    )
    if handoff_type:
        _record_metric_event(
            conn,
            event_type="handoff",
            conversation_key=conversation_key,
            conversation=str(row.get("title") or ""),
            job_id=job_id,
            reply_source=reply_source,
            handoff_type=handoff_type,
            details={"reason": "customer_requested_handoff", "preview": preview},
            created_at=now,
        )


def _context_is_read_only(value: object) -> bool:
    try:
        context = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return False
    return bool(isinstance(context, dict) and context.get("read_only") is True)


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
        existing = _merge_title_fallback_into_uid(
            conn,
            row=row,
            uid_key=conversation_key,
            existing=existing,
            now=now,
        )
        if existing:
            cached_reason = _cached_read_reason_for_existing(conn, existing, row)
            if cached_reason:
                return False, _row_to_dict(existing)
        if existing and existing["signature"] == signature:
            existing_reply_text = str(existing["reply_text"] or "").strip()
            preview_text = str(row.get("preview", "")).strip()
            if (
                existing["status"] in REPLY_PREVIEW_PROTECTED_STATUSES
                and existing_reply_text
                and existing_reply_text == preview_text
            ):
                _update_visible_row(conn, row, signature, now, conversation_key)
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                return False, _row_to_dict(item)
            handoff_session_active = _handoff_session_active(existing, now=now)
            can_reopen_done = (
                existing["status"] == "done"
                and row_has_unread
                and not _context_is_read_only(existing["context_json"])
                and (
                    handoff_session_active
                    or now - float(existing["updated_at"] or 0) >= done_reopen_cooldown
                )
            )
            can_reopen_inactive = existing["status"] in {"failed", "skipped"} and row_has_unread
            if can_reopen_done or can_reopen_inactive:
                _update_visible_row(conn, row, signature, now, conversation_key)
                if handoff_session_active:
                    conn.execute(
                        """
                        UPDATE reply_queue
                        SET status = 'pending', error = ?, locked_at = NULL,
                            context_json = NULL, reply_source = 'human',
                            reply_attachments_json = '[]',
                            updated_at = ?
                        WHERE conversation_key = ?
                        """,
                        (HANDOFF_NEW_MESSAGE_ERROR, now, conversation_key),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE reply_queue
                        SET status = 'pending', error = NULL, locked_at = NULL,
                            context_json = NULL, last_message_hash = NULL,
                            reply_text = NULL, reply_source = '',
                            reply_attachments_json = '[]',
                            handoff_type = '', handoff_reason = '',
                            updated_at = ?
                        WHERE conversation_key = ?
                        """,
                        (now, conversation_key),
                    )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                _record_customer_message_metric(
                    conn,
                    row=row,
                    conversation_key=conversation_key,
                    job_id=item["id"] if item else None,
                    now=now,
                )
                return True, _row_to_dict(item)
            return False, _row_to_dict(existing)
        if existing and existing["status"] in ACTIVE_STATUSES:
            same_preview = str(existing["preview"] or "").strip() == str(row.get("preview", "")).strip()
            existing_handoff_type = str(existing["handoff_type"] or "").strip()
            existing_error = str(existing["error"] or "").strip()
            preview_text = str(row.get("preview", "")).strip()
            existing_reply_text = str(existing["reply_text"] or "").strip()
            if (
                existing_handoff_type
                and existing_reply_text
                and preview_text == existing_reply_text
            ):
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET preview = ?, time_text = ?, tags_json = ?, raw_json = ?,
                        click_x = ?, click_y = ?, source = ?, signature = ?
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
                        conversation_key,
                    ),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                return False, _row_to_dict(item)
            if existing_handoff_type and row_has_unread and not same_preview:
                _update_visible_row(conn, row, signature, now, conversation_key)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = ?, locked_at = NULL,
                        context_json = NULL, reply_source = 'human',
                        reply_attachments_json = '[]',
                        updated_at = ?
                    WHERE conversation_key = ?
                    """,
                    (HANDOFF_NEW_MESSAGE_ERROR, now, conversation_key),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                _append_preview_customer_message(
                    conn,
                    row=row,
                    conversation_key=conversation_key,
                    job_id=item["id"] if item else None,
                    message_hash=signature,
                    now=now,
                )
                _record_customer_message_metric(
                    conn,
                    row=row,
                    conversation_key=conversation_key,
                    job_id=item["id"] if item else None,
                    now=now,
                )
                return True, _row_to_dict(item)
            if (
                existing["status"] == "ready"
                and row_has_unread
                and not same_preview
                and str(existing["reply_source"] or "").strip() in {"welcome", SUPPLEMENT_REPLY_SOURCE}
            ):
                _update_visible_row(conn, row, signature, now, conversation_key)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = ?, locked_at = NULL,
                        context_json = NULL, last_message_hash = NULL,
                        reply_text = NULL, reply_attachments_json = '[]',
                        updated_at = ?
                    WHERE conversation_key = ?
                    """,
                    ("fixed_reply_superseded_by_customer_message", now, conversation_key),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                _record_customer_message_metric(
                    conn,
                    row=row,
                    conversation_key=conversation_key,
                    job_id=item["id"] if item else None,
                    now=now,
                )
                return True, _row_to_dict(item)
            if existing["status"] in REPLACEABLE_ACTIVE_STATUSES and row_has_unread and not same_preview:
                _update_visible_row(conn, row, signature, now, conversation_key)
                conn.execute(
                    """
                    UPDATE reply_queue
                    SET status = 'pending', error = NULL, locked_at = NULL,
                        context_json = NULL, last_message_hash = NULL,
                        reply_text = NULL, reply_source = '',
                        reply_attachments_json = '[]',
                        handoff_type = '', handoff_reason = '',
                        updated_at = ?
                    WHERE conversation_key = ?
                    """,
                    (now, conversation_key),
                )
                item = conn.execute(
                    "SELECT * FROM reply_queue WHERE conversation_key = ?",
                    (conversation_key,),
                ).fetchone()
                _record_customer_message_metric(
                    conn,
                    row=row,
                    conversation_key=conversation_key,
                    job_id=item["id"] if item else None,
                    now=now,
                )
                return True, _row_to_dict(item)
            return False, _row_to_dict(existing)
        if (
            existing
            and existing["status"] in REPLY_PREVIEW_PROTECTED_STATUSES
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
                    signature = ?, status = ?, error = ?,
                    reply_attachments_json = '[]', updated_at = ?
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
        _record_customer_message_metric(
            conn,
            row=row,
            conversation_key=conversation_key,
            job_id=item["id"] if item else None,
            now=now,
        )
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


def preview_matches_last_read_customer_message(row: dict) -> bool:
    """Return whether the visible row preview matches the last read customer text."""
    return bool(cached_read_reason_for_row(row) == "preview_matches_last_read_customer_message")


def cached_read_reason_for_row(row: dict) -> str:
    """Return why a visible row can be skipped because its latest message was read."""
    preview = clean_history_message_text(row.get("preview") or "")
    conversation_key = conversation_key_for_row(row)
    if not conversation_key:
        return ""
    with connect() as conn:
        queue_row = conn.execute(
            "SELECT * FROM reply_queue WHERE conversation_key = ?",
            (conversation_key,),
        ).fetchone()
        return _cached_read_reason_for_existing(conn, queue_row, row, preview=preview)


def _cached_read_reason_for_existing(
    conn: sqlite3.Connection,
    existing: sqlite3.Row | None,
    row: dict,
    *,
    preview: str | None = None,
) -> str:
    """Return a stable skip reason for unread badges that point to read content."""
    if existing is None:
        return ""
    if str(existing["handoff_type"] or "").strip():
        return ""
    status = str(existing["status"] or "")
    if status not in ACTIVE_STATUSES and status != "done":
        return ""
    preview = clean_history_message_text(row.get("preview") or "") if preview is None else preview
    existing_preview = clean_history_message_text(existing["preview"] or "")
    context = {}
    try:
        loaded_context = json.loads(str(existing["context_json"] or "{}"))
        if isinstance(loaded_context, dict):
            context = loaded_context
    except json.JSONDecodeError:
        context = {}
    latest = context.get("latest") if isinstance(context, dict) else {}
    if preview and isinstance(latest, dict):
        latest_role = str(latest.get("role") or "")
        latest_text = clean_history_message_text(latest.get("content") or latest.get("text") or "")
        if latest_role == "用户" and latest_text == preview:
            return "preview_matches_last_read_customer_message"
    if preview and existing_preview == preview and str(existing["last_message_hash"] or "").strip():
        return "preview_unchanged_after_read"
    if (
        not preview
        and not existing_preview
        and (
            str(existing["last_message_hash"] or "").strip()
            or int(context.get("message_count") or 0) > 0
            or bool(context.get("read_only"))
        )
    ):
        return "empty_preview_unchanged_after_read"
    if not preview:
        return ""
    conversation_key = str(existing["conversation_key"] or conversation_key_for_row(row))
    if conversation_key:
        message = conn.execute(
            """
            SELECT role, text
            FROM conversation_messages
            WHERE conversation_key = ?
            ORDER BY seq DESC
            LIMIT 1
            """,
            (conversation_key,),
        ).fetchone()
        if (
            message
            and str(message["role"] or "") == "用户"
            and clean_history_message_text(message["text"] or "") == preview
        ):
            return "preview_matches_last_read_customer_message"
    return ""


def get_welcome_state(customer_key: str) -> dict | None:
    """Return the welcome flow state for one stable customer key."""
    key = str(customer_key or "").strip()
    if not key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM welcome_states WHERE customer_key = ?",
            (key,),
        ).fetchone()
    return _welcome_row_to_dict(row) if row else None


def _move_welcome_state_key(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    target_key: str,
    job_id: int | None = None,
) -> None:
    source_value = str(source_key or "").strip()
    target_value = str(target_key or "").strip()
    if not source_value or not target_value or source_value == target_value:
        return
    source = conn.execute(
        "SELECT * FROM welcome_states WHERE customer_key = ?",
        (source_value,),
    ).fetchone()
    if source is None:
        return
    target = conn.execute(
        "SELECT * FROM welcome_states WHERE customer_key = ?",
        (target_value,),
    ).fetchone()
    now = time.time()
    source_rank = {WELCOME_SKIPPED: 0, WELCOME_PENDING: 1, WELCOME_SENT: 2}.get(str(source["status"] or ""), 0)
    target_rank = (
        {WELCOME_SKIPPED: 0, WELCOME_PENDING: 1, WELCOME_SENT: 2}.get(str(target["status"] or ""), -1)
        if target is not None
        else -1
    )
    if target is None or source_rank > target_rank:
        conn.execute(
            """
            INSERT INTO welcome_states
                (customer_key, status, conversation_key, conversation, job_id,
                 reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_key) DO UPDATE SET
                status = excluded.status,
                conversation_key = excluded.conversation_key,
                conversation = excluded.conversation,
                job_id = excluded.job_id,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                target_value,
                source["status"],
                target_value,
                source["conversation"],
                job_id if job_id is not None else source["job_id"],
                source["reason"],
                source["created_at"],
                now,
            ),
        )
    conn.execute("DELETE FROM welcome_states WHERE customer_key = ?", (source_value,))


def _move_supplement_state_key(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    target_key: str,
    job_id: int | None = None,
) -> None:
    source_value = str(source_key or "").strip()
    target_value = str(target_key or "").strip()
    if not source_value or not target_value or source_value == target_value:
        return
    source = conn.execute(
        "SELECT * FROM supplement_states WHERE customer_key = ?",
        (source_value,),
    ).fetchone()
    if source is None:
        return
    target = conn.execute(
        "SELECT * FROM supplement_states WHERE customer_key = ?",
        (target_value,),
    ).fetchone()
    now = time.time()
    if target is None or float(source["updated_at"] or 0) >= float(target["updated_at"] or 0):
        conn.execute(
            """
            INSERT INTO supplement_states
                (customer_key, stage, conversation_key, conversation, job_id,
                 trace_id, digging_count, selected_needs_json, known_profile_json,
                 last_message_hash, pending_next_stage, reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_key) DO UPDATE SET
                stage = excluded.stage,
                conversation_key = excluded.conversation_key,
                conversation = excluded.conversation,
                job_id = excluded.job_id,
                trace_id = excluded.trace_id,
                digging_count = excluded.digging_count,
                selected_needs_json = excluded.selected_needs_json,
                known_profile_json = excluded.known_profile_json,
                last_message_hash = excluded.last_message_hash,
                pending_next_stage = excluded.pending_next_stage,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                target_value,
                source["stage"],
                target_value,
                source["conversation"],
                job_id if job_id is not None else source["job_id"],
                source["trace_id"],
                source["digging_count"],
                source["selected_needs_json"],
                source["known_profile_json"],
                source["last_message_hash"],
                source["pending_next_stage"],
                source["reason"],
                source["created_at"],
                now,
            ),
        )
    conn.execute("DELETE FROM supplement_states WHERE customer_key = ?", (source_value,))


def mark_welcome_status(
    customer_key: str,
    status: str,
    *,
    conversation_key: str = "",
    conversation: str = "",
    job_id: int | None = None,
    reason: str = "",
) -> dict | None:
    """Persist a welcome flow status without marking sent before delivery."""
    key = str(customer_key or "").strip()
    status_value = str(status or "").strip()
    if not key or status_value not in WELCOME_STATUSES:
        return None
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM welcome_states WHERE customer_key = ?",
            (key,),
        ).fetchone()
        created_at = float(row["created_at"]) if row else now
        conn.execute(
            """
            INSERT INTO welcome_states
                (customer_key, status, conversation_key, conversation, job_id,
                 reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_key) DO UPDATE SET
                status = excluded.status,
                conversation_key = excluded.conversation_key,
                conversation = excluded.conversation,
                job_id = excluded.job_id,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                key,
                status_value,
                str(conversation_key or ""),
                str(conversation or ""),
                job_id,
                str(reason or ""),
                created_at,
                now,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM welcome_states WHERE customer_key = ?",
            (key,),
        ).fetchone()
    return _welcome_row_to_dict(updated) if updated is not None else None


def get_supplement_state(customer_key: str) -> dict | None:
    """Return supplement recommendation flow state for one customer key."""
    key = str(customer_key or "").strip()
    if not key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM supplement_states WHERE customer_key = ?",
            (key,),
        ).fetchone()
    return _supplement_row_to_dict(row) if row else None


def mark_supplement_state(
    customer_key: str,
    stage: str,
    *,
    conversation_key: str = "",
    conversation: str = "",
    job_id: int | None = None,
    trace_id: str = "",
    digging_count: int | None = None,
    selected_needs: list | None = None,
    known_profile: dict | None = None,
    message_hash: str = "",
    pending_next_stage: str = "",
    reason: str = "",
) -> dict | None:
    """Persist supplement state. Completion stages are called after send success."""
    key = str(customer_key or "").strip()
    stage_value = str(stage or "").strip()
    if not key or stage_value not in SUPPLEMENT_STAGES:
        return None
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM supplement_states WHERE customer_key = ?",
            (key,),
        ).fetchone()
        created_at = float(row["created_at"]) if row else now
        existing_trace = str(row["trace_id"] if row else "")
        existing_digging = int(row["digging_count"] if row else 0)
        existing_needs = row["selected_needs_json"] if row else "[]"
        existing_profile = row["known_profile_json"] if row else "{}"
        conn.execute(
            """
            INSERT INTO supplement_states
                (customer_key, stage, conversation_key, conversation, job_id,
                 trace_id, digging_count, selected_needs_json, known_profile_json,
                 last_message_hash, pending_next_stage, reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_key) DO UPDATE SET
                stage = excluded.stage,
                conversation_key = excluded.conversation_key,
                conversation = excluded.conversation,
                job_id = excluded.job_id,
                trace_id = excluded.trace_id,
                digging_count = excluded.digging_count,
                selected_needs_json = excluded.selected_needs_json,
                known_profile_json = excluded.known_profile_json,
                last_message_hash = excluded.last_message_hash,
                pending_next_stage = excluded.pending_next_stage,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                key,
                stage_value,
                str(conversation_key or ""),
                str(conversation or ""),
                job_id,
                str(trace_id or existing_trace or f"supp-{int(now * 1000)}"),
                existing_digging if digging_count is None else int(digging_count),
                json.dumps(selected_needs, ensure_ascii=False) if selected_needs is not None else existing_needs,
                json.dumps(known_profile, ensure_ascii=False) if known_profile is not None else existing_profile,
                str(message_hash or (row["last_message_hash"] if row else "")),
                str(pending_next_stage or ""),
                str(reason or ""),
                created_at,
                now,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM supplement_states WHERE customer_key = ?",
            (key,),
        ).fetchone()
    return _supplement_row_to_dict(updated) if updated is not None else None


def _sanitize_supplement_details(details: dict | None) -> dict:
    sanitized: dict = {}
    for key, value in (details or {}).items():
        name = str(key)
        lowered = name.lower()
        if any(secret in lowered for secret in ("api_key", "apikey", "token", "secret", "authorization", "request_body")):
            continue
        if isinstance(value, str):
            sanitized[name] = value[:240]
        elif isinstance(value, (int, float, bool)) or value is None:
            sanitized[name] = value
        elif isinstance(value, list):
            sanitized[name] = value[:20]
        elif isinstance(value, dict):
            sanitized[name] = {str(k): v for k, v in list(value.items())[:20]}
        else:
            sanitized[name] = str(value)[:240]
    return sanitized


def log_supplement_event(
    event_type: str,
    *,
    trace_id: str = "",
    job_id: int | None = None,
    conversation_key: str = "",
    external_user_id: str = "",
    customer_id: str = "",
    conversation: str = "",
    stage: str = "",
    message_hash: str = "",
    latest_text_preview: str = "",
    details: dict | None = None,
) -> None:
    """Record one supplement-agent audit log and metrics event."""
    event = str(event_type or "").strip()
    if not event:
        return
    detail_payload = _sanitize_supplement_details(details)
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO supplement_agent_logs
                (event_type, trace_id, job_id, conversation_key, external_user_id,
                 customer_id, conversation, reply_source, stage, message_hash,
                 latest_text_preview, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event,
                str(trace_id or ""),
                job_id,
                str(conversation_key or ""),
                str(external_user_id or ""),
                str(customer_id or ""),
                str(conversation or ""),
                SUPPLEMENT_REPLY_SOURCE,
                str(stage or ""),
                str(message_hash or ""),
                str(latest_text_preview or "")[:160],
                json.dumps(detail_payload, ensure_ascii=False),
                now,
            ),
        )
        _record_metric_event(
            conn,
            event_type=event,
            conversation_key=str(conversation_key or ""),
            conversation=str(conversation or ""),
            job_id=job_id,
            reply_source=SUPPLEMENT_REPLY_SOURCE,
            details=detail_payload,
            created_at=now,
        )


def log_supplement_full_test_event(
    event_type: str,
    *,
    trace_id: str = "",
    job_id: int | None = None,
    conversation_key: str = "",
    external_user_id: str = "",
    customer_id: str = "",
    conversation: str = "",
    stage: str = "",
    message_hash: str = "",
    latest_text_preview: str = "",
    details: dict | None = None,
) -> None:
    """Record one isolated full-flow supplement test log."""
    event = str(event_type or "").strip()
    if not event:
        return
    detail_payload = _sanitize_supplement_details(details)
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO supplement_full_test_logs
                (event_type, trace_id, job_id, conversation_key, external_user_id,
                 customer_id, conversation, reply_source, stage, message_hash,
                 latest_text_preview, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event,
                str(trace_id or ""),
                job_id,
                str(conversation_key or ""),
                str(external_user_id or ""),
                str(customer_id or ""),
                str(conversation or ""),
                SUPPLEMENT_REPLY_SOURCE,
                str(stage or ""),
                str(message_hash or ""),
                str(latest_text_preview or "")[:160],
                json.dumps(detail_payload, ensure_ascii=False),
                now,
            ),
        )


def list_supplement_logs(*, trace_id: str = "", event_type: str = "", limit: int = 100) -> list[dict]:
    """List recent supplement-agent logs for tests/debugging."""
    limit_value = max(1, int(limit or 100))
    with connect() as conn:
        clauses = []
        params: list[object] = []
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT *
            FROM supplement_agent_logs
            {where}
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (*params, limit_value),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        items.append(item)
    return items


def upgrade_job_conversation_key_to_uid(job_id: int, uid: str) -> dict | None:
    """Upgrade a visible-position queue key to a stable WeCom uid key."""
    target_key = conversation_key_for_uid(uid)
    if not target_key:
        return get_job(job_id)
    now = time.time()
    with connect() as conn:
        source = conn.execute("SELECT * FROM reply_queue WHERE id = ?", (job_id,)).fetchone()
        if source is None:
            return None
        source_key = str(source["conversation_key"] or "")
        if source_key == target_key:
            return _row_to_dict(source)
        target = conn.execute("SELECT * FROM reply_queue WHERE conversation_key = ?", (target_key,)).fetchone()
        if target is not None and int(target["id"]) != int(job_id):
            row = _merge_reply_queue_row(
                conn,
                source=source,
                target=target,
                target_key=target_key,
                now=now,
            )
            return _row_to_dict(row) if row else None

        conn.execute(
            "UPDATE reply_queue SET conversation_key = ?, updated_at = ? WHERE id = ?",
            (target_key, now, job_id),
        )
        _move_conversation_related_records(
            conn,
            source_key=source_key,
            target_key=target_key,
            source_job_id=job_id,
            target_job_id=job_id,
        )
        row = conn.execute("SELECT * FROM reply_queue WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None


def queue_counts() -> dict[str, int]:
    """Return queue counts grouped by status."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM reply_queue GROUP BY status ORDER BY status"
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def handoff_pending_count() -> int:
    """Return active items that require manual handoff handling."""
    expire_stale_handoffs()
    with connect() as conn:
        placeholders = ",".join("?" for _ in HANDOFF_VISIBLE_STATUSES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM reply_queue
            WHERE status IN ({placeholders})
              AND COALESCE(handoff_type, '') != ''
            """,
            tuple(HANDOFF_VISIBLE_STATUSES),
        ).fetchone()
    return int(row["count"] if row else 0)


def handoff_attention_count() -> int:
    """Return handoff items where the customer has sent a new message."""
    expire_stale_handoffs()
    with connect() as conn:
        placeholders = ",".join("?" for _ in HANDOFF_VISIBLE_STATUSES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM reply_queue
            WHERE status IN ({placeholders})
              AND COALESCE(handoff_type, '') != ''
              AND error = ?
            """,
            (*tuple(HANDOFF_VISIBLE_STATUSES), HANDOFF_NEW_MESSAGE_ERROR),
        ).fetchone()
    return int(row["count"] if row else 0)


def expire_stale_handoffs(*, now: float | None = None) -> int:
    """Close handoff sessions that have been idle beyond the handoff window."""
    current = time.time() if now is None else now
    session_seconds = handoff_session_seconds()
    if session_seconds <= 0:
        return 0
    cutoff = current - session_seconds
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'done', handoff_type = '', handoff_reason = '',
                error = 'handoff_expired', locked_at = NULL, updated_at = ?
            WHERE status = 'ready'
              AND COALESCE(handoff_type, '') != ''
              AND error = ?
              AND updated_at < ?
            """,
            (current, HANDOFF_WAITING_ERROR, cutoff),
        )
        return cur.rowcount


def list_ready_for_review(*, handoff: bool = False, limit: int = 100) -> list[dict]:
    """List ready review jobs split by ordinary drafts vs handoff items."""
    expire_stale_handoffs()
    with connect() as conn:
        if handoff:
            placeholders = ",".join("?" for _ in HANDOFF_VISIBLE_STATUSES)
            rows = conn.execute(
                f"""
                SELECT *
                FROM reply_queue
                WHERE status IN ({placeholders})
                  AND COALESCE(handoff_type, '') != ''
                ORDER BY
                  CASE
                    WHEN error = ? THEN 0
                    WHEN status IN ('pending', 'reading', 'drafting') THEN 1
                    ELSE 2
                  END,
                  updated_at DESC
                LIMIT ?
                """,
                (*tuple(HANDOFF_VISIBLE_STATUSES), HANDOFF_NEW_MESSAGE_ERROR, limit),
            ).fetchall()
            return [_row_to_dict(row) for row in rows]
        rows = conn.execute(
            """
            SELECT *
            FROM reply_queue
            WHERE status = 'ready'
              AND COALESCE(handoff_type, '') = ''
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


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
            rows = conn.execute(
                """
                SELECT * FROM wecom_customer_bindings
                WHERE customer_name = ? OR display_name = ?
                ORDER BY updated_at DESC
                """,
                (customer_name, customer_name),
            ).fetchall()
            unique_uids = {str(item["uid"] or "") for item in rows}
            if len(unique_uids) == 1:
                row = rows[0]
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


def claim_pending_for_read() -> dict | None:
    """Claim one pending job for fast context intake."""
    return _claim_status("pending", "reading")


def claim_ready_to_send() -> dict | None:
    """Claim one AI-ready job for final sending."""
    now = time.time()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT *
            FROM reply_queue
            WHERE status = 'ready'
              AND COALESCE(handoff_type, '') = ''
              AND COALESCE(reply_source, '') NOT IN ('welcome', 'supplement')
            ORDER BY updated_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'sending', attempts = attempts + 1,
                locked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, row["id"]),
        )
        conn.commit()
        claimed = conn.execute("SELECT * FROM reply_queue WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_dict(claimed)
    finally:
        conn.close()


def claim_approved_to_send() -> dict | None:
    """Claim one web-approved job for final sending."""
    return _claim_status("approved", "sending")


def _message_type_for_record(message: dict) -> str:
    explicit = str(message.get("message_type") or "").strip().lower()
    if explicit in {"customer", "reply", "system", "unknown"}:
        return explicit
    role = str(message.get("role") or "").strip().lower()
    text = str(message.get("content") or message.get("text") or "").strip()
    if "以上是打招呼内容" in text or ("你已添加了" in text and "现在可以开始聊天了" in text):
        return "system"
    if role in {"customer", "user", "human"} or "用户" in role or "客户" in role:
        return "customer"
    if (
        role in {"reply", "service", "assistant", "agent", "staff", "ai", "bot"}
        or "客服" in role
        or "坐席" in role
    ):
        return "reply"
    return "unknown"


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
        title_row = conn.execute(
            "SELECT title FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone() if job_id is not None else None
        conversation = str(title_row["title"] if title_row else "")
        conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_key = ?",
            (conversation_key,),
        )
        for index, message in enumerate(messages):
            text = clean_history_message_text(message.get("content") or message.get("text") or "")
            media = message.get("media") if isinstance(message.get("media"), list) else []
            conn.execute(
                """
                INSERT INTO conversation_messages
                    (conversation_key, job_id, message_hash, seq, role, text,
                     time_text, source, role_confidence, message_type, media_json,
                     raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _message_type_for_record(message),
                    json.dumps(media, ensure_ascii=False),
                    json.dumps(message, ensure_ascii=False),
                    now,
                ),
            )
            for media_item in media:
                if not isinstance(media_item, dict):
                    continue
                _record_metric_event(
                    conn,
                    event_type="image_message",
                    conversation_key=conversation_key,
                    conversation=conversation,
                    job_id=job_id,
                    details={
                        "seq": index,
                        "capture_ok": media_item.get("capture_ok"),
                        "type": media_item.get("type") or "image",
                    },
                    created_at=now,
                )
                if media_item.get("capture_ok") is False:
                    _record_metric_event(
                        conn,
                        event_type="image_capture_failed",
                        conversation_key=conversation_key,
                        conversation=conversation,
                        job_id=job_id,
                        details={"seq": index, "error": str(media_item.get("error") or "")},
                        created_at=now,
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


def _reply_attachments_to_media(attachments: list[dict] | None) -> list[dict]:
    media: list[dict] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        path = str(attachment.get("path") or "").strip()
        if not path:
            continue
        media.append(
            {
                "type": str(attachment.get("type") or "image"),
                "capture_path": path,
                "capture_ok": True,
                "source": "reply_attachment",
                "name": str(attachment.get("name") or "图片"),
                "content_type": str(attachment.get("content_type") or ""),
            }
        )
    return media


def _append_conversation_reply(
    conn: sqlite3.Connection,
    *,
    conversation_key: str,
    job_id: int,
    message_hash: str,
    reply_text: str | None,
    attachments: list[dict] | None = None,
    now: float,
) -> None:
    conversation_key = str(conversation_key or "").strip()
    if not conversation_key:
        return
    final_reply = clean_history_message_text(reply_text or "")
    media = _reply_attachments_to_media(attachments)
    if not final_reply and not media:
        return
    last = conn.execute(
        """
        SELECT text, media_json, message_type
        FROM conversation_messages
        WHERE conversation_key = ?
        ORDER BY seq DESC, id DESC
        LIMIT 1
        """,
        (conversation_key,),
    ).fetchone()
    media_json = json.dumps(media, ensure_ascii=False)
    if (
        last is not None
        and str(last["message_type"] or "") == "reply"
        and str(last["text"] or "") == final_reply
        and str(last["media_json"] or "[]") == media_json
    ):
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM conversation_messages WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()
    seq = int(row["next_seq"] if row else 0)
    raw = {"source": "sent_reply", "attachments": attachments or []}
    conn.execute(
        """
        INSERT INTO conversation_messages
            (conversation_key, job_id, message_hash, seq, role, text,
             time_text, source, role_confidence, message_type, media_json,
             raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_key,
            job_id,
            message_hash,
            seq,
            "客服",
            final_reply,
            "",
            "sent_reply",
            "high",
            "reply",
            media_json,
            json.dumps(raw, ensure_ascii=False),
            now,
        ),
    )


def get_conversation_media(*, message_id: int, media_index: int) -> dict | None:
    """Return a recorded media entry by opaque DB id/index, never by path."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, media_json FROM conversation_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        media_items = json.loads(row["media_json"] or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(media_items, list) or media_index < 0 or media_index >= len(media_items):
        return None
    media = media_items[media_index]
    if not isinstance(media, dict):
        return None
    path = str(media.get("capture_path") or "").strip()
    if not path or media.get("capture_ok") is False:
        return None
    return {**media, "message_id": message_id, "media_index": media_index, "capture_path": path}


def mark_drafting(
    job_id: int,
    *,
    message_hash: str,
    messages: list[dict],
    latest: dict,
    extra_context: dict | None = None,
) -> None:
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
    if extra_context:
        context.update(extra_context)
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


def mark_read_logged(job_id: int, *, message_hash: str, messages: list[dict], reason: str = "read_only") -> None:
    """Finish a job after recording the read context without producing a reply."""
    now = time.time()
    context = {
        "message_count": len(messages),
        "read_only": True,
        "reason": reason,
    }
    with connect() as conn:
        row = conn.execute("SELECT conversation_key FROM reply_queue WHERE id = ?", (job_id,)).fetchone()
        conversation_key = str(row["conversation_key"] if row else "")
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'done', last_message_hash = ?, context_json = ?,
                reply_text = NULL, reply_source = '', error = ?,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_hash, json.dumps(context, ensure_ascii=False), reason, now, job_id),
        )
    record_conversation_messages(
        conversation_key=conversation_key,
        job_id=job_id,
        message_hash=message_hash,
        messages=messages,
    )


def mark_ready(
    job_id: int,
    *,
    reply_text: str,
    reply_source: str = "ai",
    duration_ms: float | int | None = None,
    action: str = "",
) -> None:
    final_reply = clean_customer_reply_text(reply_text)
    source = str(reply_source or "ai").strip() or "ai"
    action_value = str(action or "").strip()
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT conversation_key, title FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        conversation_key = str(row["conversation_key"] if row else "")
        conversation = str(row["title"] if row else "")
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', reply_text = ?, reply_source = ?,
                handoff_type = '', handoff_reason = '', error = NULL,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (final_reply, source, now, job_id),
        )
        _record_metric_event(
            conn,
            event_type="ai_draft_ready",
            conversation_key=conversation_key,
            conversation=conversation,
            job_id=job_id,
            reply_source=source,
            handoff_type="indirect" if action_value == "handoff" else "",
            duration_ms=duration_ms,
            details={"action": action_value, "reply_preview": final_reply[:240]},
            created_at=now,
        )
        if action_value == "handoff":
            _record_metric_event(
                conn,
                event_type="handoff",
                conversation_key=conversation_key,
                conversation=conversation,
                job_id=job_id,
                reply_source=source,
                handoff_type="indirect",
                duration_ms=duration_ms,
                details={"reason": "ai_decision_handoff"},
                created_at=now,
            )


def mark_handoff_pending(
    job_id: int,
    *,
    handoff_type: str,
    handoff_reason: str,
    reply_text: str,
    duration_ms: float | int | None = None,
) -> None:
    """Move a job into review-visible manual handoff pending state."""
    kind = str(handoff_type or "").strip().lower()
    if kind not in {"direct", "indirect"}:
        kind = "indirect"
    reason = str(handoff_reason or "").strip() or "需要人工处理"
    final_reply = clean_customer_reply_text(reply_text)
    reply_source = "ai" if final_reply else "human"
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT conversation_key, title FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        conversation_key = str(row["conversation_key"] if row else "")
        conversation = str(row["title"] if row else "")
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', reply_text = ?, reply_source = ?,
                handoff_type = ?, handoff_reason = ?, error = NULL,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (final_reply or None, reply_source, kind, reason, now, job_id),
        )
        _record_metric_event(
            conn,
            event_type="ai_draft_ready",
            conversation_key=conversation_key,
            conversation=conversation,
            job_id=job_id,
            reply_source=reply_source,
            handoff_type=kind,
            duration_ms=duration_ms,
            details={"action": "handoff", "reason": reason, "reply_preview": final_reply[:240]},
            created_at=now,
        )
        existing_handoff = conn.execute(
            """
            SELECT 1
            FROM metric_events
            WHERE event_type = 'handoff'
              AND job_id = ?
              AND handoff_type = ?
            LIMIT 1
            """,
            (job_id, kind),
        ).fetchone()
        if existing_handoff is None:
            _record_metric_event(
                conn,
                event_type="handoff",
                conversation_key=conversation_key,
                conversation=conversation,
                job_id=job_id,
                reply_source=reply_source,
                handoff_type=kind,
                duration_ms=duration_ms,
                details={"reason": reason},
                created_at=now,
            )


def mark_handoff_waiting(
    job_id: int,
    *,
    message_hash: str | None = None,
    reply_text: str | None = None,
    reply_source: str | None = None,
    attachments: list[dict] | None = None,
) -> None:
    """Keep a manual handoff conversation open after one human reply is sent."""
    final_reply = clean_customer_reply_text(reply_text) if reply_text is not None else None
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT conversation_key, title, reply_source
            FROM reply_queue
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        final_reply_source = str(reply_source or (row["reply_source"] if row else "") or "human").strip()
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', last_message_hash = ?, reply_text = ?,
                reply_source = ?, reply_attachments_json = '[]', error = ?,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_hash, final_reply, final_reply_source, HANDOFF_WAITING_ERROR, now, job_id),
        )
        if row is not None:
            _append_conversation_reply(
                conn,
                conversation_key=str(row["conversation_key"] or ""),
                job_id=job_id,
                message_hash=message_hash or "",
                reply_text=final_reply,
                attachments=attachments,
                now=now,
            )
            _set_last_reply_source(
                conn,
                conversation_key=str(row["conversation_key"] or ""),
                conversation=str(row["title"] or ""),
                reply_source=final_reply_source,
                now=now,
            )


def mark_handoff_attention(job_id: int, reason: str = HANDOFF_NEW_MESSAGE_ERROR) -> None:
    """Return a handoff job to the review page when the customer keeps chatting."""
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', reply_text = NULL, reply_source = 'human',
                reply_attachments_json = '[]',
                error = ?, locked_at = NULL, updated_at = ?
            WHERE id = ?
              AND COALESCE(handoff_type, '') != ''
            """,
            (reason or HANDOFF_NEW_MESSAGE_ERROR, now, job_id),
        )


def mark_approved(job_id: int, *, reply_text: str | None = None, attachments: list[dict] | None = None) -> bool:
    """Approve a ready reply for agent-managed sending."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status, reply_text, reply_source, conversation_key, title, handoff_type, error, reply_attachments_json
            FROM reply_queue
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row["status"] != "ready":
            return False
        final_reply = clean_customer_reply_text(reply_text if reply_text is not None else row["reply_text"] or "")
        previous_reply = clean_customer_reply_text(row["reply_text"] or "")
        if attachments is None:
            try:
                final_attachments = json.loads(row["reply_attachments_json"] or "[]")
            except json.JSONDecodeError:
                final_attachments = []
        else:
            final_attachments = attachments
        if not final_reply and not final_attachments:
            return False
        is_handoff = bool(str(row["handoff_type"] or "").strip())
        reply_was_edited = bool(reply_text is not None and final_reply != previous_reply)
        original_reply_source = str(row["reply_source"] or "").strip()
        if original_reply_source == SUPPLEMENT_REPLY_SOURCE:
            reply_source = SUPPLEMENT_REPLY_SOURCE
        elif is_handoff or reply_was_edited:
            reply_source = "human"
        else:
            reply_source = original_reply_source or "ai"
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'approved', reply_text = ?, reply_source = ?,
                reply_attachments_json = ?, error = NULL,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (final_reply, reply_source, json.dumps(final_attachments or [], ensure_ascii=False), now, job_id),
        )
        if cur.rowcount == 1 and reply_was_edited:
            _record_metric_event(
                conn,
                event_type="human_edited_reply",
                conversation_key=str(row["conversation_key"] or ""),
                conversation=str(row["title"] or ""),
                job_id=job_id,
                reply_source=reply_source,
                details={"action": "approve"},
                created_at=now,
            )
        if cur.rowcount == 1 and is_handoff:
            _append_conversation_reply(
                conn,
                conversation_key=str(row["conversation_key"] or ""),
                job_id=job_id,
                message_hash="",
                reply_text=final_reply,
                attachments=final_attachments,
                now=now,
            )
        if cur.rowcount == 1 and reply_was_edited and not is_handoff:
            create_reply_issue_task(
                job_id=job_id,
                conversation_key=str(row["conversation_key"] or ""),
                conversation=str(row["title"] or ""),
                source="review_edit",
                original_reply=previous_reply,
                final_reply=final_reply,
                attachments=final_attachments,
                conn=conn,
            )
        return cur.rowcount == 1


def save_reply(job_id: int, *, reply_text: str, attachments: list[dict] | None = None) -> bool:
    """Save a reviewer-authored reply and keep it waiting for approval."""
    final_reply = clean_customer_reply_text(reply_text)
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT conversation_key, title, handoff_type, error, reply_attachments_json FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return False
        handoff_error = str(row["error"] or "").strip()
        next_error = HANDOFF_NEW_MESSAGE_ERROR if handoff_error == HANDOFF_NEW_MESSAGE_ERROR else None
        if attachments is None:
            try:
                final_attachments = json.loads(row["reply_attachments_json"] or "[]")
            except json.JSONDecodeError:
                final_attachments = []
        else:
            final_attachments = attachments
        if not final_reply and not final_attachments:
            return False
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'ready', reply_text = ?, reply_source = 'human',
                reply_attachments_json = ?, error = ?,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
              AND status IN ('ready', 'failed', 'skipped')
            """,
            (final_reply, json.dumps(final_attachments or [], ensure_ascii=False), next_error, now, job_id),
        )
        if cur.rowcount == 1:
            _record_metric_event(
                conn,
                event_type="human_edited_reply",
                conversation_key=str(row["conversation_key"] if row else ""),
                conversation=str(row["title"] if row else ""),
                job_id=job_id,
                reply_source="human",
                details={"action": "save"},
                created_at=now,
            )
        return cur.rowcount == 1


def save_reply_attachments(job_id: int, *, attachments: list[dict]) -> bool:
    """Update reviewer image attachments without requiring reply body content."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT handoff_type, error FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return False
        if str(row["handoff_type"] or "").strip() and str(row["error"] or "").strip() == HANDOFF_WAITING_ERROR:
            return False
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET reply_attachments_json = ?, updated_at = ?
            WHERE id = ?
              AND status IN ('ready', 'failed', 'skipped')
            """,
            (json.dumps(attachments or [], ensure_ascii=False), now, job_id),
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
                last_message_hash = NULL, reply_source = '', reply_attachments_json = '[]',
                handoff_type = '', handoff_reason = '',
                error = ?, locked_at = NULL,
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
    final_reply = clean_customer_reply_text(reply_text)
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT reply_source FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        reply_source = str(row["reply_source"] if row else "").strip() or "ai"
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'approved', reply_text = ?, reply_source = ?, error = ?,
                locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (final_reply, reply_source, reason or None, now, job_id),
        )


def mark_send_verification_failed(job_id: int, *, reply_text: str, reason: str = "sent_reply_not_visible") -> None:
    """Return a failed send to review without recording it as sent or re-pasting automatically."""
    final_reply = clean_customer_reply_text(reply_text)
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT conversation_key, title, reply_source, handoff_type FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return
        is_handoff = bool(str(row["handoff_type"] or "").strip())
        next_status = "ready" if is_handoff else "failed"
        next_reply_source = str(row["reply_source"] or "").strip() or ("human" if is_handoff else "ai")
        conn.execute(
            """
            UPDATE reply_queue
            SET status = ?, reply_text = ?, reply_source = ?,
                error = ?, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (next_status, final_reply, next_reply_source, reason or "sent_reply_not_visible", now, job_id),
        )
        _record_metric_event(
            conn,
            event_type="agent_send_verification_failed",
            conversation_key=str(row["conversation_key"] or ""),
            conversation=str(row["title"] or ""),
            job_id=job_id,
            reply_source=next_reply_source,
            handoff_type=str(row["handoff_type"] or ""),
            details={"reason": reason or "sent_reply_not_visible", "reply_preview": final_reply[:240]},
            created_at=now,
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


def finish_handoff(job_id: int, reason: str = "handoff_finished") -> bool:
    """Close a manual handoff conversation when the reviewer is done."""
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE reply_queue
            SET status = 'done', handoff_type = '', handoff_reason = '',
                error = ?, locked_at = NULL, updated_at = ?
            WHERE id = ?
              AND status = 'ready'
              AND COALESCE(handoff_type, '') != ''
            """,
            (reason or "handoff_finished", now, job_id),
        )
        return cur.rowcount == 1


def mark_pending(job_id: int, reason: str = "", *, preserve_context: bool = False) -> None:
    now = time.time()
    with connect() as conn:
        if preserve_context:
            conn.execute(
                """
                UPDATE reply_queue
                SET status = 'pending', error = ?, locked_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (reason or None, now, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE reply_queue
                SET status = 'pending', error = ?, context_json = NULL,
                    last_message_hash = NULL, locked_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (reason or None, now, job_id),
            )


def mark_done(
    job_id: int,
    *,
    message_hash: str | None = None,
    reply_text: str | None = None,
    reply_source: str | None = None,
    attachments: list[dict] | None = None,
    reply_parts: list[str] | None = None,
    duration_ms: float | int | None = None,
) -> None:
    final_reply = clean_customer_reply_text(reply_text) if reply_text is not None else None
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT conversation_key, title, reply_source FROM reply_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        final_reply_source = str(reply_source or (row["reply_source"] if row else "") or "ai").strip()
        conn.execute(
            """
            UPDATE reply_queue
            SET status = 'done', last_message_hash = ?, reply_text = ?, reply_source = ?,
                error = NULL, locked_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (message_hash, final_reply, final_reply_source, now, job_id),
        )
        if row is not None:
            parts = [clean_customer_reply_text(part) for part in (reply_parts or [])]
            parts = [part for part in parts if part]
            if not parts:
                parts = [final_reply or ""]
            for index, part in enumerate(parts):
                _append_conversation_reply(
                    conn,
                    conversation_key=str(row["conversation_key"] or ""),
                    job_id=job_id,
                    message_hash=message_hash or "",
                    reply_text=part,
                    attachments=attachments if index == len(parts) - 1 else [],
                    now=now,
                )
            _set_last_reply_source(
                conn,
                conversation_key=str(row["conversation_key"] or ""),
                conversation=str(row["title"] or ""),
                reply_source=final_reply_source,
                now=now,
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
