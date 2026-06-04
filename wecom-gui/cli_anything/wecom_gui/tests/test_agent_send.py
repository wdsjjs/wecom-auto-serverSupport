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
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(
            {"text": text, "dry_run": dry_run, "submit": submit}
        )
        or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert sent == [{"text": "鱼油建议随餐服用。", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "刘裕鑫"


def test_agent_send_preflights_input_and_sidebar(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    latest = {"role": "用户", "text": "鱼油怎么吃？", "content": "鱼油怎么吃？"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="随餐服用。")

    preflight: list[str] = []
    reads = iter(
        [
            {"hash": "hash1", "messages": [latest]},
            {"hash": "after", "messages": [latest, {"role": "客服", "content": "随餐服用。", "text": "随餐服用。"}]},
        ]
    )
    sent: list[str] = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.agent._ensure_chat_input_ready_for_job",
        lambda job, stage: preflight.append(stage) or {"ok": True, "input": {"x": 1}, "sidebar": {"ok": True}},
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 1
    assert preflight == ["send"]
    assert sent == ["随餐服用。"]


def test_agent_marks_welcome_sent_only_after_successful_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "三水儿", "preview": "你已添加了 三水儿，现在可以开始聊天了。", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    trigger = {"role": "系统", "text": row["preview"], "content": row["preview"], "message_type": "system"}
    state.mark_drafting(item["id"], message_hash="welcome-hash", messages=[trigger], latest=trigger)
    state.mark_welcome_status(
        item["conversation_key"],
        state.WELCOME_PENDING,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        reason="welcome_draft_ready",
    )
    state.mark_ready(item["id"], reply_text="{WELCOME_MESSAGE}", reply_source="welcome")

    auto_result = agent._send_one_ready(last=12, mode="auto")

    assert auto_result["reason"] == "queue_empty"
    assert state.get_job(item["id"])["status"] == "ready"
    assert state.mark_approved(item["id"]) is True

    reads = iter(
        [
            {"hash": "precheck", "messages": [trigger]},
            {
                "hash": "after-send",
                "messages": [
                    trigger,
                    {"role": "客服", "content": "{WELCOME_MESSAGE}", "text": "{WELCOME_MESSAGE}"},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append({"text": text, "submit": submit}) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["sent"] == 1
    assert sent == [{"text": "{WELCOME_MESSAGE}", "submit": True}]
    assert state.get_welcome_state(item["conversation_key"])["status"] == state.WELCOME_SENT
    done = state.list_queue(status="done")[0]
    assert done["reply_source"] == "welcome"


def test_agent_marks_supplement_recommended_after_successful_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "三水儿", "preview": "最近睡眠差", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    latest = {"role": "用户", "text": "最近睡眠差", "content": "最近睡眠差"}
    state.mark_drafting(
        item["id"],
        message_hash="supp-hash",
        messages=[latest],
        latest=latest,
        extra_context={
            "agent_mode": "supplement",
            "supplement_trace_id": "trace-1",
            "supplement_customer_key": item["conversation_key"],
        },
    )
    state.mark_supplement_state(
        item["conversation_key"],
        state.SUPPLEMENT_READY_TO_RECOMMEND,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        trace_id="trace-1",
        pending_next_stage=state.SUPPLEMENT_RECOMMENDED,
        reason="supplement_draft_ready",
    )
    state.mark_ready(item["id"], reply_text="结合您的需求，为您推荐这几款产品组合。", reply_source="supplement")

    auto_result = agent._send_one_ready(last=12, mode="auto")

    assert auto_result["reason"] == "queue_empty"
    assert state.get_job(item["id"])["status"] == "ready"
    assert state.mark_approved(item["id"]) is True

    reads = iter(
        [
            {"hash": "precheck", "messages": [latest]},
            {
                "hash": "after-send",
                "messages": [
                    latest,
                    {
                        "role": "客服",
                        "content": "结合您的需求，为您推荐这几款产品组合。",
                        "text": "结合您的需求，为您推荐这几款产品组合。",
                    },
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["sent"] == 1
    assert sent == ["结合您的需求，为您推荐这几款产品组合。"]
    assert state.list_queue(status="done")[0]["reply_source"] == "supplement"
    assert state.get_supplement_state(item["conversation_key"])["stage"] == state.SUPPLEMENT_RECOMMENDED
    assert state.list_supplement_logs(event_type="supplement_sent")[-1]["trace_id"] == "trace-1"


def test_agent_sends_new_user_supplement_welcome_in_two_messages(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {
        "title": "三水儿",
        "preview": "你已添加了 三水儿，现在可以开始聊天了。",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
    }
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    trigger = {"role": "系统", "text": row["preview"], "content": row["preview"], "message_type": "system"}
    welcome_text = agent._supplement_wecom_welcome_text("三水儿")
    followup_text = agent.supplement_first_reply_with_profile()
    final_reply = f"{welcome_text}\n\n{followup_text}"
    state.mark_drafting(
        item["id"],
        message_hash="welcome-hash",
        messages=[trigger],
        latest=trigger,
        extra_context={
            "agent_mode": "supplement",
            "supplement_trace_id": "trace-welcome",
            "supplement_customer_key": item["conversation_key"],
            "supplement_from_welcome": True,
            "supplement_welcome_text": welcome_text,
            "supplement_followup_text": followup_text,
        },
    )
    state.mark_welcome_status(
        item["conversation_key"],
        state.WELCOME_PENDING,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        reason="supplement_welcome_ready",
    )
    state.mark_supplement_state(
        item["conversation_key"],
        state.SUPPLEMENT_COLLECTING_PROFILE,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        trace_id="trace-welcome",
        pending_next_stage=state.SUPPLEMENT_DIGGING_NEED,
        reason="supplement_welcome_ready",
    )
    state.mark_ready(item["id"], reply_text=final_reply, reply_source="supplement")
    assert state.mark_approved(item["id"]) is True

    reads = iter(
        [
            {"hash": "precheck", "messages": [trigger]},
            {
                "hash": "after-send",
                "messages": [
                    trigger,
                    {"role": "客服", "content": welcome_text, "text": welcome_text},
                    {"role": "客服", "content": followup_text, "text": followup_text},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["sent"] == 1
    assert sent == [welcome_text, followup_text]
    stored = state.list_conversation_messages(conversation_key=item["conversation_key"])
    assert [message["text"] for message in stored[-2:]] == [welcome_text, followup_text]
    assert state.get_welcome_state(item["conversation_key"])["status"] == state.WELCOME_SENT


def test_agent_marks_review_edited_welcome_sent(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "三水儿", "preview": "你已添加了 三水儿，现在可以开始聊天了。", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    trigger = {"role": "系统", "text": row["preview"], "content": row["preview"], "message_type": "system"}
    state.mark_drafting(item["id"], message_hash="welcome-hash", messages=[trigger], latest=trigger)
    state.mark_welcome_status(
        item["conversation_key"],
        state.WELCOME_PENDING,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        reason="welcome_draft_ready",
    )
    state.mark_ready(item["id"], reply_text="{WELCOME_MESSAGE}", reply_source="welcome")
    assert state.mark_approved(item["id"], reply_text="欢迎加入营养工厂") is True

    reads = iter(
        [
            {"hash": "precheck", "messages": [trigger]},
            {
                "hash": "after-send",
                "messages": [
                    trigger,
                    {"role": "客服", "content": "欢迎加入营养工厂", "text": "欢迎加入营养工厂"},
                ],
            },
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["sent"] == 1
    assert sent == ["欢迎加入营养工厂"]
    assert state.get_welcome_state(item["conversation_key"])["status"] == state.WELCOME_SENT
    assert state.list_queue(status="done")[0]["reply_source"] == "human"


def test_agent_skips_welcome_when_customer_message_arrives_before_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "三水儿", "preview": "你已添加了 三水儿，现在可以开始聊天了。", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    trigger = {"role": "系统", "text": row["preview"], "content": row["preview"], "message_type": "system"}
    state.mark_drafting(item["id"], message_hash="welcome-hash", messages=[trigger], latest=trigger)
    state.mark_welcome_status(
        item["conversation_key"],
        state.WELCOME_PENDING,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        reason="welcome_draft_ready",
    )
    state.mark_ready(item["id"], reply_text="{WELCOME_MESSAGE}", reply_source="welcome")
    assert state.mark_approved(item["id"]) is True

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "hash": "precheck-new-customer",
            "messages": [
                trigger,
                {"role": "用户", "content": "你好，我想咨询产品", "text": "你好，我想咨询产品"},
            ],
        },
    )
    sent = []
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda *args, **kwargs: sent.append(args) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["skipped"] == 1
    assert sent == []
    assert state.get_welcome_state(item["conversation_key"])["status"] == state.WELCOME_SKIPPED
    assert state.get_job(item["id"])["status"] == "skipped"


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
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(
            {"text": text, "dry_run": dry_run, "submit": submit}
        )
        or {"ok": True},
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
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(
            {"text": text, "dry_run": dry_run, "submit": submit}
        )
        or {"ok": True},
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
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(
            {"text": text, "dry_run": dry_run, "submit": submit}
        )
        or {"ok": True},
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

    def fake_send(text, attachments=None, dry_run=False, submit=True):
        sent["text"] = text
        return {"ok": True}

    monkeypatch.setattr("cli_anything.wecom_gui.core.reply.send_message", fake_send)
    monkeypatch.setattr("cli_anything.wecom_gui.core.worker._messages_contain_text", lambda messages, text: False)

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["sent"] == 0
    assert result["retry"] == 1
    assert sent == {}
    ready = state.list_queue(status="ready")
    assert ready[0]["error"] is None
    assert ready[0]["reply_text"] == "中老年男士复合维生素 60 元。"


def test_agent_send_verification_failure_does_not_record_sent_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "嗯", "preview": "嗯", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    latest = {"role": "用户", "text": "嗯", "content": "嗯"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="收到，我这边帮您看一下。")

    reads = iter(
        [
            {"hash": "precheck", "messages": [latest]},
            {"hash": "after-send-missing", "messages": [latest]},
            {"hash": "after-send-still-missing", "messages": [latest]},
        ]
    )
    sent = []

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr("cli_anything.wecom_gui.core.chat.read_current", lambda last=12, capture_images=False: next(reads))
    monkeypatch.setattr("cli_anything.wecom_gui.core.agent.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["error"] == "sent_reply_not_visible"
    assert sent == ["收到，我这边帮您看一下。"]
    failed = state.list_queue(status="failed")
    assert failed[0]["reply_text"] == "收到，我这边帮您看一下。"
    assert failed[0]["error"] == "sent_reply_not_visible"
    stored = state.list_conversation_messages(conversation_key=item["conversation_key"])
    assert [message["text"] for message in stored] == ["嗯"]


def test_agent_skips_send_when_reply_already_visible(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "嗯", "preview": "嗯", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    latest = {"role": "用户", "text": "嗯", "content": "嗯"}
    state.mark_drafting(item["id"], message_hash="hash1", messages=[latest], latest=latest)
    state.mark_ready(item["id"], reply_text="收到，我这边帮您看一下。")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "hash": "reply-visible",
            "messages": [
                latest,
                {"role": "客服", "text": "收到，我这边帮您看一下。", "content": "收到，我这边帮您看一下。"},
            ],
        },
    )
    sent = []
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="auto")

    assert result["already_visible"] == 1
    assert sent == []
    assert state.get_job(item["id"])["status"] == "done"
