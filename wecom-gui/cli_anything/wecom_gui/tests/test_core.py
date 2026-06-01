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


@pytest.fixture(autouse=True)
def _disable_message_debounce_by_default(monkeypatch):
    monkeypatch.setenv("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "0")


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


def test_messages_contain_text_accepts_wecom_whitespace_variants():
    reply_text = "您好，我是营养工厂客服助手。\n\n请问您想了解哪款产品呢？"
    visible_messages = [
        {"role": "客服", "content": "您好，我是营养工厂客服助手。 请问您想了解哪款产品呢？"}
    ]

    assert worker._messages_contain_text(visible_messages, reply_text) is True


def test_llm_draft_fails_without_uda_api_key(monkeypatch):
    monkeypatch.delenv("WECOM_GUI_UDA_API_KEY", raising=False)

    try:
        llm.draft_reply([{"role": "customer", "text": "你好"}], fallback="收到", provider="uda")
    except RuntimeError as exc:
        assert "WECOM_GUI_UDA_API_KEY" in str(exc)
    else:
        raise AssertionError("expected missing UDA API key to fail")


def test_llm_explicit_fallback_provider_still_returns_fallback():
    data = llm.draft_reply([{"role": "customer", "text": "你好"}], fallback="收到", provider="fallback")

    assert data == {"ok": True, "provider": "fallback", "text": "收到", "message": "收到"}


def test_uda_history_formats_recent_context_by_default():
    history = llm.build_uda_history(
        [
            {"role": "用户", "content": "鱼油含量"},
            {"role": "客服", "content": "请问是哪款鱼油？"},
            {"role": "用户", "content": "你好"},
        ]
    )

    assert history == [
        {"type": "human", "data": {"content": "用户: 鱼油含量"}},
        {"type": "human", "data": {"content": "客服: 请问是哪款鱼油？"}},
        {"type": "human", "data": {"content": "用户: 你好"}},
    ]


def test_uda_history_latest_user_mode():
    history = llm.build_uda_history(
        [
            {"role": "用户", "content": "鱼油含量"},
            {"role": "客服", "content": "请问是哪款鱼油？"},
            {"role": "用户", "content": "你好"},
        ],
        mode="latest_user",
    )

    assert history == [{"type": "human", "data": {"content": "用户: 你好"}}]


def test_uda_history_recent_context_stops_at_latest_user():
    history = llm.build_uda_history(
        [
            {"role": "用户", "content": "旧问题"},
            {"role": "客服", "content": "旧回复"},
            {"role": "用户", "content": "新问题"},
            {"role": "客服", "content": "这条不应发送"},
        ],
        mode="recent",
        max_messages=2,
    )

    assert history == [
        {"type": "human", "data": {"content": "客服: 旧回复"}},
        {"type": "human", "data": {"content": "用户: 新问题"}},
    ]


def test_uda_history_can_send_full_context():
    history = llm.build_uda_history(
        [
            {"role": "用户", "content": "鱼油含量"},
            {"role": "客服", "content": "请问是哪款鱼油？"},
        ],
        mode="full",
    )

    assert history == [
        {"type": "human", "data": {"content": "用户: 鱼油含量"}},
        {"type": "human", "data": {"content": "客服: 请问是哪款鱼油？"}},
    ]


def test_latest_user_turn_text_collects_consecutive_customer_messages():
    query = llm.latest_user_turn_text(
        [
            {"role": "用户", "content": "旧问题"},
            {"role": "客服", "content": "旧回复"},
            {"role": "用户", "content": "NMN怎么吃？"},
            {"role": "用户", "content": "鱼油怎么吃？"},
        ]
    )

    assert query == "NMN怎么吃？\n鱼油怎么吃？"


def test_latest_user_turn_text_skips_uncaptured_image_placeholder_before_text():
    query = llm.latest_user_turn_text(
        [
            {"role": "客服", "content": "旧回复"},
            {"role": "用户", "content": "[图片]", "media": [{"capture_ok": False}]},
            {"role": "用户", "content": "这是你们的支付宝吗？"},
        ]
    )

    assert query == "这是你们的支付宝吗？"


def test_latest_user_turn_text_keeps_captured_image_placeholder():
    query = llm.latest_user_turn_text(
        [
            {"role": "客服", "content": "旧回复"},
            {"role": "用户", "content": "[图片]", "media": [{"capture_ok": True, "capture_path": "/tmp/a.png"}]},
            {"role": "用户", "content": "这是什么？"},
        ]
    )

    assert query == "[图片]\n这是什么？"


def test_uda_provider_extracts_data_message(monkeypatch):
    captured = {}

    def fake_json_request(url, payload, headers):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return {"data": {"message": "这是接口回复"}}

    monkeypatch.setenv("WECOM_GUI_UDA_API_KEY", "test-key")
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm._json_request", fake_json_request)

    data = llm.draft_reply([{"role": "用户", "content": "鱼油含量"}], provider="uda")

    assert data["message"] == "这是接口回复"
    assert captured["payload"] == {
        "history": [{"type": "human", "data": {"content": "用户: 鱼油含量"}}],
        "ai_reply": True,
    }
    assert captured["headers"]["X-Api-Key"] == "test-key"


def test_uda_provider_accepts_top_level_message(monkeypatch):
    def fake_json_request(url, payload, headers):
        return {"code": 0, "message": "顶层回复", "data": []}

    monkeypatch.setenv("WECOM_GUI_UDA_API_KEY", "test-key")
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm._json_request", fake_json_request)

    data = llm.draft_reply([{"role": "用户", "content": "维生素发货时间"}], provider="uda")

    assert data["message"] == "顶层回复"


def test_codex_prompt_requires_reply_only():
    prompt = llm.build_codex_prompt(
        [
            {"role": "用户", "content": "鱼油怎么吃？"},
            {"role": "客服", "content": "请问是哪款？"},
        ]
    )

    assert "Return only the reply text" in prompt
    assert "用户: 鱼油怎么吃？" in prompt
    assert "客服: 请问是哪款？" in prompt


def test_codex_provider_invokes_codex_exec(monkeypatch):
    captured = {}

    class FakeTempFile:
        name = "/tmp/codex-reply.txt"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def seek(self, pos):
            captured["seek"] = pos

        def read(self):
            return "建议随餐服用"

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, input, text, capture_output, timeout, check):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["timeout"] = timeout
        captured["text"] = text
        captured["capture_output"] = capture_output
        captured["check"] = check
        return Result()

    monkeypatch.setenv("WECOM_GUI_CODEX_COMMAND", "/opt/homebrew/bin/codex")
    monkeypatch.setenv("WECOM_GUI_CODEX_MODEL", "gpt-5")
    monkeypatch.setenv("WECOM_GUI_CODEX_TIMEOUT", "3")
    monkeypatch.setenv("WECOM_GUI_CODEX_BACKEND", "direct")
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.tempfile.NamedTemporaryFile", lambda *a, **k: FakeTempFile())
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", fake_run)

    data = llm.draft_reply([{"role": "用户", "content": "鱼油怎么吃？"}], provider="codex")

    assert data["provider"] == "codex-cli-direct"
    assert data["message"] == "建议随餐服用"
    assert captured["cmd"] == [
        "/opt/homebrew/bin/codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-last-message",
        "/tmp/codex-reply.txt",
        "--model",
        "gpt-5",
        "-",
    ]
    assert "鱼油怎么吃？" in captured["input"]
    assert captured["timeout"] == 3.0


def test_codex_provider_invokes_csbot_autonomous_by_default(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {
                    "reply": {
                        "action": "send",
                        "reply_text": "女维每日 1 粒，随餐服用。",
                    }
                },
            },
            ensure_ascii=False,
        )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setenv("WECOM_GUI_CSBOT_PYTHON", "/opt/homebrew/bin/python3")
    monkeypatch.setenv("WECOM_GUI_CSBOT_CUSTOMER_ID", "cust-123")
    monkeypatch.setenv("WECOM_GUI_CODEX_TIMEOUT", "3")
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", fake_run)

    data = llm.draft_reply(
        [
            {"role": "客服", "content": "您好"},
            {"role": "用户", "content": "女维怎么吃？"},
        ],
        provider="codex",
    )

    assert data["provider"] == "csbot-autonomous"
    assert data["message"] == "女维每日 1 粒，随餐服用。"
    assert captured["cmd"][:3] == ["/opt/homebrew/bin/python3", "-m", "csbot"]
    assert "autonomous-reply" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--customer-id") + 1] == "cust-123"
    assert captured["cmd"][captured["cmd"].index("--query") + 1] == "女维怎么吃？"
    assert captured["kwargs"]["cwd"] == str(tmp_path)

    context = json.loads(captured["cmd"][captured["cmd"].index("--context-json") + 1])
    assert "customer_name" not in context


def test_pi_provider_invokes_csbot_autonomous(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {"reply": {"action": "send", "reply_text": "鱼油起拍数量是 4 盒。"}},
            },
            ensure_ascii=False,
        )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", fake_run)

    data = llm.draft_reply([{"role": "用户", "content": "鱼油的起拍数量"}], provider="pi")

    assert data["provider"] == "csbot-autonomous"
    assert data["message"] == "鱼油起拍数量是 4 盒。"
    assert captured["cmd"][:3] == [llm._csbot_python(), "-m", "csbot"]


def test_pi_provider_recovers_reply_text_from_malformed_stdout(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {
                    "reply": None,
                    "parse_error": "codex_output_is_not_json",
                    "validation": {"ok": False, "reason": "invalid_action"},
                    "stdout": (
                        "Now I'll compose the final JSON output.\\n</think>\\n\\n"
                        "{\\n"
                        '  "action": "send",\\n'
                        '  "reply_text": "您好，鱼油是膳食补充剂，不能替代药物或声称治疗。",\\n'
                        '  "decision_basis": "bad "quote""\\n'
                        "}"
                    ),
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", lambda cmd, **kwargs: Result())

    data = llm.draft_reply([{"role": "用户", "content": "鱼油能治疗高血脂吗？"}], provider="pi")

    assert data["provider"] == "csbot-autonomous"
    assert data["message"] == "您好，鱼油是膳食补充剂，不能替代药物或声称治疗。"
    assert data["recovered_from_stdout"] is True


def test_pi_provider_refuses_analysis_leak(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {
                    "reply": {
                        "action": "send",
                        "reply_text": (
                            "I now have all the information I need. Let me analyze the situation:\n\n"
                            "**Context from conversation:**\n"
                            "- Customer is taking: 氨糖软骨素, AKK, 维生素 D3K2 钙\n\n"
                            "**Key knowledge from PG/script sources:**"
                        ),
                    },
                    "parse_error": "",
                    "validation": {"ok": True, "reason": ""},
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", lambda cmd, **kwargs: Result())

    with pytest.raises(RuntimeError, match="analysis/debug text"):
        llm.draft_reply([{"role": "用户", "content": "减少一个钙片吗"}], provider="pi")


def test_pi_provider_does_not_send_plain_stdout_analysis(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {
                    "reply": None,
                    "parse_error": "codex_output_is_not_json",
                    "validation": {"ok": False, "reason": "invalid_action"},
                    "stdout": (
                        "I now have all the information I need. Let me analyze the situation:\n"
                        "**Context from conversation:**\n"
                        "- Customer is asking about D3K2 calcium."
                    ),
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", lambda cmd, **kwargs: Result())

    with pytest.raises(RuntimeError, match="empty reply"):
        llm.draft_reply([{"role": "用户", "content": "减少一个钙片吗"}], provider="pi")


def test_codex_provider_uses_latest_customer_turn_as_query(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {"reply": {"action": "send", "reply_text": "已分别说明 NMN 和鱼油吃法。"}},
            },
            ensure_ascii=False,
        )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", fake_run)

    llm.draft_reply(
        [
            {"role": "客服", "content": "您好"},
            {"role": "用户", "content": "NMN怎么吃？"},
            {"role": "用户", "content": "鱼油怎么吃？"},
        ],
        provider="codex",
    )

    assert captured["cmd"][captured["cmd"].index("--query") + 1] == "NMN怎么吃？\n鱼油怎么吃？"


def test_codex_provider_passes_customer_name_to_csbot_context(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {
                    "reply": {
                        "action": "handoff",
                        "reply_text": "您好，这个问题我帮您转人工客服确认处理，请您稍等。",
                    }
                },
            },
            ensure_ascii=False,
        )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", fake_run)

    llm.draft_reply(
        [{"role": "用户", "content": "我要投诉"}],
        provider="codex",
        customer_name="刘裕鑫",
    )

    context = json.loads(captured["cmd"][captured["cmd"].index("--context-json") + 1])
    assert context["customer_name"] == "刘裕鑫"
    assert context["conversation_title"] == "刘裕鑫"


def test_codex_provider_uses_bound_uid_as_customer_id(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {"reply": {"action": "send", "reply_text": "已查询。"}},
            },
            ensure_ascii=False,
        )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.delenv("WECOM_GUI_CODEX_BACKEND", raising=False)
    monkeypatch.setenv("WECOM_GUI_CSBOT_DIR", str(tmp_path))
    monkeypatch.setenv("WECOM_GUI_CSBOT_CUSTOMER_ID", "fallback-customer")
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", fake_run)

    llm.draft_reply(
        [{"role": "用户", "content": "查一下我的订单"}],
        provider="codex",
        customer_name="刘裕鑫",
        customer_uid="wm-test-uid",
    )

    assert captured["cmd"][captured["cmd"].index("--customer-id") + 1] == "wm-test-uid"
    context = json.loads(captured["cmd"][captured["cmd"].index("--context-json") + 1])
    assert context["customer_name"] == "刘裕鑫"
    assert context["external_user_id"] == "wm-test-uid"
    assert context["wecom_uid"] == "wm-test-uid"


def test_activate_app_refuses_to_launch_when_wecom_not_running(monkeypatch):
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.find_running_app", lambda: None)

    try:
        macos_backend.activate_app()
    except RuntimeError as exc:
        assert "refusing to launch" in str(exc)
    else:
        raise AssertionError("activate_app should fail when WeCom is not running")


def test_wecom_customer_binding_state_and_sidebar_payload(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    result = _bind_payload(
        {
            "uid": "wm-1",
            "customer_name": "刘裕鑫",
            "display_name": "刘同学",
            "source": "test",
        }
    )

    assert result["ok"] is True
    assert result["binding"]["uid"] == "wm-1"
    assert result["binding"]["customer_name"] == "刘裕鑫"
    assert state.lookup_wecom_customer(customer_name="刘裕鑫")["uid"] == "wm-1"
    assert state.lookup_wecom_customer(uid="wm-1")["display_name"] == "刘同学"


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


def test_sidebar_bind_payload_accepts_wecom_aliases(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    result = _bind_payload(
        {
            "external_user_id": "wm-alias",
            "conversation_title": "墨雨",
            "remark": "墨雨备注",
            "source": "wecom-sidebar-jsapi",
        }
    )

    assert result["ok"] is True
    assert result["binding"]["uid"] == "wm-alias"
    assert result["binding"]["customer_name"] == "墨雨"
    assert state.lookup_wecom_customer(customer_name="墨雨")["uid"] == "wm-alias"


def test_sidebar_bind_current_payload_enriches_name_from_wecom_api(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.sidebar_server._wecom_external_contact",
        lambda uid: {
            "external_contact": {"name": "微信客户名"},
            "follow_user": [{"remark": "备注名"}],
        },
    )

    result = sidebar_server._bind_current_payload({"uid": "wm-api", "source": "test"})

    assert result["ok"] is True
    assert result["binding"]["uid"] == "wm-api"
    assert result["binding"]["customer_name"] == "备注名"
    assert result["binding"]["display_name"] == "微信客户名"
    assert state.lookup_wecom_customer(uid="wm-api")["customer_name"] == "备注名"


def test_sidebar_bind_current_payload_reports_missing_name_without_api(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr("cli_anything.wecom_gui.core.sidebar_server._wecom_external_contact", lambda uid: {})
    monkeypatch.setattr("cli_anything.wecom_gui.core.sidebar_server._selected_customer_name", lambda: "")

    result = sidebar_server._bind_current_payload({"uid": "wm-api", "source": "test"})

    assert result["ok"] is True
    assert result["binding"]["uid"] == "wm-api"
    assert result["binding"]["customer_name"] == "wm-api"
    assert result["binding"]["raw"]["uid_only"] is True
    assert result["needs"] == {"uid": False, "customer_name": True}


def test_wecom_jsconfig_reports_missing_corp_id(monkeypatch):
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WEWORK_CORP_ID", raising=False)
    monkeypatch.setenv("WEWORK_AGENT_SECRET", "secret")

    result = sidebar_server._wecom_jsconfig("https://example.com/sidebar")

    assert result == {"ok": False, "error": "missing WECOM_CORP_ID/WEWORK_CORP_ID"}


def test_wecom_jsconfig_signs_with_app_ticket(monkeypatch):
    monkeypatch.setenv("WECOM_CORP_ID", "corp-1")
    monkeypatch.setenv("WECOM_AGENT_ID", "10001")
    monkeypatch.setenv("WEWORK_AGENT_SECRET", "secret")
    monkeypatch.setattr("cli_anything.wecom_gui.core.sidebar_server._wecom_ticket", lambda **kwargs: "ticket-1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.sidebar_server._nonce", lambda: "nonce-1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.sidebar_server.time.time", lambda: 1000)

    result = sidebar_server._wecom_jsconfig("https://example.com/sidebar")

    assert result["ok"] is True
    assert result["corpId"] == "corp-1"
    assert result["agentId"] == "10001"
    assert result["config"]["timestamp"] == 1000
    assert result["config"]["nonceStr"] == "nonce-1"
    assert result["agentConfig"]["nonceStr"] == "nonce-1"


def test_wecom_cli_bind_and_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    runner = CliRunner()

    bind_result = runner.invoke(
        cli,
        [
            "--json",
            "wecom",
            "bind",
            "--uid",
            "wm-cli",
            "--customer-name",
            "客户A",
            "--display-name",
            "客户A展示名",
        ],
    )
    assert bind_result.exit_code == 0

    lookup_result = runner.invoke(cli, ["--json", "wecom", "lookup", "--customer-name", "客户A"])
    assert lookup_result.exit_code == 0
    payload = json.loads(lookup_result.output)
    assert payload["binding"]["uid"] == "wm-cli"


def test_agent_passes_bound_uid_to_draft(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    state.bind_wecom_customer(uid="wm-bound", customer_name="客户A", source="test")
    row = {"title": "客户A", "preview": "查订单", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "hash1",
            "messages": [{"role": "用户", "content": "查订单", "text": "查订单"}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, customer_name="", customer_uid="", **kwargs):
        captured["customer_name"] = customer_name
        captured["customer_uid"] = customer_uid
        return {"ok": True, "text": "已查", "message": "已查"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        future = next(iter(futures.values()))
        assert future.result()["text"] == "已查"

    assert captured["customer_name"] == "客户A"
    assert captured["customer_uid"] == "wm-bound"


def test_agent_binds_visible_sidebar_uid_before_drafting(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "查订单", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, _item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "hash1",
            "messages": [{"role": "用户", "content": "查订单", "text": "查订单"}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "wm-visible")
    captured = {}

    def fake_draft(messages, customer_name="", customer_uid="", **kwargs):
        captured["customer_name"] = customer_name
        captured["customer_uid"] = customer_uid
        return {"ok": True, "text": "已查", "message": "已查"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        future = next(iter(futures.values()))
        assert future.result()["text"] == "已查"

    assert captured["customer_name"] == "客户A"
    assert captured["customer_uid"] == "wm-visible"
    assert state.lookup_wecom_customer(customer_name="客户A")["uid"] == "wm-visible"


def test_agent_defers_when_live_chat_messages_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "刘裕鑫", "preview": "老男复维多少钱", "time": "刚刚", "tags": ["@重庆邮电大学"], "raw": []}
    changed, _item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {"hash": "empty", "messages": []},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, customer_name="", customer_uid="", **kwargs):
        captured["messages"] = messages
        captured["customer_name"] = customer_name
        return {"ok": True, "text": "中老年男士复合维生素 60 元。", "message": "中老年男士复合维生素 60 元。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 0

    assert result["reason"] == "read_empty_retry_exhausted"
    assert futures == {}
    assert captured == {}
    assert state.list_queue(status="pending")[0]["error"] == "read_empty_retry_exhausted"


def test_agent_keeps_live_visible_history_for_ai(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "刘裕鑫", "preview": "后者", "time": "刚刚", "tags": ["@重庆邮电大学"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=4, capture_images=True: {
            "hash": "hist",
            "source": "accessibility-visible-chat-text",
            "messages": [
                {"role": "用户", "role_confidence": "medium", "content": "推荐A还是B", "text": "推荐A还是B"},
                {"role": "客服", "role_confidence": "medium", "content": "A偏力量，B偏耐力", "text": "A偏力量，B偏耐力"},
                {"role": "用户", "role_confidence": "medium", "content": "那我选哪个", "text": "那我选哪个"},
                {"role": "客服", "role_confidence": "medium", "content": "如果增肌优先A", "text": "如果增肌优先A"},
                {"role": "用户", "role_confidence": "medium", "content": "后者", "text": "后者"},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda messages, **kwargs: captured.setdefault("messages", messages) or {"ok": True, "text": "收到", "message": "收到"},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=4, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert len(captured["messages"]) == 5
    assert [message["text"] for message in captured["messages"]] == [
        "推荐A还是B",
        "A偏力量，B偏耐力",
        "那我选哪个",
        "如果增肌优先A",
        "后者",
    ]
    assert captured["messages"][-1]["role"] == "用户"
    stored = state.list_queue(status="drafting")[0]
    stored_context = json.loads(stored["context_json"])
    assert "messages" not in stored_context
    assert stored_context["latest"]["text"] == "后者"
    assert stored_context["message_count"] == 5


def test_agent_routes_image_messages_to_image_pool(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_TEXT_WORKERS", "7")
    monkeypatch.setenv("WECOM_AGENT_IMAGE_WORKERS", "3")
    row = {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    captured = {}

    def fake_read(last=6, capture_images=True):
        captured["capture_images"] = capture_images
        return {
            "hash": "image-hash",
            "messages": [
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/customer.png"}],
                }
            ],
        }

    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", fake_read)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda messages, **kwargs: {"ok": True, "text": "看到了图片", "message": "看到了图片"},
    )

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)
        assert result["drafting"] == 1
        job_id = next(iter(futures))
        assert agent._DRAFT_POOL[job_id] == "image"
        next(iter(futures.values())).result()

    assert captured["capture_images"] is True
    stored_context = json.loads(state.list_queue(status="drafting")[0]["context_json"])
    assert stored_context["latest"]["text"] == "[图片]"


def test_agent_routes_image_then_text_turn_to_image_pool(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_TEXT_WORKERS", "7")
    monkeypatch.setenv("WECOM_AGENT_IMAGE_WORKERS", "3")
    row = {"title": "客户A", "preview": "这是哪里？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "image-text-hash",
            "messages": [
                {"role": "客服", "role_confidence": "medium", "content": "您好", "text": "您好"},
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/customer.png"}],
                },
                {"role": "用户", "role_confidence": "medium", "content": "这是哪里？", "text": "这是哪里？"},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "看到了图片", "message": "看到了图片"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)
        assert result["drafting"] == 1
        job_id = next(iter(futures))
        assert agent._DRAFT_POOL[job_id] == "image"
        next(iter(futures.values())).result()

    assert [message["text"] for message in captured["messages"][-2:]] == ["[图片]", "这是哪里？"]
    assert agent._message_image_paths(captured["messages"]) == ["/tmp/customer.png"]
    stored_context = json.loads(state.list_queue(status="drafting")[0]["context_json"])
    assert stored_context["latest"]["text"] == "这是哪里？"


def test_agent_routes_uncaptured_image_then_text_turn_to_image_pool(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_TEXT_WORKERS", "7")
    monkeypatch.setenv("WECOM_AGENT_IMAGE_WORKERS", "3")
    row = {"title": "客户A", "preview": "这个是什么东西？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "uncaptured-image-text-hash",
            "messages": [
                {"role": "客服", "role_confidence": "medium", "content": "您好", "text": "您好"},
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": False, "error": "preview_not_found"}],
                },
                {"role": "用户", "role_confidence": "medium", "content": "这个是什么东西？", "text": "这个是什么东西？"},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "请补充图片信息。", "message": "请补充图片信息。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)
        assert result["drafting"] == 1
        job_id = next(iter(futures))
        assert agent._DRAFT_POOL[job_id] == "image"
        next(iter(futures.values())).result()

    assert [message["text"] for message in captured["messages"][-2:]] == ["[图片]", "这个是什么东西？"]


def test_agent_routes_misread_image_then_preview_text_to_image_pool(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_TEXT_WORKERS", "7")
    monkeypatch.setenv("WECOM_AGENT_IMAGE_WORKERS", "3")
    preview = "这俩是什么东西？"
    row = {"title": "客户A", "preview": preview, "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "misread-image-preview-text-hash",
            "messages": [
                {"role": "客服", "role_confidence": "medium", "content": "您好", "text": "您好"},
                {
                    "role": "客服",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/customer.png"}],
                },
                {"role": "客服", "role_confidence": "medium", "content": preview, "text": preview},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "看到了图片。", "message": "看到了图片。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)
        assert result["drafting"] == 1
        job_id = next(iter(futures))
        assert agent._DRAFT_POOL[job_id] == "image"
        next(iter(futures.values())).result()

    assert [message["text"] for message in captured["messages"][-2:]] == ["[图片]", preview]


def test_agent_appends_unread_preview_after_image_latest(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "这个花菜为什么这样？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "image-only-hash",
            "messages": [
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/veg.png"}],
                }
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "这是紫色花菜。", "message": "这是紫色花菜。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert [message["text"] for message in captured["messages"]] == ["[图片]", "这个花菜为什么这样？"]
    assert captured["messages"][-1]["source"] == "unread-preview"
    stored_context = json.loads(state.list_queue(status="drafting")[0]["context_json"])
    assert stored_context["latest"]["text"] == "这个花菜为什么这样？"


def test_agent_does_not_append_own_reply_preview_after_image(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    preview = "您好，这张图我看到了。"
    row = {"title": "客户A", "preview": preview, "time": "刚刚", "tags": ["@微信"], "unread": True, "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))
    job = state.claim_pending_for_read()
    state.mark_done(job["id"], message_hash="reply-hash", reply_text=preview)
    state.mark_pending(job["id"], "same_reply_preview")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "image-only-hash",
            "messages": [
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/img.png"}],
                }
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "继续处理图片", "message": "继续处理图片"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert [message["text"] for message in captured["messages"]] == ["[图片]"]


def test_agent_waits_when_image_pool_full(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_IMAGE_WORKERS", "0")
    row = {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "image-hash",
            "messages": [
                {
                    "role": "用户",
                    "role_confidence": "medium",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/customer.png"}],
                }
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=10)

    assert result["drafting"] == 0
    assert result["reason"] == "image_pool_full"
    assert futures == {}
    assert state.list_queue(status="pending")[0]["error"] == "image_pool_full"


def test_agent_uses_unread_preview_when_latest_role_is_misread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    preview = "藻油80岁能吃吗？男士维生素可以给女生吃吗？女生吃男士维生素会出事吗？那男生吃女士维生素呢？"
    row = {"title": "刘裕鑫", "preview": preview, "time": "刚刚", "tags": ["@重庆邮电大学"], "unread": True, "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "hash": "role-misread",
            "source": "accessibility-chat-table",
            "messages": [
                {"role": "用户", "content": "旧问题", "text": "旧问题"},
                {"role": "客服", "content": "旧回复", "text": "旧回复"},
                {"role": "客服", "content": "藻油80岁能吃吗？", "text": "藻油80岁能吃吗？"},
                {"role": "客服", "content": "男士维生素可以给女生吃吗？", "text": "男士维生素可以给女生吃吗？"},
                {"role": "客服", "content": "女生吃男士维生素会出事吗？", "text": "女生吃男士维生素会出事吗？"},
                {"role": "客服", "content": "那男生吃女士维生素呢？", "text": "那男生吃女士维生素呢？"},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "可以继续处理", "message": "可以继续处理"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert [message["role"] for message in captured["messages"][-4:]] == ["用户", "用户", "用户", "用户"]
    assert captured["messages"][-1]["role_confidence"] == "preview_fallback"
    stored = state.list_queue(status="drafting")[0]
    stored_context = json.loads(stored["context_json"])
    assert stored_context["latest"]["text"] == "那男生吃女士维生素呢？"
    assert stored_context["latest"]["role_confidence"] == "preview_fallback"


def test_agent_does_not_reclassify_service_preview_as_user(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    preview = "鱼油目前有现货的～您可以直接下单，下单后从香港仓库发出，中通国际配送。"
    row = {"title": "闫俞", "preview": preview, "time": "刚刚", "tags": ["@微信"], "unread": True, "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))
    job = state.claim_pending_for_read()
    state.mark_done(job["id"], message_hash="reply-hash", reply_text=preview)
    state.mark_pending(job["id"], "same_reply_preview")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=False: {
            "hash": "service-preview",
            "source": "accessibility-chat-table",
            "messages": [
                {"role": "用户", "content": "鱼油什么时候有货", "text": "鱼油什么时候有货"},
                {"role": "客服", "content": preview, "text": preview},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda messages, **kwargs: (_ for _ in ()).throw(AssertionError("should not draft from service preview")),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=1)

    assert result["drafting"] == 0
    assert result["reason"] == "latest_message_not_user:客服"
    assert futures == {}
    assert state.list_queue(status="skipped")[0]["error"] == "latest_message_not_user:客服"


def test_agent_reclassifies_matching_customer_preview_even_if_role_is_service(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    preview = "这3个东西可以和你们的鱼油一起吃吗？"
    row = {"title": "LeoFree", "preview": preview, "time": "刚刚", "tags": ["@微信"], "unread": True, "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "misread-customer",
            "source": "accessibility-chat-table",
            "messages": [
                {"role": "用户", "content": "[图片]", "text": "[图片]", "media": [{"type": "image", "capture_ok": False}]},
                {"role": "客服", "content": preview, "text": preview},
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "可以一起吃。", "message": "可以一起吃。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert captured["messages"][-1]["role"] == "用户"
    assert captured["messages"][-1]["role_confidence"] == "preview_fallback"
    assert captured["messages"][-1]["original_role"] == "客服"
    assert state.list_queue(status="drafting")[0]["preview"] == preview


def test_agent_reclassifies_image_preview_even_if_role_is_service(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "LeoFree", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "unread": True, "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=6, capture_images=True: {
            "hash": "misread-image",
            "source": "accessibility-chat-table",
            "messages": [
                {
                    "role": "客服",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/customer.png"}],
                }
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "收到图片。", "message": "收到图片。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=6, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert captured["messages"][-1]["role"] == "用户"
    assert captured["messages"][-1]["role_confidence"] == "preview_fallback"
    assert captured["messages"][-1]["original_role"] == "客服"


def test_latest_preview_turn_excludes_misread_old_service_reply():
    messages = [
        {"role": "用户", "role_confidence": "medium", "text": "这是哪里的景色？", "content": "这是哪里的景色？"},
        {
            "role": "用户",
            "role_confidence": "medium",
            "text": "这是一张透过纱窗拍摄的黄昏景色",
            "content": "这是一张透过纱窗拍摄的黄昏景色",
        },
        {
            "role": "用户",
            "role_confidence": "medium",
            "text": "[图片]",
            "content": "[图片]",
            "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/one.png"}],
        },
        {
            "role": "用户",
            "role_confidence": "preview_fallback",
            "text": "[图片]",
            "content": "[图片]",
            "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/two.png"}],
        },
        {
            "role": "用户",
            "role_confidence": "preview_fallback",
            "text": "[图片]",
            "content": "[图片]",
            "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/three.png"}],
        },
        {
            "role": "用户",
            "role_confidence": "preview_fallback",
            "text": "这3张哪一张拍得最好看？",
            "content": "这3张哪一张拍得最好看？",
        },
    ]

    agent_turn = agent._latest_user_turn_messages(messages)
    llm_turn = llm.latest_user_turn_messages(messages)

    assert [message["text"] for message in agent_turn] == ["[图片]", "[图片]", "[图片]", "这3张哪一张拍得最好看？"]
    assert [message["text"] for message in llm_turn] == ["[图片]", "[图片]", "[图片]", "这3张哪一张拍得最好看？"]
    assert llm.latest_user_turn_image_paths(messages) == ["/tmp/one.png", "/tmp/two.png", "/tmp/three.png"]
    assert "黄昏景色" not in llm.latest_user_turn_text(messages)


def test_agent_send_recheck_allows_same_latest_text_with_misread_role(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "刘裕鑫", "preview": "鱼油怎么吃？", "time": "刚刚", "tags": ["@重庆邮电大学"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "鱼油怎么吃？", "content": "鱼油怎么吃？"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="鱼油建议随餐服用。")

    reads = iter(
        [
            {
                "hash": "same-text-misread",
                "messages": [{"role": "客服", "content": "鱼油怎么吃？", "text": "鱼油怎么吃？"}],
            },
            {
                "hash": "after-send",
                "messages": [
                    {"role": "客服", "content": "鱼油怎么吃？", "text": "鱼油怎么吃？"},
                    {"role": "客服", "content": "鱼油建议随餐服用。", "text": "鱼油建议随餐服用。"},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert sent == [{"text": "鱼油建议随餐服用。", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "刘裕鑫"


def test_review_mode_does_not_send_unapproved_ready_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "鱼油怎么吃？", "content": "鱼油怎么吃？"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="鱼油建议随餐服用。")

    sent = []
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: (_ for _ in ()).throw(AssertionError("should not open GUI")))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append(text),
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["reason"] == "queue_empty"
    assert sent == []
    assert state.list_queue(status="ready")[0]["reply_text"] == "鱼油建议随餐服用。"


def test_review_mode_sends_approved_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "鱼油怎么吃？", "content": "鱼油怎么吃？"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="鱼油建议随餐服用。")
    assert state.mark_approved(item["id"]) is True

    reads = iter(
        [
            {
                "hash": "precheck",
                "messages": [{"role": "用户", "content": "鱼油怎么吃？", "text": "鱼油怎么吃？"}],
            },
            {
                "hash": "after-send",
                "messages": [
                    {"role": "用户", "content": "鱼油怎么吃？", "text": "鱼油怎么吃？"},
                    {"role": "客服", "content": "鱼油建议随餐服用。", "text": "鱼油建议随餐服用。"},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["sent"] == 1
    assert sent == [{"text": "鱼油建议随餐服用。", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "客户A"


def test_agent_send_recheck_retries_empty_live_read(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "刘裕鑫", "preview": "鱼油怎么吃？", "time": "刚刚", "tags": ["@重庆邮电大学"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "鱼油怎么吃？", "content": "鱼油怎么吃？"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="鱼油建议随餐服用。")

    reads = iter(
        [
            {"hash": "empty-precheck", "messages": []},
            {
                "hash": "expected-precheck",
                "messages": [{"role": "用户", "content": "鱼油怎么吃？", "text": "鱼油怎么吃？"}],
            },
            {
                "hash": "after-empty",
                "messages": [],
            },
            {
                "hash": "after-send",
                "messages": [
                    {"role": "用户", "content": "鱼油怎么吃？", "text": "鱼油怎么吃？"},
                    {"role": "客服", "content": "鱼油建议随餐服用。", "text": "鱼油建议随餐服用。"},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert sent == [{"text": "鱼油建议随餐服用。", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "刘裕鑫"


def test_agent_send_recheck_allows_image_latest_after_misread_own_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "LeoFree", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "[图片]", "content": "[图片]"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="这张图片里是小象鲜牛奶，不是我们营养工厂的产品哦。")

    reads = iter(
        [
            {
                "hash": "precheck",
                "messages": [
                    {
                        "role": "用户",
                        "content": "您好～这张图片我看不到具体内容呢",
                        "text": "您好～这张图片我看不到具体内容呢",
                    },
                    {"role": "用户", "content": "[图片]", "text": "[图片]", "media": [{"type": "image"}]},
                ],
            },
            {
                "hash": "after-send",
                "messages": [
                    {"role": "用户", "content": "[图片]", "text": "[图片]", "media": [{"type": "image"}]},
                    {
                        "role": "客服",
                        "content": "这张图片里是小象鲜牛奶，不是我们营养工厂的产品哦。",
                        "text": "这张图片里是小象鲜牛奶，不是我们营养工厂的产品哦。",
                    },
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert sent == [
        {
            "text": "这张图片里是小象鲜牛奶，不是我们营养工厂的产品哦。",
            "dry_run": False,
            "submit": True,
        }
    ]
    assert state.list_queue(status="done")[0]["title"] == "LeoFree"


def test_agent_send_recheck_allows_same_user_turn_with_image_and_text(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "LeoFree", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "这是哪里？", "content": "这是哪里？"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="这是北京央视总部大楼。")

    reads = iter(
        [
            {
                "hash": "precheck-image-last",
                "messages": [
                    {"role": "用户", "content": "这是哪里？", "text": "这是哪里？"},
                    {"role": "用户", "content": "[图片]", "text": "[图片]", "media": [{"type": "image"}]},
                ],
            },
            {
                "hash": "after-send",
                "messages": [
                    {"role": "用户", "content": "这是哪里？", "text": "这是哪里？"},
                    {"role": "用户", "content": "[图片]", "text": "[图片]", "media": [{"type": "image"}]},
                    {"role": "客服", "content": "这是北京央视总部大楼。", "text": "这是北京央视总部大楼。"},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert sent == [{"text": "这是北京央视总部大楼。", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "LeoFree"


def test_agent_send_recheck_keeps_ready_when_live_read_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "刘裕鑫", "preview": "老男复维多少钱", "time": "刚刚", "tags": ["@重庆邮电大学"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "老男复维多少钱", "content": "老男复维多少钱"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="中老年男士复合维生素 60 元。")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {"hash": "empty", "messages": []},
    )
    sent = {}

    def fake_send(text, dry_run=False, submit=True):
        sent["text"] = text
        return {"ok": True}

    monkeypatch.setattr("cli_anything.wecom_gui.core.reply.send_text", fake_send)
    monkeypatch.setattr("cli_anything.wecom_gui.core.worker._messages_contain_text", lambda messages, text: False)

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 0
    assert result["retry"] == 1
    assert sent == {}
    ready = state.list_queue(status="ready")
    assert ready[0]["error"] is None
    assert ready[0]["reply_text"] == "中老年男士复合维生素 60 元。"


def test_codex_provider_reports_failure(monkeypatch):
    class FakeTempFile:
        name = "/tmp/codex-reply.txt"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def seek(self, pos):
            pass

        def read(self):
            return ""

    class Result:
        returncode = 2
        stdout = ""
        stderr = "not logged in"

    monkeypatch.setenv("WECOM_GUI_CODEX_BACKEND", "direct")
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.tempfile.NamedTemporaryFile", lambda *a, **k: FakeTempFile())
    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.subprocess.run", lambda *a, **k: Result())

    try:
        llm.draft_reply([{"role": "用户", "content": "你好"}], provider="codex")
    except RuntimeError as exc:
        assert "not logged in" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")


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


def test_cli_reply_send_dry_run_json():
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "reply", "send", "--text", "hello", "--dry-run"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["text"] == "hello"


def test_cli_loads_env_local_for_inbox_scan(monkeypatch, tmp_path):
    monkeypatch.delenv("WECOM_GUI_REQUIRED_TAGS", raising=False)
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


def test_agent_debounces_and_uses_newer_message_before_drafting(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "3")
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    opened: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: opened.append(job["title"]))
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: sleeps.append(seconds))
    reads = iter(
        [
            {
                "hash": "old-hash",
                "messages": [{"role": "用户", "content": "旧问题", "text": "旧问题"}],
            },
            {
                "hash": "new-hash",
                "messages": [{"role": "用户", "content": "新问题", "text": "新问题"}],
            },
        ]
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: next(reads),
    )
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "新回复", "message": "新回复"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert opened == ["客户A", "客户A"]
    assert 3 in sleeps
    assert captured["messages"] == [{"role": "用户", "content": "新问题", "text": "新问题"}]
    stored = state.list_queue(status="drafting")[0]
    stored_context = json.loads(stored["context_json"])
    assert stored["last_message_hash"] == "new-hash"
    assert stored_context["latest"]["text"] == "新问题"


def test_agent_debounce_pauses_old_message_when_chat_changes_without_user_latest(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "3")
    monkeypatch.setenv("WECOM_AGENT_READ_ATTEMPTS", "1")
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: None)
    reads = iter(
        [
            {
                "hash": "old-hash",
                "messages": [{"role": "用户", "content": "旧问题", "text": "旧问题"}],
            },
            {
                "hash": "changed-hash",
                "messages": [
                    {"role": "用户", "content": "旧问题", "text": "旧问题"},
                    {"role": "客服", "content": "处理中", "text": "处理中"},
                ],
            },
        ]
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: next(reads),
    )
    captured = {}
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda messages, **kwargs: captured.setdefault("messages", messages) or {"ok": True, "text": "旧回复", "message": "旧回复"},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)

    assert result["drafting"] == 0
    assert result["reason"] == "debounce_changed_unresolved"
    assert futures == {}
    assert captured == {}
    assert state.list_queue(status="pending")[0]["error"] == "debounce_changed_unresolved"


def test_agent_debounce_keeps_initial_read_when_recheck_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_AGENT_NEW_MESSAGE_DEBOUNCE_SECONDS", "3")
    monkeypatch.setenv("WECOM_AGENT_READ_ATTEMPTS", "1")
    row = {"title": "客户A", "preview": "订单现在到哪了？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: None)
    reads = iter(
        [
            {
                "hash": "initial-hash",
                "messages": [{"role": "用户", "content": "订单现在到哪了？", "text": "订单现在到哪了？"}],
            },
            {"hash": "", "messages": []},
        ]
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: next(reads),
    )
    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        return {"ok": True, "text": "我帮您查一下物流。", "message": "我帮您查一下物流。"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert captured["messages"] == [{"role": "用户", "content": "订单现在到哪了？", "text": "订单现在到哪了？"}]
    stored = state.list_queue(status="drafting")[0]
    assert stored["last_message_hash"] == "initial-hash"


def test_review_approve_and_reject_ready_items(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    ready_row = {"title": "客户A", "preview": "鱼油怎么吃", "time": "刚刚", "tags": ["@微信"], "raw": []}
    rejected_row = {"title": "客户B", "preview": "想退款", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(ready_row, "sig-ready")
    state.enqueue_conversation(rejected_row, "sig-reject")
    first = state.claim_pending_for_read()
    second = state.claim_pending_for_read()
    state.mark_drafting(
        first["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "鱼油怎么吃"}],
        latest={"role": "用户", "content": "鱼油怎么吃"},
    )
    state.mark_drafting(
        second["id"],
        message_hash="hash-b",
        messages=[{"role": "用户", "content": "想退款"}],
        latest={"role": "用户", "content": "想退款"},
    )
    state.mark_ready(first["id"], reply_text="每天一粒，随餐吃。")
    state.mark_ready(second["id"], reply_text="我帮您转人工处理。")

    assert review_server.approve_item(first["id"])["ok"] is True
    assert state.get_job(first["id"])["status"] == "approved"
    assert review_server.approve_item(first["id"])["ok"] is False

    assert review_server.reject_item(second["id"])["ok"] is True
    rejected = state.get_job(second["id"])
    assert rejected["status"] == "skipped"
    assert rejected["error"] == "review_rejected"


def test_review_items_include_latest_context(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧预览", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "最新问题"}],
        latest={"role": "用户", "content": "最新问题"},
    )
    state.mark_ready(job["id"], reply_text="审核回复")

    items = review_server.list_review_items(status="ready")

    assert len(items) == 1
    assert items[0]["title"] == "客户A"
    assert items[0]["latest_text"] == "最新问题"
    assert items[0]["reply_text"] == "审核回复"
    assert items[0]["conversation_key"]
    assert items[0]["messages"][0]["role"] == "用户"
    assert items[0]["messages"][0]["text"] == "最新问题"


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


def test_review_http_allows_actions_without_token(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "查订单", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "查订单"}],
        latest={"role": "用户", "content": "查订单"},
    )
    state.mark_ready(job["id"], reply_text="请发我订单号。")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with request.urlopen(f"{base}/api/review/items?status=ready", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["items"][0]["reply_text"] == "请发我订单号。"

        approve_req = request.Request(
            f"{base}/api/review/items/{job['id']}/approve",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(approve_req, timeout=5) as resp:
            approved = json.loads(resp.read().decode("utf-8"))
        assert approved["ok"] is True
        assert state.get_job(job["id"])["status"] == "approved"

        with request.urlopen(f"{base}/api/review/counts", timeout=5) as resp:
            counts = json.loads(resp.read().decode("utf-8"))
        assert counts["counts"]["approved"] == 1
        for status in ["ready", "approved", "done", "skipped", "failed"]:
            assert status in counts["counts"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_finish_drafts_discards_stale_result_after_message_replaced(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True, "unread_count": 1}
    state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="old-hash",
        messages=[{"role": "用户", "content": "旧问题"}],
        latest={"role": "用户", "content": "旧问题"},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: {"ok": True, "text": "旧回复", "message": "旧回复"})
        future.result()
        futures = {claimed["id"]: future}
        agent._DRAFT_STARTED_AT[claimed["id"]] = agent.time.perf_counter()
        agent._DRAFT_POOL[claimed["id"]] = "text"
        agent._DRAFT_MESSAGE_HASH[claimed["id"]] = "old-hash"

        changed, item = state.enqueue_conversation(
            {
                "title": "客户A",
                "preview": "新问题",
                "time": "刚刚",
                "tags": ["@微信"],
                "raw": [],
                "unread": True,
                "unread_count": 2,
            },
            "sig2",
        )
        assert changed is True
        assert item["status"] == "pending"

        logs = []
        monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", logs.append)
        result = agent._finish_drafts(futures)

    assert result == {"ready": 0, "failed": 0}
    assert state.list_queue(status="ready") == []
    pending = state.list_queue(status="pending")[0]
    assert pending["preview"] == "新问题"
    assert pending["reply_text"] is None
    assert any("AI回复已丢弃：客户A" in line for line in logs)


def test_superseded_draft_is_untracked_before_pool_capacity_check(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True, "unread_count": 1}
    state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="old-hash",
        messages=[{"role": "用户", "content": "旧问题"}],
        latest={"role": "用户", "content": "旧问题"},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: {"ok": True, "text": "旧回复", "message": "旧回复"})
        futures = {claimed["id"]: future}
        agent._DRAFT_STARTED_AT[claimed["id"]] = agent.time.perf_counter()
        agent._DRAFT_POOL[claimed["id"]] = "text"
        agent._DRAFT_MESSAGE_HASH[claimed["id"]] = "old-hash"

        state.enqueue_conversation(
            {
                "title": "客户A",
                "preview": "新问题",
                "time": "刚刚",
                "tags": ["@微信"],
                "raw": [],
                "unread": True,
                "unread_count": 2,
            },
            "sig2",
        )
        logs = []
        monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", logs.append)
        result = agent._drop_superseded_drafts(futures)

    assert result == {"discarded": 1}
    assert futures == {}
    assert claimed["id"] not in agent._DRAFT_POOL
    assert claimed["id"] not in agent._DRAFT_MESSAGE_HASH
    assert state.list_queue(status="pending")[0]["preview"] == "新问题"
    assert any("AI旧请求已让位：客户A" in line for line in logs)


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


def test_latest_user_message_accepts_low_confidence_for_unread_queue():
    latest = watcher.latest_user_message(
        [{"role": "用户", "role_confidence": "low", "content": "真实未读队列消息"}]
    )

    assert latest["content"] == "真实未读队列消息"


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


def test_fast_agent_pipeline_reads_drafts_and_sends(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig1")
    opened = []
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_by_name", lambda title: opened.append(title))
    reads = iter(
        [
            {
                "ok": True,
                "hash": "hash1",
                "messages": [{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
            },
            {
                "ok": True,
                "hash": "hash1",
                "messages": [{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
            },
            {
                "ok": True,
                "hash": "hash2",
                "messages": [
                    {"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"},
                    {"role": "客服", "content": "随餐服用", "text": "随餐服用"},
                ],
            },
        ]
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda messages, **kwargs: {"ok": True, "provider": "fake", "text": "随餐服用", "message": "随餐服用"},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        intake = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert intake["drafting"] == 1
        assert state.list_queue(status="drafting")[0]["title"] == "客户A"

        logs = []
        monkeypatch.setattr("cli_anything.wecom_gui.core.agent._log", logs.append)
        finished = agent._finish_drafts(futures)
        assert finished["ready"] == 1
        assert state.list_queue(status="ready")[0]["reply_text"] == "随餐服用"
        assert any("AI回复已生成：客户A｜耗时=" in line for line in logs)

        result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert opened == ["客户A", "客户A"]
    assert sent == [{"text": "随餐服用", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "客户A"


def test_fast_agent_rechecks_without_resending_when_reply_not_visible_once(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash1",
        messages=[{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
        latest={"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"},
    )
    state.mark_ready(claimed["id"], reply_text="随餐服用")

    reads = iter(
        [
            {
                "ok": True,
                "hash": "precheck",
                "messages": [{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
            },
            {
                "ok": True,
                "hash": "after-ax-missing",
                "messages": [{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
            },
            {
                "ok": True,
                "hash": "after-clipboard",
                "messages": [
                    {"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"},
                    {"role": "客服", "content": "随餐服用", "text": "随餐服用"},
                ],
            },
        ]
    )
    sent = []
    fallback = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append({"text": text, "dry_run": dry_run, "submit": submit}),
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.paste_and_enter",
        lambda text, submit=True: fallback.append({"text": text, "submit": submit}) or {"ok": True},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: None)

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert sent == [{"text": "随餐服用", "dry_run": False, "submit": True}]
    assert fallback == []
    assert state.list_queue(status="done")[0]["reply_text"] == "随餐服用"


def test_worker_does_not_mark_done_when_sent_reply_is_not_visible(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig1")
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda row: {"ok": True})
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "ok": True,
            "hash": "hash1",
            "messages": [{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda messages, **kwargs: {"ok": True, "provider": "fake", "text": "随餐服用", "message": "随餐服用"},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: {"ok": True, "submitted": True},
    )

    result = worker.process_one(last=12, mode="auto")

    assert result["ok"] is False
    assert result["sent"] == 0
    failed = state.list_queue(status="failed")[0]
    assert failed["title"] == "客户A"
    assert "sent_reply_not_visible" in failed["error"]


def test_fast_agent_skips_stale_context_before_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _, item = state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash-old",
        messages=[{"role": "用户", "content": "旧问题"}],
        latest={"role": "用户", "content": "旧问题"},
    )
    state.mark_ready(claimed["id"], reply_text="旧回复")
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_by_name", lambda title: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "ok": True,
            "hash": "hash-new",
            "messages": [{"role": "用户", "content": "新问题", "text": "新问题"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append(text),
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert item["title"] == "客户A"
    assert result["reason"] == "stale_context"
    assert sent == []
    assert state.list_queue(status="skipped")[0]["error"] == "stale_context"


def test_review_mode_skips_stale_context_before_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "旧问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _, item = state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash-old",
        messages=[{"role": "用户", "content": "旧问题"}],
        latest={"role": "用户", "content": "旧问题"},
    )
    state.mark_ready(claimed["id"], reply_text="旧回复")
    assert state.mark_approved(claimed["id"]) is True
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "ok": True,
            "hash": "hash-new",
            "messages": [{"role": "用户", "content": "新问题", "text": "新问题"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_text",
        lambda text, dry_run=False, submit=True: sent.append(text),
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert item["title"] == "客户A"
    assert result["reason"] == "stale_context"
    assert sent == []
    assert state.list_queue(status="skipped")[0]["error"] == "stale_context"


def test_fast_agent_read_failure_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig1")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.inbox.open_by_name",
        lambda title: (_ for _ in ()).throw(RuntimeError("not visible")),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = agent._read_one_pending(last=12, executor=executor, futures={}, max_drafts=1)

    assert result["ok"] is False
    assert result["failed"] == 1
    assert state.list_queue(status="failed")[0]["error"] == "not visible"


def test_fast_agent_send_failure_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "你好", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _, item = state.enqueue_conversation(row, "sig1")
    claimed = state.claim_pending_for_read()
    state.mark_drafting(
        claimed["id"],
        message_hash="hash",
        messages=[{"role": "用户", "content": "你好"}],
        latest={"role": "用户", "content": "你好"},
    )
    state.mark_ready(claimed["id"], reply_text="您好")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.inbox.open_by_name",
        lambda title: (_ for _ in ()).throw(RuntimeError("not visible")),
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert item["title"] == "客户A"
    assert result["ok"] is False
    assert result["failed"] == 1
    assert state.list_queue(status="failed")[0]["error"] == "not visible"


def test_gui_lock_uses_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    with state.gui_lock():
        assert (tmp_path / "gui.lock").exists()


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
