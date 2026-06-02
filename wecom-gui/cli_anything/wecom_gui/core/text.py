"""Text normalization shared by review display and final sending."""

from __future__ import annotations

import re


_CODE_FENCE_RE = re.compile(r"^\s*```.*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_DIRECTIVE_RE = re.compile(r"^\s*::[a-zA-Z0-9_-]+(?:\{.*\})?\s*$")
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:_-]*(?:\s+[^<>]*)?>")
_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BULLET_RE = re.compile(r"^(\s*)[*+]\s+")


def clean_customer_reply_text(text: object) -> str:
    """Remove Markdown/control syntax while keeping readable reply structure."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return ""

    cleaned_lines: list[str] = []
    in_fence = False
    for raw_line in value.split("\n"):
        line = raw_line.rstrip()
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if _DIRECTIVE_RE.match(line):
            continue
        line = _HEADING_RE.sub("", line)
        line = _QUOTE_RE.sub("", line)
        line = _BULLET_RE.sub(r"\1- ", line)
        line = _TAG_RE.sub("", line)
        line = _INLINE_CODE_RE.sub(r"\1", line)
        previous = None
        while previous != line:
            previous = line
            line = _BOLD_RE.sub(r"\2", line)
            line = _ITALIC_STAR_RE.sub(r"\1", line)
            line = _ITALIC_UNDERSCORE_RE.sub(r"\1", line)
        cleaned_lines.append(line.strip() if not line.startswith(" ") else line.rstrip())

    result = "\n".join(cleaned_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
