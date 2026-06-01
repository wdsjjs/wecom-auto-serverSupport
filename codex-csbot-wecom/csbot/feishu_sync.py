from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from .db import connect, ensure_schema
from .textutil import json_dumps, normalize_text


DOMAIN = "https://open.feishu.cn"

FEISHU_TABLES = [
    ("tblrlPjUGqDbPUvr", "1 产品发货状态", "feishu_product_shipment_status"),
    ("tblygxrNFwOimCpY", "2 促单活动", "feishu_promotion_activities"),
    ("tblasepcOjZxgtRN", "3 限时通知", "feishu_limited_notices"),
    ("tbl4xjT0cxTUGTqF", "13 产品检测报告", "feishu_product_test_reports"),
    ("tblrwmlB4kEF4dIR", "5 产品常规信息", "feishu_product_basic_info"),
    ("tblbU274OBq3JFBQ", "6 论文表", "feishu_papers"),
    ("tblrrnnljeflBypR", "7 L0级注意事项", "feishu_l0_precautions"),
    ("tblWgK9tRwGYUlKV", "16 异常物流话术", "feishu_logistics_faq"),
    ("tbluEoPcOEUrBdlY", "10 补剂推荐", "feishu_supplement_recommendations"),
    ("tbl5t69JcshJOpW3", "12原料专利、认证等材料", "feishu_product_certifications"),
    ("tblwIAhq5ETG9MFy", "15 发货状态通用话术库", "feishu_shipping_status_templates"),
]


class FeishuSyncError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, *, app_id: str, app_secret: str, app_token: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self._token = ""
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> "FeishuClient":
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        app_token = os.environ.get("FEISHU_APP_TOKEN", "").strip()
        if not (app_id and app_secret and app_token):
            raise FeishuSyncError("FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_APP_TOKEN are required")
        return cls(app_id=app_id, app_secret=app_secret, app_token=app_token)

    def token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        url = f"{DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if data.get("code") != 0:
            raise FeishuSyncError(f"Feishu auth failed: {data}")
        self._token = data["tenant_access_token"]
        self._expires_at = time.time() + float(data.get("expire", 7200)) - 300
        return self._token

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{DOMAIN}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token()}"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
        if data.get("code", 0) != 0:
            raise FeishuSyncError(f"Feishu API failed: {data}")
        return data

    def list_records(self, table_id: str) -> list[dict]:
        records: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._get(
                f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
                params=params,
            )
            body = data.get("data") or {}
            records.extend(item for item in body.get("items", []) if isinstance(item, dict))
            if not body.get("has_more"):
                break
            page_token = body.get("page_token") or ""
            if not page_token:
                break
            time.sleep(0.1)
        return records


def _plain_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return normalize_text(value.get("text") or value.get("link") or value.get("name") or json_dumps(value))
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(normalize_text(item.get("text") or item.get("name") or item.get("link") or json_dumps(item)))
            else:
                parts.append(normalize_text(item))
        return ", ".join(part for part in parts if part)
    return normalize_text(value)


def normalize_fields(fields: dict) -> dict[str, str]:
    return {str(key): _plain_value(value) for key, value in (fields or {}).items()}


def _is_blank_test_report(fields: dict) -> bool:
    meaningful = ("产品", "产品常用名", "批次号", "出厂报告", "第三方检测报告", "SGS 英文报告（用于全球官网）")
    return not any(normalize_text(fields.get(key)) for key in meaningful)


def _hash_fields(fields: dict) -> str:
    return hashlib.md5(json_dumps(fields).encode("utf-8")).hexdigest()


def _ensure_source_table(conn, pg_table: str) -> None:
    if not re.match(r"^feishu_[a-z0-9_]+$", pg_table):
        raise FeishuSyncError(f"unsafe feishu source table name: {pg_table}")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {pg_table} (
            record_id TEXT PRIMARY KEY,
            fields_json TEXT NOT NULL DEFAULT '{{}}',
            content_hash TEXT NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def sync_feishu_tables(*, dry_run: bool = False, table_number: int | None = None, db_path=None) -> dict:
    client = FeishuClient.from_env()
    tables = FEISHU_TABLES
    if table_number is not None:
        if table_number < 1 or table_number > len(FEISHU_TABLES):
            raise FeishuSyncError(f"table_number must be 1-{len(FEISHU_TABLES)}")
        tables = [FEISHU_TABLES[table_number - 1]]

    results = []
    total = 0
    conn = None if dry_run else connect(db_path)
    try:
        if conn is not None:
            ensure_schema(conn)
        for table_id, label, pg_table in tables:
            records = client.list_records(table_id)
            rows = []
            for record in records:
                fields = normalize_fields(record.get("fields") or {})
                if pg_table == "feishu_product_test_reports" and _is_blank_test_report(fields):
                    continue
                rows.append(
                    {
                        "record_id": record.get("record_id", ""),
                        "fields": fields,
                        "content_hash": _hash_fields(fields),
                    }
                )
            if conn is not None:
                _ensure_source_table(conn, pg_table)
                conn.execute("DELETE FROM feishu_sync_records WHERE table_id = ?", (table_id,))
                conn.execute(f"DELETE FROM {pg_table}")
                for row in rows:
                    conn.execute(
                        """
                        INSERT INTO feishu_sync_records
                            (table_id, table_label, pg_table, record_id, fields_json, content_hash, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT (table_id, record_id) DO UPDATE SET
                            table_label = EXCLUDED.table_label,
                            pg_table = EXCLUDED.pg_table,
                            fields_json = EXCLUDED.fields_json,
                            content_hash = EXCLUDED.content_hash,
                            synced_at = CURRENT_TIMESTAMP
                        """,
                        (table_id, label, pg_table, row["record_id"], json_dumps(row["fields"]), row["content_hash"]),
                    )
                    conn.execute(
                        f"""
                        INSERT INTO {pg_table}
                            (record_id, fields_json, content_hash, synced_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT (record_id) DO UPDATE SET
                            fields_json = EXCLUDED.fields_json,
                            content_hash = EXCLUDED.content_hash,
                            synced_at = CURRENT_TIMESTAMP
                        """,
                        (row["record_id"], json_dumps(row["fields"]), row["content_hash"]),
                    )
                conn.commit()
            total += len(rows)
            results.append({"table_id": table_id, "label": label, "pg_table": pg_table, "rows": len(rows)})
    finally:
        if conn is not None:
            conn.close()
    return {"ok": True, "dry_run": dry_run, "tables": results, "rows": total}


def safe_topic(text: str) -> str:
    text = normalize_text(text)
    return re.sub(r"\s+", " ", text)[:120]
