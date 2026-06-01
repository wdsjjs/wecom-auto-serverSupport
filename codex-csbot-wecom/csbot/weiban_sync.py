from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request

from .db import connect, ensure_schema
from .textutil import json_dumps, normalize_text


REQUEST_DELAY = 0.3
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class WeibanSyncError(RuntimeError):
    pass


class TokenManager:
    def __init__(self, *, base_url: str, corp_id: str, secret: str):
        self.base_url = base_url.rstrip("/")
        self.corp_id = corp_id
        self.secret = secret
        self._token = ""
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        payload = json.dumps({"corp_id": self.corp_id, "secret": self.secret}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/open-api/access_token/get",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
        if data.get("errcode") != 0:
            raise WeibanSyncError(f"Weiban token failed: {data}")
        self._token = data["access_token"]
        self._expires_at = time.time() + float(data.get("expires_in", 7200)) - 300
        return self._token


def _client_from_env() -> TokenManager:
    base_url = os.environ.get("WEIBAN_BASE_URL", "https://open.weibanzhushou.com").strip()
    corp_id = os.environ.get("WEIBAN_CORP_ID", "").strip()
    secret = os.environ.get("WEIBAN_SECRET", "").strip()
    if not (base_url and corp_id and secret):
        raise WeibanSyncError("WEIBAN_BASE_URL, WEIBAN_CORP_ID and WEIBAN_SECRET are required")
    return TokenManager(base_url=base_url, corp_id=corp_id, secret=secret)


def _api_get(tm: TokenManager, path: str, params: dict | None = None) -> dict:
    query = {"access_token": tm.token(), **(params or {})}
    url = f"{tm.base_url}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=45, context=SSL_CTX) as resp:
        return json.loads(resp.read())


def _clean_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _safe_filename(name: str, max_len: int = 80) -> str:
    name = name or "unnamed"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:max_len]


def _groups(tm: TokenManager) -> list[tuple[int, str, str]]:
    data = _api_get(tm, "/open-api/quick_reply_v3/group/list")
    groups = data.get("objects", []) or []
    result: list[tuple[int, str, str]] = []
    for group in groups:
        name = normalize_text(group.get("name"))
        if group.get("id") is not None:
            result.append((int(group["id"]), name, ""))
        for child in group.get("children", []) or []:
            if child.get("id") is not None:
                result.append((int(child["id"]), normalize_text(child.get("name")), name))
    return result


def _group_content(tm: TokenManager, group_id: int) -> list[dict]:
    offset = 0
    objects: list[dict] = []
    while True:
        data = _api_get(
            tm,
            "/open-api/quick_reply_v3/list",
            {"id": group_id, "limit": 100, "offset": offset},
        )
        objects.extend(item for item in data.get("objects", []) or [] if isinstance(item, dict))
        if not data.get("has_next"):
            return objects
        offset += 100
        time.sleep(REQUEST_DELAY)


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
    tm = _client_from_env()
    items = []
    seen_ids: set[int] = set()
    groups = _groups(tm)
    for group_id, group_name, parent_group_name in groups:
        for obj in _group_content(tm, group_id):
            for child in obj.get("children", []) or []:
                if child.get("id") is None:
                    continue
                item_id = int(child["id"])
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                item = _convert_item(obj, child, group_id=group_id, group_name=group_name, parent_group_name=parent_group_name)
                if not item.pop("is_expired"):
                    items.append(item)
        time.sleep(REQUEST_DELAY)

    if dry_run:
        return {"ok": True, "dry_run": True, "groups": len(groups), "rows": len(items)}

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
    return {"ok": True, "dry_run": False, "groups": len(groups), "rows": len(items)}
