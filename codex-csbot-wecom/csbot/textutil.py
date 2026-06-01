from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Iterable


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
WORD_RE = re.compile(r"[A-Za-z0-9_.+-]+")


def normalize_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    text = normalize_text(text).lower()
    tokens = WORD_RE.findall(text)
    chars = CJK_RE.findall(text)
    tokens.extend(chars)
    for size in (2, 3, 4):
        for i in range(0, max(0, len(chars) - size + 1)):
            tokens.append("".join(chars[i : i + size]))
    return tokens


def stable_id(*parts: object) -> str:
    raw = "|".join(normalize_text(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def score_text(query: str, text: str) -> float:
    q = Counter(tokenize(query))
    d = Counter(tokenize(text))
    if not q or not d:
        return 0.0
    overlap = sum(min(q[token], d[token]) for token in q)
    norm = math.sqrt(sum(v * v for v in q.values())) * math.sqrt(sum(v * v for v in d.values()))
    return overlap / norm if norm else 0.0


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: object) -> object:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def first_non_empty(values: Iterable[object]) -> str:
    for value in values:
        text = normalize_text(value)
        if text:
            return text
    return ""
