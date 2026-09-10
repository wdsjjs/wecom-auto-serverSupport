"""Ordered capture identities, independent of sender direction and viewport position."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from cli_anything.wecom_gui.core import edge_state


HISTORY_LIMIT = 200


@contextmanager
def transaction():
    with edge_state._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS edge_message_ledger_heads (
            conversation_key TEXT PRIMARY KEY, initialized_at REAL NOT NULL)""")
        edge_state._ensure_column(conn, "edge_message_ledger_heads", "stream_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS edge_message_ledger (
            conversation_key TEXT NOT NULL, sequence INTEGER NOT NULL,
            match_key TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE,
            event_id TEXT NOT NULL, occurred_at REAL NOT NULL,
            capture_status TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (conversation_key, sequence))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS edge_message_alignment_pending (
            conversation_key TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, observed_at REAL NOT NULL,
            PRIMARY KEY (conversation_key, snapshot_hash))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS edge_media_capture_state (
            event_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            files_json TEXT NOT NULL DEFAULT '[]')""")
        yield conn


def _alignment(history: list[dict], keys: list[str]) -> tuple[int, int] | None:
    """Only accept a unique longest contiguous overlap with the persisted sequence."""
    matches = []
    for start in range(len(history)):
        size = min(len(history) - start, len(keys))
        if [row["match_key"] for row in history[start:start + size]] == keys[:size]:
            matches.append((start, size))
    if not matches:
        return None
    longest = max(size for _start, size in matches)
    best = [(start, size) for start, size in matches if size == longest]
    return best[0] if len(best) == 1 else None


def prepare(conn, conversation_key: str, candidates: list[dict], *, bootstrap_recent_count: int = 0):
    """Return persistent rows for a snapshot, or retain a gap without moving the tail.

    Candidate match keys exclude relative time labels, sender and capture paths.
    Legacy observations only seed the first ordered snapshot; they never determine
    the identity of later messages with the same text.
    """
    now = time.time()
    head = conn.execute(
        "SELECT * FROM edge_message_ledger_heads WHERE conversation_key = ?", (conversation_key,)
    ).fetchone()
    history = [dict(row) for row in conn.execute(
        "SELECT * FROM edge_message_ledger WHERE conversation_key = ? ORDER BY sequence DESC LIMIT ?",
        (conversation_key, HISTORY_LIMIT),
    )][::-1]
    keys = [item["match_key"] for item in candidates]
    snapshot_hash = hashlib.sha256(json.dumps(keys).encode()).hexdigest()

    def gap():
        conn.execute("""INSERT OR IGNORE INTO edge_message_alignment_pending
            (conversation_key, snapshot_hash, snapshot_json, observed_at) VALUES (?, ?, ?, ?)""",
            (conversation_key, snapshot_hash, json.dumps(candidates, ensure_ascii=False), now))
        return [], "message_alignment_pending"

    if not keys:
        legacy_head = conn.execute(
            "SELECT 1 FROM edge_chat_observation_baselines WHERE conversation_key = ?", (conversation_key,)
        ).fetchone()
        if head is None and legacy_head is None:
            conn.execute("INSERT INTO edge_message_ledger_heads(conversation_key, initialized_at, stream_id) VALUES (?, ?, ?)",
                         (conversation_key, now, str(uuid.uuid4())))
        return [], "empty_snapshot"

    baseline_end = 0
    inherited = {}
    if head is None:
        legacy_head = conn.execute(
            "SELECT 1 FROM edge_chat_observation_baselines WHERE conversation_key = ?", (conversation_key,)
        ).fetchone()
        if legacy_head:
            for index, item in enumerate(candidates):
                observation = conn.execute("""SELECT * FROM edge_chat_observations
                    WHERE conversation_key = ? AND fingerprint = ?""",
                    (conversation_key, item["legacy_fingerprint"])).fetchone()
                if observation:
                    baseline_end = index + 1
                    # The legacy hash can only be reused when its capture time also
                    # ties it to this observation. Text alone is not an identity.
                    event = conn.execute("SELECT * FROM edge_inbound_events WHERE dedupe_key = ?",
                        (f'{conversation_key}:{item["legacy_hash"]}',)).fetchone()
                    if event and abs(event["created_at"] - observation["observed_at"]) < 5:
                        inherited[index] = json.loads(event["payload_json"])
            if not baseline_end:
                return gap()
        else:
            baseline_end = len(keys) - min(len(keys), max(0, bootstrap_recent_count))
        conn.execute("INSERT INTO edge_message_ledger_heads(conversation_key, initialized_at, stream_id) VALUES (?, ?, ?)",
                     (conversation_key, now, str(uuid.uuid4())))
        start, overlap = 0, 0
    elif history:
        alignment = _alignment(history, keys)
        if alignment is None:
            return gap()
        start, overlap = alignment
        # Identical image placeholders do not prove that a sliding window is unchanged.
        # Retain the snapshot for recovery until text or captured visual identity anchors it.
        if overlap >= 2 and all(candidates[i].get("media_only") for i in range(overlap)):
            return gap()
    else:
        start, overlap = 0, 0

    rows = history[start:start + overlap]
    sequence = history[-1]["sequence"] if history else 0
    for index in range(overlap, len(keys)):
        sequence += 1
        event_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        row = {
            "conversation_key": conversation_key, "sequence": sequence,
            "match_key": keys[index], "event_hash": event_hash,
            "event_id": f"edge-msg-{event_hash[:32]}", "occurred_at": now,
            "capture_status": "baseline" if index < baseline_end else "pending_direction",
            "direction": "unknown",
        }
        previous = inherited.get(index)
        if previous:
            message = previous["message"]
            direction = message.get("direction") or previous["event_type"].removesuffix("_message")
            row.update(event_hash=message["hash"], event_id=message["id"], direction=direction,
                       capture_status="pending_direction" if direction == "unknown" else "captured")
        conn.execute("""INSERT INTO edge_message_ledger
            (conversation_key, sequence, match_key, event_hash, event_id, occurred_at, capture_status, direction)
            VALUES (:conversation_key, :sequence, :match_key, :event_hash, :event_id, :occurred_at, :capture_status, :direction)""", row)
        rows.append(row)
    conn.execute("DELETE FROM edge_message_alignment_pending WHERE conversation_key = ? AND snapshot_hash = ?",
                 (conversation_key, snapshot_hash))
    stream_id = conn.execute("SELECT stream_id FROM edge_message_ledger_heads WHERE conversation_key = ?",
                             (conversation_key,)).fetchone()[0]
    if not stream_id:
        stream_id = str(uuid.uuid4())
        conn.execute("UPDATE edge_message_ledger_heads SET stream_id = ? WHERE conversation_key = ?", (stream_id, conversation_key))
    for row in rows:
        row["stream_id"] = stream_id
    return rows, "aligned"


def mark(conn, row: dict, *, status: str, direction: str = "unknown"):
    conn.execute("""UPDATE edge_message_ledger SET capture_status = ?, direction = ?
        WHERE conversation_key = ? AND sequence = ?""",
        (status, direction, row["conversation_key"], row["sequence"]))


def media_state(entry: dict) -> dict:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM edge_media_capture_state WHERE event_hash = ?", (entry["event_hash"],)).fetchone()
        return dict(row) if row else {"attempts": 0, "next_attempt_at": 0, "files_json": "[]"}


def save_media(entry: dict, media: list[dict], *, error: str = ""):
    with transaction() as conn:
        old = conn.execute("SELECT attempts FROM edge_media_capture_state WHERE event_hash = ?", (entry["event_hash"],)).fetchone()
        attempts = (old[0] if old else 0) + 1 if error else 0
        next_attempt = time.time() + min(60, 2 ** min(6, attempts)) if error else 0
        conn.execute("""INSERT INTO edge_media_capture_state(event_hash, attempts, next_attempt_at, last_error, files_json)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_hash) DO UPDATE SET
            attempts=excluded.attempts, next_attempt_at=excluded.next_attempt_at,
            last_error=excluded.last_error, files_json=excluded.files_json""",
            (entry["event_hash"], attempts, next_attempt, error[:96], json.dumps(media, ensure_ascii=False)))
        if error:
            mark(conn, entry, status="pending_media", direction=entry["direction"])
