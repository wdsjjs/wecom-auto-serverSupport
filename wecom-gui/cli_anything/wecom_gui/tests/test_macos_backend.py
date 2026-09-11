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


def test_read_current_filters_noise_and_hashes(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.chat_messages",
        lambda app_name=None, last=10, capture_images=False: [],
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.visible_text",
        lambda app_name=None: ["搜索", "客户A", "你好", "你好", "请问能退款吗"],
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.visible_accessibility_text",
        lambda app_name=None: [],
    )

    data = chat.read_current(last=10)

    assert data["ok"] is True
    assert data["message_count"] == 3
    assert [m["text"] for m in data["messages"]] == ["客户A", "你好", "请问能退款吗"]
    assert len(data["hash"]) == 64

def test_read_current_prefers_chat_table(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.chat_messages",
        lambda app_name=None, last=10, capture_images=False: [
            {"role": "unknown", "text": "客户问", "time": "", "x": 100, "right": 180, "source": "accessibility-chat-table"},
            {"role": "unknown", "text": "客服答", "time": "10:00", "x": 120, "right": 300, "source": "accessibility-chat-table"},
        ],
    )

    data = chat.read_current(last=10)

    assert data["source"] == "accessibility-chat-table"
    assert [m["text"] for m in data["messages"]] == ["客户问", "客服答"]
    assert [m["role"] for m in data["messages"]] == ["用户", "客服"]
    assert [m["content"] for m in data["messages"]] == ["客户问", "客服答"]

def test_read_current_passes_capture_images_flag(monkeypatch):
    captured = {}

    def fake_chat_messages(app_name=None, last=10, capture_images=False, include_image_media=None):
        captured["capture_images"] = capture_images
        captured["include_image_media"] = include_image_media
        return [{"role": "unknown", "text": "客户发图", "time": "", "x": 100, "right": 180}]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.chat_messages", fake_chat_messages)

    data = chat.read_current(last=10, capture_images=False)

    assert captured["capture_images"] is False
    assert captured["include_image_media"] is True
    assert data["capture_images"] is False

def test_read_current_captures_only_latest_user_turn_images(monkeypatch):
    captured_batches = []

    def fake_chat_messages(app_name=None, last=10, capture_images=False, include_image_media=None):
        assert capture_images is False
        assert include_image_media is True
        return [
            {"role": "unknown", "text": "[图片]", "x": 337, "right": 417, "media": [{"rect": {"x": 1}}]},
            {"role": "unknown", "text": "旧问题", "x": 337, "right": 450},
            {"role": "unknown", "text": "旧回复", "x": 574, "right": 1444},
            {"role": "unknown", "text": "[图片]", "x": 337, "right": 417, "media": [{"rect": {"x": 2}}]},
            {"role": "unknown", "text": "最新问题", "x": 337, "right": 450},
        ]

    def fake_capture(messages):
        captured_batches.append(messages)
        return messages

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.chat_messages", fake_chat_messages)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fake_capture)

    data = chat.read_current(last=10, capture_images=True)

    assert len(captured_batches) == 1
    captured = captured_batches[0]
    assert captured[0].get("media") is None
    assert captured[3].get("media") == [{"rect": {"x": 2}}]
    assert [message["text"] for message in data["messages"]][-2:] == ["[图片]", "最新问题"]

def test_read_current_preserves_uncaptured_latest_turn_image_media(monkeypatch):
    captured_batches = []

    def fake_chat_messages(app_name=None, last=10, capture_images=False, include_image_media=None):
        assert capture_images is False
        assert include_image_media is True
        return [
            {"role": "unknown", "text": "旧回复", "x": 574, "right": 1444},
            {"role": "unknown", "text": "[图片]", "x": 337, "right": 417, "media": [{"rect": {"x": 2}}]},
            {"role": "unknown", "text": "最新问题", "x": 337, "right": 450},
        ]

    def fake_capture(messages):
        captured_batches.append(messages)
        return [
            {
                **message,
                "media": [
                    {**media, "capture_ok": False, "error": "preview_not_found"}
                    for media in message.get("media", [])
                ],
            }
            for message in messages
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.chat_messages", fake_chat_messages)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fake_capture)

    data = chat.read_current(last=10, capture_images=True)

    assert len(captured_batches) == 1
    assert captured_batches[0][1]["media"] == [{"rect": {"x": 2}}]
    assert data["messages"][1]["media"][0]["capture_ok"] is False


def test_read_current_does_not_capture_service_history_media(monkeypatch):
    captured_batches = []

    def fake_chat_messages(app_name=None, last=10, capture_images=False, include_image_media=None):
        assert capture_images is False
        assert include_image_media is True
        return [
            {
                "role": "unknown",
                "text": "[图片]",
                "x": 574,
                "right": 1444,
                "media": [{"type": "image", "rect": {"x": 9}}],
            },
            {"role": "unknown", "text": "最新客户消息", "x": 337, "right": 450},
        ]

    def fake_capture(messages):
        captured_batches.append(messages)
        return messages

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.chat_messages", fake_chat_messages)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fake_capture)

    data = chat.read_current(last=10, capture_images=True)

    assert len(captured_batches) == 1
    assert captured_batches[0][0].get("media") is None
    assert data["messages"][0]["role"] == "客服"
    assert data["messages"][1]["role"] == "用户"


def test_read_current_skips_animated_sticker_capture_from_preview(monkeypatch):
    def fake_chat_messages(app_name=None, last=10, capture_images=False, include_image_media=None):
        assert capture_images is False
        assert include_image_media is True
        return [
            {
                "role": "unknown",
                "text": "[图片]",
                "x": 337,
                "right": 417,
                "media": [{"type": "image", "rect": {"x": 2}}],
            }
        ]

    def fail_capture(messages):
        raise AssertionError("animated stickers should be marked before capture")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.chat_messages", fake_chat_messages)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fail_capture)

    data = chat.read_current(last=10, capture_images=False, media_preview="[动画表情]")

    assert data["messages"][0]["text"] == "[动画表情]"
    assert data["messages"][0]["media"][0]["type"] == "animated_sticker"
    assert data["messages"][0]["media"][0]["skip_capture"] is True


def test_ax_chat_messages_marks_animated_sticker_media(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        return [
            {
                "index": 2,
                "texts": [],
                "x": 311,
                "width": 1159,
                "mediaElements": [
                    {
                        "mediaType": "animated_sticker",
                        "skipCapture": True,
                        "texts": ["动画表情"],
                        "x": 420,
                        "y": 300,
                        "width": 118,
                        "height": 118,
                    }
                ],
            },
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert messages[0]["text"] == "[动画表情]"
    assert messages[0]["media"][0]["type"] == "animated_sticker"
    assert messages[0]["media"][0]["skip_capture"] is True


def test_ax_chat_messages_marks_mini_program_card_media(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        return [
            {
                "index": 20,
                "texts": [
                    "21:19",
                    "UndoAge 营养工厂",
                    "营养工厂幸运大抽奖｜免单、NMN、鱼油…",
                    "WXMsg WeAppLogo",
                    "小程序",
                ],
                "x": 314,
                "y": 208,
                "width": 649,
                "height": 361,
                "bubbleX": 343,
                "bubbleY": 291,
                "bubbleWidth": 181,
                "bubbleHeight": 38,
                "bubbleTexts": ["营养工厂幸运大抽奖｜免单、NMN、鱼油…"],
            },
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert messages[0]["text"] == "UndoAge 营养工厂 营养工厂幸运大抽奖｜免单、NMN、鱼油… WXMsg WeAppLogo 小程序"
    assert messages[0]["media"][0]["type"] == "mini_program"
    assert messages[0]["media"][0]["source"] == "axuielement-chat-mini-program-card"
    assert messages[0]["media"][0]["rect"]["width"] >= 260
    assert messages[0]["media"][0]["rect"]["height"] >= 220


def test_capture_chat_images_screenshots_mini_program_card(monkeypatch, tmp_path):
    captured = []

    def fake_screenshot(rect, output_path):
        captured.append((rect, output_path))
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nmini")
        return {"ok": True, "path": str(output_path), "rect": rect}

    def fail_preview(_rect):
        raise AssertionError("mini-program cards should be captured by rect screenshot")

    monkeypatch.setenv("WECOM_GUI_CAPTURE_IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._screenshot_rect", fake_screenshot)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._click_image_and_capture", fail_preview)

    messages = macos_backend.capture_chat_images(
        [
            {
                "role": "用户",
                "text": "小程序",
                "media": [
                    {
                        "type": "mini_program",
                        "rect": {"x": 320, "y": 250, "width": 300, "height": 390},
                    }
                ],
            }
        ]
    )

    media = messages[0]["media"][0]
    assert captured
    assert media["capture_ok"] is True
    assert media["capture_mode"] == "mini_program_card"
    assert media["capture_path"].endswith(".png")


def test_hidden_image_row_rect_keeps_short_preview_bubbles(monkeypatch):
    monkeypatch.setenv("WECOM_GUI_MIN_IMAGE_BUBBLE_SIZE", "64")

    rect = macos_backend._chat_image_row_rect({"x": 311, "y": 350, "width": 1159, "height": 258}, anchor_x=337)

    assert rect["width"] >= 64
    assert rect["height"] >= 64
    assert macos_backend._is_chat_image_rect(rect) is True

def test_ax_chat_messages_filters_sidebar_rows(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        return [
            {"index": 1, "texts": ["客户A", "侧栏预览", "@微信"], "x": 60, "width": 250},
            {"index": 2, "texts": ["真正聊天消息"], "x": 311, "width": 1159},
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert [message["text"] for message in messages] == ["真正聊天消息"]


def test_ax_conversation_rows_keeps_preview_after_wechat_tag(monkeypatch):
    def fake_swift(command):
        if command == "rows":
            return [
                {
                    "index": 1,
                    "texts": ["三水儿", "@微信", "你已添加了三水儿，现在可以开始聊天了。", "3分钟前"],
                    "x": 60,
                    "y": 40,
                    "width": 250,
                    "height": 90,
                    "selected": True,
                }
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    rows = macos_backend._ax_conversation_rows(limit=10)

    assert rows[0]["title"] == "三水儿"
    assert rows[0]["tags"] == ["@微信"]
    assert rows[0]["preview"] == "你已添加了三水儿，现在可以开始聊天了。"
    assert rows[0]["time"] == "3分钟前"

def test_time_text_age_minutes_parses_recent_values():
    fixed_now = macos_backend.datetime(2026, 6, 4, 14, 40)

    assert macos_backend._time_text_age_minutes("刚刚", now=fixed_now) == 0
    assert macos_backend._time_text_age_minutes("9分钟前", now=fixed_now) == 9
    assert macos_backend._time_text_age_minutes("14:33", now=fixed_now) == 7
    assert macos_backend._time_text_age_minutes("昨天", now=fixed_now) == 24 * 60
    assert macos_backend._time_text_age_minutes("星期二", now=fixed_now) == 24 * 60

def test_bounded_conversation_rows_stops_after_old_time(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "ensure-single-chat":
            return [{"ok": True}]
        if isinstance(command, list) and command[0] == "recent-rows":
            return [
                {
                    "texts": ["客户A", "你好", "9分钟前"],
                    "timeText": "9分钟前",
                    "x": 100,
                    "y": 100,
                    "width": 300,
                    "height": 64,
                },
                {
                    "texts": ["客户B", "旧消息", "昨天"],
                    "timeText": "昨天",
                    "x": 100,
                    "y": 164,
                    "width": 300,
                    "height": 64,
                },
                {
                    "texts": ["客户C", "不应继续读", "刚刚"],
                    "timeText": "刚刚",
                    "x": 100,
                    "y": 228,
                    "width": 300,
                    "height": 64,
                },
            ]
        return []

    monkeypatch.setenv("WECOM_GUI_RECENT_SCAN_MINUTES", "10")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.datetime", mock.Mock(now=lambda: macos_backend.datetime(2026, 6, 4, 14, 40)))

    rows = macos_backend._bounded_conversation_rows(limit=10)

    assert [row["title"] for row in rows] == ["客户A"]
    assert rows[0]["source"] == "axuielement-bounded"
    assert commands[0] == "ensure-single-chat"

def test_row_from_ax_item_does_not_double_count_unread_marker():
    row = macos_backend._row_from_ax_item(
        {
            "texts": ["客户A", "1", "刚刚", "@微信"],
            "timeText": "刚刚",
            "hasUnreadMarker": True,
            "x": 100,
            "y": 100,
            "width": 300,
            "height": 64,
        },
        index=1,
        source="axuielement-bounded",
    )

    assert row["unread_count"] == 1
    assert row["unread"] is True

def test_swift_ax_sdkroot_ignores_incompatible_default_sdk(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_AX_SDKROOT", raising=False)
    monkeypatch.setenv("SDKROOT", "/Library/Developer/CommandLineTools/SDKs/MacOSX26.2.sdk")

    assert not macos_backend._swift_ax_sdkroot().endswith("MacOSX26.2.sdk")

def test_conversation_rows_falls_back_when_bounded_scan_empty(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "ensure-single-chat":
            return [{"ok": False, "error": "single_chat_row_not_found"}]
        if command == "rows":
            return [
                {
                    "texts": ["客户A", "你好", "刚刚", "@微信"],
                    "x": 60,
                    "y": 40,
                    "width": 250,
                    "height": 64,
                }
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    rows = macos_backend.conversation_rows(limit=10)

    assert rows[0]["title"] == "客户A"
    assert rows[0]["source"] == "axuielement"
    assert "rows" in commands

def test_sidebar_identity_returns_swift_uid(monkeypatch):
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend._swift_ax",
        lambda command: [{"ok": True, "external_user_id": "wm_123456789"}] if command == "sidebar-identity" else [],
    )

    assert macos_backend.sidebar_identity()["external_user_id"] == "wm_123456789"

def test_ensure_input_ready_returns_swift_payload(monkeypatch):
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend._swift_ax",
        lambda command: [{"ok": True, "input": {"x": 1}, "sidebar": {"ok": True}}] if command == "input-ready" else [],
    )

    assert macos_backend.ensure_input_ready()["input"]["x"] == 1


def test_selected_conversation_row_prefers_swift_selected_row(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "selected-row":
            return [
                {
                    "texts": ["客户A", "刚刚", "查订单", "@微信"],
                    "timeText": "刚刚",
                    "x": 1600,
                    "y": 200,
                    "width": 420,
                    "height": 64,
                    "selected": True,
                }
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    row = macos_backend.selected_conversation_row(limit=8)

    assert row["title"] == "客户A"
    assert row["selected"] is True
    assert row["source"] == "axuielement-selected"
    assert commands == ["rows", "selected-row"]


def test_selected_conversation_row_uses_selected_flag_from_row_scan_before_fallback(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "rows":
            return [{
                "texts": ["客户A", "刚刚", "查订单", "@微信"],
                "x": 60,
                "y": 100,
                "width": 250,
                "height": 64,
                "selected": True,
            }]
        raise AssertionError("selected-row should not run when the row scan is selected")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    row = macos_backend.selected_conversation_row(limit=8)

    assert row["title"] == "客户A"
    assert row["selected"] is True
    assert commands == ["rows"]


def test_ax_chat_messages_accepts_chat_pane_on_sidebar_boundary(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 59, "width": 252}}]
        return [
            {"index": 1, "texts": ["LeoFree", "这是你们的支付宝吗？", "8分钟前", "@微信"], "x": 60, "width": 250},
            {"index": 2, "texts": ["这是你们的支付宝吗？"], "x": 311, "width": 1159},
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert [message["text"] for message in messages] == ["这是你们的支付宝吗？"]


def test_ax_chat_messages_filters_conversation_list_when_chat_left_present(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [
                {
                    "ok": True,
                    "sidebar": {"x": 1470, "width": 160},
                    "conversationList": {"x": 1630, "width": 455},
                    "chatLeft": 2085,
                    "rightSidebarLeft": 3500,
                }
            ]
        if command == ["chat", "10"]:
            return [
                {
                    "index": 1,
                    "texts": ["iChen", "老师，这里出bug", "昨天", "@微信"],
                    "x": 1630,
                    "width": 455,
                },
                {
                    "index": 2,
                    "texts": ["嗯"],
                    "x": 2088,
                    "width": 900,
                    "bubbleX": 2100,
                    "bubbleWidth": 80,
                },
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert [message["text"] for message in messages] == ["嗯"]
    assert messages[0]["x"] == 2100


def test_ax_chat_messages_falls_back_to_sidebar_boundary_when_chat_left_missing(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == ["chat", "10"]:
            return [
                {"index": 1, "texts": ["客户A", "预览", "@微信"], "x": 60, "width": 250},
                {"index": 2, "texts": ["真正消息"], "x": 311, "width": 1159},
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert [message["text"] for message in messages] == ["真正消息"]


def test_send_via_ax_text_input_preflights_send_ready(monkeypatch):
    commands = []
    events = []

    def fake_swift(command):
        commands.append(command)
        if command == "send-ready":
            return [{"ok": True, "input": {"x": 10, "valueLength": 0}}]
        if command == ["send", "hello"]:
            return [{"ok": True, "submitted": True, "chars": 5}]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._append_event", lambda event: events.append(event))

    result = macos_backend.send_via_ax_text_input("hello", submit=True)

    assert result["ok"] is True
    assert commands == ["send-ready", ["send", "hello"]]
    assert events[0]["type"] == "wecom_send_input_preflight"

def test_ax_chat_messages_returns_image_placeholder(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        return [
            {
                "index": 2,
                "texts": [],
                "x": 311,
                "width": 1159,
                "mediaElements": [{"x": 420, "y": 300, "width": 180, "height": 160}],
            },
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert messages[0]["text"] == "[图片]"
    assert messages[0]["media"][0]["rect"] == {"x": 420, "y": 300, "width": 180, "height": 160}

def test_ax_chat_messages_can_include_hidden_image_rows(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == ["chat", "10"]:
            return [
                {"index": 34, "texts": ["客服答"], "x": 431, "width": 1013, "bubbleX": 431, "bubbleWidth": 1013},
                {"index": 36, "texts": ["这个牛奶是你们产品吗"], "x": 311, "width": 1159, "bubbleX": 337, "bubbleWidth": 143},
            ]
        if command == "chat-all":
            return [
                {"index": 34, "texts": ["客服答"], "x": 431, "width": 1013},
                {"index": 35, "texts": [], "x": 311, "y": 285, "width": 1159, "height": 336},
                {"index": 36, "texts": ["这个牛奶是你们产品吗"], "x": 311, "width": 1159, "bubbleX": 337, "bubbleWidth": 143},
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10, include_hidden_images=True)

    hidden = [message for message in messages if message["source"] == "axuielement-chat-hidden-image-row"][0]
    assert hidden["text"] == "[图片]"
    assert hidden["row"] == 35
    assert hidden["media"][0]["rect"] == {"x": 337, "y": 413, "width": 80, "height": 80}
    assert "chat-all" in commands

def test_ax_chat_messages_enriches_image_placeholder_rows(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == ["chat", "10"]:
            return [
                {"index": 35, "texts": ["[图片]"], "x": 311, "y": 285, "width": 1159, "height": 336},
                {"index": 36, "texts": ["这是哪里？"], "x": 311, "width": 1159, "bubbleX": 337, "bubbleWidth": 120},
            ]
        if command == "chat-all":
            return [
                {"index": 35, "texts": ["[图片]"], "x": 311, "y": 285, "width": 1159, "height": 336},
                {"index": 36, "texts": ["这是哪里？"], "x": 311, "width": 1159, "bubbleX": 337, "bubbleWidth": 120},
            ]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10, include_hidden_images=True)

    image = messages[0]
    assert image["text"] == "[图片]"
    assert image["source"] == "axuielement-chat-hidden-image-row"
    assert image["media"][0]["rect"] == {"x": 337, "y": 413, "width": 80, "height": 80}
    assert image["x"] == 337
    assert image["right"] == 417
    assert [message["text"] for message in messages] == ["[图片]", "这是哪里？"]
    assert "chat-all" in commands


@pytest.mark.parametrize("image_y", [-600, 1200])
def test_offscreen_image_rows_keep_their_place_in_the_snapshot(monkeypatch, image_y):
    viewport = {"x": 311, "y": 100, "width": 700, "height": 600}
    rows = [
        {"index": 1, "texts": ["before"], "messageTexts": ["before"],
         "bubbleImageSupported": False, "x": 311, "width": 700, "height": 84},
        {"index": 2, "texts": [], "messageTexts": [], "bubbleImageSupported": True,
         "x": 311, "y": image_y, "width": 700, "height": 290,
         "directionEvidence": {"source": "screencapturekit", "side": "unknown",
                               "status": "outside_viewport_or_unlaid_out"}},
        {"index": 3, "texts": ["after"], "messageTexts": ["after"],
         "bubbleImageSupported": False, "x": 311, "width": 700, "height": 84},
    ]
    for row in rows:
        row.update(snapshotComplete=True, chatViewport=viewport)
    monkeypatch.setattr(macos_backend, "_swift_ax", lambda command: rows)

    messages = macos_backend._ax_chat_messages(last=20, include_hidden_images=True)

    assert [msg["row"] for msg in messages] == [1, 2, 3]
    assert [msg["text"] for msg in messages] == ["before", "[图片]", "after"]
    assert messages[1]["media"][0]["type"] == "image"
    assert messages[1]["direction_evidence"]["side"] == "unknown"
    assert not messages[1]["media"][0].get("capture_path")


def test_ax_chat_messages_does_not_read_hidden_rows_unless_requested(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == ["chat", "10"]:
            return [{"index": 2, "texts": ["真正聊天消息"], "x": 311, "width": 1159}]
        if command == "chat-all":
            raise AssertionError("chat-all should only run for formal image capture")
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10, include_hidden_images=False)

    assert [message["text"] for message in messages] == ["真正聊天消息"]
    assert "chat-all" not in commands

def test_ax_chat_messages_ignores_small_media_icons(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        return [
            {
                "index": 2,
                "texts": ["客户消息"],
                "x": 311,
                "width": 1159,
                "mediaElements": [{"x": 420, "y": 300, "width": 32, "height": 32}],
            },
        ]

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert messages == [
        {
            "row": 2,
            "capture_row_id": "",
            "role": "unknown",
            "text": "客户消息",
            "time": "",
            "x": 311,
            "width": 1159,
            "right": 1470,
            "source": "axuielement-chat-table",
            "identity_text": "客户消息",
            "identity_time": "",
            "direction_evidence": {"source": "screencapturekit", "status": "unavailable", "side": "unknown"},
        }
    ]

def test_ax_chat_messages_dedupes_duplicate_text_nodes_in_same_row(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == ["chat", "10"]:
            return [
                {
                    "index": 2,
                    "texts": ["这个是什么东西？", "这个是什么东西？"],
                    "x": 311,
                    "width": 1159,
                    "bubbleX": 337,
                    "bubbleWidth": 105,
                },
            ]
        if command == "chat-all":
            return []
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)

    messages = macos_backend._ax_chat_messages(last=10)

    assert messages[0]["text"] == "这个是什么东西？"

def test_capture_images_false_does_not_capture(monkeypatch):
    calls = {"capture": 0}
    hidden_flags = []

    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend._ax_chat_messages",
        lambda last, include_hidden_images=False, include_hidden_image_media=True: hidden_flags.append(
            (include_hidden_images, include_hidden_image_media)
        )
        or [
            {
                "role": "unknown",
                "text": "[图片]",
                "x": 420,
                "right": 600,
            }
        ],
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.activate_app", lambda chosen: None)

    def fail_capture(messages):
        calls["capture"] += 1
        raise AssertionError("capture should not run")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fail_capture)

    messages = macos_backend.chat_messages(last=10, capture_images=False)

    assert messages[0]["text"] == "[图片]"
    assert calls["capture"] == 0
    assert messages[0].get("media") is None
    assert hidden_flags == [(True, False)]

def test_capture_images_true_reads_hidden_rows_and_captures(monkeypatch):
    hidden_flags = []
    calls = {"capture": 0}

    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend._ax_chat_messages",
        lambda last, include_hidden_images=False, include_hidden_image_media=True: hidden_flags.append(
            (include_hidden_images, include_hidden_image_media)
        )
        or [{"role": "unknown", "text": "[图片]", "media": []}],
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.activate_app", lambda chosen: None)

    def fake_capture(messages):
        calls["capture"] += 1
        return messages

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fake_capture)

    messages = macos_backend.chat_messages(last=10, capture_images=True)

    assert messages[0]["text"] == "[图片]"
    assert calls["capture"] == 1
    assert hidden_flags == [(True, True)]


def test_capture_chat_images_defaults_to_preview_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("WECOM_GUI_IMAGE_CAPTURE_DIR", str(tmp_path))
    commands = []

    def fake_swift(command):
        commands.append(command)
        if isinstance(command, list) and command[0] == "doubleclick":
            return [{"ok": True}]
        if command == "preview":
            return [{"ok": True, "image": {"x": 100, "y": 120, "width": 300, "height": 200}}]
        if command == "close-preview":
            return [{"ok": True}]
        return []

    def fake_screenshot(rect, output_path):
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nfake image bytes")
        return {"ok": True, "path": str(output_path), "rect": rect}

    monkeypatch.delenv("WECOM_GUI_MEDIA_CAPTURE_MODE", raising=False)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._screenshot_rect", fake_screenshot)

    messages = macos_backend.capture_chat_images(
        [{"role": "用户", "text": "[图片]", "media": [{"type": "image", "rect": {"x": 1, "y": 2, "width": 80, "height": 80}}]}]
    )

    media = messages[0]["media"][0]
    assert commands[0][0] == "doubleclick"
    assert "preview" in commands
    assert media["capture_ok"] is True
    assert media["capture_mode"] == "preview"
    assert media["capture_path"]


def test_capture_chat_images_skips_animated_stickers(monkeypatch):
    def fail_swift(command):
        raise AssertionError("animated sticker should not touch preview capture")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fail_swift)

    messages = macos_backend.capture_chat_images(
        [
            {
                "role": "用户",
                "text": "[动画表情]",
                "media": [{"type": "animated_sticker", "rect": {"x": 1}, "skip_capture": True}],
            }
        ]
    )

    media = messages[0]["media"][0]
    assert media["capture_ok"] is False
    assert media["capture_mode"] == "skipped"
    assert media["error"] == "media_capture_skipped"


def test_capture_chat_images_skips_sticker_hints(monkeypatch):
    def fail_swift(command):
        raise AssertionError("sticker hint should skip preview capture")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fail_swift)

    messages = macos_backend.capture_chat_images(
        [
            {
                "role": "用户",
                "text": "[图片]",
                "media": [{"type": "image", "texts": ["动画表情"], "rect": {"x": 1}}],
            }
        ]
    )

    media = messages[0]["media"][0]
    assert media["type"] == "animated_sticker"
    assert media["skip_capture"] is True
    assert media["capture_mode"] == "skipped"


def test_capture_chat_images_caches_previewless_media_as_skipped(monkeypatch):
    calls = []
    rect = {"x": 420, "y": 300, "width": 180, "height": 160}

    def fake_swift(command):
        calls.append(command)
        if isinstance(command, list) and command[0] == "doubleclick":
            return [{"ok": True}]
        if command == "preview":
            return [{"ok": False, "error": "preview_image_not_found"}]
        if command == "close-preview":
            return [{"ok": True}]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    macos_backend._MEDIA_SKIP_CACHE.clear()

    first = macos_backend.capture_chat_images(
        [{"role": "用户", "text": "[图片]", "media": [{"type": "image", "source": "axuielement-chat-media", "row": 3, "rect": rect}]}]
    )
    second = macos_backend.capture_chat_images(
        [{"role": "用户", "text": "[图片]", "media": [{"type": "image", "source": "axuielement-chat-media", "row": 3, "rect": rect}]}]
    )

    doubleclicks = [command for command in calls if isinstance(command, list) and command[0] == "doubleclick"]
    assert len(doubleclicks) == 1
    assert first[0]["media"][0]["error"] == "preview_image_not_found"
    assert second[0]["media"][0]["type"] == "animated_sticker"
    assert second[0]["media"][0]["capture_mode"] == "skipped"


def test_capture_chat_images_preview_mode_uses_doubleclick(monkeypatch, tmp_path):
    monkeypatch.setenv("WECOM_GUI_IMAGE_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("WECOM_GUI_MEDIA_CAPTURE_MODE", "preview")
    commands = []

    def fake_swift(command):
        commands.append(command)
        if isinstance(command, list) and command[0] == "doubleclick":
            return [{"ok": True}]
        if command == "preview":
            return [{"ok": True, "image": {"x": 100, "y": 120, "width": 300, "height": 200}}]
        if command == "close-preview":
            return [{"ok": True}]
        return []

    def fake_screenshot(rect, output_path):
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nfake image bytes")
        return {"ok": True, "path": str(output_path), "rect": rect}

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._screenshot_rect", fake_screenshot)

    messages = macos_backend.capture_chat_images(
        [{"role": "用户", "text": "[图片]", "media": [{"type": "image", "rect": {"x": 1, "y": 2, "width": 80, "height": 80}}]}]
    )

    assert commands[0][0] == "doubleclick"
    assert "preview" in commands
    assert messages[0]["media"][0]["capture_ok"] is True
    assert messages[0]["media"][0]["capture_mode"] == "preview"


def test_chat_messages_can_read_image_media_without_capture(monkeypatch):
    hidden_flags = []
    calls = {"capture": 0}

    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend._ax_chat_messages",
        lambda last, include_hidden_images=False, include_hidden_image_media=True: hidden_flags.append(
            (include_hidden_images, include_hidden_image_media)
        )
        or [{"role": "unknown", "text": "[图片]", "media": [{"type": "image", "rect": {"x": 1}}]}],
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.resolve_app_name", lambda app_name=None: "企业微信")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.activate_app", lambda chosen: None)

    def fail_capture(messages):
        calls["capture"] += 1
        raise AssertionError("capture should not run")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.capture_chat_images", fail_capture)

    messages = macos_backend.chat_messages(last=10, capture_images=False, include_image_media=True)

    assert messages[0]["media"] == [{"type": "image", "rect": {"x": 1}}]
    assert calls["capture"] == 0
    assert hidden_flags == [(True, True)]

def test_swift_ax_timeout_returns_error(monkeypatch):
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.shutil.which", lambda command: "/usr/bin/swift")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.Path.exists", lambda self: True)

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.subprocess.run", timeout_run)

    result = macos_backend._swift_ax("preview")

    assert result[0]["ok"] is False
    assert result[0]["error"] == "swift_ax_timeout"
    assert result[0]["command"] == "preview"

def test_swift_ax_scan_commands_keep_sidebar_timeout_short(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_AX_TIMEOUT", raising=False)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.shutil.which", lambda command: "/usr/bin/swift")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.Path.exists", lambda self: True)
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.subprocess.run", fake_run)

    macos_backend._swift_ax("rows")

    assert captured["timeout"] == 2.0

def test_swift_ax_chat_and_geometry_use_longer_timeouts(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_AX_TIMEOUT", raising=False)
    monkeypatch.delenv("WECOM_GUI_AX_CHAT_TIMEOUT", raising=False)
    monkeypatch.delenv("WECOM_GUI_AX_GEOMETRY_TIMEOUT", raising=False)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.shutil.which", lambda command: "/usr/bin/swift")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.Path.exists", lambda self: True)
    captured = {}

    def fake_run(args, **kwargs):
        captured[args[-1]] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.subprocess.run", fake_run)

    macos_backend._swift_ax("chat")
    macos_backend._swift_ax("chat-all")
    macos_backend._swift_ax("geometry")

    assert captured["chat"] == 8.0
    assert captured["chat-all"] == 8.0
    assert captured["geometry"] == 5.0

def test_swift_ax_scan_timeout_opens_circuit(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_AX_TIMEOUT", raising=False)
    monkeypatch.setenv("WECOM_GUI_AX_SCAN_TIMEOUT_COOLDOWN", "30")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.shutil.which", lambda command: "/usr/bin/swift")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.Path.exists", lambda self: True)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax_runner", lambda script_path: ["swift", str(script_path)])
    now = {"value": 100.0}
    calls = {"run": 0}
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.time.monotonic", lambda: now["value"])
    macos_backend._AX_SCAN_DISABLED_UNTIL = 0.0

    def timeout_run(*args, **kwargs):
        calls["run"] += 1
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.subprocess.run", timeout_run)

    first = macos_backend._swift_ax("rows")
    second = macos_backend._swift_ax("geometry")

    assert first[0]["error"] == "swift_ax_timeout"
    assert second[0]["error"] == "swift_ax_scan_circuit_open"
    assert calls["run"] == 1

def test_swift_ax_chat_timeouts_do_not_open_scan_circuit(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_AX_TIMEOUT", raising=False)
    monkeypatch.setenv("WECOM_GUI_AX_SCAN_TIMEOUT_COOLDOWN", "30")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.shutil.which", lambda command: "/usr/bin/swift")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.Path.exists", lambda self: True)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax_runner", lambda script_path: ["swift", str(script_path)])
    now = {"value": 100.0}
    calls = {"run": 0}
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.time.monotonic", lambda: now["value"])
    macos_backend._AX_SCAN_DISABLED_UNTIL = 0.0

    def fake_run(args, **kwargs):
        calls["run"] += 1
        if args[-1] in {"chat-all", "chat"}:
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.subprocess.run", fake_run)

    first = macos_backend._swift_ax("chat-all")
    second = macos_backend._swift_ax("chat")
    third = macos_backend._swift_ax("rows")

    assert first[0]["error"] == "swift_ax_timeout"
    assert second[0]["error"] == "swift_ax_timeout"
    assert third == []
    assert calls["run"] == 3

def test_swift_ax_uses_compiled_helper_when_available(monkeypatch, tmp_path):
    script = tmp_path / "ax_wecom.swift"
    script.write_text("print(\"ok\")", encoding="utf-8")
    binary = tmp_path.parent / ".codex-run" / "ax_wecom"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    newer_time = script.stat().st_mtime + 10
    os.utime(binary, (newer_time, newer_time))
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.shutil.which", lambda command: "/usr/bin/swiftc")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.Path.resolve",
        lambda self: tmp_path / "pkg" / "utils" / "macos_backend.py",
    )

    runner = macos_backend._swift_ax_runner(script)

    assert runner == [str(binary)]

def test_capture_chat_images_records_screenshot_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("WECOM_GUI_IMAGE_CAPTURE_DIR", str(tmp_path))

    def fake_swift(command):
        if isinstance(command, list) and command[0] == "doubleclick":
            return [{"ok": True}]
        if command == "preview":
            return [{"ok": True, "image": {"x": 100, "y": 120, "width": 300, "height": 200}}]
        if command == "close-preview":
            return [{"ok": True}]
        return []

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.subprocess.run", timeout_run)

    messages = macos_backend.capture_chat_images(
        [
            {
                "role": "用户",
                "text": "[图片]",
                "media": [{"type": "image", "rect": {"x": 420, "y": 300, "width": 180, "height": 160}}],
            }
        ]
    )

    media = messages[0]["media"][0]
    assert messages[0]["text"] == "[图片]"
    assert media["capture_ok"] is False
    assert media["error"] == "screencapture_timeout"

def test_capture_chat_images_closes_preview_when_preview_detection_fails(monkeypatch, tmp_path):
    events = []
    calls = []

    def fake_swift(command):
        calls.append(command)
        if isinstance(command, list) and command[0] == "doubleclick":
            return [{"ok": True}]
        if command == "preview":
            return [{"ok": False, "error": "preview_image_not_found", "windowCount": 4}]
        if command == "close-preview":
            return [{"ok": True, "method": "CGCloseButtonPoint"}]
        return []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._append_event", lambda event: events.append(event))

    messages = macos_backend.capture_chat_images(
        [
            {
                "role": "用户",
                "text": "[图片]",
                "media": [{"type": "image", "rect": {"x": 420, "y": 300, "width": 180, "height": 160}}],
            }
        ]
    )

    media = messages[0]["media"][0]
    assert media["capture_ok"] is False
    assert media["error"] == "preview_image_not_found"
    assert "close-preview" in calls
    assert events[-1]["type"] == "image_capture_failed"
    assert events[-1]["close_preview"]["ok"] is True


@pytest.mark.parametrize("fail_at", ["doubleclick", "preview", "screenshot"])
def test_preview_cleanup_runs_after_exceptions_or_expired_deadline(monkeypatch, fail_at):
    now = 100.0
    calls = []
    cleanup_timeouts = []
    monkeypatch.setattr(macos_backend.time, "monotonic", lambda: now)
    monkeypatch.setattr(macos_backend.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(macos_backend, "_append_event", lambda event: None)

    def fail():
        nonlocal now
        now += 15
        raise macos_backend.CaptureDeadlineExceeded("media_time_budget_exhausted")

    def native(command):
        name = command[0] if isinstance(command, list) else command
        calls.append(name)
        if name == fail_at:
            fail()
        if name == "close-preview":
            cleanup_timeouts.append(macos_backend._capture_timeout(8))
        return [{"ok": True, "image": {"x": 10, "y": 20, "width": 100, "height": 100}}]

    monkeypatch.setattr(macos_backend, "_swift_ax", native)
    monkeypatch.setattr(macos_backend, "_screenshot_rect", lambda *args: fail())
    with pytest.raises(macos_backend.CaptureDeadlineExceeded):
        with macos_backend.capture_deadline(now + 15):
            macos_backend._click_image_and_capture({"x": 10, "y": 20, "width": 100, "height": 100})
    assert calls[-1] == "close-preview"
    assert cleanup_timeouts == [2]
    assert macos_backend._capture_timeout(8) == 8


def test_native_applescript_and_screenshot_use_remaining_capture_deadline(monkeypatch, tmp_path):
    now = 100.0
    timeouts = []
    monkeypatch.setattr(macos_backend.time, "monotonic", lambda: now)
    monkeypatch.setattr(macos_backend, "_swift_ax_runner", lambda source: ["ax-helper"])
    monkeypatch.setattr(macos_backend, "_swift_ax_env", lambda: {})
    monkeypatch.setattr(macos_backend.shutil, "which", lambda name: "/usr/bin/swift")

    def run(args, **kwargs):
        nonlocal now
        timeouts.append(kwargs["timeout"])
        now += 0.6
        return subprocess.CompletedProcess(args, 0, stdout='{}', stderr='')

    monkeypatch.setattr(macos_backend.subprocess, "run", run)
    with macos_backend.capture_deadline(101.0):
        macos_backend._swift_ax(["chat", "20"])
        macos_backend.run_osascript("return 1")
        with pytest.raises(macos_backend.CaptureDeadlineExceeded):
            macos_backend._screenshot_rect({"x": 1, "y": 1, "width": 100, "height": 100}, tmp_path / "unused.png")
    assert timeouts == pytest.approx([1, 0.4])

def test_infer_roles_treats_none_role_as_unknown_user():
    messages = chat.infer_roles([{"role": None, "text": "老男复维多少钱"}])

    assert messages[0]["role"] == "用户"
    assert messages[0]["role_confidence"] == "low"
    assert messages[0]["content"] == "老男复维多少钱"

def test_infer_roles_keeps_wide_left_customer_bubble_as_user():
    messages = chat.infer_roles(
        [
            {
                "role": "unknown",
                "text": "客户发了一段很长很长的问题",
                "x": 311,
                "width": 760,
                "right": 1071,
            },
            {
                "role": "unknown",
                "text": "客服回复",
                "x": 820,
                "width": 260,
                "right": 1080,
            },
        ]
    )

    assert [message["role"] for message in messages] == ["用户", "客服"]
    assert messages[0]["role_confidence"] == "medium"

def test_infer_roles_does_not_trust_zero_width_text_nodes():
    messages = chat.infer_roles(
        [
            {"role": "unknown", "text": "客户问", "x": 311, "width": 0, "right": 311},
            {"role": "unknown", "text": "客服答", "x": 820, "width": 0, "right": 820},
        ]
    )

    assert all(message["role_confidence"] == "low" for message in messages)

def test_activate_app_refuses_to_launch_when_wecom_not_running(monkeypatch):
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.find_running_app", lambda: None)

    try:
        macos_backend.activate_app()
    except RuntimeError as exc:
        assert "refusing to launch" in str(exc)
    else:
        raise AssertionError("activate_app should fail when WeCom is not running")

def test_extract_wecom_external_user_id_from_sidebar_text():
    wm_uid = macos_backend.extract_wecom_external_user_id(
        [
            "HTML内容 Description: 企微侧边栏（UndoAge）",
            "**测试页面\n\nwmapJOBwAAhEK25tzmHoIS9cIKKdIBMw\n**",
        ]
    )
    wo_uid = macos_backend.extract_wecom_external_user_id(["woapJOBwAAHkJVlpqbTCpkWpgZ1-_1pg"])

    assert wm_uid == "wmapJOBwAAhEK25tzmHoIS9cIKKdIBMw"
    assert wo_uid == "woapJOBwAAHkJVlpqbTCpkWpgZ1-_1pg"

def test_ax_text_input_does_not_resend_after_empty_swift_result(monkeypatch):
    calls = []
    results = iter(
        [
            [{"ok": True, "input": {"x": 1, "valueLength": 0}}],
            [],
            [{"ok": True, "submitted": True, "chars": 5, "method": "ax_text_input"}],
        ]
    )

    def fake_swift_ax(command):
        calls.append(command)
        return next(results)

    monkeypatch.setenv("WECOM_GUI_AX_SEND_ATTEMPTS", "2")
    monkeypatch.setenv("WECOM_GUI_AX_SEND_RETRY_DELAY", "0")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift_ax)

    with pytest.raises(macos_backend.TextSendError, match="ax_send_result_missing") as raised:
        macos_backend.send_via_ax_text_input("hello", submit=True)
    assert raised.value.submitted is None
    assert calls == ["send-ready", ["send", "hello"]]


def test_ax_text_input_preserves_specific_error_without_logging_body(monkeypatch):
    events = []
    results = iter([
        [{"ok": True, "input": {"valueLength": 0}}],
        [{"ok": False, "submitted": False, "method": "targeted_return",
          "submit": {"submitted": False, "reason": "chat_input_focus_not_confirmed"}}],
    ])
    monkeypatch.setattr(macos_backend, "_swift_ax", lambda _command: next(results))
    monkeypatch.setattr(macos_backend, "_append_event", events.append)
    with pytest.raises(macos_backend.TextSendError) as raised:
        macos_backend.send_via_ax_text_input("private customer reply")
    assert raised.value.reason_code == "chat_input_focus_not_confirmed"
    assert raised.value.submitted is False
    assert events[-1]["error"] == "chat_input_focus_not_confirmed"
    assert "private customer reply" not in str(events)


def test_text_preflight_failure_never_calls_native_send(monkeypatch):
    calls = []
    def read(command):
        calls.append(command)
        return [{"ok": False, "error": "chat_input_value_unavailable"}]
    monkeypatch.setattr(macos_backend, "_swift_ax", read)
    with pytest.raises(macos_backend.TextSendError) as raised:
        macos_backend.send_via_ax_text_input("hello")
    assert raised.value.submitted is False
    assert calls == ["send-ready"]


def test_native_send_failure_without_submission_evidence_remains_uncertain(monkeypatch):
    results = iter([[{"ok": True, "input": {"valueLength": 0}}], [{"ok": False, "error": "unexpected"}]])
    monkeypatch.setattr(macos_backend, "_swift_ax", lambda command: next(results))
    with pytest.raises(macos_backend.TextSendError) as raised:
        macos_backend.send_via_ax_text_input("hello")
    assert raised.value.submitted is None


def test_scroll_sidebar_uses_adaptive_geometry(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.window_geometry",
        lambda: {"ok": True, "scrollPoint": {"x": 321.5, "y": 654.2}},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend._swift_ax",
        lambda args: calls.append(args) or [{"ok": True}],
    )

    result = macos_backend.scroll_sidebar("down", ticks=4)

    assert result["ok"] is True
    assert calls == [["scroll", "down", "4", "321", "654"]]
