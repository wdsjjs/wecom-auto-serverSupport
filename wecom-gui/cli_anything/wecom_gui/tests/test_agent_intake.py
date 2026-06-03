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


def test_agent_upgrades_visible_queue_key_to_sidebar_uid(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {
        "title": "客户A",
        "preview": "查订单",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "source": "ocr",
        "click_y": 240.0,
    }
    changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True
    assert item["conversation_key"].startswith("visible:")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "hash1",
            "messages": [{"role": "用户", "content": "查订单", "text": "查订单"}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "wm-visible")

    def fake_draft(messages, customer_name="", customer_uid="", **kwargs):
        return {"ok": True, "text": f"uid={customer_uid}", "message": f"uid={customer_uid}"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        job_id = next(iter(futures))
        assert futures[job_id].result()["text"] == "uid=wm-visible"

    upgraded = state.get_job(job_id)
    assert upgraded["conversation_key"] == "uid:wm-visible"
    assert state.get_job(item["id"])["conversation_key"] == "uid:wm-visible"


def test_agent_creates_welcome_ready_draft_from_system_text(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {
        "title": "三水儿",
        "preview": "你已添加了 三水儿，现在可以开始聊天了。",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "source": "ocr",
        "click_y": 240.0,
    }
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    messages = [
        {"role": "系统", "content": "你已添加了 三水儿，现在可以开始聊天了。", "text": "你已添加了 三水儿，现在可以开始聊天了。"},
        {"role": "系统", "content": "以上是打招呼内容", "text": "以上是打招呼内容"},
    ]
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {"hash": "welcome-hash", "messages": messages},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("welcome should not call AI")),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)

    assert result["supplement"] == 1
    assert futures == {}
    ready = state.list_queue(status="ready")[0]
    assert ready["reply_text"].startswith("您好~可以简单介绍下您的基本信息")
    assert "20.儿童成长" in ready["reply_text"]
    assert ready["reply_source"] == "supplement"
    assert json.loads(ready["context_json"])["latest"]["role"] == "系统"
    supplement = state.get_supplement_state(ready["conversation_key"])
    assert supplement["stage"] == state.SUPPLEMENT_COLLECTING_PROFILE
    assert supplement["pending_next_stage"] == state.SUPPLEMENT_DIGGING_NEED
    stored = state.list_conversation_messages(conversation_key=ready["conversation_key"])
    assert [message["message_type"] for message in stored] == ["system", "system"]


def test_agent_creates_supplement_first_prompt_from_recommendation_intent(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "补剂推荐", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "supp-hash",
            "messages": [{"role": "用户", "content": "补剂推荐", "text": "补剂推荐"}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first prompt should not call AI")),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)

    assert result["supplement"] == 1
    assert futures == {}
    ready = state.list_queue(status="ready")[0]
    assert ready["reply_source"] == "supplement"
    assert ready["reply_text"] == agent.SUPPLEMENT_FIRST_REPLY_WITH_PROFILE
    assert state.get_supplement_state(ready["conversation_key"])["stage"] == state.SUPPLEMENT_COLLECTING_PROFILE
    logs = state.list_supplement_logs()
    assert logs[-1]["event_type"] == "supplement_route_evaluated"


def test_agent_supplement_active_flow_passes_agent_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "最近经常熬夜", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    state.mark_supplement_state(
        item["conversation_key"],
        state.SUPPLEMENT_DIGGING_NEED,
        conversation_key=item["conversation_key"],
        conversation="客户A",
        job_id=item["id"],
        trace_id="trace-1",
        digging_count=1,
        selected_needs=["睡眠质量差"],
        reason="need_selection_ack",
    )

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "dig-hash",
            "messages": [{"role": "用户", "content": "最近经常熬夜", "text": "最近经常熬夜"}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, customer_name="", customer_uid="", agent_mode="", agent_context=None, **kwargs):
        captured["agent_mode"] = agent_mode
        captured["agent_context"] = agent_context
        return {"ok": True, "text": "请问您入睡困难还是容易醒？", "message": "请问您入睡困难还是容易醒？", "action": "clarify"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        job_id = next(iter(futures))
        assert futures[job_id].result()["text"] == "请问您入睡困难还是容易醒？"

    assert captured["agent_mode"] == "supplement"
    assert captured["agent_context"]["trace_id"] == "trace-1"
    assert captured["agent_context"]["selected_needs"] == ["睡眠质量差"]


def test_agent_supplement_profile_opt_out_reaches_agent_context(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "不想提供个人信息", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    state.mark_supplement_state(
        item["conversation_key"],
        state.SUPPLEMENT_DIGGING_NEED,
        conversation_key=item["conversation_key"],
        conversation="客户A",
        job_id=item["id"],
        trace_id="trace-opt-out",
        digging_count=1,
        selected_needs=["睡眠质量差"],
        known_profile={"has_basic_profile": False},
        reason="need_selection_ack",
    )

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "opt-out-hash",
            "messages": [{"role": "用户", "content": "不想提供个人信息，睡眠不好", "text": "不想提供个人信息，睡眠不好"}],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")
    captured = {}

    def fake_draft(messages, customer_name="", customer_uid="", agent_mode="", agent_context=None, **kwargs):
        captured["agent_context"] = agent_context
        return {"ok": True, "text": "请问您是入睡困难还是容易醒？", "message": "请问您是入睡困难还是容易醒？", "action": "clarify"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)
        assert result["drafting"] == 1
        next(iter(futures.values())).result()

    assert captured["agent_context"]["profile_opt_out"] is True
    assert captured["agent_context"]["known_profile"]["profile_opt_out"] is True
    assert captured["agent_context"]["digging_question_policy"]["max_questions_per_reply"] == 1
    saved = state.get_supplement_state(item["conversation_key"])
    assert saved["known_profile"]["profile_opt_out"] is True


def test_agent_skips_duplicate_supplement_prompt_when_already_active(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {
        "title": "三水儿",
        "preview": "你已添加了 三水儿，现在可以开始聊天了。",
        "time": "刚刚",
        "tags": ["@微信"],
        "raw": [],
        "source": "ocr",
        "click_y": 240.0,
    }
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    state.mark_supplement_state(
        item["conversation_key"],
        state.SUPPLEMENT_COLLECTING_PROFILE,
        conversation_key=item["conversation_key"],
        conversation="三水儿",
        job_id=item["id"],
        trace_id="trace-existing",
        reason="first_prompt_ready",
    )

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "welcome-hash",
            "messages": [
                {"role": "系统", "content": "你已添加了 三水儿，现在可以开始聊天了。", "text": "你已添加了 三水儿，现在可以开始聊天了。"}
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = agent._read_one_pending(last=12, executor=executor, futures={}, max_drafts=1)

    assert result["supplement"] == 1
    ready = state.list_queue(status="ready")[0]
    assert ready["reply_source"] == "supplement"
    assert ready["reply_text"] == agent.SUPPLEMENT_FIRST_REPLY_WITH_PROFILE


def test_agent_read_only_logs_context_without_drafting(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    changed, _item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    assert changed is True

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "read-hash",
            "source": "accessibility-chat-table",
            "capture_images": True,
            "messages": [
                {
                    "role": "用户",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_mode": "preview"}],
                }
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")

    def fail_draft(*args, **kwargs):
        raise AssertionError("read-only mode should not call AI")

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fail_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1, read_only=True)

    assert result["read_only"] is True
    assert futures == {}
    job = state.list_queue(status="done")[0]
    assert job["last_message_hash"] == "read-hash"
    assert job["reply_source"] == ""
    assert json.loads(job["context_json"])["read_only"] is True
    stored_messages = state.list_conversation_messages(conversation_key=job["conversation_key"])
    assert stored_messages[0]["text"] == "[图片]"
    events = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["type"] == "agent_read_context" for line in events)
    assert any(json.loads(line)["type"] == "agent_read_only_completed" for line in events)


def test_agent_builds_supplement_prompt_for_new_customer_system_message(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setenv("WECOM_GUI_WELCOME_MESSAGE", "欢迎加入")
    row = {
        "title": "三水儿",
        "preview": "你已添加了三水儿，现在可以开始聊天了。",
        "time": "3分钟前",
        "tags": ["@微信"],
        "raw": ["三水儿", "@微信", "你已添加了三水儿，现在可以开始聊天了。", "3分钟前"],
    }
    state.enqueue_conversation(row, watcher._conversation_signature(row))

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "welcome-hash",
            "source": "accessibility-chat-table",
            "messages": [
                {
                    "role": "system",
                    "content": "你已添加了三水儿，现在可以开始聊天了。",
                    "text": "你已添加了三水儿，现在可以开始聊天了。",
                }
            ],
        },
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")

    def fail_draft(*args, **kwargs):
        raise AssertionError("new-customer supplement prompt should not call the ordinary AI drafter")

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fail_draft)

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {}
        result = agent._read_one_pending(last=12, executor=executor, futures=futures, max_drafts=1)

    assert result["supplement"] == 1
    assert futures == {}
    ready = state.list_queue(status="ready")[0]
    assert ready["reply_text"] == agent.SUPPLEMENT_FIRST_REPLY_WITH_PROFILE
    assert ready["reply_source"] == "supplement"
    assert state.get_supplement_state(ready["conversation_key"])["stage"] == state.SUPPLEMENT_COLLECTING_PROFILE


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
