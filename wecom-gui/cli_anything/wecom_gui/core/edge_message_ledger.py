"""Ordered capture identities, independent of sender direction and viewport position."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from cli_anything.wecom_gui.core import edge_state


HISTORY_LIMIT = 200
MEDIA_ATTEMPT_LIMIT = 5


class MediaIdentityError(ValueError):
    """A bounded reason code, never a customer message or a native error string."""


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
        for column in ("paused_reason", "capture_row_id", "image_fingerprint"):
            edge_state._ensure_column(conn, "edge_media_capture_state", column, "TEXT NOT NULL DEFAULT ''")
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
        return dict(row) if row else {"attempts": 0, "next_attempt_at": 0, "files_json": "[]",
                                     "last_error": "", "paused_reason": "", "capture_row_id": "", "image_fingerprint": ""}


def remember_media_identity(conn, entry: dict, message: dict):
    """Pin initial evidence even when this tick cannot afford to capture the image."""
    conn.execute("""INSERT OR IGNORE INTO edge_media_capture_state
        (event_hash, capture_row_id, image_fingerprint) VALUES (?, ?, ?)""",
        (entry["event_hash"], message.get("capture_row_id") or "",
         (message.get("direction_evidence") or {}).get("imageFingerprint") or ""))


def media_fingerprints_match(expected: str, observed: str, *, allow_render_drift: bool = True) -> bool:
    if expected == observed and not expected.startswith("rgb32-v2:"):
        return bool(expected)

    def decode(value):
        parts = value.split(":")
        if len(parts) == 2 and parts[0] == "rgb32-v1":
            return parts[1], None
        if len(parts) != 3 or parts[0] != "rgb32-v2" or len(parts[1]) != 64 or len(parts[2]) != 4096:
            return None
        try:
            pixels = base64.b64decode(parts[2], validate=True)
        except (ValueError, binascii.Error):
            return None
        if len(pixels) != 32 * 32 * 3:
            return None
        digest = hashlib.sha256(bytes(value & 0xf8 for value in pixels)).hexdigest()
        return (digest, pixels) if digest == parts[1] else None

    before, after = decode(expected), decode(observed)
    if before is None or after is None:
        return False
    if before[0] == after[0]:
        return True
    if not allow_render_drift or before[1] is None or after[1] is None:
        return False
    # Per-channel AND average bounds; do not allow a small replaced region to
    # disappear in a whole-image average. Always compare against the pinned anchor.
    differences = [abs(a - b) for a, b in zip(before[1], after[1])]
    if max(differences) <= 12 and sum(differences) <= 5 * len(differences):
        return True
    # A one-pixel bubble-boundary change resamples high-contrast edges. Require
    # small total error AND small local low-frequency error, not just similarity.
    if max(differences) > 64 or sum(differences) > 5 * len(differences):
        return False
    signed = [a - b for a, b in zip(before[1], after[1])]
    smoothed = []
    for y in range(2, 30):
        for x in range(2, 30):
            for channel in range(3):
                error = abs(sum(signed[(yy * 32 + xx) * 3 + channel]
                    for yy in range(y - 2, y + 3) for xx in range(x - 2, x + 3)))
                if error > 10 * 25:
                    return False
                smoothed.append(error)
    return sum(smoothed) <= 3.2 * 25 * len(smoothed)


def verify_media_identity(entry: dict, message: dict, *, require_pixels: bool = False):
    row_id = str(message.get("capture_row_id") or "")
    fingerprint = str((message.get("direction_evidence") or {}).get("imageFingerprint") or "")
    if not row_id:
        raise MediaIdentityError("media_row_identity_unavailable")
    if require_pixels and not fingerprint:
        raise MediaIdentityError("media_fingerprint_unavailable")
    with transaction() as conn:
        remember_media_identity(conn, entry, message)
        old = conn.execute("SELECT * FROM edge_media_capture_state WHERE event_hash = ?", (entry["event_hash"],)).fetchone()
        if old["image_fingerprint"] and fingerprint and not media_fingerprints_match(old["image_fingerprint"], fingerprint):
            raise MediaIdentityError("media_fingerprint_changed")
        if old["capture_row_id"] and old["capture_row_id"] != row_id:
            # An app restart invalidates AX handles. Rebind only with already pinned
            # pixels and the ordered ledger; never on another identical placeholder.
            restarted = old["capture_row_id"].rsplit(":", 1)[0] != row_id.rsplit(":", 1)[0]
            if not (restarted and fingerprint and media_fingerprints_match(
                    old["image_fingerprint"], fingerprint, allow_render_drift=False)):
                raise MediaIdentityError("media_row_identity_changed")
        upgrade = old["image_fingerprint"].startswith("rgb32-v1:") and fingerprint.startswith("rgb32-v2:")
        conn.execute("""UPDATE edge_media_capture_state SET capture_row_id = ?,
            image_fingerprint = CASE WHEN image_fingerprint = '' OR ? THEN ? ELSE image_fingerprint END
            WHERE event_hash = ?""", (row_id, upgrade, fingerprint, entry["event_hash"]))


def begin_media_attempt(entry: dict) -> bool:
    """Reserve before GUI work so a crash cannot reset the retry limit."""
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO edge_media_capture_state(event_hash) VALUES (?)", (entry["event_hash"],))
        old = conn.execute("SELECT * FROM edge_media_capture_state WHERE event_hash = ?", (entry["event_hash"],)).fetchone()
        if old["paused_reason"] or old["attempts"] >= MEDIA_ATTEMPT_LIMIT or old["next_attempt_at"] > time.time():
            return False
        attempts = old["attempts"] + 1
        conn.execute("""UPDATE edge_media_capture_state SET attempts = ?, next_attempt_at = ?,
            last_error = 'media_capture_interrupted', paused_reason = ? WHERE event_hash = ?""",
            (attempts, time.time() + min(60, 2 ** attempts),
             "media_retry_limit" if attempts >= MEDIA_ATTEMPT_LIMIT else "", entry["event_hash"]))
        return True


def save_media(entry: dict, media: list[dict], *, error: str = "", complete: bool = True):
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO edge_media_capture_state(event_hash) VALUES (?)", (entry["event_hash"],))
        done = complete and not error
        conn.execute("""UPDATE edge_media_capture_state SET files_json = ?, last_error = ?,
            attempts = CASE WHEN ? THEN 0 ELSE attempts END,
            next_attempt_at = CASE WHEN ? OR ? = '' THEN 0 ELSE next_attempt_at END,
            paused_reason = CASE WHEN ? THEN '' WHEN attempts >= ? THEN 'media_retry_limit' ELSE '' END
            WHERE event_hash = ?""",
            (json.dumps(media, ensure_ascii=False), error[:96], done, done, error, done, MEDIA_ATTEMPT_LIMIT, entry["event_hash"]))
        if error:
            conn.execute("UPDATE edge_message_ledger SET capture_status = 'pending_media' WHERE event_hash = ?",
                         (entry["event_hash"],))


def resume_media(event_id: str) -> dict:
    """Resume a paused local task without changing event identity or pinned evidence."""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM edge_message_ledger WHERE event_id = ?", (event_id,)).fetchone()
        if not row or row["capture_status"] != "pending_media":
            return {"ok": False, "reason": "pending_media_not_found"}
        updated = conn.execute("""UPDATE edge_media_capture_state SET attempts = 0, next_attempt_at = 0,
            paused_reason = '', last_error = '' WHERE event_hash = ? AND (paused_reason != '' OR attempts >= ?)""",
            (row["event_hash"], MEDIA_ATTEMPT_LIMIT)).rowcount
        return {"ok": bool(updated), "event_id": event_id,
                "reason": "media_retry_resumed" if updated else "media_not_paused"}
