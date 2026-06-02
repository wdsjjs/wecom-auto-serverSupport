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
    monkeypatch.setattr("cli_anything.wecom_gui.core.reply.send_text", lambda text, dry_run=False, submit=True: sent.append(text))

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
