"""Inbox scanning helpers.

The first implementation is accessibility-text based. It returns visible text
that may include conversation names and unread badges, but deliberately labels
the result as heuristic because WeCom layouts vary by version.
"""

from __future__ import annotations

import os
import unicodedata

from cli_anything.wecom_gui.utils import macos_backend


NAVIGATION_NOISE = {
    "消息",
    "通讯录",
    "工作台",
    "会议",
    "文档",
    "邮件",
    "日程",
    "搜索",
}

SENSITIVE_OR_NON_CHAT_MARKERS = (
    "authorization bearer",
    "x-api-key",
    "request headers",
    "response headers",
    "query string parameters",
    "content-type",
    "application/json",
)

BLOCKED_TAGS = {"部门"}
GROUP_TAGS = {"外部"}
MAX_TITLE_LENGTH = 90
MAX_PREVIEW_LENGTH = 500
DEFAULT_REQUIRED_TAG = "@微信"


def _clean_label(value: object) -> str:
    """Normalize labels exposed by Accessibility, removing invisible marks."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").strip()


def _tag_key(value: object) -> str:
    """Normalize tag text for matching OCR variants like '@ 重庆邮电大学'."""
    return _clean_label(value).replace(" ", "")


def required_tags() -> list[str]:
    """Return accepted customer tags, supporting comma-separated env config."""
    raw = os.environ.get("WECOM_GUI_REQUIRED_TAGS", "").strip()
    if not raw:
        raw = os.environ.get("WECOM_GUI_REQUIRED_TAG", DEFAULT_REQUIRED_TAG).strip()
    tags = [_clean_label(part) for part in raw.replace("，", ",").split(",")]
    return [tag for tag in tags if tag]


def is_customer_candidate(row: dict) -> bool:
    """Return whether an inbox row is safe enough to enqueue for replies."""
    title = _clean_label(row.get("title"))
    preview = _clean_label(row.get("preview"))
    tags = {_clean_label(tag) for tag in row.get("tags", [])}
    tag_keys = {_tag_key(tag) for tag in tags}
    required_tag_keys = {_tag_key(tag) for tag in required_tags()}
    raw_text = " ".join(str(part) for part in row.get("raw", []))
    haystack = f"{title} {preview} {raw_text}".lower()

    if not title or title in NAVIGATION_NOISE:
        return False
    require_wechat_tag = os.environ.get("WECOM_GUI_REQUIRE_WECHAT_TAG", "1") != "0"
    if required_tag_keys and not (required_tag_keys & tag_keys):
        if require_wechat_tag:
            return False
        if tags or os.environ.get("WECOM_GUI_ALLOW_UNTAGGED") != "1":
            return False
    if len(title) > MAX_TITLE_LENGTH or len(preview) > MAX_PREVIEW_LENGTH:
        return False
    if any(marker in haystack for marker in SENSITIVE_OR_NON_CHAT_MARKERS):
        return False
    if tags & BLOCKED_TAGS:
        return False
    if tags & GROUP_TAGS and os.environ.get("WECOM_GUI_INCLUDE_EXTERNAL_GROUPS") != "1":
        return False
    return True


def scan_visible(app_name: str | None = None, *, limit: int = 30) -> dict:
    """Return visible inbox-ish labels from the current WeCom window."""
    rows = [
        row
        for row in macos_backend.conversation_rows(app_name, limit=limit)
        if is_customer_candidate(row)
    ]
    return {
        "ok": True,
        "source": "accessibility",
        "heuristic": True,
        "conversation_count": len(rows),
        "conversations": rows,
    }


def open_by_name(name: str, app_name: str | None = None) -> dict:
    """Open a visible conversation by exact accessible label."""
    return macos_backend.click_text(name, app_name)


def open_row(row: dict, app_name: str | None = None) -> dict:
    """Open a visible conversation row, using OCR coordinates when present."""
    if row.get("source") == "axuielement":
        if app_name is None:
            return open_by_name(str(row.get("title") or ""))
        return open_by_name(str(row.get("title") or ""), app_name)
    click_x = row.get("click_x")
    click_y = row.get("click_y")
    if click_x is not None and click_y is not None:
        return macos_backend.click_point(float(click_x), float(click_y), app_name)
    if app_name is None:
        return open_by_name(str(row.get("title") or ""))
    return open_by_name(str(row.get("title") or ""), app_name)
