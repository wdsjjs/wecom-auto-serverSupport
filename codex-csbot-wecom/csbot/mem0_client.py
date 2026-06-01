from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import MEM0_GLOBAL_USER_ID, resolve_mem0_api_key, resolve_mem0_url
from .textutil import normalize_text


class Mem0Error(RuntimeError):
    pass


@dataclass
class Mem0Client:
    base_url: str
    api_key: str
    timeout: float = 90.0

    @classmethod
    def from_env(cls) -> "Mem0Client | None":
        base_url = resolve_mem0_url()
        if not base_url:
            return None
        api_key = resolve_mem0_api_key()
        if not api_key:
            raise Mem0Error("CSBOT_MEM0_URL is set but no CSBOT_MEM0_API_KEY or ADMIN_API_KEY was found")
        return cls(base_url=base_url, api_key=api_key)

    def _request(self, method: str, path: str, payload: dict | None = None, query: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise Mem0Error(f"Mem0 HTTP {exc.code}: {body}") from exc
        except TimeoutError as exc:
            raise Mem0Error(f"Mem0 request timed out after {self.timeout}s: {method} {path}") from exc
        except socket.timeout as exc:
            raise Mem0Error(f"Mem0 request timed out after {self.timeout}s: {method} {path}") from exc
        except urllib.error.URLError as exc:
            raise Mem0Error(f"Mem0 connection failed: {exc}") from exc

    def _request_with_retries(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict | None = None,
        *,
        attempts: int = 3,
    ) -> Any:
        last_error: Mem0Error | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._request(method, path, payload=payload, query=query)
            except Mem0Error as exc:
                last_error = exc
                if attempt == attempts:
                    break
                time.sleep(float(attempt))
        raise last_error or Mem0Error(f"Mem0 request failed: {method} {path}")

    def configure(self) -> dict:
        return self._request("GET", "/configure")

    def add_memory(
        self,
        *,
        customer_id: str,
        text: str,
        metadata: dict | None = None,
        infer: bool = False,
    ) -> Any:
        return self._request_with_retries(
            "POST",
            "/memories",
            {
                "messages": [{"role": "user", "content": text}],
                "user_id": normalize_text(customer_id),
                "metadata": metadata or {},
                "infer": infer,
            },
        )

    def delete_memory(self, memory_id: str) -> Any:
        return self._request("DELETE", f"/memories/{urllib.parse.quote(memory_id)}")

    def delete_all_memories(self, *, customer_id: str) -> Any:
        return self._request("DELETE", "/memories", query={"user_id": normalize_text(customer_id)})

    def clear_memories(self, *, customer_id: str, max_batches: int = 100) -> int:
        deleted_batches = 0
        for _ in range(max_batches):
            if not self.list_memories(customer_id=customer_id):
                return deleted_batches
            self.delete_all_memories(customer_id=customer_id)
            deleted_batches += 1
        raise Mem0Error(f"Mem0 clear did not finish for user_id={normalize_text(customer_id)}")

    def list_memories(self, *, customer_id: str) -> list[dict]:
        result = self._request("GET", "/memories", query={"user_id": normalize_text(customer_id)})
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            return result["results"]
        if isinstance(result, list):
            return result
        return []

    def search(
        self,
        *,
        customer_id: str,
        query: str,
        limit: int = 5,
        include_global: bool = True,
    ) -> list[dict]:
        user_ids = [normalize_text(customer_id)]
        if include_global:
            user_ids.append(MEM0_GLOBAL_USER_ID)
        hits: list[dict] = []
        for user_id in [item for item in user_ids if item]:
            result = self._request(
                "POST",
                "/search",
                {"query": query, "user_id": user_id, "top_k": limit},
            )
            for row in _extract_results(result):
                hit = _normalize_hit(row)
                if hit:
                    hits.append(hit)
        return _dedupe_hits(hits)[:limit]


def _extract_results(result: Any) -> list[dict]:
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        return [row for row in result["results"] if isinstance(row, dict)]
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    return []


def _normalize_hit(row: dict) -> dict | None:
    text = row.get("memory") or row.get("text") or row.get("data")
    if not text:
        return None
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        "id": row.get("id", ""),
        "type": metadata.get("type", "memory"),
        "text": text,
        "score": round(float(row.get("score") or 0.0), 4),
        "metadata": metadata,
        "provider": "mem0",
    }


def _dedupe_hits(hits: list[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for hit in sorted(hits, key=lambda item: item["score"], reverse=True):
        metadata = hit.get("metadata") or {}
        key = str(metadata.get("kb_doc_id") or hit.get("id") or hit.get("text"))
        if key not in unique:
            unique[key] = hit
    return list(unique.values())
