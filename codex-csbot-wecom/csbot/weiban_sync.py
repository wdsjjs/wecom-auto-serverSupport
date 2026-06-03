from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

from .db import connect, ensure_schema
from .textutil import json_dumps, normalize_text


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


REQUEST_DELAY = _env_float("WEIBAN_REQUEST_DELAY_SECONDS", 0.2)
TOKEN_TIMEOUT = _env_float("WEIBAN_TOKEN_TIMEOUT_SECONDS", 20)
REQUEST_TIMEOUT = _env_float("WEIBAN_REQUEST_TIMEOUT_SECONDS", 45)
MAX_WORKERS = min(max(1, _env_int("WEIBAN_SYNC_WORKERS", 4)), 12)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class WeibanSyncError(RuntimeError):
    pass


def _log(event: str, **fields) -> None:
    if os.environ.get("WEIBAN_SYNC_LOG", "1").strip() == "0":
        return
    payload = {"event": event, **fields}
    print(f"[weiban_sync] {json_dumps(payload)}", file=sys.stderr, flush=True)


class TokenManager:
    def __init__(self, *, base_url: str, corp_id: str, secret: str):
        self.base_url = base_url.rstrip("/")
        self.corp_id = corp_id
        self.secret = secret
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._last_request_started_at = 0.0

    def _token_is_valid(self) -> bool:
        return bool(self._token and time.time() < self._expires_at)

    def wait_for_request_slot(self) -> int:
        if REQUEST_DELAY <= 0:
            return 0
        with self._request_lock:
            now = time.monotonic()
            wait_seconds = self._last_request_started_at + REQUEST_DELAY - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            waited_ms = round(max(0.0, time.monotonic() - now) * 1000)
            self._last_request_started_at = time.monotonic()
            return waited_ms

    def token(self) -> str:
        if self._token_is_valid():
            return self._token
        with self._token_lock:
            if self._token_is_valid():
                return self._token
            started = time.monotonic()
            _log("token_refresh_started", base_url=self.base_url, timeout_seconds=TOKEN_TIMEOUT)
            payload = json.dumps({"corp_id": self.corp_id, "secret": self.secret}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/open-api/access_token/get",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=TOKEN_TIMEOUT, context=SSL_CTX) as resp:
                    data = json.loads(resp.read())
            except Exception as exc:
                _log(
                    "token_refresh_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                raise
            if data.get("errcode") != 0:
                _log(
                    "token_refresh_failed",
                    errcode=data.get("errcode"),
                    errmsg=data.get("errmsg") or data.get("msg"),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                raise WeibanSyncError(f"Weiban token failed: {data}")
            self._token = data["access_token"]
            self._expires_at = time.time() + float(data.get("expires_in", 7200)) - 300
            _log(
                "token_refresh_done",
                expires_in=data.get("expires_in"),
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return self._token


def _client_from_env() -> TokenManager:
    base_url = os.environ.get("WEIBAN_BASE_URL", "https://open.weibanzhushou.com").strip()
    corp_id = os.environ.get("WEIBAN_CORP_ID", "").strip()
    secret = os.environ.get("WEIBAN_SECRET", "").strip()
    if not (base_url and corp_id and secret):
        raise WeibanSyncError("WEIBAN_BASE_URL, WEIBAN_CORP_ID and WEIBAN_SECRET are required")
    return TokenManager(base_url=base_url, corp_id=corp_id, secret=secret)


def _api_get(tm: TokenManager, path: str, params: dict | None = None) -> dict:
    safe_params = dict(params or {})
    token = tm.token()
    waited_ms = tm.wait_for_request_slot()
    started = time.monotonic()
    _log(
        "api_get_started",
        path=path,
        params=safe_params,
        timeout_seconds=REQUEST_TIMEOUT,
        waited_ms=waited_ms,
    )
    query = {"access_token": token, **safe_params}
    url = f"{tm.base_url}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as resp:
            raw_body = resp.read()
    except Exception as exc:
        _log(
            "api_get_failed",
            path=path,
            params=safe_params,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        raise
    data = json.loads(raw_body)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    _log(
        "api_get_done",
        path=path,
        params=safe_params,
        errcode=data.get("errcode"),
        object_count=len(data.get("objects") or []),
        has_next=bool(data.get("has_next")),
        elapsed_ms=elapsed_ms,
    )
    if data.get("errcode") not in (None, 0):
        raise WeibanSyncError(
            f"Weiban API failed: path={path} errcode={data.get('errcode')} "
            f"msg={data.get('errmsg') or data.get('msg')}"
        )
    return data


def _clean_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _safe_filename(name: str, max_len: int = 80) -> str:
    name = name or "unnamed"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:max_len]


def _group_fetch_mode() -> str:
    raw = os.environ.get("WEIBAN_GROUP_FETCH_MODE", "top_level").strip().lower().replace("-", "_")
    if raw in {"all", "flattened", "full"}:
        return "all"
    if raw in {"children", "children_only", "child"}:
        return "children_only"
    if raw in {"top", "top_level", "parent", "parents", "root"}:
        return "top_level"
    _log("group_fetch_mode_invalid", raw=raw, fallback="top_level")
    return "top_level"


def _groups(tm: TokenManager, *, fetch_mode: str) -> tuple[list[tuple[int, str, str]], dict]:
    started = time.monotonic()
    _log("groups_started", fetch_mode=fetch_mode)
    data = _api_get(tm, "/open-api/quick_reply_v3/group/list")
    groups = data.get("objects", []) or []
    top_level: list[tuple[int, str, str]] = []
    children: list[tuple[int, str, str]] = []
    child_count = 0
    for group in groups:
        name = normalize_text(group.get("name"))
        if group.get("id") is not None:
            top_level.append((int(group["id"]), name, ""))
        for child in group.get("children", []) or []:
            if child.get("id") is not None:
                child_count += 1
                children.append((int(child["id"]), normalize_text(child.get("name")), name))
    if fetch_mode == "all":
        result = [*top_level, *children]
    elif fetch_mode == "children_only":
        result = children
    else:
        result = top_level
    stats = {
        "fetch_mode": fetch_mode,
        "top_level_count": len(top_level),
        "child_count": child_count,
        "flattened_count": len(top_level) + len(children),
        "selected_count": len(result),
    }
    _log(
        "groups_done",
        **stats,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    return result, stats


def _group_content(tm: TokenManager, group_id: int) -> list[dict]:
    offset = 0
    objects: list[dict] = []
    while True:
        page_started = time.monotonic()
        _log("group_page_started", group_id=group_id, offset=offset, limit=100)
        data = _api_get(
            tm,
            "/open-api/quick_reply_v3/list",
            {"id": group_id, "limit": 100, "offset": offset},
        )
        page_objects = [item for item in data.get("objects", []) or [] if isinstance(item, dict)]
        objects.extend(page_objects)
        has_next = bool(data.get("has_next"))
        _log(
            "group_page_done",
            group_id=group_id,
            offset=offset,
            object_count=len(page_objects),
            total_object_count=len(objects),
            has_next=has_next,
            elapsed_ms=round((time.monotonic() - page_started) * 1000),
        )
        if not has_next:
            return objects
        offset += 100


def _fetch_group(tm: TokenManager, *, index: int, total: int, group: tuple[int, str, str]) -> dict:
    group_id, group_name, parent_group_name = group
    group_started = time.monotonic()
    _log(
        "group_started",
        index=index,
        total=total,
        group_id=group_id,
        group_name=group_name,
        parent_group_name=parent_group_name,
    )
    try:
        group_items = _group_content(tm, group_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _log(
            "group_failed",
            index=index,
            total=total,
            group_id=group_id,
            group_name=group_name,
            parent_group_name=parent_group_name,
            error=error,
            elapsed_ms=round((time.monotonic() - group_started) * 1000),
        )
        return {
            "index": index,
            "group_id": group_id,
            "group_name": group_name,
            "parent_group_name": parent_group_name,
            "group_items": [],
            "error": error,
            "fetch_elapsed_ms": round((time.monotonic() - group_started) * 1000),
        }
    elapsed_ms = round((time.monotonic() - group_started) * 1000)
    _log(
        "group_fetch_done",
        index=index,
        total=total,
        group_id=group_id,
        group_name=group_name,
        collection_count=len(group_items),
        elapsed_ms=elapsed_ms,
    )
    return {
        "index": index,
        "group_id": group_id,
        "group_name": group_name,
        "parent_group_name": parent_group_name,
        "group_items": group_items,
        "error": "",
        "fetch_elapsed_ms": elapsed_ms,
    }


def _fetch_groups(tm: TokenManager, groups: list[tuple[int, str, str]], *, workers: int) -> list[dict]:
    if not groups:
        return []
    workers = min(max(1, workers), len(groups))
    _log("fetch_pool_started", groups=len(groups), workers=workers, request_delay_seconds=REQUEST_DELAY)
    if workers == 1:
        return [_fetch_group(tm, index=index, total=len(groups), group=group) for index, group in enumerate(groups, start=1)]

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="weiban-sync") as executor:
        futures = [
            executor.submit(_fetch_group, tm, index=index, total=len(groups), group=group)
            for index, group in enumerate(groups, start=1)
        ]
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            _log(
                "fetch_progress",
                completed=completed,
                total=len(groups),
                group_index=result["index"],
                group_id=result["group_id"],
                error=bool(result["error"]),
            )
    return sorted(results, key=lambda result: int(result["index"]))


def _convert_item(obj: dict, child: dict, *, group_id: int, group_name: str, parent_group_name: str) -> dict:
    item_id = int(child["id"])
    content_type = normalize_text(child.get("content_type")) or "text"
    content = child.get("content") or ""
    text_content = _clean_html(content) if content_type == "text" else ""
    image_url = ""
    image_size = ""
    if content_type == "image" and content:
        try:
            image = json.loads(content)
            image_url = normalize_text(image.get("url"))
            image_size = normalize_text(image.get("size"))
        except (TypeError, json.JSONDecodeError):
            pass

    collection_name = normalize_text(obj.get("name"))
    item_name = normalize_text(child.get("name") or collection_name)
    title = " | ".join(part for part in (group_name, collection_name, item_name) if part)
    title = title or f"item_{item_id}"
    fuzzy_keywords = child.get("fuzzy_keywords", []) or []
    exact_keywords = child.get("exact_keywords", []) or []
    is_expired = any("过期" in str(value or "") for value in (parent_group_name, group_name, collection_name, item_name))
    file_name = f"{_safe_filename(f'{group_name}____{item_name}')}.md"
    item_json = {
        "id": f"knowledge-weiban-{item_id}",
        "type": "faq_item",
        "biz_key": f"weiban://quick_reply/{group_id}/{item_id}",
        "title": title,
        "summary": text_content if text_content else f"[附带图片: {image_size}]" if image_url else "",
        "body": text_content,
        "structured_content": {
            "source": "weiban",
            "group_id": group_id,
            "group_name": group_name,
            "parent_group_name": parent_group_name,
            "weiban_collection_id": obj.get("id"),
            "weiban_collection_name": collection_name,
            "weiban_id": item_id,
            "content_type": content_type,
            "fuzzy_keywords": fuzzy_keywords,
            "exact_keywords": exact_keywords,
            "image_url": image_url,
            "image_size": image_size,
            "file_name": file_name,
        },
    }
    content_hash = hashlib.md5(json_dumps(item_json).encode("utf-8")).hexdigest()
    return {
        "weiban_id": item_id,
        "group_id": group_id,
        "group_name": group_name,
        "parent_group_name": parent_group_name,
        "weiban_collection_id": obj.get("id"),
        "content_type": content_type,
        "title": title,
        "summary": item_json["summary"],
        "body": text_content,
        "fuzzy_keywords": fuzzy_keywords,
        "exact_keywords": exact_keywords,
        "image_url": image_url,
        "image_size": image_size,
        "local_image_path": "",
        "file_name": file_name,
        "biz_key": item_json["biz_key"],
        "risk_level": "medium",
        "item_json": item_json,
        "content_hash": content_hash,
        "is_expired": is_expired,
    }


def sync_weiban_faq(*, dry_run: bool = False, db_path=None) -> dict:
    sync_started = time.monotonic()
    _log(
        "sync_started",
        dry_run=dry_run,
        request_timeout_seconds=REQUEST_TIMEOUT,
        token_timeout_seconds=TOKEN_TIMEOUT,
        request_delay_seconds=REQUEST_DELAY,
        workers=MAX_WORKERS,
        group_fetch_mode=_group_fetch_mode(),
    )
    tm = _client_from_env()
    items = []
    seen_ids: set[int] = set()
    fetch_mode = _group_fetch_mode()
    groups, group_stats = _groups(tm, fetch_mode=fetch_mode)
    max_groups = _env_int("WEIBAN_SYNC_MAX_GROUPS", 0)
    if max_groups:
        _log("groups_limited", original_count=len(groups), max_groups=max_groups)
        groups = groups[:max_groups]
    skipped_groups = []
    group_results = _fetch_groups(tm, groups, workers=MAX_WORKERS)
    for group_result in group_results:
        group_started = time.monotonic()
        group_id = int(group_result["group_id"])
        group_name = str(group_result["group_name"])
        parent_group_name = str(group_result["parent_group_name"])
        if group_result["error"]:
            skipped_groups.append(
                {
                    "group_id": group_id,
                    "group_name": group_name,
                    "parent_group_name": parent_group_name,
                    "error": group_result["error"],
                }
            )
            continue
        group_items = group_result["group_items"]
        child_count = 0
        added_count = 0
        duplicate_count = 0
        expired_count = 0
        for obj in group_items:
            for child in obj.get("children", []) or []:
                if child.get("id") is None:
                    continue
                child_count += 1
                item_id = int(child["id"])
                if item_id in seen_ids:
                    duplicate_count += 1
                    continue
                seen_ids.add(item_id)
                item = _convert_item(obj, child, group_id=group_id, group_name=group_name, parent_group_name=parent_group_name)
                if item.pop("is_expired"):
                    expired_count += 1
                else:
                    added_count += 1
                    items.append(item)
        _log(
            "group_done",
            index=group_result["index"],
            total=len(groups),
            group_id=group_id,
            group_name=group_name,
            collection_count=len(group_items),
            child_count=child_count,
            added_count=added_count,
            duplicate_count=duplicate_count,
            expired_count=expired_count,
            total_rows=len(items),
            fetch_elapsed_ms=group_result["fetch_elapsed_ms"],
            elapsed_ms=round((time.monotonic() - group_started) * 1000),
        )

    if dry_run:
        _log(
            "sync_done",
            dry_run=True,
            groups=len(groups),
            rows=len(items),
            skipped_groups=len(skipped_groups),
            group_stats=group_stats,
            elapsed_ms=round((time.monotonic() - sync_started) * 1000),
        )
        return {
            "ok": True,
            "dry_run": True,
            "groups": len(groups),
            "group_fetch_mode": fetch_mode,
            "group_stats": group_stats,
            "workers": min(max(1, MAX_WORKERS), len(groups)) if groups else 0,
            "rows": len(items),
            "skipped_groups": skipped_groups,
        }

    _log("db_write_started", rows=len(items), skipped_groups=len(skipped_groups))
    db_started = time.monotonic()
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        ids = []
        for item in items:
            ids.append(item["weiban_id"])
            conn.execute(
                """
                INSERT INTO weiban_customer_service_faq
                    (weiban_id, group_id, group_name, parent_group_name, weiban_collection_id,
                     content_type, title, summary, body, fuzzy_keywords, exact_keywords,
                     image_url, image_size, local_image_path, file_name, biz_key, risk_level,
                     item_json, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (weiban_id) DO UPDATE SET
                    group_id = EXCLUDED.group_id,
                    group_name = EXCLUDED.group_name,
                    parent_group_name = EXCLUDED.parent_group_name,
                    weiban_collection_id = EXCLUDED.weiban_collection_id,
                    content_type = EXCLUDED.content_type,
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    body = EXCLUDED.body,
                    fuzzy_keywords = EXCLUDED.fuzzy_keywords,
                    exact_keywords = EXCLUDED.exact_keywords,
                    image_url = EXCLUDED.image_url,
                    image_size = EXCLUDED.image_size,
                    local_image_path = EXCLUDED.local_image_path,
                    file_name = EXCLUDED.file_name,
                    biz_key = EXCLUDED.biz_key,
                    risk_level = EXCLUDED.risk_level,
                    item_json = EXCLUDED.item_json,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    item["weiban_id"],
                    item["group_id"],
                    item["group_name"],
                    item["parent_group_name"],
                    item["weiban_collection_id"],
                    item["content_type"],
                    item["title"],
                    item["summary"],
                    item["body"],
                    json_dumps(item["fuzzy_keywords"]),
                    json_dumps(item["exact_keywords"]),
                    item["image_url"],
                    item["image_size"],
                    item["local_image_path"],
                    item["file_name"],
                    item["biz_key"],
                    item["risk_level"],
                    json_dumps(item["item_json"]),
                    item["content_hash"],
                ),
            )
        if ids:
            if getattr(conn, "is_pg", False):
                conn.execute("DELETE FROM weiban_customer_service_faq WHERE NOT (weiban_id = ANY(?))", (ids,))
            else:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM weiban_customer_service_faq WHERE weiban_id NOT IN ({placeholders})", ids)
        else:
            conn.execute("DELETE FROM weiban_customer_service_faq")
        conn.commit()
    finally:
        conn.close()
    _log(
        "db_write_done",
        rows=len(items),
        elapsed_ms=round((time.monotonic() - db_started) * 1000),
    )
    _log(
        "sync_done",
        dry_run=False,
        groups=len(groups),
        rows=len(items),
        skipped_groups=len(skipped_groups),
        group_stats=group_stats,
        elapsed_ms=round((time.monotonic() - sync_started) * 1000),
    )
    return {
        "ok": True,
        "dry_run": False,
        "groups": len(groups),
        "group_fetch_mode": fetch_mode,
        "group_stats": group_stats,
        "workers": min(max(1, MAX_WORKERS), len(groups)) if groups else 0,
        "rows": len(items),
        "skipped_groups": skipped_groups,
    }
