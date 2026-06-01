from __future__ import annotations

import time
from pathlib import Path

from .db import connect, ensure_schema

IDLE = "idle"
BUSY = "busy"
COMPRESSING = "compressing"
OFFLINE = "offline"

CLAIMABLE_STATUSES = {IDLE}
STALE_RECLAIMABLE_STATUSES = {BUSY}


def _now(value: float | None = None) -> float:
    return time.time() if value is None else float(value)


def ensure_worker_schema(conn) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codex_workers (
            worker_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'idle',
            current_job_id TEXT,
            lease_until REAL NOT NULL DEFAULT 0,
            last_seen REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()


def _row_to_dict(row) -> dict:
    if not row:
        return {}
    item = dict(row)
    item["job_id"] = item.get("current_job_id")
    return item


def register_worker(db_path: str | Path, worker_id: str, *, now: float | None = None) -> dict:
    ts = _now(now)
    conn = connect(db_path)
    try:
        ensure_worker_schema(conn)
        conn.execute(
            """
            INSERT INTO codex_workers
                (worker_id, status, current_job_id, lease_until, last_seen, updated_at)
            VALUES (?, 'idle', NULL, 0, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                status = CASE
                    WHEN codex_workers.status = 'offline' THEN 'idle'
                    ELSE codex_workers.status
                END,
                last_seen = excluded.last_seen,
                updated_at = excluded.updated_at
            """,
            (worker_id, ts, ts),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM codex_workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def heartbeat_worker(
    db_path: str | Path,
    worker_id: str,
    *,
    status: str = IDLE,
    now: float | None = None,
    job_id: str | None = None,
    lease_timeout: float = 300.0,
) -> dict:
    ts = _now(now)
    lease_until = ts + lease_timeout if status in {BUSY, COMPRESSING} else 0
    conn = connect(db_path)
    try:
        ensure_worker_schema(conn)
        conn.execute(
            """
            INSERT INTO codex_workers
                (worker_id, status, current_job_id, lease_until, last_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                status = excluded.status,
                current_job_id = excluded.current_job_id,
                lease_until = excluded.lease_until,
                last_seen = excluded.last_seen,
                updated_at = excluded.updated_at
            """,
            (worker_id, status, job_id, lease_until, ts, ts),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM codex_workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def claim_worker(
    db_path: str | Path,
    *,
    job_id: str,
    now: float | None = None,
    lease_timeout: float = 300.0,
) -> dict | None:
    ts = _now(now)
    conn = connect(db_path)
    try:
        ensure_worker_schema(conn)
        if getattr(conn, "is_pg", False):
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT * FROM codex_workers
                WHERE status = 'idle'
                   OR (status = 'busy' AND lease_until < ?)
                ORDER BY
                    CASE WHEN status = 'idle' THEN 0 ELSE 1 END,
                    last_seen ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (ts,),
            ).fetchone()
        else:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM codex_workers
                WHERE status = 'idle'
                   OR (status = 'busy' AND lease_until < ?)
                ORDER BY
                    CASE WHEN status = 'idle' THEN 0 ELSE 1 END,
                    last_seen ASC
                LIMIT 1
                """,
                (ts,),
            ).fetchone()
        if row is None:
            conn.commit()
            return None
        lease_until = ts + lease_timeout
        conn.execute(
            """
            UPDATE codex_workers
            SET status = 'busy',
                current_job_id = ?,
                lease_until = ?,
                last_seen = ?,
                updated_at = ?
            WHERE worker_id = ?
            """,
            (job_id, lease_until, ts, ts, row["worker_id"]),
        )
        conn.commit()
        claimed = conn.execute("SELECT * FROM codex_workers WHERE worker_id = ?", (row["worker_id"],)).fetchone()
        return _row_to_dict(claimed)
    finally:
        conn.close()


def complete_worker(db_path: str | Path, worker_id: str, *, job_id: str, now: float | None = None) -> bool:
    ts = _now(now)
    conn = connect(db_path)
    try:
        ensure_worker_schema(conn)
        cur = conn.execute(
            """
            UPDATE codex_workers
            SET status = 'idle',
                current_job_id = NULL,
                lease_until = 0,
                last_seen = ?,
                updated_at = ?
            WHERE worker_id = ?
              AND current_job_id = ?
            """,
            (ts, ts, worker_id, job_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def list_workers(db_path: str | Path) -> list[dict]:
    conn = connect(db_path)
    try:
        ensure_worker_schema(conn)
        rows = conn.execute("SELECT * FROM codex_workers ORDER BY worker_id").fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()
