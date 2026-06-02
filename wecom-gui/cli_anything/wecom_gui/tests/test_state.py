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


def test_latest_user_message_requires_user_role():
    assert watcher.latest_user_message([]) is None
    assert watcher.latest_user_message([{"role": "客服", "content": "已回复"}]) is None
    latest = watcher.latest_user_message(
        [
            {"role": "客服", "content": "已回复"},
            {"role": "用户", "content": "继续问"},
        ]
    )
    assert latest == {"role": "用户", "content": "继续问"}

def test_conversation_signature_includes_unread_count():
    first = watcher._conversation_signature(
        {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "unread_count": 1}
    )
    second = watcher._conversation_signature(
        {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "unread_count": 2}
    )

    assert first != second

def test_done_queue_item_is_not_reopened_when_only_relative_time_changes(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {
        "title": "客户A",
        "preview": "你好",
        "time": "刚刚",
        "tags": ["@重庆邮电大学"],
        "unread": True,
        "unread_count": 1,
        "raw": [],
    }
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True

    state.mark_done(item["id"], message_hash="hash", reply_text="已回复")

    refreshed_row = {**row, "time": "10分钟前"}
    changed_again, existing = state.enqueue_conversation(
        refreshed_row,
        watcher._conversation_signature(refreshed_row),
    )

    assert changed_again is False
    assert existing["status"] == "done"
    assert state.list_queue(status="pending") == []
    assert state.list_queue(status="done")[0]["reply_text"] == "已回复"

def test_done_queue_item_is_not_reopened_when_preview_is_our_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {
        "title": "客户A",
        "preview": "hello",
        "time": "刚刚",
        "tags": ["@重庆邮电大学"],
        "unread": True,
        "unread_count": 1,
        "raw": [],
    }
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    state.mark_done(item["id"], message_hash="hash", reply_text="您好，请问有什么可以帮您？")

    own_reply_row = {**row, "preview": "您好，请问有什么可以帮您？", "time": "1分钟前"}
    changed_again, existing = state.enqueue_conversation(
        own_reply_row,
        watcher._conversation_signature(own_reply_row),
    )

    assert changed_again is False
    assert existing["status"] == "done"
    assert existing["preview"] == "您好，请问有什么可以帮您？"
    assert state.list_queue(status="pending") == []

def test_done_queue_item_reopens_when_same_signature_still_has_unread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_GUI_DONE_REOPEN_COOLDOWN_SECONDS", "0")
    row = {
        "title": "客户A",
        "preview": "还在吗",
        "time": "刚刚",
        "tags": ["@重庆邮电大学"],
        "unread": True,
        "unread_count": 1,
        "raw": [],
    }
    signature = watcher._conversation_signature(row)
    changed, item = state.enqueue_conversation(row, signature)
    assert changed is True
    state.mark_done(item["id"], message_hash="hash", reply_text="您好，请问有什么可以帮您？")

    changed_again, existing = state.enqueue_conversation(row, signature)

    assert changed_again is True
    assert existing["status"] == "pending"
    assert existing["reply_text"] is None
    assert state.list_queue(status="pending")[0]["title"] == "客户A"

def test_conversation_filter_skips_system_rows():
    assert watcher._should_consider({"title": "企业微信团队", "preview": "登录"}) is False
    assert watcher._should_consider({"title": "客户咨询", "preview": "聊天已结束"}) is False
    assert watcher._should_consider({"title": "iChen", "preview": "你好"}) is True

def test_queue_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "raw": ["客户A", "你好"]}

    changed, item = state.enqueue_conversation(row, "sig1")
    assert changed is True
    assert item["status"] == "pending"
    assert state.list_queue(status="pending")[0]["title"] == "客户A"

    changed_again, _ = state.enqueue_conversation(row, "sig1")
    assert changed_again is False

    claimed = state.claim_next()
    assert claimed["title"] == "客户A"
    assert claimed["status"] == "processing"

    state.mark_done(claimed["id"], message_hash="hash", reply_text="reply")
    done = state.list_queue(status="done")[0]
    assert done["reply_text"] == "reply"

    assert state.clear_queue() == 1

def test_queue_isolates_same_title_by_external_uid(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    first_row = {
        "title": "同名客户",
        "preview": "第一位的问题",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "external_userid": "wm_uid_001",
    }
    second_row = {
        "title": "同名客户",
        "preview": "第二位的问题",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "external_userid": "wm_uid_002",
    }

    changed_first, first = state.enqueue_conversation(first_row, "sig-a")
    changed_second, second = state.enqueue_conversation(second_row, "sig-b")

    assert changed_first is True
    assert changed_second is True
    assert first["conversation_key"] == "uid:wm_uid_001"
    assert second["conversation_key"] == "uid:wm_uid_002"
    items = state.list_queue(status="pending")
    assert len(items) == 2
    assert sorted(item["preview"] for item in items) == ["第一位的问题", "第二位的问题"]

def test_queue_isolates_same_title_by_visible_slot(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    first_row = {
        "title": "同名客户",
        "preview": "上方客户的问题",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "source": "axuielement",
        "click_y": 240.0,
    }
    second_row = {
        "title": "同名客户",
        "preview": "下方客户的问题",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "source": "axuielement",
        "click_y": 320.0,
    }

    changed_first, first = state.enqueue_conversation(first_row, "sig-a")
    changed_second, second = state.enqueue_conversation(second_row, "sig-b")

    assert changed_first is True
    assert changed_second is True
    assert first["conversation_key"] != second["conversation_key"]
    assert len(state.list_queue(status="pending")) == 2

def test_ready_queue_item_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash",
        messages=[{"role": "用户", "content": "旧问题"}],
        latest={"role": "用户", "content": "旧问题"},
    )
    state.mark_ready(claimed["id"], reply_text="旧回复")

    changed, item = state.enqueue_conversation(
        {"title": "客户A", "preview": "新问题", "time": "刚刚", "tags": ["@微信"], "raw": []},
        "sig2",
    )

    assert changed is False
    assert item["status"] == "ready"
    assert state.list_queue(status="ready")[0]["reply_text"] == "旧回复"

def test_active_drafting_item_is_replaced_when_unread_preview_changes(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True, "unread_count": 1}
    state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash",
        messages=[{"role": "用户", "content": "旧问题"}],
        latest={"role": "用户", "content": "旧问题"},
    )

    changed, item = state.enqueue_conversation(
        {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True, "unread_count": 2},
        "sig2",
    )

    assert changed is True
    assert item["status"] == "pending"
    assert item["preview"] == "[图片]"
    assert item["reply_text"] is None
    assert state.list_queue(status="drafting") == []

def test_active_drafting_item_is_not_replaced_when_unread_preview_is_same(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "同一条问题", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True, "unread_count": 1}
    state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash",
        messages=[{"role": "用户", "content": "同一条问题"}],
        latest={"role": "用户", "content": "同一条问题"},
    )

    changed, item = state.enqueue_conversation({**row, "unread_count": 2}, "sig2")

    assert changed is False
    assert item["status"] == "drafting"
    assert item["preview"] == "同一条问题"
    assert state.list_queue(status="pending") == []

def test_mark_drafting_replaces_persisted_conversation_messages(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧预览", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()

    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[
            {"role": "用户", "content": "第一条", "time": "10:00"},
            {"role": "客服", "content": "已回复", "time": "10:01"},
        ],
        latest={"role": "用户", "content": "第一条"},
    )
    state.mark_drafting(
        job["id"],
        message_hash="hash-b",
        messages=[{"role": "用户", "content": "第二条", "time": "10:02"}],
        latest={"role": "用户", "content": "第二条"},
    )

    stored = state.list_conversation_messages(conversation_key=job["conversation_key"])

    assert len(stored) == 1
    assert stored[0]["message_hash"] == "hash-b"
    assert stored[0]["role"] == "用户"
    assert stored[0]["text"] == "第二条"


def test_metrics_summary_tracks_reply_source_handoff_and_latency(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    first_row = {"title": "客户A", "preview": "鱼油怎么吃", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True}
    changed, first = state.enqueue_conversation(first_row, "sig-a")
    assert changed is True
    state.mark_done(first["id"], message_hash="hash-a", reply_text="AI回复", reply_source="ai")

    second_row = {**first_row, "preview": "转人工", "unread_count": 2}
    changed, reopened = state.enqueue_conversation(second_row, "sig-b")
    assert changed is True
    state.mark_ready(reopened["id"], reply_text="这个问题我转人工确认。", duration_ms=16_000, action="handoff")
    assert state.mark_approved(reopened["id"], reply_text="客服改写后回复") is True
    state.mark_done(reopened["id"], message_hash="hash-b", reply_text="客服改写后回复")

    third_row = {**first_row, "preview": "谢谢", "unread_count": 3}
    changed, _third = state.enqueue_conversation(third_row, "sig-c")
    assert changed is True

    summary = state.metrics_summary(since_hours=24)

    assert summary["served_users"] == 1
    assert summary["customer_messages"]["unknown"] == 1
    assert summary["customer_messages"]["after_ai_reply"] == 1
    assert summary["customer_messages"]["after_human_reply"] == 1
    assert summary["handoffs"]["direct"] == 1
    assert summary["handoffs"]["indirect"] == 1
    assert summary["response_time_buckets"]["15-30s"] == 1
    assert summary["diagnostics"]["ai_draft_ready"] == 1
    assert summary["diagnostics"]["human_edited_reply"] == 1


def test_record_conversation_messages_tracks_image_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()

    state.mark_drafting(
        job["id"],
        message_hash="hash-image",
        messages=[
            {
                "role": "用户",
                "content": "[图片]",
                "media": [
                    {"type": "image", "capture_ok": True, "capture_path": "/tmp/ok.png"},
                    {"type": "image", "capture_ok": False, "error": "preview_not_found"},
                ],
            }
        ],
        latest={"role": "用户", "content": "[图片]"},
    )

    summary = state.metrics_summary(since_hours=24)

    assert summary["diagnostics"]["image_message"] == 2
    assert summary["diagnostics"]["image_capture_failed"] == 1

def test_latest_user_message_accepts_low_confidence_for_unread_queue():
    latest = watcher.latest_user_message(
        [{"role": "用户", "role_confidence": "low", "content": "真实未读队列消息"}]
    )

    assert latest["content"] == "真实未读队列消息"

def test_gui_lock_uses_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    with state.gui_lock():
        assert (tmp_path / "gui.lock").exists()
