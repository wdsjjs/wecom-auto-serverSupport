from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib import request

import pytest
from click.testing import CliRunner

from cli_anything.wecom_gui.core import agent, chat, inbox, llm, reply, state, watcher, worker
from cli_anything.wecom_gui.core import review_server, sidebar_server
from cli_anything.wecom_gui.core.sidebar_server import _bind_payload
from cli_anything.wecom_gui.utils import macos_backend
from cli_anything.wecom_gui.wecom_gui_cli import cli


def test_current_open_chat_is_enqueued_without_sidebar_unread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "active-chat-hash",
            "messages": [{"role": "用户", "content": "我还想问一下", "text": "我还想问一下"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {
            "title": "客户A",
            "preview": "旧预览",
            "time": "刚刚",
            "tags": ["@微信"],
            "unread": True,
            "unread_count": 1,
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 1
    assert result["conversation"] == "客户A"
    queued = state.list_queue(status="pending")[0]
    assert queued["title"] == "客户A"
    assert queued["preview"] == "我还想问一下"

def test_current_open_chat_enqueues_selected_conversation_without_unread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "active-chat-hash",
            "messages": [{"role": "用户", "role_confidence": "medium", "content": "我还想问一下", "text": "我还想问一下"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {
            "title": "客户A",
            "preview": "旧预览",
            "time": "刚刚",
            "tags": ["@微信"],
            "unread": False,
            "unread_count": 0,
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 1
    assert result["conversation"] == "客户A"
    queued = state.list_queue(status="pending")[0]
    assert queued["title"] == "客户A"
    assert queued["preview"] == "我还想问一下"

def test_current_open_chat_reclassifies_selected_preview_when_role_is_service(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    preview = "这3个东西可以和你们的鱼油一起吃吗？"
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "active-chat-hash",
            "messages": [
                {"role": "客服", "role_confidence": "high", "content": preview, "text": preview}
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {
            "title": "客户A",
            "preview": preview,
            "time": "刚刚",
            "tags": ["@微信"],
            "unread": False,
            "unread_count": 0,
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 1
    assert result["latest"] == preview
    queued = state.list_queue(status="pending")[0]
    assert queued["title"] == "客户A"
    assert queued["preview"] == preview

def test_log_current_chat_records_not_enqueued_reason(monkeypatch):
    logs = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", lambda message: logs.append(message))

    agent._log_current_chat({"ok": True, "enqueued": 0, "reason": "unexpected_failure"})

    assert logs == ["[AI客服] 当前会话未入队：原因=unexpected_failure"]

def test_log_current_chat_suppresses_common_not_enqueued_reason(monkeypatch):
    logs = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", lambda message: logs.append(message))

    agent._log_current_chat({"ok": True, "enqueued": 0, "reason": "selected_conversation_not_found"})

    assert logs == []

def test_current_open_chat_does_not_reenqueue_same_active_message(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    selected = {
        "title": "客户A",
        "preview": "这是哪里？",
        "time": "刚刚",
        "tags": ["@微信"],
        "unread": True,
        "unread_count": 1,
        "raw": [],
        "selected": True,
    }
    current = {
        "ok": True,
        "hash": "same-chat-hash",
        "messages": [
            {"role": "用户", "role_confidence": "medium", "content": "这是哪里？", "text": "这是哪里？"}
        ],
    }
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=4, capture_images=False: current)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: selected,
    )

    first = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)
    assert first["enqueued"] == 1
    job = state.list_queue(status="pending")[0]
    state.mark_drafting(
        job["id"],
        message_hash="same-chat-hash",
        messages=current["messages"],
        latest=current["messages"][-1],
    )

    second = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert second["enqueued"] == 0
    assert second["reason"] == "same_current_message"
    assert state.queue_counts().get("drafting") == 1
    assert state.queue_counts().get("pending", 0) == 0

def test_stale_active_seconds_respects_ai_timeout(monkeypatch):
    monkeypatch.delenv("WECOM_AGENT_STALE_ACTIVE_SECONDS", raising=False)
    monkeypatch.setenv("WECOM_GUI_CODEX_TIMEOUT", "300")
    monkeypatch.setenv("WECOM_GUI_PI_TIMEOUT", "300")

    assert agent._stale_active_seconds() == 360.0

def test_stale_active_seconds_can_be_overridden(monkeypatch):
    monkeypatch.setenv("WECOM_AGENT_STALE_ACTIVE_SECONDS", "45")
    monkeypatch.setenv("WECOM_GUI_CODEX_TIMEOUT", "300")
    monkeypatch.setenv("WECOM_GUI_PI_TIMEOUT", "300")

    assert agent._stale_active_seconds() == 45.0

def test_agent_skips_sidebar_scan_when_current_chat_enqueued(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._normalize_wecom_window", lambda: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.worker.enqueue_current_chat_if_changed",
        lambda last, inbox_limit: {"ok": True, "enqueued": 1, "conversation": "客户A", "latest": "新消息"},
    )

    def fail_scan_once(**kwargs):
        raise AssertionError("sidebar scan should be skipped when current chat is enqueued")

    monkeypatch.setattr("cli_anything.wecom_gui.core.worker.scan_once", fail_scan_once)
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._finish_drafts", lambda futures: {"ready": 0, "failed": 0})
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._send_one_ready", lambda last, mode: {"sent": 0})
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._read_one_pending", lambda **kwargs: {"read": 0, "reason": "queue_empty"})
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", lambda message: None)

    result = agent.agent_loop(
        mode="dry-run",
        poll=0,
        scan_interval=0,
        inbox_limit=8,
        max_drafts=1,
        last=4,
        log_interval=999,
        once=True,
        scan_pages=2,
        scroll_ticks=20,
        deep_scan_interval=30,
    )

    assert result["scanned"] == 1

def test_current_open_chat_ignores_low_role_confidence(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "active-chat-hash",
            "messages": [{"role": "用户", "role_confidence": "low", "content": "自己发出的消息", "text": "自己发出的消息"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=8: {
            "title": "客户A",
            "preview": "自己发出的消息",
            "time": "刚刚",
            "tags": ["@微信"],
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 0
    assert result["reason"] == "selected_conversation_low_role_confidence"

def test_current_open_chat_ignores_latest_service_message(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "service-hash",
            "messages": [{"role": "客服", "content": "已回复", "text": "已回复"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=8: {
            "title": "客户A",
            "preview": "客户上一条问题",
            "time": "刚刚",
            "tags": ["@微信"],
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 0
    assert result["reason"] == "latest_message_not_user"
    assert state.list_queue(status="pending") == []

def test_current_open_chat_ignores_recent_own_reply_even_if_role_is_misread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, "sig1")
    assert changed is True
    state.mark_done(item["id"], message_hash="hash", reply_text="您好～这个不是我们产品哦。")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "active-chat-hash",
            "messages": [
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "您好～这个不是我们产品哦。",
                    "text": "您好～这个不是我们产品哦。",
                }
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {
            "title": "客户A",
            "preview": "您好～这个不是我们产品哦。",
            "time": "刚刚",
            "tags": ["@微信"],
            "unread": False,
            "unread_count": 0,
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 0
    assert result["reason"] == "latest_visible_message_is_own_reply"
    assert state.list_queue(status="pending") == []

def test_current_open_chat_preview_fallback_still_ignores_own_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    reply_text = "您好～这个不是我们产品哦。"
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, "sig1")
    assert changed is True
    state.mark_done(item["id"], message_hash="hash", reply_text=reply_text)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "active-chat-hash",
            "messages": [
                {
                    "role": "客服",
                    "role_confidence": "high",
                    "content": reply_text,
                    "text": reply_text,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {
            "title": "客户A",
            "preview": reply_text,
            "time": "刚刚",
            "tags": ["@微信"],
            "unread": False,
            "unread_count": 0,
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 0
    assert result["reason"] == "latest_visible_message_is_own_reply"
    assert state.list_queue(status="pending") == []

def test_current_open_chat_ignores_system_selected_conversation(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=False: {
            "ok": True,
            "hash": "system-hash",
            "messages": [{"role": "用户", "content": "登录操作通知", "text": "登录操作通知"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {
            "title": "企业微信团队",
            "preview": "登录操作通知",
            "time": "昨天",
            "tags": [],
            "unread": False,
            "unread_count": 0,
            "raw": [],
            "selected": True,
        },
    )

    result = worker.enqueue_current_chat_if_changed(last=4, inbox_limit=8)

    assert result["enqueued"] == 0
    assert result["reason"] == "selected_conversation_not_customer"
    assert state.list_queue(status="pending") == []

def test_scan_once_enqueues_without_clicking(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.inbox.scan_visible",
        lambda limit=12: {
            "ok": True,
                "conversations": [
                    {
                        "title": "客户A",
                        "preview": "你好",
                        "time": "刚刚",
                        "tags": ["@微信"],
                        "unread": True,
                        "unread_count": 1,
                        "raw": [],
                    },
                    {
                        "title": "客户B",
                        "preview": "已回复",
                        "time": "刚刚",
                        "tags": ["@微信"],
                        "unread": False,
                        "unread_count": 0,
                        "raw": [],
                    },
                    {"title": "企业微信团队", "preview": "登录", "time": "刚刚", "tags": [], "raw": []},
                ],
            },
    )
    clicked = {"value": False}
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_by_name", lambda title: clicked.update(value=True))

    result = worker.scan_once(inbox_limit=12)

    assert result["enqueued"] == 1
    assert result["ignored"] == 1
    assert clicked["value"] is False
    assert state.list_queue(status="pending")[0]["title"] == "客户A"

def test_scan_once_scrolls_multiple_pages_and_deduplicates(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    pages = [
        [
            {
                "title": "客户A",
                "preview": "你好",
                "time": "刚刚",
                "tags": ["@微信"],
                "unread": True,
                "unread_count": 1,
                "raw": [],
            }
        ],
        [
            {
                "title": "客户A",
                "preview": "你好",
                "time": "刚刚",
                "tags": ["@微信"],
                "unread": True,
                "unread_count": 1,
                "raw": [],
            },
            {
                "title": "客户C",
                "preview": "在吗",
                "time": "刚刚",
                "tags": ["@重庆邮电大学"],
                "unread": True,
                "unread_count": 1,
                "raw": [],
            },
        ],
    ]
    calls = {"scan": 0, "scroll": []}

    def fake_scan_visible(limit=12):
        index = min(calls["scan"], len(pages) - 1)
        calls["scan"] += 1
        return {"ok": True, "conversations": pages[index]}

    def fake_scroll(direction, *, ticks=6, x=None, y=None):
        calls["scroll"].append((direction, ticks, x, y))
        return {"ok": True}

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.scan_visible", fake_scan_visible)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.scroll_sidebar", fake_scroll)

    result = worker.scan_once(inbox_limit=12, scan_pages=2, scroll_ticks=5)

    assert result["pages_scanned"] == 2
    assert result["visible"] == 2
    assert result["enqueued"] == 2
    assert calls["scan"] == 2
    assert calls["scroll"] == [("down", 5, None, None), ("up", 5, None, None)]
    assert sorted(item["title"] for item in state.list_queue(status="pending")) == ["客户A", "客户C"]

def test_scan_once_tolerates_scroll_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "unread": True, "raw": []}

    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.inbox.scan_visible",
        lambda limit=30: {"ok": True, "conversations": [row]},
    )

    def fail_scroll(direction, *, ticks=6, x=None, y=None):
        raise RuntimeError("scroll not supported")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.scroll_sidebar", fail_scroll)

    result = worker.scan_once(inbox_limit=5, scan_pages=2, scroll_ticks=5)

    assert result["ok"] is True
    assert result["enqueued"] == 1
    assert state.list_queue(status="pending")[0]["title"] == "客户A"

def test_agent_loop_only_deep_scans_on_interval(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    pages_used = []
    times = iter([0, 0.5, 1.0])

    reset_values = []
    monkeypatch.setenv("WECOM_GUI_CODEX_TIMEOUT", "300")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.state.reset_stale_active",
        lambda older_than_seconds=180: reset_values.append(older_than_seconds) or 0,
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.worker.enqueue_current_chat_if_changed",
        lambda last, inbox_limit: {"ok": True, "enqueued": 0},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.worker.scan_once",
        lambda inbox_limit, scan_pages=1, scroll_ticks=6: pages_used.append(scan_pages)
        or {
            "ok": True,
            "visible": 0,
            "unread": 0,
            "enqueued": 0,
            "ignored_no_unread": 0,
            "ignored_existing": 0,
            "pages_scanned": scan_pages,
            "items": [],
            "unread_items": [],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._finish_drafts", lambda futures: {"ready": 0, "failed": 0})
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._send_one_ready", lambda last, mode: {"sent": 0})
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.agent._read_one_pending",
        lambda last, executor, futures, max_drafts: {"ok": True, "read": 0, "reason": "queue_empty"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log_heartbeat", lambda **kwargs: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", lambda message: None)
    def fake_sleep(seconds):
        if len(pages_used) >= 3:
            raise StopIteration

    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.time", lambda: next(times))
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", fake_sleep)

    try:
        agent.agent_loop(
            poll=0,
            scan_interval=0.1,
            inbox_limit=8,
            last=4,
            mode="dry-run",
            max_drafts=1,
            scan_pages=3,
            scroll_ticks=6,
            deep_scan_interval=30,
            once=False,
        )
    except StopIteration:
        pass

    assert pages_used == [3, 1, 1]
    assert reset_values and all(value == 360.0 for value in reset_values)
