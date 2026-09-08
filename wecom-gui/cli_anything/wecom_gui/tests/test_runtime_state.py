from __future__ import annotations

import time

from cli_anything.wecom_gui.core import runtime_state, state


def test_runtime_processes_keep_independent_redacted_state(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)

    runtime_state.report(
        "edge_channel", status="waiting", phase="direction_confirmation",
        conversation_label="会话-001", direction="unknown", rationale="user_candidate",
        metrics={"queue_depth": 2, "message": "must-not-store", "credential": "must-not-store"},
    )
    runtime_state.report("ai_reply", status="running", phase="drafting", conversation_label="会话-002")

    snapshot = runtime_state.snapshot()
    states = {item["process"]: item for item in snapshot["states"]}
    assert states["edge_channel"]["phase"] == "direction_confirmation"
    assert states["ai_reply"]["phase"] == "drafting"
    assert states["edge_channel"]["metrics"] == {"queue_depth": 2}
    assert len(snapshot["events"]) == 2


def test_runtime_snapshot_prunes_expired_events(monkeypatch, tmp_path):
    monkeypatch.setattr("cli_anything.wecom_gui.core.state.state_dir", lambda: tmp_path)
    runtime_state.report("edge_channel", status="idle", phase="ready")
    with state.connect() as conn:
        conn.execute("UPDATE runtime_activity_event SET expires_at = ?", (time.time() - 1,))

    assert runtime_state.snapshot()["events"] == []
