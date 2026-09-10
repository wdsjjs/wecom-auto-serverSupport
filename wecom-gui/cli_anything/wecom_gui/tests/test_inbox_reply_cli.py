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


def test_messages_contain_text_accepts_wecom_whitespace_variants():
    reply_text = "您好，我是营养工厂客服助手。\n\n请问您想了解哪款产品呢？"
    visible_messages = [
        {"role": "客服", "content": "您好，我是营养工厂客服助手。 请问您想了解哪款产品呢？"}
    ]

    assert worker._messages_contain_text(visible_messages, reply_text) is True

def test_inbox_scan_filters_navigation_noise(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {"title": "消息", "preview": "", "time": "", "tags": [], "raw": []},
            {"title": "客户A", "preview": "你好", "time": "10:00", "tags": ["@微信"], "raw": []},
            {"title": "客户B", "preview": "退款", "time": "10:01", "tags": ["@微信"], "raw": []},
        ],
    )

    data = inbox.scan_visible(limit=10)

    assert data["heuristic"] is True
    assert [row["title"] for row in data["conversations"]] == ["客户A", "客户B"]

def test_inbox_scan_supports_configured_required_tag(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_REQUIRED_TAGS", raising=False)
    monkeypatch.setenv("WECOM_GUI_REQUIRED_TAG", "@重庆邮电大学")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {"title": "客户A", "preview": "你好", "time": "10:00", "tags": ["@微信"], "raw": []},
            {"title": "客户B", "preview": "报名咨询", "time": "10:01", "tags": ["@重庆邮电大学"], "raw": []},
        ],
    )

    data = inbox.scan_visible(limit=10)

    assert [row["title"] for row in data["conversations"]] == ["客户B"]

def test_inbox_scan_supports_multiple_required_tags(monkeypatch):
    monkeypatch.setenv("WECOM_GUI_REQUIRED_TAGS", "@微信,@重庆邮电大学")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {"title": "客户A", "preview": "微信咨询", "time": "10:00", "tags": ["@微信"], "raw": []},
            {"title": "客户B", "preview": "报名咨询", "time": "10:01", "tags": ["@重庆邮电大学"], "raw": []},
            {"title": "客户C", "preview": "内部", "time": "10:02", "tags": ["@其他"], "raw": []},
        ],
    )

    data = inbox.scan_visible(limit=10)

    assert [row["title"] for row in data["conversations"]] == ["客户A", "客户B"]

def test_inbox_scan_allows_untagged_bounded_single_chat_rows(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_REQUIRE_WECHAT_TAG", raising=False)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {
                "title": "客户A",
                "preview": "你好",
                "time": "刚刚",
                "tags": [],
                "raw": [],
                "source": "axuielement-bounded",
            },
            {
                "title": "客户B",
                "preview": "旧链路无标签",
                "time": "刚刚",
                "tags": [],
                "raw": [],
                "source": "axuielement",
            },
        ],
    )

    data = inbox.scan_visible(limit=10)

    assert [row["title"] for row in data["conversations"]] == ["客户A"]

def test_inbox_scan_can_require_tag_for_bounded_rows(monkeypatch):
    monkeypatch.setenv("WECOM_GUI_REQUIRE_WECHAT_TAG", "1")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": [], "raw": [], "source": "axuielement-bounded"},
            {"title": "客户B", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "raw": [], "source": "axuielement-bounded"},
        ],
    )

    data = inbox.scan_visible(limit=10)

    assert [row["title"] for row in data["conversations"]] == ["客户B"]

def test_parse_conversation_parts_uses_configured_tag_marker(monkeypatch):
    monkeypatch.setenv("WECOM_GUI_REQUIRED_TAG", "@重庆邮电大学")

    title, preview, time_text, tags = macos_backend._parse_conversation_parts(
        ["客户B 报名咨询", "刚刚 @重庆邮电大学"]
    )

    assert title == "客户B"
    assert preview == "报名咨询"
    assert time_text == "刚刚"
    assert tags == ["@重庆邮电大学"]

def test_inbox_scan_filters_sensitive_and_non_customer_rows(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {"title": "客户A", "preview": "鱼油含量", "time": "10:00", "tags": ["@微信"], "raw": []},
            {"title": "企业微信团队", "preview": "登录操作通知", "time": "10:01", "tags": [], "raw": []},
            {"title": "部门群", "preview": "内部通知", "time": "10:01", "tags": ["部门"], "raw": []},
            {"title": "外部群", "preview": "群消息", "time": "10:02", "tags": ["外部\u200b"], "raw": []},
            {
                "title": "Debug",
                "preview": "Request Headers Authorization Bearer secret x-api-key abc",
                "time": "10:03",
                "tags": ["@微信"],
                "raw": [],
            },
            {"title": "LongOk", "preview": "x" * 500, "time": "10:04", "tags": ["@微信"], "raw": []},
            {"title": "LongBlocked", "preview": "x" * 501, "time": "10:05", "tags": ["@微信"], "raw": []},
        ],
    )

    data = inbox.scan_visible(limit=10)

    assert [row["title"] for row in data["conversations"]] == ["客户A", "LongOk"]

def test_extract_unread_marker_from_accessibility_button():
    parts, unread_count = macos_backend._extract_unread(
        ["客户A", "你好", "刚刚", "@微信", "__UNREAD__:1"]
    )

    assert parts == ["客户A", "你好", "刚刚", "@微信"]
    assert unread_count == 1

def test_reply_send_dry_run_does_not_touch_gui():
    data = reply.send_text("hello", dry_run=True)

    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["submitted"] is False
    assert data["chars"] == 5

def test_reply_send_uses_accessibility_text_input(monkeypatch):
    captured = {}

    def fake_send(text, submit=True):
        captured["text"] = text
        captured["submit"] = submit
        return {"ok": True, "submitted": submit, "chars": len(text), "method": "ax_text_input"}

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.send_via_ax_text_input", fake_send)

    data = reply.send_text("hello", dry_run=False, submit=True)

    assert data["ok"] is True
    assert data["method"] == "ax_text_input"
    assert data["dry_run"] is False
    assert captured == {"text": "hello", "submit": True}

def test_reply_send_falls_back_to_clipboard_when_ax_fails(monkeypatch):
    calls = []

    def fake_send(text, submit=True):
        calls.append(("ax", text, submit))
        raise RuntimeError("AX text input send failed: no result")

    def fake_paste(text, submit=True):
        calls.append(("clipboard", text, submit))
        return {"ok": True, "submitted": submit, "chars": len(text)}

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.send_via_ax_text_input", fake_send)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.paste_and_enter", fake_paste)

    data = reply.send_text("hello", dry_run=False, submit=True)

    assert data["ok"] is True
    assert data["method"] == "clipboard_fallback"
    assert data["fallback_reason"] == "AX text input send failed: no result"
    assert data["dry_run"] is False
    assert calls == [("ax", "hello", True), ("clipboard", "hello", True)]

def test_reply_send_message_supports_image_attachments(monkeypatch, tmp_path):
    image_path = tmp_path / "reply.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    calls = []

    def fake_stage(text, dry_run=False, submit=True, allow_clipboard_fallback=True):
        calls.append(("text", text, submit))
        return {"ok": True, "submitted": submit, "chars": len(text)}

    def fake_file(path, submit=True):
        calls.append(("file", str(path), submit))
        return {"ok": True, "submitted": submit, "path": str(path)}

    monkeypatch.setattr("cli_anything.wecom_gui.core.reply.send_text", fake_stage)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.paste_file_and_enter", fake_file)

    data = reply.send_message("hello", attachments=[{"type": "image", "path": str(image_path)}], submit=True)

    assert data["ok"] is True
    assert data["attachment_count"] == 1
    assert calls == [("text", "hello", False), ("file", str(image_path), True)]


def test_mixed_message_honors_disabled_clipboard_fallback(monkeypatch, tmp_path):
    from cli_anything.wecom_gui.utils import macos_backend
    def fail_stage(text, *, submit=True):
        assert submit is False
        raise macos_backend.TextSendError("chat_input_not_empty", submitted=False)
    monkeypatch.setattr(macos_backend, "send_via_ax_text_input", fail_stage)
    monkeypatch.setattr(macos_backend, "paste_and_enter", lambda *args, **kwargs: pytest.fail("must not paste text"))
    monkeypatch.setattr(macos_backend, "paste_file_and_enter", lambda *args, **kwargs: pytest.fail("must not paste media"))
    with pytest.raises(macos_backend.TextSendError) as raised:
        reply.send_message("hello", attachments=[{"type": "image", "path": str(tmp_path / "image.png")}],
                           allow_clipboard_fallback=False)
    assert raised.value.submitted is False


def test_cli_reply_send_dry_run_json():
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "reply", "send", "--text", "hello", "--dry-run"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["text"] == "hello"

def test_cli_loads_env_local_for_inbox_scan(monkeypatch, tmp_path):
    monkeypatch.delenv("WECOM_GUI_REQUIRED_TAGS", raising=False)
    # Track the initially absent key so dotenv cannot leak it into later tests.
    monkeypatch.setenv("WECOM_GUI_REQUIRED_TAG", "")
    monkeypatch.delenv("WECOM_GUI_REQUIRED_TAG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text("WECOM_GUI_REQUIRED_TAG='@重庆邮电大学'\n", encoding="utf-8")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.conversation_rows",
        lambda app_name=None, limit=30: [
            {"title": "客户A", "preview": "报名咨询", "time": "刚刚", "tags": ["@重庆邮电大学"], "raw": []},
            {"title": "客户B", "preview": "微信咨询", "time": "刚刚", "tags": ["@微信"], "raw": []},
        ],
    )

    result = CliRunner().invoke(cli, ["--json", "inbox", "scan", "--limit", "8"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [row["title"] for row in payload["conversations"]] == ["客户A"]
