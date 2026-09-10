"""Controlled lifecycle manager for the local WeCom edge channel."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from cli_anything.wecom_gui.core import runtime_state


ROOT_DIR = Path(__file__).resolve().parents[3]
RUN_DIR = ROOT_DIR / ".codex-run"
SERVICES = {
    "edge": {"session": "wecom-edge-channel", "process": "edge_channel", "log": "wecom-edge-channel.log"},
}


def _screen_available() -> bool:
    return shutil.which("screen") is not None


def _session_running(session: str) -> bool:
    if not _screen_available():
        return False
    result = subprocess.run(["screen", "-ls"], text=True, capture_output=True, check=False)
    return f".{session}" in f"{result.stdout}\n{result.stderr}"


def _session_pid(session: str) -> int | None:
    if not _screen_available():
        return None
    result = subprocess.run(["screen", "-ls"], text=True, capture_output=True, check=False)
    for token in f"{result.stdout}\n{result.stderr}".split():
        if f".{session}" not in token:
            continue
        raw_pid = token.split(".", 1)[0]
        if raw_pid.isdigit():
            return int(raw_pid)
    return None


def _descendant_pids(root_pid: int | None) -> list[int]:
    """Return only processes currently descended from this supervisor screen."""
    if not root_pid:
        return []
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid="], text=True, capture_output=True, check=False,
    )
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        pid, parent_pid = (int(part) for part in parts)
        children.setdefault(parent_pid, []).append(pid)
    descendants: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def _terminate_descendants(pids: list[int]) -> None:
    for pid in reversed(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue


def _command(service: str) -> str:
    python = os.environ.get("WECOM_GUI_PYTHON", sys.executable)
    python_path = os.environ.get("WECOM_GUI_PYTHONPATH", "")
    prefix = f"PYTHONPATH={shlex.quote(python_path)} " if python_path else ""
    if service == "edge":
        args = [python, "-u", "-m", "cli_anything.wecom_gui", "edge-channel", "run", "--poll", os.environ.get("WECOM_EDGE_CHANNEL_POLL", "1"), "--inbox-limit", os.environ.get("WECOM_EDGE_CHANNEL_INBOX_LIMIT", "8"), "--last", os.environ.get("WECOM_EDGE_CHANNEL_LAST", "20")]
    else:
        raise ValueError("service must be edge")
    return f"cd {shlex.quote(str(ROOT_DIR))} && {prefix}{' '.join(shlex.quote(str(item)) for item in args)}"


def status() -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    services = []
    runtime_states = {item["process"]: item for item in runtime_state.snapshot(limit=1)["states"]}
    records = {item["service_name"]: item for item in runtime_state.supervisor_snapshot()}
    for name, spec in SERVICES.items():
        log_path = RUN_DIR / spec["log"]
        process_state = runtime_states.get(spec["process"])
        running = _session_running(spec["session"])
        updated_at = float((process_state or {}).get("updated_at") or 0)
        services.append({
            "service": name,
            "process": spec["process"],
            "running": running,
            "session": spec["session"],
            "pid": _session_pid(spec["session"]),
            "log_path": str(log_path),
            "health": "healthy" if running and time.time() - updated_at < 90 else "stale" if running else "stopped",
            "runtime": process_state,
            "last_action": (records.get(name) or {}).get("last_action", ""),
            "last_result": (records.get(name) or {}).get("last_result", ""),
            "command_line": (records.get(name) or {}).get("command_line", ""),
        })
    return {"ok": True, "services": services, "screen_available": _screen_available()}


def start(service: str) -> dict:
    if service not in SERVICES:
        raise ValueError("service must be edge")
    if not _screen_available():
        raise RuntimeError("screen is required")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    spec = SERVICES[service]
    if _session_running(spec["session"]):
        runtime_state.record_supervisor(
            service, process=spec["process"], screen_session=spec["session"], pid=_session_pid(spec["session"]),
            command_line=_command(service), log_path=str(RUN_DIR / spec["log"]), action="start", result="already_running",
        )
        return {"ok": True, "service": service, "changed": False, "reason": "already_running"}
    log_path = RUN_DIR / spec["log"]
    log_path.touch(exist_ok=True)
    command = f"{_command(service)} >> {shlex.quote(str(log_path))} 2>&1"
    result = subprocess.run(["screen", "-dmS", spec["session"], "zsh", "-lc", command], text=True, capture_output=True, check=False)
    if result.returncode:
        runtime_state.record_supervisor(
            service, process=spec["process"], screen_session=spec["session"], pid=None,
            command_line=_command(service), log_path=str(log_path), action="start", result="failed",
        )
        runtime_state.report(spec["process"], status="failed", phase="supervisor_start_failed", error_code="screen_start_failed")
        raise RuntimeError((result.stderr or result.stdout or "screen start failed").strip())
    runtime_state.report(spec["process"], status="running", phase="supervisor_started")
    runtime_state.record_supervisor(
        service, process=spec["process"], screen_session=spec["session"], pid=_session_pid(spec["session"]),
        command_line=_command(service), log_path=str(log_path), action="start", result="started",
    )
    return {"ok": True, "service": service, "changed": True, "log_path": str(log_path)}


def stop(service: str) -> dict:
    if service not in SERVICES:
        raise ValueError("service must be edge")
    spec = SERVICES[service]
    if not _session_running(spec["session"]):
        runtime_state.report(spec["process"], status="stopped", phase="supervisor_stopped")
        runtime_state.record_supervisor(
            service, process=spec["process"], screen_session=spec["session"], pid=None,
            command_line=_command(service), log_path=str(RUN_DIR / spec["log"]), action="stop", result="already_stopped",
        )
        return {"ok": True, "service": service, "changed": False, "reason": "already_stopped"}
    descendants = _descendant_pids(_session_pid(spec["session"]))
    result = subprocess.run(["screen", "-S", spec["session"], "-X", "quit"], text=True, capture_output=True, check=False)
    if result.returncode:
        runtime_state.record_supervisor(
            service, process=spec["process"], screen_session=spec["session"], pid=_session_pid(spec["session"]),
            command_line=_command(service), log_path=str(RUN_DIR / spec["log"]), action="stop", result="failed",
        )
        raise RuntimeError((result.stderr or result.stdout or "screen stop failed").strip())
    _terminate_descendants(descendants)
    runtime_state.report(spec["process"], status="stopped", phase="supervisor_stopped")
    runtime_state.record_supervisor(
        service, process=spec["process"], screen_session=spec["session"], pid=None,
        command_line=_command(service), log_path=str(RUN_DIR / spec["log"]), action="stop", result="stopped",
    )
    return {"ok": True, "service": service, "changed": True}


def restart(service: str) -> dict:
    stopped = stop(service)
    started = start(service)
    return {"ok": True, "service": service, "stopped": stopped, "started": started}


def action(service: str, verb: str) -> dict:
    targets = tuple(SERVICES) if service == "all" else (service,)
    if verb == "status":
        return status()
    handler = {"start": start, "stop": stop, "restart": restart}.get(verb)
    if handler is None:
        raise ValueError("verb must be start, stop, restart, or status")
    return {"ok": True, "results": [handler(target) for target in targets]}
