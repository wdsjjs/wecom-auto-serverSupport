from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

from .config import MEM0_GLOBAL_USER_ID
from .db import connect, ensure_schema
from .mem0_client import Mem0Client
from .textutil import json_dumps, json_loads, normalize_text, score_text, tokenize

EMBEDDING_DIMS = 1024


def embed_text(text: str, dims: int = EMBEDDING_DIMS) -> list[float]:
    """Deterministic local embedding fallback with a fixed 1024-dim shape.

    Production can swap this for UDA `text-embedding-v4`; the schema and tests
    still enforce the expected 1024-dimensional vector boundary.
    """
    vec = [0.0] * dims
    for token in tokenize(text):
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[index] += sign
    norm = math.sqrt(sum(value * value for value in vec))
    if norm:
        vec = [value / norm for value in vec]
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def add_memory(db_path: str | Path, *, customer_id: str, text: str, metadata: dict | None = None) -> int | str:
    client = Mem0Client.from_env()
    if client is not None:
        result = client.add_memory(customer_id=customer_id, text=text, metadata=metadata or {}, infer=False)
        results = result.get("results") if isinstance(result, dict) else None
        if isinstance(results, list) and results:
            return str(results[0].get("id", ""))
        return ""
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        params = (
            normalize_text(customer_id),
            normalize_text(text),
            json_dumps(metadata or {}),
            json.dumps(embed_text(text)),
        )
        if getattr(conn, "is_pg", False):
            cur = conn.execute(
                """
                INSERT INTO vector_memories (customer_id, text, metadata_json, embedding_json)
                VALUES (?, ?, ?, ?)
                RETURNING id
                """,
                params,
            )
            row = cur.fetchone()
            memory_id = int(row["id"]) if row else 0
        else:
            cur = conn.execute(
                """
                INSERT INTO vector_memories (customer_id, text, metadata_json, embedding_json)
                VALUES (?, ?, ?, ?)
                """,
                params,
            )
            memory_id = int(cur.lastrowid)
        conn.commit()
        return memory_id
    finally:
        conn.close()


def search_memories(
    db_path: str | Path,
    *,
    customer_id: str,
    query: str,
    limit: int = 5,
    include_global: bool = True,
) -> list[dict]:
    client = Mem0Client.from_env()
    if client is not None:
        return client.search(customer_id=customer_id, query=query, limit=limit, include_global=include_global)
    query_vec = embed_text(query)
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        if include_global:
            rows = conn.execute(
                "SELECT * FROM vector_memories WHERE customer_id IN (?, '')",
                (normalize_text(customer_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vector_memories WHERE customer_id = ?",
                (normalize_text(customer_id),),
            ).fetchall()
    finally:
        conn.close()
    hits = []
    for row in rows:
        emb = json_loads(row["embedding_json"], [])
        if not isinstance(emb, list):
            emb = []
        semantic = cosine(query_vec, emb)
        lexical = score_text(query, row["text"])
        score = 0.65 * semantic + 0.35 * lexical
        if score <= 0 and row["customer_id"]:
            score = 0.05
        if score <= 0:
            continue
        metadata = json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        hits.append(
            {
                "id": row["id"],
                "type": metadata.get("type", "memory"),
                "text": row["text"],
                "score": round(float(score), 4),
                "metadata": metadata,
            }
        )
    unique: dict[str, dict] = {}
    for hit in sorted(hits, key=lambda item: item["score"], reverse=True):
        metadata = hit.get("metadata") or {}
        key = str(metadata.get("kb_doc_id") or hit["id"])
        if key not in unique:
            unique[key] = hit
    return list(unique.values())[:limit]


def import_kb_docs_as_memories(db_path: str | Path, *, progress: bool = False) -> int:
    client = Mem0Client.from_env()
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        if client is None:
            if getattr(conn, "is_pg", False):
                conn.execute(
                    """
                    DELETE FROM vector_memories
                    WHERE customer_id = ''
                      AND COALESCE(metadata_json::jsonb ->> 'type', '') = 'knowledge'
                    """
                )
            else:
                conn.execute(
                    "DELETE FROM vector_memories WHERE customer_id = '' AND json_extract(metadata_json, '$.type') = 'knowledge'"
                )
            conn.commit()
        else:
            deleted_batches = client.clear_memories(customer_id=MEM0_GLOBAL_USER_ID)
            if progress:
                print(f"cleared_mem0_batches={deleted_batches}", file=sys.stderr, flush=True)
        docs = conn.execute("SELECT * FROM kb_docs").fetchall()
    finally:
        conn.close()
    count = 0
    for row in docs:
        metadata = {
            "type": "knowledge",
            "kb_doc_id": row["kb_doc_id"],
            "business_type": row["business_type"],
            "product": row["product"],
            "topic": row["topic"],
            "source_sheet": row["source_sheet"],
            "source_row": row["source_row"],
            "source_field": row["source_field"] or "",
            "source": _source_from_facts(row["facts_json"]),
            "embedding_dims": EMBEDDING_DIMS,
        }
        if client is None:
            add_memory(db_path, customer_id="", text=row["text"], metadata=metadata)
        else:
            client.add_memory(customer_id=MEM0_GLOBAL_USER_ID, text=row["text"], metadata=metadata, infer=False)
        count += 1
        if progress and (count == len(docs) or count % 50 == 0):
            print(f"imported_vector_memories={count}/{len(docs)}", file=sys.stderr, flush=True)
    return count


def _source_from_facts(raw: str) -> str:
    facts = json_loads(raw, {})
    if isinstance(facts, dict):
        return normalize_text(facts.get("_source"))
    return ""
