from __future__ import annotations

import json
import os
import base64
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
        "cli_anything.wecom_gui.core.reply.send_message",
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
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(
            {"text": text, "dry_run": dry_run, "submit": submit}
        )
        or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["sent"] == 1
    assert sent == [{"text": "鱼油建议随餐服用。", "dry_run": False, "submit": True}]
    assert state.list_queue(status="done")[0]["title"] == "客户A"


def test_review_item_exposes_welcome_reply_source_and_system_messages(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "三水儿", "preview": "你已添加了 三水儿，现在可以开始聊天了。", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _changed, item = state.enqueue_conversation(row, watcher._conversation_signature(row))
    trigger = {"role": "系统", "text": row["preview"], "content": row["preview"], "message_type": "system"}
    state.mark_drafting(item["id"], message_hash="welcome-hash", messages=[trigger], latest=trigger)
    state.mark_ready(item["id"], reply_text="{WELCOME_MESSAGE}", reply_source="welcome")

    review_item = review_server.list_review_items(status="ready")[0]

    assert review_item["reply_source"] == "welcome"
    assert review_item["reply_text"] == "{WELCOME_MESSAGE}"
    assert review_item["messages"][0]["role"] == "系统"
    assert review_item["messages"][0]["message_type"] == "system"


def test_supplement_full_test_uses_isolated_log_table(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    payload = review_server.supplement_test_status("客户A", full_flow=True)
    customer_key = payload["customer_key"]
    assert customer_key.startswith("supplement-full-test:")

    state.log_supplement_full_test_event(
        "full_flow_started",
        trace_id="trace-full",
        conversation_key=customer_key,
        customer_id=customer_key,
        conversation="客户A",
        stage=state.SUPPLEMENT_DIGGING_NEED,
        details={"step": "start"},
    )
    state.log_supplement_event(
        "ordinary_started",
        trace_id="trace-normal",
        conversation_key=customer_key,
        customer_id=customer_key,
        conversation="客户A",
        stage=state.SUPPLEMENT_DIGGING_NEED,
        details={"step": "normal"},
    )

    payload = review_server.supplement_test_status("客户A", full_flow=True)
    assert [item["event_type"] for item in payload["logs"]] == ["full_flow_started"]

    reset = review_server.supplement_test_reset("客户A", full_flow=True)
    assert reset["customer_key"] == customer_key
    assert reset["logs"] == []
    assert state.list_supplement_logs(trace_id="trace-normal")[0]["event_type"] == "ordinary_started"


def test_supplement_test_old_user_non_supplement_uses_ordinary_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {
            "ok": True,
            "provider": "csbot-autonomous",
            "text": "您好，发货时间我帮您查一下。",
            "message": "您好，发货时间我帮您查一下。",
            "action": "send",
        }

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    result = review_server.supplement_test_send("老客户A", "你好", full_flow=True)

    assert result["ok"] is True
    assert result["ordinary_agent"] is True
    assert result["reply_text"] == "您好，发货时间我帮您查一下。"
    assert result["state"] == {}
    assert result["job"]["status"] == "done"
    assert result["job"]["reply_source"] == "ai"
    assert captured["kwargs"].get("agent_mode", "") == ""
    assert [message["message_type"] for message in result["messages"]] == ["customer", "reply"]


def test_supplement_full_test_same_customer_keeps_history_and_routes_ordinary_to_ai(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    replies = iter(["您好，我在。", "鱼油发货时间我帮您按订单查询。"])

    def stable_fake_draft(messages, **kwargs):
        text = next(replies)
        return {"ok": True, "provider": "csbot-autonomous", "text": text, "message": text, "action": "send"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", stable_fake_draft)

    first = review_server.supplement_test_send("补剂完整问答客户3", "你好", full_flow=True)
    second = review_server.supplement_test_send("补剂完整问答客户3", "我的鱼油什么时候发货", full_flow=True)

    assert first["ordinary_agent"] is True
    assert second["ordinary_agent"] is True
    assert first["customer_key"] == second["customer_key"]
    assert [message["text"] for message in second["messages"]] == [
        "你好",
        "您好，我在。",
        "我的鱼油什么时候发货",
        "鱼油发货时间我帮您按订单查询。",
    ]
    assert second["state"] == {}
    assert not [item for item in second["logs"] if item["event_type"] == "supplement_state_loaded"]


def test_supplement_test_new_user_gets_fixed_welcome_followup(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    result = review_server.supplement_test_send("新客户A", "", full_flow=True, new_user=True)

    assert result["ok"] is True
    assert result["reply_text"].startswith("您好~可以简单介绍下您的基本信息")
    assert result["state"]["stage"] == state.SUPPLEMENT_COLLECTING_PROFILE
    assert [message["message_type"] for message in result["messages"]] == ["reply", "reply"]
    assert result["messages"][0]["text"].startswith("您好，新客户A")
    assert "营养工厂健康顾问" in result["messages"][0]["text"]
    assert "领产品说明书 https://docs.qq.com/s/tHMpjD9S811JnjY369QC2G" in result["messages"][0]["text"]
    assert result["messages"][1]["text"].startswith("您好~可以简单介绍下您的基本信息")
    assert result["logs"][0]["event_type"] == "supplement_route_evaluated"
    assert result["logs"][0]["details"]["trigger_source"] == "new_user_welcome"
    assert not [item for item in result["logs"] if item["event_type"] == "supplement_backend_agent_started"]


def test_supplement_test_new_user_restart_does_not_call_backend_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    first = review_server.supplement_test_send("新客户B", "", full_flow=True, new_user=True)
    assert first["ok"] is True

    def fake_draft(messages, **kwargs):
        return {"ok": True, "text": "您好，我继续帮您处理。", "message": "您好，我继续帮您处理。", "action": "send"}

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)
    second = review_server.supplement_test_send("新客户B", "", full_flow=True, new_user=True)

    assert second["ok"] is True
    assert second["ordinary_agent"] is True
    assert second["reply_text"] == "您好，我继续帮您处理。"
    assert second["state"]["stage"] == state.SUPPLEMENT_COLLECTING_PROFILE
    assert second["state"]["digging_count"] == 0
    assert not second["messages"][-2]["text"].startswith("您好，新客户B")
    assert second["messages"][-1]["text"] == "您好，我继续帮您处理。"
    assert not [item for item in second["logs"] if item["event_type"] == "supplement_backend_agent_started"]


def test_supplement_test_old_user_explicit_supplement_question_starts_flow_then_uses_backend_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    first = review_server.supplement_test_send("老客户B", "补剂推荐", full_flow=True)

    assert first["ok"] is True
    assert first["reply_text"].startswith("您好~可以简单介绍下您的基本信息")
    assert first["state"]["stage"] == state.SUPPLEMENT_COLLECTING_PROFILE

    captured = {}

    def fake_draft(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {
            "ok": True,
            "provider": "csbot-autonomous",
            "worker": "pi",
            "model": "deepseek-v4-flash",
            "action": "clarify",
            "text": "最近入睡大概需要多久？",
            "message": "最近入睡大概需要多久？",
            "raw": {
                "codex": {
                    "reply": {
                        "action": "clarify",
                        "reply_text": "最近入睡大概需要多久？",
                        "commands_run": ["retrieve --customer-id supplement-full-test:test"],
                        "retrieval_summary": "已走补剂 scoped 检索。",
                        "used_script_sources": [],
                        "used_vector_memories": [],
                        "confidence": 0.7,
                        "decision_basis": "继续挖需",
                        "conflicts": [],
                    }
                }
            },
        }

    monkeypatch.setattr("cli_anything.wecom_gui.core.llm.draft_reply", fake_draft)

    result = review_server.supplement_test_send("老客户B", "2", full_flow=True)

    assert result["ok"] is True
    assert result["reply_text"] == "最近入睡大概需要多久？"
    assert captured["kwargs"]["provider"] == "pi"
    assert captured["kwargs"]["agent_mode"] == agent.SUPPLEMENT_REPLY_SOURCE
    assert captured["kwargs"]["agent_context"]["customer_key"] == result["customer_key"]
    assert [item["event_type"] for item in result["logs"] if item["event_type"].startswith("supplement_backend_agent")] == [
        "supplement_backend_agent_started",
        "supplement_backend_agent_done",
    ]


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

def test_review_regenerate_moves_inactive_jobs_back_to_pending(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    rows = [
        {"title": "客户A", "preview": "被拒绝", "time": "刚刚", "tags": ["@微信"], "raw": []},
        {"title": "客户B", "preview": "失败了", "time": "刚刚", "tags": ["@微信"], "raw": []},
        {"title": "客户C", "preview": "待审核", "time": "刚刚", "tags": ["@微信"], "raw": []},
    ]
    jobs = []
    for index, row in enumerate(rows):
        state.enqueue_conversation(row, f"sig-{index}")
        job = state.claim_pending_for_read()
        state.mark_drafting(
            job["id"],
            message_hash=f"hash-{index}",
            messages=[{"role": "用户", "content": row["preview"]}],
            latest={"role": "用户", "content": row["preview"]},
        )
        state.mark_ready(job["id"], reply_text=f"旧回复{index}")
        jobs.append(job)

    assert review_server.reject_item(jobs[0]["id"])["ok"] is True
    state.mark_failed(jobs[1]["id"], "draft_error")

    for job in jobs:
        result = review_server.regenerate_item(job["id"])
        assert result["ok"] is True
        regenerated = state.get_job(job["id"])
        assert regenerated["status"] == "pending"
        assert regenerated["reply_text"] is None
        assert regenerated["last_message_hash"] is None
        assert regenerated["context_json"] is None
        assert state.list_conversation_messages(conversation_key=job["conversation_key"]) == []

    pending = state.claim_pending_for_read()
    assert pending["id"] in {job["id"] for job in jobs}

def test_review_save_and_approve_uses_final_reply_text(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "鱼油怎么吃", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "鱼油怎么吃", "text": "鱼油怎么吃"}],
        latest={"role": "用户", "content": "鱼油怎么吃"},
    )
    state.mark_ready(job["id"], reply_text="AI草稿")

    saved = review_server.save_item(job["id"], reply_text="客服改过的回复")
    assert saved["ok"] is True
    assert saved["item"]["status"] == "ready"
    assert saved["item"]["reply_text"] == "客服改过的回复"

    approved = review_server.approve_item(job["id"], reply_text="最终确认回复")
    assert approved["ok"] is True
    approved_job = state.get_job(job["id"])
    assert approved_job["status"] == "approved"
    assert approved_job["reply_text"] == "最终确认回复"

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
    assert items[0]["messages"][0]["message_type"] == "customer"
    assert items[0]["messages"][0]["text"] == "最新问题"

def test_review_items_include_reply_message_category(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "新问题", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[
            {"role": "用户", "content": "新问题"},
            {"role": "客服", "content": "旧回复"},
        ],
        latest={"role": "用户", "content": "新问题"},
    )
    state.mark_ready(job["id"], reply_text="审核回复")

    item = review_server.list_review_items(status="ready")[0]

    assert [message["message_type"] for message in item["messages"]] == ["customer", "reply"]

def test_review_status_filters_include_rejected_items(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "退款", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "退款"}],
        latest={"role": "用户", "content": "退款"},
    )
    state.mark_ready(job["id"], reply_text="我帮您转人工。")
    assert review_server.reject_item(job["id"])["ok"] is True

    skipped_items = review_server.list_review_items(status="skipped")
    counts = review_server.review_counts()

    assert len(skipped_items) == 1
    assert skipped_items[0]["status"] == "skipped"
    assert counts["skipped"] == 1

def test_review_approve_can_use_human_edited_reply(monkeypatch, tmp_path):
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
    state.mark_ready(job["id"], reply_text="AI原文")

    result = review_server.approve_item(job["id"], reply_text="客服改写")

    assert result["ok"] is True
    approved = state.get_job(job["id"])
    assert approved["status"] == "approved"
    assert approved["reply_text"] == "客服改写"


def test_review_approve_edited_reply_creates_pending_issue_task(monkeypatch, tmp_path):
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
    state.mark_ready(job["id"], reply_text="AI原文")

    result = review_server.approve_item(job["id"], reply_text="客服改写")

    assert result["ok"] is True
    tasks = review_server.list_review_items(status="issues")
    assert len(tasks) == 1
    assert tasks[0]["source"] == "review_edit"
    assert tasks[0]["original_reply"] == "AI原文"
    assert tasks[0]["final_reply"] == "客服改写"
    assert review_server.review_counts()["issues"] == 1

def test_review_items_classify_customer_reply_and_unknown_messages(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "我想咨询", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[
            {"role": "用户", "content": "我想咨询"},
            {"role": "客服", "content": "您好"},
            {"role": "system", "content": "时间分割线"},
        ],
        latest={"role": "用户", "content": "我想咨询"},
    )
    state.mark_ready(job["id"], reply_text="请问您想了解哪款？")

    item = review_server.list_review_items(status="ready")[0]

    assert [message["message_type"] for message in item["messages"]] == ["customer", "reply", "unknown"]


def test_review_items_include_image_media_payloads_and_failures(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    row = {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[
            {
                "role": "用户",
                "content": "[图片]",
                "media": [
                    {"type": "image", "capture_ok": True, "capture_path": str(image_path)},
                    {"type": "image", "capture_ok": False, "error": "preview_not_found"},
                ],
            }
        ],
        latest={"role": "用户", "content": "[图片]"},
    )
    state.mark_ready(job["id"], reply_text="我看到了图片。")

    message = review_server.list_review_items(status="ready")[0]["messages"][0]

    assert message["media"][0]["capture_ok"] is True
    assert message["media"][0]["url"].startswith("/api/review/media/")
    assert "capture_path" not in message["media"][0]
    assert message["media"][1]["capture_ok"] is False
    assert message["media"][1]["error"] == "preview_not_found"


def test_review_media_route_serves_only_recorded_captured_images(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    row = {"title": "客户A", "preview": "[图片]", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[
            {
                "role": "用户",
                "content": "[图片]",
                "media": [{"type": "image", "capture_ok": True, "capture_path": str(image_path)}],
            }
        ],
        latest={"role": "用户", "content": "[图片]"},
    )
    state.mark_ready(job["id"], reply_text="我看到了图片。")
    message_id = state.list_conversation_messages(conversation_key=job["conversation_key"])[0]["id"]

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with request.urlopen(f"{base}/api/review/media/{message_id}/0", timeout=5) as resp:
            body = resp.read()
            content_type = resp.headers.get("Content-Type")
        assert body.startswith(b"\x89PNG")
        assert content_type == "image/png"

        with pytest.raises(Exception):
            request.urlopen(f"{base}/api/review/media/{message_id}/1", timeout=5)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_review_classifies_white_plush_toy_reply_as_reply(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "这个白色毛绒玩具是什么？", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[
            {"role": "用户", "content": "这个白色毛绒玩具是什么？"},
            {"role": "assistant", "message_type": "reply", "content": "这个白色毛绒玩具看起来是小羊玩偶。"},
            {"role": "用户", "content": "那能买吗？"},
        ],
        latest={"role": "用户", "content": "那能买吗？"},
    )
    state.mark_ready(job["id"], reply_text="我帮您确认一下。")

    item = review_server.list_review_items(status="ready")[0]

    assert item["messages"][1]["message_type"] == "reply"


def test_direct_handoff_enters_handoff_queue_without_ai_draft(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "我要转人工", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "hash-a",
            "messages": [{"role": "用户", "content": "我要转人工", "text": "我要转人工"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI should not draft direct handoff")),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = agent._read_one_pending(last=12, executor=executor, futures={}, max_drafts=1)

    assert result["handoff"] == 1
    item = review_server.list_review_items(status="handoff")[0]
    assert item["handoff_pending"] is True
    assert item["handoff_type"] == "direct"
    assert "转人工" in item["handoff_reason"]
    assert item["reply_text"] == ""


def test_complaint_with_image_enters_handoff_queue_before_ai(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "我要投诉你", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "hash-complaint-image",
            "messages": [
                {
                    "role": "用户",
                    "content": "[图片]",
                    "text": "[图片]",
                    "media": [{"type": "image", "capture_ok": True, "capture_path": "/tmp/customer.png"}],
                },
                {"role": "用户", "content": "我要投诉你", "text": "我要投诉你"},
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI should not draft complaint handoff")),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = agent._read_one_pending(last=12, executor=executor, futures={}, max_drafts=1)

    assert result["handoff"] == 1
    item = review_server.list_review_items(status="handoff")[0]
    assert item["handoff_pending"] is True
    assert item["handoff_type"] == "indirect"
    assert "投诉" in item["handoff_reason"]
    assert item["reply_text"] == ""


def test_indirect_ai_handoff_enters_handoff_queue(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "这个问题很复杂", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "这个问题很复杂"}],
        latest={"role": "用户", "content": "这个问题很复杂"},
    )
    future = mock.Mock()
    future.done.return_value = True
    future.result.return_value = {
        "text": "您好，这个问题我帮您转人工客服确认处理，请您稍等。",
        "action": "handoff",
        "raw": {"codex": {"reply": {"decision_basis": "复杂售后问题"}}},
    }
    futures = {job["id"]: future}
    agent._DRAFT_MESSAGE_HASH[job["id"]] = "hash-a"

    result = agent._finish_drafts(futures)

    assert result["ready"] == 1
    item = review_server.list_review_items(status="handoff")[0]
    assert item["handoff_type"] == "indirect"
    assert item["handoff_reason"] == "复杂售后问题"
    assert item["reply_text"] == ""


def test_markdown_cleanup_for_review_save_approve_and_send(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "说明一下", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "说明一下", "text": "说明一下"}],
        latest={"role": "用户", "content": "说明一下"},
    )
    state.mark_ready(job["id"], reply_text="# 标题\n**重点**\n* 项目一\n> 引用\n```json\n{}\n```\n<tag>内容</tag>")

    saved = review_server.save_item(job["id"], reply_text="## 回复\n**您好**\n* 第一条\n_第二条_\n::debug{bad}")
    assert saved["item"]["reply_text"] == "回复\n您好\n- 第一条\n第二条"
    approved = review_server.approve_item(job["id"], reply_text="**最终**\n+ 清单")
    assert approved["item"]["reply_text"] == "最终\n- 清单"

    reads = iter(
        [
            {"hash": "precheck", "messages": [{"role": "用户", "content": "说明一下", "text": "说明一下"}]},
            {
                "hash": "after",
                "messages": [
                    {"role": "用户", "content": "说明一下", "text": "说明一下"},
                    {"role": "客服", "content": "最终\n- 清单", "text": "最终\n- 清单"},
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
    assert sent == ["最终\n- 清单"]


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


def test_review_page_primary_actions_are_send_only():
    frontend = review_server.REVIEW_HTML + review_server.REVIEW_FRONTEND_JS
    assert "data-action=\"regenerate\"" not in frontend
    assert "data-action=\"reject\"" not in frontend
    assert "data-action=\"save\"" not in frontend
    assert "重新生成" not in frontend
    assert "拒绝</button>" not in frontend
    assert "保存修改" not in frontend
    assert "data-action=\"complete-issue\"" in review_server.REVIEW_HTML


def test_review_http_serves_split_frontend_assets(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with request.urlopen(f"{base}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert "/static/styles.css" in html
        assert "/static/app.js" in html

        with request.urlopen(f"{base}/static/styles.css", timeout=5) as resp:
            css = resp.read().decode("utf-8")
            assert resp.headers["Content-Type"].startswith("text/css")
        assert ".wecom-status" in css

        with request.urlopen(f"{base}/static/app.js", timeout=5) as resp:
            js = resp.read().decode("utf-8")
            assert resp.headers["Content-Type"].startswith("text/javascript")
        assert "/api/review/items" in js
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_review_http_supports_save_approve_with_reply_and_regenerate(monkeypatch, tmp_path):
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
    state.mark_ready(job["id"], reply_text="AI草稿")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        save_req = request.Request(
            f"{base}/api/review/items/{job['id']}/save",
            data=json.dumps({"reply_text": "客服保存稿"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(save_req, timeout=5) as resp:
            saved = json.loads(resp.read().decode("utf-8"))
        assert saved["ok"] is True
        assert state.get_job(job["id"])["reply_text"] == "客服保存稿"

        approve_req = request.Request(
            f"{base}/api/review/items/{job['id']}/approve",
            data=json.dumps({"reply_text": "最终发送稿"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(approve_req, timeout=5) as resp:
            approved = json.loads(resp.read().decode("utf-8"))
        assert approved["ok"] is True
        assert state.get_job(job["id"])["status"] == "approved"
        assert state.get_job(job["id"])["reply_text"] == "最终发送稿"

        state.mark_skipped(job["id"], "manual_reject")
        regenerate_req = request.Request(
            f"{base}/api/review/items/{job['id']}/regenerate",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(regenerate_req, timeout=5) as resp:
            regenerated = json.loads(resp.read().decode("utf-8"))
        assert regenerated["ok"] is True
        pending = state.get_job(job["id"])
        assert pending["status"] == "pending"
        assert pending["reply_text"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

def test_review_http_metrics_summary(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "查订单", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "查订单"}],
        latest={"role": "用户", "content": "查订单"},
    )
    state.mark_ready(job["id"], reply_text="AI草稿", duration_ms=3_000)
    assert review_server.save_item(job["id"], reply_text="客服改写")["ok"] is True

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with request.urlopen(f"{base}/api/review/metrics?since_hours=24", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert payload["ok"] is True
    metrics = payload["metrics"]
    assert metrics["served_users"] == 1
    assert metrics["response_time_buckets"]["0-5s"] == 1
    assert metrics["diagnostics"]["review_saved"] == 1
    assert metrics["diagnostics"]["human_edited_reply"] == 1


def test_review_http_completes_reply_issue_task(monkeypatch, tmp_path):
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
    state.mark_ready(job["id"], reply_text="AI草稿")
    assert review_server.approve_item(job["id"], reply_text="客服改写")["ok"] is True
    task = state.list_reply_issue_tasks(status="pending")[0]

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        complete_req = request.Request(
            f"{base}/api/review/issues/{task['id']}/complete",
            data=json.dumps({"issue_text": "AI没有说明订单号来源"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(complete_req, timeout=5) as resp:
            completed = json.loads(resp.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert completed["ok"] is True
    assert completed["item"]["status"] == "completed"
    assert state.reply_issue_pending_count() == 0
    assert state.list_reply_issue_tasks(status="completed")[0]["issue_text"] == "AI没有说明订单号来源"
    assert state.metrics_summary(since_hours=24)["diagnostics"]["review_reply_issue"] == 1


def test_review_http_wecom_status(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    class FakeStatus:
        ok = True
        app_name = "企业微信"
        app_running = True
        accessibility_ok = True
        osascript_ok = True
        notes = []

    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.doctor_status", lambda: FakeStatus())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.ReviewHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with request.urlopen(f"{base}/api/review/wecom-status", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert payload["ok"] is True
    assert payload["status"]["ok"] is True
    assert payload["status"]["app_name"] == "企业微信"


def test_review_upload_delete_and_approve_image_attachment(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "转人工", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "转人工"}],
        latest={"role": "用户", "content": "转人工"},
    )
    state.mark_handoff_pending(
        job["id"],
        handoff_type="direct",
        handoff_reason="客户要求人工",
        reply_text="",
    )
    image_body = b"\x89PNG\r\n\x1a\n" + b"0" * 32

    uploaded = review_server.upload_attachment(
        job["id"],
        filename="answer.png",
        content_type="image/png",
        data_base64=base64.b64encode(image_body).decode("ascii"),
    )

    assert uploaded["ok"] is True
    attachment = uploaded["item"]["reply_attachments"][0]
    assert attachment["name"] == "answer.png"

    deleted = review_server.delete_attachment(job["id"], attachment["id"])
    assert deleted["ok"] is True
    assert deleted["item"]["reply_attachments"] == []

    uploaded_again = review_server.upload_attachment(
        job["id"],
        filename="answer.png",
        content_type="image/png",
        data_base64=base64.b64encode(image_body).decode("ascii"),
    )
    attachment_id = uploaded_again["item"]["reply_attachments"][0]["id"]
    approved = review_server.approve_item(job["id"], reply_text="", attachment_ids=[attachment_id])

    assert approved["ok"] is True
    approved_job = state.get_job(job["id"])
    assert approved_job["status"] == "approved"
    assert approved_job["reply_text"] == ""
    assert approved_job["reply_attachments"][0]["name"] == "answer.png"

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
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert item["title"] == "客户A"
    assert result["reason"] == "stale_context"
    assert sent == []
    assert state.list_queue(status="skipped")[0]["error"] == "stale_context"

def test_handoff_reply_stays_in_handoff_queue_until_finished(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "转人工", "time": "刚刚", "tags": ["@微信"], "raw": []}
    _, item = state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-handoff",
        messages=[{"role": "用户", "content": "转人工", "text": "转人工"}],
        latest={"role": "用户", "content": "转人工"},
    )
    state.mark_handoff_pending(
        job["id"],
        handoff_type="direct",
        handoff_reason="客户要求人工",
        reply_text="您好，我来帮您处理。",
    )
    assert review_server.approve_item(job["id"], reply_text="人工回复第一条")["ok"] is True

    reads = iter(
        [
            {"hash": "precheck", "messages": [{"role": "用户", "content": "转人工", "text": "转人工"}]},
            {
                "hash": "after-send",
                "messages": [
                    {"role": "用户", "content": "转人工", "text": "转人工"},
                    {"role": "客服", "content": "人工回复第一条", "text": "人工回复第一条"},
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

    assert item["title"] == "客户A"
    assert result["sent"] == 1
    assert sent == ["人工回复第一条"]
    handoff_item = review_server.list_review_items(status="handoff")[0]
    assert handoff_item["handoff_waiting"] is True
    assert handoff_item["handoff_attention"] is False
    assert handoff_item["reply_text"] == "人工回复第一条"
    assert handoff_item["messages"][-1]["message_type"] == "reply"
    assert handoff_item["messages"][-1]["text"] == "人工回复第一条"
    assert review_server.list_review_items(status="issues") == []
    assert state.get_job(job["id"])["reply_source"] == "human"
    assert review_server.save_item(job["id"], reply_text="人工回复第二条")["ok"] is True
    assert review_server.approve_item(job["id"], reply_text="人工回复第二条")["ok"] is True
    state.mark_handoff_waiting(
        job["id"],
        message_hash="hash-second",
        reply_text="人工回复第二条",
        reply_source="human",
    )
    assert review_server.review_counts()["issues"] == 0
    assert review_server.finish_item(job["id"])["ok"] is True
    assert review_server.review_counts()["handoff"] == 0
    assert state.get_job(job["id"])["status"] == "done"

def test_handoff_duplicate_visible_reply_is_not_sent_again(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "转人工", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-handoff",
        messages=[{"role": "用户", "content": "转人工", "text": "转人工"}],
        latest={"role": "用户", "content": "转人工"},
    )
    state.mark_handoff_pending(
        job["id"],
        handoff_type="direct",
        handoff_reason="客户要求人工",
        reply_text="您好，我来处理。",
    )
    assert review_server.approve_item(job["id"], reply_text="人工回复第一条")["ok"] is True

    sent = []
    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=False: {
            "hash": "after-send",
            "messages": [
                {"role": "用户", "content": "转人工", "text": "转人工"},
                {"role": "客服", "content": "人工回复第一条", "text": "人工回复第一条"},
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.reply.send_message",
        lambda text, attachments=None, dry_run=False, submit=True: sent.append(text) or {"ok": True},
    )

    result = agent._send_one_ready(last=12, mode="review")

    assert result["reason"] == "handoff_reply_already_visible"
    assert sent == []
    item = review_server.list_review_items(status="handoff")[0]
    assert item["handoff_waiting"] is True
    assert item["reply_text"] == "人工回复第一条"

def test_handoff_new_message_returns_to_attention_without_ai(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "转人工", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True}
    _, item = state.enqueue_conversation(row, "sig1")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "转人工", "text": "转人工"}],
        latest={"role": "用户", "content": "转人工"},
    )
    state.mark_handoff_pending(
        job["id"],
        handoff_type="direct",
        handoff_reason="客户要求人工",
        reply_text="您好，我来处理。",
    )
    state.mark_handoff_waiting(
        job["id"],
        message_hash="hash-sent",
        reply_text="您好，我来处理。",
        reply_source="human",
    )

    changed, reopened = state.enqueue_conversation(
        {**row, "preview": "我还想问一下", "unread_count": 2},
        "sig2",
    )

    assert item["title"] == "客户A"
    assert changed is True
    assert reopened["status"] == "pending"
    assert reopened["handoff_type"] == "direct"

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "ok": True,
            "hash": "hash-new",
            "messages": [{"role": "用户", "content": "我还想问一下", "text": "我还想问一下"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("handoff should not call AI")),
    )

    result = agent._read_one_pending(last=12, executor=ThreadPoolExecutor(max_workers=1), futures={}, max_drafts=1)

    assert result["handoff"] == 1
    assert review_server.review_counts()["handoff_attention"] == 1
    item = review_server.list_review_items(status="handoff")[0]
    assert item["handoff_attention"] is True
    assert item["reply_text"] == ""
    assert review_server.save_item(job["id"], reply_text="新的人工回复")["ok"] is True
    assert review_server.approve_item(job["id"], reply_text="新的人工回复")["ok"] is True


def test_direct_handoff_enters_review_without_external_notification(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "转人工", "time": "刚刚", "tags": ["@微信"], "raw": []}
    state.enqueue_conversation(row, "sig")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "ok": True,
            "hash": "hash-direct-handoff",
            "messages": [{"role": "用户", "content": "我要转人工", "text": "我要转人工"}],
        },
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = agent._read_one_pending(last=12, executor=executor, futures={}, max_drafts=1)

    assert result["handoff"] == 1
    assert review_server.review_counts()["handoff"] == 1
    item = review_server.list_review_items(status="handoff")[0]
    assert item["title"] == "客户A"
    assert item["handoff_pending"] is True
    assert item["handoff_type"] == "direct"


def test_handoff_waiting_same_hash_does_not_trigger_attention(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "转人工", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True}
    state.enqueue_conversation(row, "sig1")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "转人工", "text": "转人工"}],
        latest={"role": "用户", "content": "转人工"},
    )
    state.mark_handoff_pending(
        job["id"],
        handoff_type="direct",
        handoff_reason="客户要求人工",
        reply_text="您好，我来处理。",
    )
    state.mark_handoff_waiting(
        job["id"],
        message_hash="hash-sent",
        reply_text="您好，我来处理。",
        reply_source="human",
    )
    state.mark_pending(job["id"], "left_sidebar_retry")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "ok": True,
            "hash": "hash-sent",
            "messages": [
                {"role": "用户", "content": "转人工", "text": "转人工"},
                {"role": "客服", "content": "您好，我来处理。", "text": "您好，我来处理。"},
            ],
        },
    )

    result = agent._read_one_pending(last=12, executor=ThreadPoolExecutor(max_workers=1), futures={}, max_drafts=1)

    assert result["reason"] == "handoff_waiting_same_hash"
    assert review_server.review_counts()["handoff_attention"] == 0
    item = review_server.list_review_items(status="handoff")[0]
    assert item["handoff_waiting"] is True


def test_handoff_latest_service_message_stays_open_without_ai_or_skip(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    row = {"title": "客户A", "preview": "人工回复", "time": "刚刚", "tags": ["@微信"], "raw": [], "unread": True}
    state.enqueue_conversation(row, "sig1")
    job = state.claim_pending_for_read()
    state.mark_drafting(
        job["id"],
        message_hash="hash-a",
        messages=[{"role": "用户", "content": "转人工", "text": "转人工"}],
        latest={"role": "用户", "content": "转人工"},
    )
    state.mark_handoff_pending(
        job["id"],
        handoff_type="direct",
        handoff_reason="客户要求人工",
        reply_text="您好，我来处理。",
    )
    state.mark_handoff_waiting(
        job["id"],
        message_hash="hash-sent",
        reply_text="您好，我来处理。",
        reply_source="human",
    )
    state.mark_pending(job["id"], "left_sidebar_retry")

    monkeypatch.setattr("cli_anything.wecom_gui.core.inbox.open_row", lambda job: None)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "ok": True,
            "hash": "hash-service-latest",
            "messages": [
                {"role": "用户", "content": "转人工", "text": "转人工"},
                {"role": "客服", "content": "您好，我来处理。", "text": "您好，我来处理。"},
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.llm.draft_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("handoff should not call AI")),
    )

    result = agent._read_one_pending(last=12, executor=ThreadPoolExecutor(max_workers=1), futures={}, max_drafts=1)

    assert result["handoff"] == 0
    assert state.get_job(job["id"])["status"] == "ready"
    assert review_server.list_review_items(status="handoff")[0]["handoff_waiting"] is True
