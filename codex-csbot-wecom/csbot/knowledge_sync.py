from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar

from .db import connect, ensure_schema
from .feishu_sync import sync_feishu_tables
from .kb_rebuild import rebuild_kb_docs_from_sources
from .textutil import json_dumps
from .vector_store import import_kb_docs_as_memories
from .weiban_sync import sync_weiban_faq


T = TypeVar("T")
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_time(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _log(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    print(f"[knowledge_sync] {json_dumps(payload)}", file=sys.stderr, flush=True)


def _compact_status(status: dict) -> dict:
    return {
        key: status.get(key)
        for key in ("ok", "fresh", "reason", "last_success_at", "age_seconds", "max_age_seconds")
        if key in status
    }


def _upsert_meta(conn, key: str, value: str) -> None:
    if getattr(conn, "is_pg", False):
        conn.execute(
            "INSERT INTO kb_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
    else:
        conn.execute("INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)", (key, value))


def record_sync_log(
    *,
    source: str,
    status: str,
    event_type: str = "knowledge_sync",
    attempts: int = 0,
    message: str = "",
    detail: dict | None = None,
    db_path=None,
) -> None:
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO knowledge_sync_log
                (event_type, source, status, attempts, message, detail_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_type, source, status, attempts, message, json_dumps(detail or {})),
        )
        conn.commit()
    finally:
        conn.close()


def _load_sync_meta(db_path=None) -> dict:
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        rows = conn.execute("SELECT key, value FROM kb_meta WHERE key LIKE 'sync.%'").fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()


def knowledge_sync_status(*, db_path=None, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> dict:
    last_error = ""
    try:
        for attempt in range(1, 4):
            try:
                meta = _load_sync_meta(db_path)
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= 3:
                    raise
                time.sleep(0.5 * attempt)
    except Exception as exc:
        return {
            "ok": False,
            "fresh": False,
            "reason": last_error or f"{type(exc).__name__}: {exc}",
            "max_age_seconds": max_age_seconds,
        }

    last_success = meta.get("sync.all.last_success_at", "")
    parsed = _parse_time(last_success)
    age_seconds = None
    fresh = False
    reason = "missing_last_success"
    if parsed is not None:
        age_seconds = max(0, round((_utc_now() - parsed).total_seconds()))
        fresh = age_seconds <= max_age_seconds
        reason = "fresh" if fresh else "stale"
    elif last_success:
        reason = "invalid_last_success"

    return {
        "ok": True,
        "fresh": fresh,
        "reason": reason,
        "last_success_at": last_success,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "meta": meta,
    }


def _mark_sync_success(conn, *, source: str, result: dict) -> None:
    now = _iso_now()
    _upsert_meta(conn, f"sync.{source}.last_success_at", now)
    _upsert_meta(conn, f"sync.{source}.last_result_json", json_dumps(result))


def _run_with_retries(
    *,
    source: str,
    action: Callable[[], T],
    attempts: int,
    retry_delay_seconds: float,
    db_path=None,
) -> dict:
    started = time.monotonic()
    last_error = ""
    for attempt in range(1, attempts + 1):
        _log("task_attempt_started", source=source, attempt=attempt, attempts=attempts)
        try:
            result = action()
            elapsed_ms = round((time.monotonic() - started) * 1000)
            _log("task_attempt_done", source=source, attempt=attempt, elapsed_ms=elapsed_ms)
            return {
                "ok": True,
                "source": source,
                "attempts": attempt,
                "elapsed_ms": elapsed_ms,
                "result": result,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _log("task_attempt_failed", source=source, attempt=attempt, attempts=attempts, error=last_error)
            if attempt < attempts and retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)

    elapsed_ms = round((time.monotonic() - started) * 1000)
    failure = {
        "ok": False,
        "source": source,
        "attempts": attempts,
        "elapsed_ms": elapsed_ms,
        "error": last_error,
    }
    _log("task_failed", **failure)
    try:
        record_sync_log(
            source=source,
            status="failed",
            attempts=attempts,
            message=last_error,
            detail=failure,
            db_path=db_path,
        )
    except Exception as log_exc:
        _log("task_failure_log_failed", source=source, error=f"{type(log_exc).__name__}: {log_exc}")
    return failure


def _commit_task_results(*, results: dict[str, dict], db_path=None) -> None:
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        for source, result in results.items():
            if result.get("ok"):
                _mark_sync_success(conn, source=source, result=result)
        successful_sources = sorted(source for source, result in results.items() if result.get("ok"))
        failed_sources = sorted(source for source, result in results.items() if not result.get("ok"))
        if successful_sources:
            _upsert_meta(conn, "sync.partial.last_success_at", _iso_now())
        if not failed_sources:
            _upsert_meta(conn, "sync.all.last_success_at", _iso_now())
            _upsert_meta(conn, "sync.all.last_result_json", json_dumps(results))
        _upsert_meta(conn, "sync.all.last_sources_json", json_dumps({"successful": successful_sources, "failed": failed_sources}))
        conn.commit()
    finally:
        conn.close()


def sync_all_parallel(
    *,
    dry_run: bool = False,
    db_path=None,
    kb_version: str = "pg",
    skip_mem0: bool = False,
    progress: bool = False,
    attempts: int | None = None,
    retry_delay_seconds: float | None = None,
) -> dict:
    started = time.monotonic()
    retry_attempts = attempts if attempts is not None else _env_int("CSBOT_SYNC_RETRY_ATTEMPTS", 3)
    retry_delay = retry_delay_seconds if retry_delay_seconds is not None else _env_float("CSBOT_SYNC_RETRY_DELAY_SECONDS", 2.0)
    retry_attempts = max(1, retry_attempts)

    source_tasks: dict[str, Callable[[], dict]] = {
        "feishu": lambda: sync_feishu_tables(dry_run=dry_run, db_path=db_path),
        "weiban": lambda: sync_weiban_faq(dry_run=dry_run, db_path=db_path),
    }
    source_results: dict[str, dict] = {}
    _log("parallel_sources_started", sources=sorted(source_tasks), attempts=retry_attempts, dry_run=dry_run)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(source_tasks), thread_name_prefix="knowledge-sync") as executor:
        future_map = {
            executor.submit(
                _run_with_retries,
                source=source,
                action=action,
                attempts=retry_attempts,
                retry_delay_seconds=retry_delay,
                db_path=db_path,
            ): source
            for source, action in source_tasks.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            source = future_map[future]
            source_results[source] = future.result()

    if dry_run:
        return {
            "ok": all(result.get("ok") for result in source_results.values()),
            "dry_run": True,
            "parallel": True,
            "sources": source_results,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    followup_results: dict[str, dict] = {}
    if all(source_results.get(source, {}).get("ok") for source in source_tasks):
        followup_results["kb_rebuild"] = _run_with_retries(
            source="kb_rebuild",
            action=lambda: rebuild_kb_docs_from_sources(kb_version=kb_version, db_path=db_path),
            attempts=retry_attempts,
            retry_delay_seconds=retry_delay,
            db_path=db_path,
        )
        if not skip_mem0 and followup_results["kb_rebuild"].get("ok"):
            followup_results["mem0_import"] = _run_with_retries(
                source="mem0_import",
                action=lambda: {"imported": import_kb_docs_as_memories(db_path, progress=progress)},
                attempts=retry_attempts,
                retry_delay_seconds=retry_delay,
                db_path=db_path,
            )
    else:
        failed = sorted(source for source, result in source_results.items() if not result.get("ok"))
        _log("followup_skipped", reason="source_sync_failed", failed_sources=failed)

    all_results = {**source_results, **followup_results}
    try:
        _commit_task_results(results=all_results, db_path=db_path)
    except Exception as exc:
        _log("sync_commit_failed", error=f"{type(exc).__name__}: {exc}")
        try:
            record_sync_log(
                source="sync_commit",
                status="failed",
                attempts=1,
                message=f"{type(exc).__name__}: {exc}",
                detail={"results": all_results},
                db_path=db_path,
            )
        except Exception as log_exc:
            _log("sync_commit_failure_log_failed", error=f"{type(log_exc).__name__}: {log_exc}")

    return {
        "ok": all(result.get("ok") for result in all_results.values()) if all_results else False,
        "dry_run": False,
        "parallel": True,
        "knowledge_backend": "postgres" if os.environ.get("CSBOT_PG_DSN", "").strip() else "sqlite",
        "sources": source_results,
        "followups": followup_results,
        "mem0_skipped": bool(skip_mem0),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def sync_if_stale(
    *,
    db_path=None,
    max_age_seconds: int | None = None,
    kb_version: str = "startup",
    skip_mem0: bool = True,
    progress: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    age_limit = max_age_seconds if max_age_seconds is not None else _env_int("CSBOT_SYNC_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS)
    status = knowledge_sync_status(db_path=db_path, max_age_seconds=age_limit)
    if status.get("fresh") and not force:
        _log("sync_skipped_fresh", status=_compact_status(status))
        return {"ok": True, "skipped": True, "reason": "fresh", "status": status}

    _log("sync_required", force=force, status=_compact_status(status))
    result = sync_all_parallel(
        dry_run=dry_run,
        db_path=db_path,
        kb_version=kb_version,
        skip_mem0=skip_mem0,
        progress=progress,
    )
    return {"ok": bool(result.get("ok")), "skipped": False, "reason": status.get("reason"), "status": status, "sync": result}
