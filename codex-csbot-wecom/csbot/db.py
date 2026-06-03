from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import resolve_pg_dsn


class PgCompatConnection:
    is_pg = True

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple | list | dict | None = None):
        cur = self._conn.cursor()
        cur.execute(_pg_sql(sql), params or ())
        return cur

    def executescript(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            for statement in _split_sql_script(sql):
                cur.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self._conn.rollback()
        self.close()


def _pg_sql(sql: str) -> str:
    return sql.replace("?", "%s")


def _split_sql_script(sql: str) -> list[str]:
    statements = []
    for part in sql.split(";"):
        statement = part.strip()
        if statement:
            statements.append(statement)
    return statements


def _connect_pg(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    try:
        connect_timeout = int(os.environ.get("CSBOT_PG_CONNECT_TIMEOUT", "8"))
    except ValueError:
        connect_timeout = 8
    return PgCompatConnection(psycopg.connect(dsn, row_factory=dict_row, connect_timeout=max(1, connect_timeout)))


def connect(db_path: str | Path | None = None):
    dsn = resolve_pg_dsn()
    if dsn:
        return _connect_pg(dsn)
    if db_path is None:
        raise ValueError("db_path is required when CSBOT_PG_DSN is not set")
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn) -> None:
    if getattr(conn, "is_pg", False):
        _ensure_pg_schema(conn)
    else:
        _ensure_sqlite_schema(conn)


def _ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kb_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kb_docs (
            kb_doc_id TEXT PRIMARY KEY,
            kb_version TEXT NOT NULL,
            business_type TEXT NOT NULL,
            product TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            facts_json TEXT NOT NULL DEFAULT '{}',
            source_sheet TEXT NOT NULL,
            source_row INTEGER NOT NULL,
            source_field TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS kb_aliases (
            alias TEXT NOT NULL,
            product TEXT NOT NULL,
            kb_doc_id TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            PRIMARY KEY (alias, product, kb_doc_id)
        );

        CREATE TABLE IF NOT EXISTS vector_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reply_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            query TEXT NOT NULL,
            retrieval_json TEXT NOT NULL,
            reply_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS feishu_sync_records (
            table_id TEXT NOT NULL,
            table_label TEXT NOT NULL,
            pg_table TEXT NOT NULL,
            record_id TEXT NOT NULL,
            fields_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (table_id, record_id)
        );

        CREATE TABLE IF NOT EXISTS weiban_customer_service_faq (
            weiban_id INTEGER PRIMARY KEY,
            group_id INTEGER,
            group_name TEXT,
            parent_group_name TEXT,
            weiban_collection_id INTEGER,
            content_type TEXT,
            title TEXT NOT NULL,
            summary TEXT,
            body TEXT,
            fuzzy_keywords TEXT NOT NULL DEFAULT '[]',
            exact_keywords TEXT NOT NULL DEFAULT '[]',
            image_url TEXT,
            image_size TEXT,
            local_image_path TEXT,
            file_name TEXT NOT NULL,
            biz_key TEXT NOT NULL UNIQUE,
            risk_level TEXT,
            item_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_feishu_sync_records_pg_table ON feishu_sync_records (pg_table);
        CREATE INDEX IF NOT EXISTS idx_weiban_customer_service_faq_group_name
            ON weiban_customer_service_faq (group_name);
        CREATE INDEX IF NOT EXISTS idx_weiban_customer_service_faq_content_hash
            ON weiban_customer_service_faq (content_hash);

        CREATE TABLE IF NOT EXISTS knowledge_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_knowledge_sync_log_source_created
            ON knowledge_sync_log (source, created_at);
        """
    )
    conn.commit()


def _ensure_pg_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kb_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kb_docs (
            kb_doc_id TEXT PRIMARY KEY,
            kb_version TEXT NOT NULL,
            business_type TEXT NOT NULL,
            product TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            facts_json TEXT NOT NULL DEFAULT '{}',
            source_sheet TEXT NOT NULL,
            source_row INTEGER NOT NULL DEFAULT 0,
            source_field TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS kb_aliases (
            alias TEXT NOT NULL,
            product TEXT NOT NULL,
            kb_doc_id TEXT NOT NULL,
            weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            PRIMARY KEY (alias, product, kb_doc_id)
        );

        CREATE TABLE IF NOT EXISTS vector_memories (
            id BIGSERIAL PRIMARY KEY,
            customer_id TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS reply_audit (
            id BIGSERIAL PRIMARY KEY,
            customer_id TEXT NOT NULL,
            query TEXT NOT NULL,
            retrieval_json TEXT NOT NULL,
            reply_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_kb_docs_business_type ON kb_docs (business_type);
        CREATE INDEX IF NOT EXISTS idx_kb_docs_product ON kb_docs (product);
        CREATE INDEX IF NOT EXISTS idx_kb_aliases_alias ON kb_aliases (alias);

        CREATE TABLE IF NOT EXISTS feishu_sync_records (
            table_id TEXT NOT NULL,
            table_label TEXT NOT NULL,
            pg_table TEXT NOT NULL,
            record_id TEXT NOT NULL,
            fields_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (table_id, record_id)
        );

        CREATE TABLE IF NOT EXISTS weiban_customer_service_faq (
            weiban_id BIGINT PRIMARY KEY,
            group_id BIGINT,
            group_name TEXT,
            parent_group_name TEXT,
            weiban_collection_id BIGINT,
            content_type TEXT,
            title TEXT NOT NULL,
            summary TEXT,
            body TEXT,
            fuzzy_keywords TEXT NOT NULL DEFAULT '[]',
            exact_keywords TEXT NOT NULL DEFAULT '[]',
            image_url TEXT,
            image_size TEXT,
            local_image_path TEXT,
            file_name TEXT NOT NULL,
            biz_key TEXT NOT NULL UNIQUE,
            risk_level TEXT,
            item_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_feishu_sync_records_pg_table ON feishu_sync_records (pg_table);
        CREATE INDEX IF NOT EXISTS idx_weiban_customer_service_faq_group_name
            ON weiban_customer_service_faq (group_name);
        CREATE INDEX IF NOT EXISTS idx_weiban_customer_service_faq_content_hash
            ON weiban_customer_service_faq (content_hash);

        CREATE TABLE IF NOT EXISTS knowledge_sync_log (
            id BIGSERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_knowledge_sync_log_source_created
            ON knowledge_sync_log (source, created_at);
        """
    )
    conn.commit()
