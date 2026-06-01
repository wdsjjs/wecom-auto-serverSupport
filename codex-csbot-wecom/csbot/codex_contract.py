from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_ACTIONS = {"send", "clarify", "handoff", "no_answer"}


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_key(source: dict) -> tuple:
    return (
        source.get("sheet"),
        source.get("row"),
        source.get("field", ""),
        source.get("kb_doc_id", ""),
    )


def validate_codex_reply(reply: dict, retrieval: dict) -> ValidationResult:
    action = reply.get("action")
    if action not in VALID_ACTIONS:
        return ValidationResult(False, "invalid_action")
    if action in {"send", "clarify", "handoff", "no_answer"} and not _has_text(reply.get("reply_text")):
        return ValidationResult(False, "empty_reply_text")
    if action == "send":
        used_sources = reply.get("used_script_sources") or []
        if not isinstance(used_sources, list) or not used_sources:
            return ValidationResult(False, "send_requires_used_script_sources")
        available = {
            _source_key(hit.get("source") or {})
            for hit in retrieval.get("script_hits", [])
            if isinstance(hit.get("source"), dict)
        }
        for source in used_sources:
            if not isinstance(source, dict):
                return ValidationResult(False, "invalid_source_shape")
            if _source_key(source) not in available:
                return ValidationResult(False, "used_source_not_in_retrieval")
    return ValidationResult(True)


def validate_autonomous_reply(reply: dict) -> ValidationResult:
    action = reply.get("action")
    if action not in VALID_ACTIONS:
        return ValidationResult(False, "invalid_action")
    if action in {"send", "clarify", "handoff", "no_answer"} and not _has_text(reply.get("reply_text")):
        return ValidationResult(False, "empty_reply_text")
    for field in ("used_script_sources", "used_vector_memories"):
        if not isinstance(reply.get(field), list):
            return ValidationResult(False, f"{field}_must_be_array")
    confidence = reply.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        return ValidationResult(False, "invalid_confidence")
    return ValidationResult(True)
