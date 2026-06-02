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


def test_scan_once_enqueues_new_customer_without_unread(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.inbox.scan_visible",
        lambda limit=30: {
            "ok": True,
            "conversations": [
                {
                    "title": "三水儿",
                    "preview": "你已添加了三水儿，现在可以开始聊天了。",
                    "time": "3分钟前",
                    "tags": ["@微信"],
                    "raw": ["三水儿", "@微信", "你已添加了三水儿，现在可以开始聊天了。", "3分钟前"],
                    "unread": False,
                    "unread_count": 0,
                }
            ],
        },
    )

    result = worker.scan_once(inbox_limit=30)

    assert result["enqueued"] == 1
    assert result["ignored_no_unread"] == 0
    assert result["welcome_items"][0]["title"] == "三水儿"
    item = state.list_queue(status="pending")[0]
    assert item["preview"] == "你已添加了三水儿，现在可以开始聊天了。"

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

def test_fast_agent_does_not_mark_done_when_sent_reply_is_not_visible(monkeypatch, tmp_path):
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
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: None)

    result = agent._send_one_ready(last=12, mode="auto")

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
