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
