from __future__ import annotations

import json
import os
import subprocess
import sys


def _env():
    env = os.environ.copy()
    deps = env.get("CLI_ANYTHING_TEST_DEPS")
    if deps:
        env["PYTHONPATH"] = deps + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_module_help_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "cli_anything.wecom_gui", "--help"],
        text=True,
        capture_output=True,
        env=_env(),
        check=True,
    )

    assert "cli-anything-wecom-gui" in result.stdout or "WeCom desktop" in result.stdout
    assert "doctor" in result.stdout


def test_doctor_json_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "cli_anything.wecom_gui", "--json", "doctor"],
        text=True,
        capture_output=True,
        env=_env(),
        check=True,
    )

    payload = json.loads(result.stdout)
    assert "ok" in payload
    assert "platform" in payload
    assert "notes" in payload
