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
