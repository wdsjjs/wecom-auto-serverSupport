from __future__ import annotations

from types import SimpleNamespace

from cli_anything.wecom_gui.core import supervisor


def test_supervisor_does_not_create_a_second_controlled_session(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "RUN_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "_screen_available", lambda: True)
    monkeypatch.setattr(supervisor, "_session_running", lambda session: session == "wecom-edge-channel")
    monkeypatch.setattr(supervisor.runtime_state, "record_supervisor", lambda *args, **kwargs: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(supervisor.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))

    result = supervisor.start("edge")

    assert result["changed"] is False
    assert result["reason"] == "already_running"
    # Reading the controlled screen PID is allowed; no screen creation occurs.
    assert calls == [["screen", "-ls"]]


def test_supervisor_stops_only_its_named_screen_session(monkeypatch):
    monkeypatch.setattr(supervisor, "_screen_available", lambda: True)
    monkeypatch.setattr(supervisor, "_session_running", lambda session: session == "wecom-edge-channel")
    monkeypatch.setattr(supervisor, "_session_pid", lambda session: 123)
    monkeypatch.setattr(supervisor, "_descendant_pids", lambda pid: [124, 125])
    terminated: list[int] = []
    monkeypatch.setattr(supervisor, "_terminate_descendants", lambda pids: terminated.extend(pids))
    monkeypatch.setattr(supervisor.runtime_state, "record_supervisor", lambda *args, **kwargs: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(supervisor.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    reports: list[dict] = []
    monkeypatch.setattr(supervisor.runtime_state, "report", lambda *args, **kwargs: reports.append(kwargs))

    result = supervisor.stop("edge")

    assert result["changed"] is True
    assert calls == [["screen", "-S", "wecom-edge-channel", "-X", "quit"]]
    assert terminated == [124, 125]
    assert reports == [{"status": "stopped", "phase": "supervisor_stopped"}]
