from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

from .db import connect, ensure_schema
from .script_search import extract_child_age, script_search
from .script_search import _is_supplement_scope
from .textutil import json_loads, normalize_text, score_text
from .vector_store import search_memories


def _known_user_facts(query: str, vector_hits: list[dict], context: dict | None) -> dict:
    facts: dict[str, str] = {}
    if isinstance(context, dict):
        known = context.get("known_facts")
        if isinstance(known, dict):
            facts.update({str(k): str(v) for k, v in known.items() if v})
    child_age = extract_child_age(query)
    for hit in vector_hits:
        metadata = hit.get("metadata") or {}
        hit_type = metadata.get("type") or hit.get("type")
        if hit_type == "customer_profile":
            child_age = child_age or extract_child_age(hit.get("text", ""))
    if child_age:
        facts["child_age"] = child_age
    return facts


def _hydrate_vector_knowledge(db_path: str | Path, vector_hits: list[dict]) -> list[dict]:
    hydrated: list[dict] = []
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        for hit in vector_hits:
            metadata = hit.get("metadata") or {}
            kb_doc_id = metadata.get("kb_doc_id")
            if not kb_doc_id:
                hydrated.append(hit)
                continue
            row = conn.execute("SELECT * FROM kb_docs WHERE kb_doc_id = ?", (kb_doc_id,)).fetchone()
            if not row:
                hydrated.append(hit)
                continue
            enriched = dict(hit)
            facts = json_loads(row["facts_json"], {})
            enriched["product"] = row["product"]
            enriched["business_type"] = row["business_type"]
            enriched["source"] = {
                "sheet": row["source_sheet"],
                "row": row["source_row"],
                "field": row["source_field"] or "",
                "kb_doc_id": row["kb_doc_id"],
            }
            enriched["facts"] = facts if isinstance(facts, dict) else {}
            hydrated.append(enriched)
    finally:
        conn.close()
    return hydrated


def _filter_supplement_vector_hits(vector_hits: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for hit in vector_hits:
        metadata = hit.get("metadata") or {}
        hit_type = metadata.get("type") or hit.get("type")
        if hit_type == "customer_profile":
            filtered.append(hit)
            continue
        if hit_type != "knowledge":
            continue
        if metadata.get("business_type") in {
            "recommendation_rule",
            "product_profile",
            "safety_policy",
            "research_evidence",
        } and metadata.get("source_sheet") in {"10 补剂推荐", "5 产品常规信息", "7 L0级注意事项", "6 论文表"}:
            filtered.append(hit)
    return filtered


def _product_from_profile(vector_hits: list[dict]) -> str:
    profile_candidates = [hit for hit in vector_hits if (hit.get("metadata") or {}).get("type") == "customer_profile"]
    for hit in profile_candidates:
        metadata = hit.get("metadata") or {}
        product = normalize_text(metadata.get("product"))
        if product:
            return product
    for hit in vector_hits:
        metadata = hit.get("metadata") or {}
        product = normalize_text(metadata.get("product"))
        if product:
            return product
        text = hit.get("text", "")
        # Keep this deliberately conservative; aliases still go through script_search.
        for candidate in ("婴幼少儿 DHA 藻油", "女士复合维生素", "男士复合维生素"):
            if candidate in text:
                return candidate
    return ""


def retrieve(
    *,
    customer_id: str,
    query: str,
    db_path: str | Path,
    context: dict | None = None,
) -> dict:
    started = time.perf_counter()
    timing: dict[str, int | None] = {
        "script_ms": None,
        "vector_ms": None,
        "hydrate_ms": None,
    }
    context = context or {}
    disable_vector = bool(context.get("disable_vector"))
    supplement_scope = _is_supplement_scope(context)
    vector_hits: list[dict] = []
    vector_error = None

    if disable_vector:
        script_context = context
        script_started = time.perf_counter()
        script_result = script_search(query, db_path, script_context)
        timing["script_ms"] = round((time.perf_counter() - script_started) * 1000)
    else:
        vector_started = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                vector_future = executor.submit(search_memories, db_path, customer_id=customer_id, query=query)
                vector_hits = vector_future.result(timeout=10)
                timing["vector_ms"] = round((time.perf_counter() - vector_started) * 1000)
            hydrate_started = time.perf_counter()
            vector_hits = _hydrate_vector_knowledge(db_path, vector_hits)
            if supplement_scope:
                vector_hits = _filter_supplement_vector_hits(vector_hits)
            timing["hydrate_ms"] = round((time.perf_counter() - hydrate_started) * 1000)
        except Exception as exc:
            if timing["vector_ms"] is None:
                timing["vector_ms"] = round((time.perf_counter() - vector_started) * 1000)
            vector_error = str(exc)
            vector_hits = []
        product = _product_from_profile(vector_hits)
        script_context = dict(context)
        if product and not script_context.get("known_product"):
            script_context["known_product"] = product
        script_started = time.perf_counter()
        script_result = script_search(query, db_path, script_context)
        timing["script_ms"] = round((time.perf_counter() - script_started) * 1000)

    script_hits = script_result["hits"]
    conflicts = list(script_result.get("conflicts", []))
    needs_clarification = bool(script_result.get("needs_clarification"))
    if not script_hits and vector_hits:
        needs_clarification = True
    timing["total_ms"] = round((time.perf_counter() - started) * 1000)

    return {
        "query": query,
        "customer_id": customer_id,
        "script_hits": script_hits,
        "vector_hits": vector_hits,
        "merged_context": {
            "answer_basis": "script_first" if script_hits else "vector_only",
            "known_user_facts": _known_user_facts(query, vector_hits, context),
            "conflicts": conflicts,
            "needs_clarification": needs_clarification,
            "vector_error": vector_error,
        },
        "metrics": {
            "timing": timing,
            "script_count": len(script_hits),
            "vector_count": len(vector_hits),
            "vector_disabled": disable_vector,
            "scope": "supplement" if supplement_scope else "",
        },
    }
