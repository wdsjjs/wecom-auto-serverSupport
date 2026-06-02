from __future__ import annotations

import json

from click.testing import CliRunner

from cli_anything.wecom_gui.core import agent_input
from cli_anything.wecom_gui.wecom_gui_cli import cli


def test_build_current_agent_input_exports_payload_without_ai(monkeypatch):
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=12, capture_images=True: {
            "hash": "read-hash",
            "source": "accessibility-chat-table",
            "capture_images": capture_images,
            "messages": [
                {"role": "客服", "content": "请问想改善哪方面？", "text": "请问想改善哪方面？"},
                {"role": "用户", "content": "想推荐补剂", "text": "想推荐补剂"},
            ],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "客户A"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "wm-1")

    payload = agent_input.build_current_agent_input(last=12)

    assert payload["read"]["hash"] == "read-hash"
    assert payload["agent_input"]["customer_name"] == "客户A"
    assert payload["agent_input"]["customer_uid"] == "wm-1"
    assert payload["csbot_input"]["customer_id"] == "wm-1"
    assert payload["csbot_input"]["query"] == "想推荐补剂"
    assert payload["csbot_input"]["context"]["messages"][-1]["text"] == "想推荐补剂"


def test_agent_input_cli_writes_json_file(monkeypatch, tmp_path):
    output_path = tmp_path / "agent-input.json"
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "cli_anything.wecom_gui.core.chat.read_current",
        lambda last=8, capture_images=False: {
            "hash": "cli-hash",
            "source": "accessibility-visible-chat-text",
            "capture_images": capture_images,
            "messages": [{"role": "用户", "content": "我是三水儿", "text": "我是三水儿"}],
        },
    )
    monkeypatch.setattr(
        "cli_anything.wecom_gui.utils.macos_backend.selected_conversation_row",
        lambda limit=30: {"title": "三水儿"},
    )
    monkeypatch.setattr("cli_anything.wecom_gui.utils.macos_backend.current_external_user_id", lambda: "")

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "agent-input",
            "--last",
            "8",
            "--no-capture-images",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["read"]["hash"] == "cli-hash"
    assert saved["agent_input"]["customer_name"] == "三水儿"
    assert saved["csbot_input"]["query"] == "我是三水儿"
