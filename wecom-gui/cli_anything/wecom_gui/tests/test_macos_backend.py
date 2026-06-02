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
        if command == "chat":
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
        if command == "chat":
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

def test_ax_chat_messages_does_not_read_hidden_rows_unless_requested(monkeypatch):
    commands = []

    def fake_swift(command):
        commands.append(command)
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == "chat":
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
            "role": "unknown",
            "text": "客户消息",
            "time": "",
            "x": 311,
            "width": 1159,
            "right": 1470,
            "source": "axuielement-chat-table",
        }
    ]

def test_ax_chat_messages_dedupes_duplicate_text_nodes_in_same_row(monkeypatch):
    def fake_swift(command):
        if command == "geometry":
            return [{"ok": True, "sidebar": {"x": 60, "width": 250}}]
        if command == "chat":
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

def test_infer_roles_uses_left_edge_when_right_edge_is_broken():
    messages = chat.infer_roles(
        [
            {"role": "unknown", "text": "客户问", "x": 311, "width": 0, "right": 311},
            {"role": "unknown", "text": "客服答", "x": 820, "width": 0, "right": 820},
        ]
    )

    assert [message["role"] for message in messages] == ["用户", "客服"]

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

def test_ax_text_input_retries_empty_swift_result(monkeypatch):
    calls = []
    results = iter([[], [{"ok": True, "submitted": True, "chars": 5, "method": "ax_text_input"}]])

    def fake_swift_ax(command):
        calls.append(command)
        return next(results)

    monkeypatch.setenv("WECOM_GUI_AX_SEND_ATTEMPTS", "2")
    monkeypatch.setenv("WECOM_GUI_AX_SEND_RETRY_DELAY", "0")
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend._swift_ax", fake_swift_ax)

    data = macos_backend.send_via_ax_text_input("hello", submit=True)

    assert data["ok"] is True
    assert data["method"] == "ax_text_input"
    assert calls == [["send", "hello"], ["send", "hello"]]

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
