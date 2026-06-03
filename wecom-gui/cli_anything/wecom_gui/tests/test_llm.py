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


def test_pi_provider_uses_agent_context_customer_key(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "ok": True,
                "mode": "autonomous",
                "codex": {"reply": {"action": "clarify", "reply_text": "最近入睡大概需要多久？"}},
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
        [{"role": "用户", "content": "想改善睡眠"}],
        provider="pi",
        agent_mode="supplement",
        agent_context={"customer_key": "supplement-full-test:abc123", "trace_id": "trace-1"},
    )

    assert captured["cmd"][captured["cmd"].index("--customer-id") + 1] == "supplement-full-test:abc123"
    context = json.loads(captured["cmd"][captured["cmd"].index("--context-json") + 1])
    assert context["customer_id"] == "supplement-full-test:abc123"
    assert context["agent_context"]["customer_key"] == "supplement-full-test:abc123"

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
