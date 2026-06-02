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
