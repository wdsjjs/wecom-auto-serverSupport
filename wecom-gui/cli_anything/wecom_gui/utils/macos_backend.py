"""macOS GUI backend for driving WeCom through Accessibility and AppleScript.

This backend intentionally exposes low-level primitives. Higher-level modules
own policy decisions such as whether a reply may be sent automatically.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import json
import time
import uuid
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_NAMES = ("企业微信", "WeCom", "WeChat Work")
DEFAULT_TAG_MARKERS = ("@微信", "外部", "部门", "BOT")
NAVIGATION_ROW_TITLES = {"单聊", "群聊", "@我", "未读", "内部聊天"}
_AX_SCAN_DISABLED_UNTIL = 0.0


def _append_event(event: dict) -> None:
    try:
        from cli_anything.wecom_gui.core import state

        state.append_event(event)
    except Exception:
        return


def _required_tags() -> list[str]:
    raw = os.environ.get("WECOM_GUI_REQUIRED_TAGS", "").strip()
    if not raw:
        raw = os.environ.get("WECOM_GUI_REQUIRED_TAG", "@微信").strip()
    tags = [part.strip() for part in raw.replace("，", ",").split(",")]
    return [tag for tag in tags if tag]


def tag_markers() -> tuple[str, ...]:
    """Return tag markers to preserve while parsing conversation rows."""
    markers = list(DEFAULT_TAG_MARKERS)
    for required_tag in reversed(_required_tags()):
        if required_tag and required_tag not in markers:
            markers.insert(0, required_tag)
    return tuple(markers)


def _applescript_tag_condition(variable_name: str = "rowText") -> str:
    markers = [marker.replace('"', '\\"') for marker in tag_markers()]
    return " or ".join(f'{variable_name} contains "{marker}"' for marker in markers)

MAIN_WINDOW_SCRIPT = '''
        set targetWindow to missing value
        repeat with w in windows
          try
            if (count of UI elements of w) > 1 then
              set targetWindow to w
              exit repeat
            end if
          end try
        end repeat
        if targetWindow is missing value then
          if not (exists window 1) then return ""
          set targetWindow to window 1
        end if
'''


@dataclass(frozen=True)
class BackendStatus:
    ok: bool
    platform: str
    app_name: str | None
    app_running: bool
    accessibility_ok: bool
    osascript_ok: bool
    tesseract_ok: bool
    notes: list[str]


def candidate_app_names() -> list[str]:
    """Return app names to try, with env override first."""
    configured = os.environ.get("WECOM_GUI_APP_NAME", "").strip()
    names: list[str] = []
    if configured:
        names.append(configured)
    names.extend(name for name in DEFAULT_APP_NAMES if name not in names)
    return names


def resolve_app_name(app_name: str | None = None) -> str | None:
    """Resolve the app name, trusting env/explicit values before probing."""
    if app_name:
        return app_name
    configured = os.environ.get("WECOM_GUI_APP_NAME", "").strip()
    if configured:
        return configured
    return find_running_app()


def run_osascript(script: str, *, check: bool = True) -> str:
    """Run an AppleScript snippet and return stdout."""
    args: list[str] = ["osascript"]
    for line in script.strip().splitlines():
        args.extend(["-e", line])
    try:
        timeout = float(os.environ.get("WECOM_GUI_OSASCRIPT_TIMEOUT", "8"))
    except ValueError:
        timeout = 8.0
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("osascript timed out while reading the WeCom accessibility tree") from exc
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "osascript failed")
    return proc.stdout.strip()


def is_macos() -> bool:
    return platform.system() == "Darwin"


def find_running_app() -> str | None:
    """Find the first configured WeCom process currently visible to System Events."""
    if not is_macos() or not shutil.which("osascript"):
        return None
    for app_name in candidate_app_names():
        script = f'''
        tell application "System Events"
          if exists process "{app_name}" then
            return "yes"
          else
            return "no"
          end if
        end tell
        '''
        try:
            if run_osascript(script) == "yes":
                return app_name
        except RuntimeError:
            continue
    return None


def activate_app(app_name: str | None = None) -> dict:
    """Activate WeCom by name."""
    chosen = app_name or find_running_app()
    if not chosen:
        raise RuntimeError("WeCom is not running; refusing to launch it automatically.")
    script = f'''
    tell application "System Events"
      if exists process "{chosen}" then
        set frontmost of process "{chosen}" to true
      else
        error "WeCom is not running"
      end if
    end tell
    return "{chosen}"
    '''
    activated = run_osascript(script)
    return {"ok": True, "app_name": activated or chosen}


def check_accessibility(app_name: str | None = None) -> bool:
    """Return whether UI scripting can inspect the app process."""
    chosen = app_name or find_running_app() or candidate_app_names()[0]
    script = f'''
    tell application "System Events"
      if exists process "{chosen}" then
        tell process "{chosen}"
          set _name to name
        end tell
        return "yes"
      else
        return "no"
      end if
    end tell
    '''
    try:
        return run_osascript(script) == "yes"
    except RuntimeError:
        return False


def doctor_status() -> BackendStatus:
    """Inspect local prerequisites."""
    notes: list[str] = []
    osascript_ok = shutil.which("osascript") is not None
    if not is_macos():
        notes.append("Only macOS is implemented in this MVP.")
    if not osascript_ok:
        notes.append("osascript was not found.")
    app_name = find_running_app()
    if app_name is None:
        notes.append("WeCom is not running or its app name differs. Set WECOM_GUI_APP_NAME.")
    accessibility_ok = bool(app_name and check_accessibility(app_name))
    if app_name and not accessibility_ok:
        notes.append("Grant Accessibility permission to Terminal/Codex in System Settings.")
    tesseract_ok = shutil.which("tesseract") is not None
    if not tesseract_ok:
        notes.append("tesseract not found; OCR fallback is unavailable.")
    ok = is_macos() and osascript_ok and bool(app_name) and accessibility_ok
    return BackendStatus(
        ok=ok,
        platform=platform.system(),
        app_name=app_name,
        app_running=app_name is not None,
        accessibility_ok=accessibility_ok,
        osascript_ok=osascript_ok,
        tesseract_ok=tesseract_ok,
        notes=notes,
    )


def visible_text(app_name: str | None = None) -> list[str]:
    """Return visible static text from the front WeCom window.

    AppleScript UI recursion is intentionally capped by using the front window's
    entire contents. Some WeCom builds expose message bubbles as static text;
    others require OCR in a later backend.
    """
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError(
            "WeCom is not running or its app name differs. "
            "Start WeCom or set WECOM_GUI_APP_NAME."
        )
    if os.environ.get("WECOM_GUI_ACTIVATE_BEFORE_SCAN", "0") == "1":
        activate_app(chosen)
    script = f'''
    tell application "System Events"
      tell process "{chosen}"
{MAIN_WINDOW_SCRIPT}
        set outText to ""

        -- WeCom's conversation list does not reliably expand through
        -- `entire contents of window 1`, so first walk the known sidebar table.
        try
          set convTable to UI element 1 of UI element 4 of UI element 28 of splitter group 1 of targetWindow
          repeat with r from 1 to count of rows of convTable
            try
              set cellObj to UI element 1 of row r of convTable
              repeat with j from 1 to count of UI elements of cellObj
                set elem to UI element j of cellObj
                try
                  set roleName to role of elem as text
                  if roleName is "AXStaticText" or roleName is "AXTextField" or roleName is "AXTextArea" then
                    set valueText to ""
                    try
                      set valueText to value of elem as text
                    end try
                    if valueText is "" then
                      try
                        set valueText to name of elem as text
                      end try
                    end if
                    if valueText is not "" and valueText is not "missing value" then set outText to outText & valueText & linefeed
                  end if
                end try
              end repeat
            end try
          end repeat
        end try

        -- Generic fallback for other visible text regions, including the
        -- right-side chat pane when it is exposed by Accessibility.
        repeat with elem in UI elements of targetWindow
          try
            set roleName to role of elem as text
            if roleName is "AXStaticText" or roleName is "AXTextField" or roleName is "AXTextArea" then
              set valueText to ""
              try
                set valueText to value of elem as text
              end try
              if valueText is not "" then set outText to outText & valueText & linefeed
            end if
          end try
        end repeat
        return outText
      end tell
    end tell
    '''
    raw = run_osascript(script)
    lines = [line.strip() for line in raw.splitlines()]
    return [line for line in lines if line]


def visible_accessibility_text(app_name: str | None = None) -> list[str]:
    """Return deep accessibility text, including sidebar WebView content."""
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError(
            "WeCom is not running or its app name differs. "
            "Start WeCom or set WECOM_GUI_APP_NAME."
        )
    if os.environ.get("WECOM_GUI_ACTIVATE_BEFORE_SCAN", "0") == "1":
        activate_app(chosen)
    rows = _swift_ax("texts")
    texts: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
    if texts:
        extra = _deep_visible_text_applescript(chosen)
        for text in extra:
            if text not in texts:
                texts.append(text)
        return texts
    return _deep_visible_text_applescript(chosen) or visible_text(chosen)


def _deep_visible_text_applescript(chosen: str) -> list[str]:
    script = f'''
    tell application "System Events"
      tell process "{chosen}"
{MAIN_WINDOW_SCRIPT}
        set outText to ""
        try
          repeat with elem in entire contents of targetWindow
            set valueText to ""
            try
              set valueText to value of elem as text
            end try
            if valueText is "" or valueText is "missing value" then
              try
                set valueText to description of elem as text
              end try
            end if
            if valueText is "" or valueText is "missing value" then
              try
                set valueText to name of elem as text
              end try
            end if
            if valueText is not "" and valueText is not "missing value" then
              set outText to outText & valueText & linefeed
            end if
          end repeat
        end try
        return outText
      end tell
    end tell
    '''
    raw = run_osascript(script)
    lines = [line.strip() for line in raw.splitlines()]
    texts: list[str] = []
    for line in lines:
        if line and line not in texts:
            texts.append(line)
    return texts


def extract_wecom_external_user_id(texts: list[str]) -> str:
    """Extract a WeCom external_userid displayed by the debug sidebar."""
    for text in texts:
        match = re.search(r"\b(w[mo][a-zA-Z0-9_-]{8,})\b", text)
        if match:
            return match.group(1)
    return ""


def current_external_user_id(app_name: str | None = None) -> str:
    """Read current external_userid from visible WeCom sidebar debug content."""
    try:
        identity = sidebar_identity(app_name)
        uid = str(identity.get("external_user_id") or identity.get("external_userid") or "").strip()
        if uid:
            return uid
    except Exception:
        pass
    return extract_wecom_external_user_id(visible_accessibility_text(app_name))


def _swift_ax_sdkroot() -> str:
    """Return a Swift SDKROOT that works with the local command-line Swift toolchain."""
    def usable(path_text: str) -> bool:
        path = Path(path_text)
        if not path.exists():
            return False
        match = re.fullmatch(r"MacOSX(\d+(?:\.\d+)*)\.sdk", path.name)
        if not match:
            return True
        version = tuple(int(part) for part in match.group(1).split("."))
        return not version or version[0] < 26

    configured = os.environ.get("WECOM_GUI_AX_SDKROOT", "").strip()
    if configured and Path(configured).exists():
        return configured
    existing = os.environ.get("SDKROOT", "").strip()
    if existing and usable(existing):
        return existing
    sdk_dir = Path("/Library/Developer/CommandLineTools/SDKs")
    if not sdk_dir.exists():
        return ""
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for path in sdk_dir.glob("MacOSX*.sdk"):
        match = re.fullmatch(r"MacOSX(\d+(?:\.\d+)*)\.sdk", path.name)
        if not match:
            continue
        version = tuple(int(part) for part in match.group(1).split("."))
        if version and version[0] >= 26:
            continue
        candidates.append((version, path))
    if not candidates:
        return ""
    return str(max(candidates, key=lambda item: item[0])[1])


def _swift_ax_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("WECOM_GUI_BUNDLE_ID", "com.tencent.WeWorkMac")
    sdkroot = _swift_ax_sdkroot()
    if sdkroot:
        env["SDKROOT"] = sdkroot
    return env


def _swift_ax(command: str | list[str]) -> list[dict]:
    """Read WeCom's accessibility tree through AXUIElement, not OCR."""
    global _AX_SCAN_DISABLED_UNTIL
    if shutil.which("swift") is None:
        return []
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "ax_wecom.swift"
    if not script_path.exists():
        return []
    env = _swift_ax_env()
    args = command.split(" ") if isinstance(command, str) else command
    scan_commands = {"geometry", "rows", "selected-row", "chat", "chat-all", "texts"}
    circuit_breaker_commands = {"geometry", "rows", "selected-row", "texts"}
    is_scan_command = bool(args and args[0] in scan_commands)
    uses_scan_circuit = bool(args and args[0] in circuit_breaker_commands)
    if uses_scan_circuit and time.monotonic() < _AX_SCAN_DISABLED_UNTIL:
        return [{"ok": False, "error": "swift_ax_scan_circuit_open", "command": " ".join(args)}]
    command_timeouts = {
        "chat": os.environ.get("WECOM_GUI_AX_CHAT_TIMEOUT", "8"),
        "chat-all": os.environ.get("WECOM_GUI_AX_CHAT_ALL_TIMEOUT", os.environ.get("WECOM_GUI_AX_CHAT_TIMEOUT", "8")),
        "geometry": os.environ.get("WECOM_GUI_AX_GEOMETRY_TIMEOUT", "5"),
    }
    default_timeout = command_timeouts.get(args[0], "2" if args and args[0] in scan_commands else "8") if args else "8"
    timeout = float(os.environ.get("WECOM_GUI_AX_TIMEOUT", default_timeout))
    runner = _swift_ax_runner(script_path)
    try:
        proc = subprocess.run(
            [*runner, *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        if uses_scan_circuit:
            cooldown = float(os.environ.get("WECOM_GUI_AX_SCAN_TIMEOUT_COOLDOWN", "30"))
            _AX_SCAN_DISABLED_UNTIL = time.monotonic() + max(0.0, cooldown)
        return [{"ok": False, "error": "swift_ax_timeout", "command": " ".join(args), "timeout": timeout}]
    if proc.returncode != 0:
        return []
    items: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _swift_ax_runner(script_path: Path) -> list[str]:
    """Return a fast command for the AX helper, compiling it once when possible."""
    configured = os.environ.get("WECOM_GUI_AX_HELPER", "").strip()
    if configured:
        return [configured]
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return ["swift", str(script_path)]

    binary_path = Path(__file__).resolve().parents[3] / ".codex-run" / "ax_wecom"
    try:
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        source_mtime = script_path.stat().st_mtime
        binary_mtime = binary_path.stat().st_mtime if binary_path.exists() else 0
        if binary_mtime >= source_mtime:
            return [str(binary_path)]
        compile_timeout = float(os.environ.get("WECOM_GUI_AX_COMPILE_TIMEOUT", "45"))
        proc = subprocess.run(
            [swiftc, "-O", str(script_path), "-o", str(binary_path)],
            text=True,
            capture_output=True,
            timeout=compile_timeout,
            check=False,
            env=_swift_ax_env(),
        )
        if proc.returncode == 0 and binary_path.exists():
            return [str(binary_path)]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ["swift", str(script_path)]


def send_via_ax_text_input(text: str, *, submit: bool = True) -> dict:
    """Set the right-side chat input through AXUIElement and optionally submit."""
    command = "send" if submit else "stage"
    ready_items = _swift_ax("send-ready")
    ready = ready_items[-1] if ready_items else {"ok": False, "error": "input_ready_unavailable"}
    _append_event(
        {
            "type": "wecom_send_input_preflight",
            "ok": bool(ready.get("ok")),
            "error": ready.get("error") or "",
            "sidebar": ready.get("sidebar") if isinstance(ready.get("sidebar"), dict) else {},
            "input": ready.get("input") if isinstance(ready.get("input"), dict) else {},
        }
    )
    if not ready.get("ok"):
        raise RuntimeError(f"AX text input not ready: {ready}")
    items: list[dict] = []
    attempts = max(1, int(os.environ.get("WECOM_GUI_AX_SEND_ATTEMPTS", "2")))
    retry_delay = float(os.environ.get("WECOM_GUI_AX_SEND_RETRY_DELAY", "0.25"))
    for attempt in range(attempts):
        items = _swift_ax([command, text])
        if items:
            break
        if attempt < attempts - 1:
            time.sleep(retry_delay)
    if not items:
        raise RuntimeError("AX text input send failed: no result")
    result = items[-1]
    if not result.get("ok"):
        raise RuntimeError(f"AX text input send failed: {result}")
    return result


def send_input_ready(app_name: str | None = None) -> dict:
    """Check the chat input without activating WeCom or opening its sidebar."""
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError("WeCom is not running or its app name differs. Start WeCom or set WECOM_GUI_APP_NAME.")
    items = _swift_ax("send-ready")
    result = items[-1] if items else {"ok": False, "error": "chat_input_not_found"}
    _append_event({"type": "wecom_send_ready", "ok": bool(result.get("ok")), "error": result.get("error") or ""})
    return result


def window_geometry() -> dict:
    """Return WeCom window, screen, sidebar frame, and adaptive scroll point."""
    items = _swift_ax("geometry")
    if items and items[-1].get("ok"):
        return items[-1]
    return {"ok": False, "source": "unavailable"}


def normalize_window(*, mode: str | None = None) -> dict:
    """Normalize WeCom window geometry through AX fullscreen or maximized fallback."""
    selected_mode = (mode or os.environ.get("WECOM_GUI_WINDOW_MODE", "fullscreen")).strip() or "fullscreen"
    items = _swift_ax(["normalize", selected_mode])
    if items and items[-1].get("ok"):
        return items[-1]
    chosen = find_running_app()
    if chosen:
        activate_app(chosen)
    return {"ok": False, "fullscreen": False, "fallbackMaximized": False, "error": "ax_normalize_failed"}


def _swift_ax_open(title: str) -> bool:
    items = _swift_ax(["open", title])
    return bool(items and items[-1].get("ok"))


def _adaptive_scroll_point() -> tuple[int, int]:
    geometry = window_geometry()
    point = geometry.get("scrollPoint") if isinstance(geometry, dict) else None
    if isinstance(point, dict):
        try:
            x = int(float(point.get("x")))
            y = int(float(point.get("y")))
            if x > 0 and y > 0:
                return x, y
        except (TypeError, ValueError):
            pass
    raise RuntimeError(f"WeCom sidebar frame unavailable for adaptive scroll: {geometry}")


def scroll_sidebar(direction: str = "down", *, ticks: int = 6, x: int | None = None, y: int | None = None) -> dict:
    """Scroll the WeCom sidebar at an adaptive AX-derived point."""
    if direction not in {"down", "up"}:
        raise ValueError("direction must be down or up")
    if x is None or y is None:
        x, y = _adaptive_scroll_point()
    items = _swift_ax(["scroll", direction, str(ticks), str(int(x)), str(int(y))])
    if items and items[-1].get("ok"):
        result = items[-1]
        result.setdefault("source", "ax-adaptive")
        return result
    chosen = find_running_app()
    if chosen:
        activate_app(chosen)
    amount = -abs(int(ticks)) if direction == "down" else abs(int(ticks))
    script = f'''
    tell application "System Events"
      scroll at {{{int(x)}, {int(y)}}} by {amount}
    end tell
    '''
    run_osascript(script)
    return {"ok": True, "direction": direction, "ticks": ticks, "x": x, "y": y, "source": "osascript"}


def _ax_conversation_rows(limit: int) -> list[dict]:
    rows: list[dict] = []
    for item in _swift_ax("rows"):
        texts = [
            str(text).strip()
            for text in item.get("texts", [])
            if str(text).strip() and not str(text).strip().startswith(("icon ", "avatar "))
        ]
        title = texts[0] if texts else ""
        if not title:
            continue
        x = float(item.get("x") or 0)
        width = float(item.get("width") or 0)
        preview = ""
        time_text = ""
        tags: list[str] = []
        unread_count = 0
        for text in texts[1:]:
            if text.startswith("@") or any(marker in text for marker in tag_markers()):
                tags.append(text)
            elif re.search(r"\d+分钟前|刚刚|\d{1,2}:\d{2}|星期|\\d+/\\d+", text):
                time_text = text
            elif re.fullmatch(r"\d+", text):
                unread_count += int(text)
            else:
                preview = preview or text
        rows.append(
            {
                "index": len(rows) + 1,
                "title": title,
                "preview": preview,
                "time": time_text,
                "tags": tags,
                "unread": unread_count > 0,
                "unread_count": unread_count,
                "raw": texts,
                "source": "axuielement",
                "click_x": x + width / 2,
                "click_y": float(item.get("y") or 0) + float(item.get("height") or 0) / 2,
                "selected": bool(item.get("selected")),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _bounded_scan_enabled() -> bool:
    return os.environ.get("WECOM_GUI_BOUNDED_SCAN", "1").strip().lower() not in {"0", "false", "off", "no"}


def _recent_scan_minutes() -> int:
    try:
        return max(1, int(os.environ.get("WECOM_GUI_RECENT_SCAN_MINUTES", "10")))
    except ValueError:
        return 10


def _time_text_age_minutes(time_text: object, *, now: datetime | None = None) -> float | None:
    text = str(time_text or "").strip()
    if not text:
        return None
    now = now or datetime.now()
    if text == "刚刚":
        return 0.0
    match = re.search(r"(\d+)\s*分钟前", text)
    if match:
        return float(match.group(1))
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = [int(part) for part in text.split(":", 1)]
        seen = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if seen > now + timedelta(minutes=1):
            seen -= timedelta(days=1)
        return max(0.0, (now - seen).total_seconds() / 60.0)
    if "昨天" in text or "星期" in text or re.fullmatch(r"\d{1,2}/\d{1,2}", text):
        return 24 * 60.0
    return None


def _row_is_recent_or_actionable(row: dict, *, minutes: int, now: datetime | None = None) -> tuple[bool, bool]:
    """Return `(keep, stop_scan)` for a visible conversation row."""
    if int(row.get("unread_count") or 0) > 0 or bool(row.get("unread")):
        return True, False
    preview = str(row.get("preview") or "").strip()
    if "你已添加了" in preview and "现在可以开始聊天了" in preview:
        return True, False
    age = _time_text_age_minutes(row.get("time"), now=now)
    if age is None:
        return True, False
    if age <= minutes:
        return True, False
    return False, True


def _row_from_ax_item(item: dict, *, index: int, source: str) -> dict | None:
    texts = [
        str(text).strip()
        for text in item.get("texts", [])
        if str(text).strip() and not str(text).strip().startswith(("icon ", "avatar "))
    ]
    title = texts[0] if texts else ""
    if not title:
        return None
    x = float(item.get("x") or 0)
    width = float(item.get("width") or 0)
    preview = ""
    time_text = str(item.get("timeText") or "").strip()
    tags: list[str] = []
    unread_count = 0
    for text in texts[1:]:
        if text.startswith("@") or any(marker in text for marker in tag_markers()):
            tags.append(text)
        elif re.search(r"\d+分钟前|刚刚|\d{1,2}:\d{2}|星期|\d+/\d+|昨天", text):
            if not time_text:
                time_text = text
        elif re.fullmatch(r"\d+", text):
            unread_count += int(text)
        else:
            preview = preview or text
    if bool(item.get("hasUnreadMarker")) and unread_count == 0:
        unread_count = 1
    return {
        "index": index,
        "title": title,
        "preview": preview,
        "time": time_text,
        "tags": tags,
        "unread": unread_count > 0,
        "unread_count": unread_count,
        "raw": texts,
        "source": source,
        "click_x": x + width / 2,
        "click_y": float(item.get("y") or 0) + float(item.get("height") or 0) / 2,
        "selected": bool(item.get("selected")),
    }


def _bounded_conversation_rows(limit: int) -> list[dict]:
    minutes = _recent_scan_minutes()
    started = time.perf_counter()
    rows: list[dict] = []
    ensure = _swift_ax("ensure-single-chat")
    if ensure and ensure[-1].get("ok") is False:
        _append_event(
            {
                "type": "wecom_bounded_scan_failed",
                "stage": "ensure_single_chat",
                "minutes": minutes,
                "limit": limit,
                "result": ensure[-1],
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
        return []
    items = _swift_ax(["recent-rows", str(minutes), str(limit)])
    now = datetime.now()
    stop_reason = ""
    for item in items:
        row = _row_from_ax_item(item, index=len(rows) + 1, source="axuielement-bounded")
        if row is None:
            continue
        keep, stop_scan = _row_is_recent_or_actionable(row, minutes=minutes, now=now)
        if keep:
            rows.append(row)
            if len(rows) >= limit:
                stop_reason = "limit_reached"
                break
        if stop_scan:
            stop_reason = f"older_than_{minutes}m:{row.get('time', '')}"
            break
    _append_event(
        {
            "type": "wecom_bounded_scan",
            "minutes": minutes,
            "limit": limit,
            "raw_count": len(items),
            "row_count": len(rows),
            "stop_reason": stop_reason,
            "ensure_single_chat": ensure[-1] if ensure else {},
            "rows": [
                {
                    "title": row.get("title", ""),
                    "preview": row.get("preview", ""),
                    "time": row.get("time", ""),
                    "unread": row.get("unread", False),
                    "unread_count": row.get("unread_count", 0),
                }
                for row in rows[:20]
            ],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )
    return rows


def ensure_input_ready(app_name: str | None = None) -> dict:
    """Ensure the current chat input is usable and the sidebar can be opened."""
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError("WeCom is not running or its app name differs. Start WeCom or set WECOM_GUI_APP_NAME.")
    if os.environ.get("WECOM_GUI_ACTIVATE_BEFORE_SCAN", "0") == "1":
        activate_app(chosen)
    items = _swift_ax("input-ready")
    result = items[-1] if items else {"ok": False, "error": "input_ready_unavailable"}
    _append_event({"type": "wecom_input_ready", "result": result})
    return result


def sidebar_identity(app_name: str | None = None) -> dict:
    """Read the visible right sidebar identity payload when available."""
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError("WeCom is not running or its app name differs. Start WeCom or set WECOM_GUI_APP_NAME.")
    if os.environ.get("WECOM_GUI_ACTIVATE_BEFORE_SCAN", "0") == "1":
        activate_app(chosen)
    items = _swift_ax("sidebar-identity")
    result = items[-1] if items else {"ok": False, "error": "sidebar_identity_unavailable", "external_user_id": ""}
    _append_event(
        {
            "type": "wecom_sidebar_identity",
            "ok": bool(result.get("ok")),
            "external_user_id": result.get("external_user_id") or result.get("external_userid") or "",
            "error": result.get("error") or "",
        }
    )
    return result


def selected_conversation_row(app_name: str | None = None, *, limit: int = 30) -> dict | None:
    """Return the currently selected visible sidebar conversation when AX exposes it."""
    chosen = resolve_app_name(app_name)
    if chosen:
        if os.environ.get("WECOM_GUI_ACTIVATE_BEFORE_SCAN", "0") == "1":
            activate_app(chosen)
        # The full row scan exposes `selected` reliably on current WeCom builds,
        # while the dedicated selected-row query can time out independently.
        for row in _ax_conversation_rows(limit):
            if str(row.get("title") or "").strip() in NAVIGATION_ROW_TITLES:
                continue
            if row.get("selected"):
                return row
        items = _swift_ax("selected-row")
        for item in items:
            if item.get("ok") is False:
                continue
            row = _row_from_ax_item(item, index=1, source="axuielement-selected")
            if row is not None:
                if str(row.get("title") or "").strip() in NAVIGATION_ROW_TITLES:
                    continue
                row["selected"] = True
                return row
    for row in conversation_rows(app_name, limit=limit):
        if str(row.get("title") or "").strip() in NAVIGATION_ROW_TITLES:
            continue
        if row.get("selected"):
            return row
    return None


def conversation_rows(app_name: str | None = None, limit: int = 30) -> list[dict]:
    """Return visible conversation rows from the WeCom sidebar."""
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError(
            "WeCom is not running or its app name differs. "
            "Start WeCom or set WECOM_GUI_APP_NAME."
        )
    if os.environ.get("WECOM_GUI_ACTIVATE_BEFORE_SCAN", "0") == "1":
        activate_app(chosen)
    if _bounded_scan_enabled():
        bounded_rows = _bounded_conversation_rows(limit)
        if bounded_rows:
            return bounded_rows
        _append_event(
            {
                "type": "wecom_bounded_scan_fallback",
                "reason": "bounded_rows_empty",
                "limit": limit,
            }
        )
    ax_rows = _ax_conversation_rows(limit)
    if ax_rows:
        return ax_rows
    script = f'''
    tell application "System Events"
      tell process "{chosen}"
{MAIN_WINDOW_SCRIPT}
        set outText to ""
        try
          set convTable to UI element 1 of UI element 4 of UI element 28 of splitter group 1 of targetWindow
          set rowCount to count of rows of convTable
          if rowCount > {limit} then set rowCount to {limit}
          repeat with r from 1 to rowCount
            try
              set cellObj to UI element 1 of row r of convTable
              set vals to {{}}
              try
                set vals to vals & (value of static texts of cellObj)
              end try
              try
                set vals to vals & (value of text fields of cellObj)
              end try
              try
                set vals to vals & (value of text areas of cellObj)
              end try
              set rowText to ""
              repeat with v in vals
                set valueText to v as text
                if valueText is not "" and valueText is not "missing value" then
                  if rowText is "" then
                    set rowText to valueText
                  else
                    set rowText to rowText & " ||| " & valueText
                  end if
                end if
              end repeat
              repeat with b in buttons of cellObj
                set badgeText to ""
                try
                  set badgeText to name of b as text
                end try
                if badgeText is "" or badgeText is "missing value" then
                  try
                    set badgeText to title of b as text
                  end try
                end if
                if badgeText is "" or badgeText is "missing value" then
                  try
                    set badgeText to value of b as text
                  end try
                end if
                if badgeText is not "" and badgeText is not "missing value" then
                  if rowText is "" then
                    set rowText to "__UNREAD__:" & badgeText
                  else
                    set rowText to rowText & " ||| " & "__UNREAD__:" & badgeText
                  end if
                end if
              end repeat
              if rowText is not "" then set outText to outText & rowText & linefeed
            end try
          end repeat
        end try
        return outText
      end tell
    end tell
    '''
    raw = run_osascript(script)
    if not raw.strip():
        raw = _conversation_rows_fallback(chosen, limit)
    rows: list[dict] = []
    for index, line in enumerate(raw.splitlines(), start=1):
        parts = [part.strip() for part in line.split(" ||| ") if part.strip()]
        if not parts:
            continue
        parts, unread_count = _extract_unread(parts)
        title, preview, time_text, tags = _parse_conversation_parts(parts)
        if not title:
            continue
        rows.append(
            {
                "index": index,
                "title": title,
                "preview": preview,
                "time": time_text,
                "tags": tags,
                "unread": unread_count > 0,
                "unread_count": unread_count,
                "raw": parts,
            }
        )
    if rows:
        return rows
    return _conversation_rows_ocr(chosen, limit)


def _vision_ocr_lines(image_path: str) -> list[dict]:
    """Return OCR observations from macOS Vision via the bundled Swift helper."""
    if shutil.which("swift") is None:
        return []
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "vision_ocr.swift"
    if not script_path.exists():
        return []
    proc = subprocess.run(
        ["swift", str(script_path), image_path],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return []
    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        x, y, width, height, text = parts
        try:
            rows.append(
                {
                    "x": float(x),
                    "y": float(y),
                    "width": float(width),
                    "height": float(height),
                    "text": text.strip(),
                }
            )
        except ValueError:
            continue
    return rows


def _required_tag_variants() -> list[str]:
    variants: list[str] = []
    for required in _required_tags():
        compact = required.replace(" ", "")
        bare = compact[1:] if compact.startswith("@") else compact
        variants.extend([required, compact, bare])
    return variants


def _ocr_text_matches_required_tag(text: str) -> bool:
    normalized = text.replace(" ", "")
    return any(variant and variant.replace(" ", "") in normalized for variant in _required_tag_variants())


def _screen_scale() -> float:
    try:
        value = float(os.environ.get("WECOM_GUI_SCREEN_SCALE", "2"))
        return value if value > 0 else 2.0
    except ValueError:
        return 2.0


def _conversation_rows_ocr(chosen: str, limit: int) -> list[dict]:
    """Fallback for WeCom builds whose CEF sidebar is invisible to Accessibility."""
    if os.environ.get("WECOM_GUI_ENABLE_OCR_SCAN", "0") != "1":
        return []
    activate_app(chosen)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        image_path = tmp.name
    try:
        capture = subprocess.run(["screencapture", "-x", image_path], capture_output=True, text=True, check=False)
        if capture.returncode != 0:
            return []
        observations = _vision_ocr_lines(image_path)
    finally:
        try:
            os.unlink(image_path)
        except OSError:
            pass

    sidebar = [item for item in observations if 150 <= item["x"] <= 650 and 180 <= item["y"] <= 1850]
    matched: list[dict] = []
    seen_titles: set[str] = set()
    scale = _screen_scale()
    for tag_item in sidebar:
        if len(matched) >= limit:
            break
        if not _ocr_text_matches_required_tag(tag_item["text"]):
            continue
        row_y = tag_item["y"]
        band = [
            item
            for item in sidebar
            if abs(item["y"] - row_y) <= 45 or abs((item["y"] + item["height"]) - (row_y + tag_item["height"])) <= 45
        ]
        band.sort(key=lambda item: item["x"])
        title_text = tag_item["text"]
        preview_text = ""
        time_text = ""
        for item in band:
            text = item["text"]
            if item is tag_item:
                continue
            if item["x"] > 470:
                time_text = text
            elif not preview_text:
                preview_text = text
        title = title_text
        for variant in _required_tag_variants():
            title = title.replace(variant, "")
        title = title.replace("®", "").replace("@", "").strip()
        title = re.sub(r"\s+", " ", title)
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        matched.append(
            {
                "index": len(matched) + 1,
                "title": title,
                "preview": preview_text,
                "time": time_text,
                "tags": [tag_item["text"]],
                "unread": True,
                "unread_count": 1,
                "raw": [item["text"] for item in band],
                "source": "vision-ocr",
                "click_x": max(200.0, min(tag_item["x"], 580.0)) / scale,
                "click_y": (row_y + tag_item["height"] / 2) / scale,
            }
        )
    return matched


def _conversation_rows_fallback(chosen: str, limit: int) -> str:
    """Read rows from any visible table when WeCom's UI hierarchy shifts."""
    tag_condition = _applescript_tag_condition("rowText")
    script = f'''
    tell application "System Events"
      tell process "{chosen}"
{MAIN_WINDOW_SCRIPT}
        set outText to ""
        set emitted to 0
        try
          set tableList to every table of entire contents of targetWindow
          repeat with t in tableList
            try
              repeat with r from 1 to count of rows of t
                if emitted >= {limit} then exit repeat
                set rowObj to row r of t
                set rowText to ""
                try
                  set cellObj to UI element 1 of rowObj
                on error
                  set cellObj to rowObj
                end try
                repeat with elem in entire contents of cellObj
                  try
                    set roleName to role of elem as text
                    if roleName is "AXStaticText" or roleName is "AXTextField" or roleName is "AXTextArea" then
                      set valueText to ""
                      try
                        set valueText to value of elem as text
                      end try
                      if valueText is "" then
                        try
                          set valueText to name of elem as text
                        end try
                      end if
                      if valueText is not "" and valueText is not "missing value" then
                        if rowText is "" then
                          set rowText to valueText
                        else
                          set rowText to rowText & " ||| " & valueText
                        end if
                      end if
                    else if roleName is "AXButton" then
                      set badgeText to ""
                      try
                        set badgeText to name of elem as text
                      end try
                      if badgeText is "" or badgeText is "missing value" then
                        try
                          set badgeText to title of elem as text
                        end try
                      end if
                      if badgeText is "" or badgeText is "missing value" then
                        try
                          set badgeText to value of elem as text
                        end try
                      end if
                      if badgeText is not "" and badgeText is not "missing value" then
                        if rowText is "" then
                          set rowText to "__UNREAD__:" & badgeText
                        else
                          set rowText to rowText & " ||| " & "__UNREAD__:" & badgeText
                        end if
                      end if
                    end if
                  end try
                end repeat
                if {tag_condition} then
                  set outText to outText & rowText & linefeed
                  set emitted to emitted + 1
                end if
              end repeat
            end try
          end repeat
        end try
        return outText
      end tell
    end tell
    '''
    return run_osascript(script)


def _extract_unread(parts: list[str]) -> tuple[list[str], int]:
    clean_parts: list[str] = []
    unread_count = 0
    for part in parts:
        if not part.startswith("__UNREAD__:"):
            clean_parts.append(part)
            continue
        badge = part.split(":", 1)[1].strip()
        if not badge:
            continue
        match = re.search(r"\d+", badge)
        if match:
            unread_count += int(match.group(0))
        else:
            unread_count = max(unread_count, 1)
    return clean_parts, unread_count


def _parse_conversation_parts(parts: list[str]) -> tuple[str, str, str, list[str]]:
    title = parts[0]
    preview = ""
    time_text = ""
    tags: list[str] = []
    markers = tag_markers()

    def _is_tag_text(value: str) -> bool:
        return value.startswith("@") or value.startswith("·") or any(marker in value for marker in markers)

    def _split_time_and_tag(value: str) -> tuple[str, str] | None:
        match = re.match(r"^(刚刚|\d+分钟前|\d{1,2}:\d{2}|星期\S*|\\d+/\\d+)\s+(.+)$", value)
        if not match:
            return None
        maybe_tag = match.group(2).strip()
        if not _is_tag_text(maybe_tag):
            return None
        return match.group(1).strip(), maybe_tag

    remaining = parts[1:]
    if remaining and (_is_tag_text(remaining[0]) or _split_time_and_tag(remaining[0]) is not None):
        maybe_title_preview = title.split(" ", 1)
        if len(maybe_title_preview) == 2:
            title, preview = maybe_title_preview

    for part in remaining:
        split = _split_time_and_tag(part)
        if split is not None:
            maybe_time, maybe_tag = split
            if not time_text:
                time_text = maybe_time
            tags.append(maybe_tag)
        elif _is_tag_text(part):
            tags.append(part)
        elif re.search(r"\d+分钟前|刚刚|\d{1,2}:\d{2}|星期|\\d+/\\d+", part) and not time_text:
            time_text = part
        elif not preview:
            preview = part
        else:
            preview = f"{preview} {part}".strip()
    return title, preview, time_text, tags


CHAT_NOISE_TEXTS = {
    "个人名片",
    "以上是打招呼内容",
}

IMAGE_PLACEHOLDER_TEXTS = {"[图片]", "图片"}
MINI_PROGRAM_HINTS = ("小程序", "WXMsg", "WeAppLogo")
ANIMATED_STICKER_HINTS = (
    "动画表情",
    "表情",
    "贴纸",
    "动图",
    "sticker",
    "emoji",
    "gif",
)

IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
_MEDIA_SKIP_CACHE: dict[str, float] = {}


def _float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rect_from_item(item: dict, *, prefix: str = "") -> dict:
    return {
        "x": int(_float_value(item.get(f"{prefix}x"))),
        "y": int(_float_value(item.get(f"{prefix}y"))),
        "width": int(_float_value(item.get(f"{prefix}width"))),
        "height": int(_float_value(item.get(f"{prefix}height"))),
    }


def _is_chat_image_rect(rect: dict) -> bool:
    min_size = int(os.environ.get("WECOM_GUI_MIN_IMAGE_BUBBLE_SIZE", "64"))
    min_area = int(os.environ.get("WECOM_GUI_MIN_IMAGE_BUBBLE_AREA", "4096"))
    width = int(_float_value(rect.get("width")))
    height = int(_float_value(rect.get("height")))
    return width >= min_size and height >= min_size and width * height >= min_area


def _chat_pane_boundaries(geometry: dict) -> tuple[float, float, str]:
    """Return chat-left and right-sidebar-left boundaries from AX geometry."""
    source = "fallback"
    chat_left = _float_value(geometry.get("chatLeft"))
    if chat_left > 0:
        source = "chatLeft"
    else:
        conversation_list = geometry.get("conversationList") if isinstance(geometry, dict) else None
        if isinstance(conversation_list, dict):
            list_left = _float_value(conversation_list.get("x"))
            list_width = _float_value(conversation_list.get("width"))
            if list_left > 0 and list_width > 0:
                chat_left = list_left + list_width
                source = "conversationList"
        if chat_left <= 0:
            sidebar = geometry.get("sidebar") if isinstance(geometry, dict) else None
            if isinstance(sidebar, dict):
                sidebar_left = _float_value(sidebar.get("x"))
                sidebar_width = _float_value(sidebar.get("width"))
                if sidebar_left > 0 and sidebar_width > 0:
                    chat_left = sidebar_left + sidebar_width
                    source = "sidebar"
    if chat_left <= 0:
        chat_left = 300.0
        source = "default"

    right_sidebar_left = _float_value(geometry.get("rightSidebarLeft"))
    return chat_left, right_sidebar_left, source


def _is_chat_pane_item(item: dict, chat_left: float, right_sidebar_left: float = 0.0) -> bool:
    x = float(item.get("x") or 0)
    width = float(item.get("width") or 0)
    right = x + width
    tolerance = float(os.environ.get("WECOM_GUI_CHAT_PANE_BOUNDARY_TOLERANCE", "2"))
    return (
        x >= chat_left - tolerance
        and width >= 250
        and (right_sidebar_left <= 0 or right <= right_sidebar_left + tolerance or x < right_sidebar_left - tolerance)
    )


def _meaningful_chat_texts(item: dict, *, use_message_nodes: bool = True) -> tuple[str, list[str], str]:
    structured = use_message_nodes and isinstance(item.get("messageTexts"), list)
    values = item["messageTexts"] if structured else item.get("texts", [])
    texts = [str(text).strip() for text in values if str(text).strip()]
    stamp = str(item.get("timestampText") or "") if structured else ""
    content_parts: list[str] = []
    seen_content: set[str] = set()
    for text in texts:
        if not structured and re.fullmatch(r"\d{1,2}:\d{2}", text):
            stamp = text
        elif text not in CHAT_NOISE_TEXTS and "shield checkmark" not in text and "b2b rich tips" not in text:
            key = "".join(text.split())
            if key in seen_content:
                continue
            seen_content.add(key)
            content_parts.append(text)
    return " ".join(content_parts).strip(), content_parts, stamp


def _is_image_placeholder_content(content: str, content_parts: list[str]) -> bool:
    normalized = "".join(str(content or "").split())
    if normalized in IMAGE_PLACEHOLDER_TEXTS:
        return True
    return bool(content_parts) and all("".join(part.split()) in IMAGE_PLACEHOLDER_TEXTS for part in content_parts)


def _is_mini_program_card(content: str, content_parts: list[str]) -> bool:
    haystack = " ".join([str(content or ""), *[str(part or "") for part in content_parts]])
    return bool(haystack.strip()) and any(hint in haystack for hint in MINI_PROGRAM_HINTS)


def _mini_program_card_rect(item: dict) -> dict:
    """Return a screenshot rect for a visible WeCom mini-program card row."""
    row_x = int(_float_value(item.get("x")))
    row_y = int(_float_value(item.get("y")))
    row_width = int(_float_value(item.get("width")))
    row_height = int(_float_value(item.get("height")))
    if row_width <= 0 or row_height <= 0:
        return {}

    bubble_x = int(_float_value(item.get("bubbleX"), row_x))
    bubble_y = int(_float_value(item.get("bubbleY"), row_y))
    bubble_width = int(_float_value(item.get("bubbleWidth"), min(row_width, 360)))
    bubble_height = int(_float_value(item.get("bubbleHeight"), 0))
    if bubble_width <= 0:
        return {}

    padding_x = int(os.environ.get("WECOM_GUI_MINI_PROGRAM_CAPTURE_PADDING_X", "28"))
    padding_top = int(os.environ.get("WECOM_GUI_MINI_PROGRAM_CAPTURE_PADDING_TOP", "72"))
    padding_bottom = int(os.environ.get("WECOM_GUI_MINI_PROGRAM_CAPTURE_PADDING_BOTTOM", "330"))
    max_width = int(os.environ.get("WECOM_GUI_MINI_PROGRAM_CAPTURE_MAX_WIDTH", "520"))
    min_height = int(os.environ.get("WECOM_GUI_MINI_PROGRAM_CAPTURE_MIN_HEIGHT", "220"))

    x = max(row_x, bubble_x - padding_x)
    y = max(0, bubble_y - padding_top)
    width = min(max_width, max(bubble_width + padding_x * 2, 260))
    width = min(width, max(1, row_x + row_width - x))
    target_bottom = bubble_y + max(bubble_height, 1) + padding_bottom
    height = max(min_height, target_bottom - y)
    height = min(height, max(1, row_y + row_height - y))
    if width <= 0 or height <= 0:
        return {}
    return {"x": x, "y": y, "width": width, "height": height}


def _media_texts(media: dict) -> list[str]:
    values: list[str] = []
    raw_values = media.get("texts")
    if isinstance(raw_values, list):
        values.extend(str(value).strip() for value in raw_values if str(value).strip())
    for key in ("text", "description", "title", "role", "subrole"):
        value = str(media.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def _is_animated_sticker_media(media: dict) -> bool:
    media_type = str(media.get("type") or media.get("mediaType") or "").strip().lower()
    if media_type in {"animated_sticker", "sticker", "emoji"}:
        return True
    if media.get("skip_capture") or media.get("skipCapture"):
        return True
    haystack = " ".join(_media_texts(media)).lower()
    return any(hint in haystack for hint in ANIMATED_STICKER_HINTS)


def _media_skip_cache_key(media: dict, rect: dict) -> str:
    return "|".join(
        [
            str(media.get("source") or ""),
            str(media.get("row") or ""),
            str(int(_float_value(rect.get("x")))),
            str(int(_float_value(rect.get("y")))),
            str(int(_float_value(rect.get("width")))),
            str(int(_float_value(rect.get("height")))),
        ]
    )


def _remember_media_capture_skip(media: dict, rect: dict) -> None:
    _MEDIA_SKIP_CACHE[_media_skip_cache_key(media, rect)] = time.time()


def _media_capture_skip_cached(media: dict, rect: dict) -> bool:
    key = _media_skip_cache_key(media, rect)
    if not key.strip("|"):
        return False
    ttl = max(0.0, float(os.environ.get("WECOM_GUI_MEDIA_SKIP_CACHE_SECONDS", "900")))
    created = _MEDIA_SKIP_CACHE.get(key)
    if created is None:
        return False
    if ttl > 0 and time.time() - created > ttl:
        _MEDIA_SKIP_CACHE.pop(key, None)
        return False
    return True


def _chat_image_row_rect(item: dict, *, anchor_x: int | None = None) -> dict:
    """Approximate the pure image row click target when WeCom hides image children from AX."""
    x = int(_float_value(item.get("x")))
    y = int(_float_value(item.get("y")))
    width = int(_float_value(item.get("width")))
    height = int(_float_value(item.get("height")))
    if width <= 0 or height < 80:
        return {}

    min_size = int(os.environ.get("WECOM_GUI_MIN_IMAGE_BUBBLE_SIZE", "64"))
    click_size = max(min_size, min(96, int(min(width, height) * 0.24)))
    if anchor_x is None:
        anchor_x = x + max(24, int(width * 0.02))
    center_x = max(x + click_size // 2, min(anchor_x + click_size // 2, x + width - click_size // 2))
    center_y = y + height // 2
    return {
        "x": center_x - click_size // 2,
        "y": center_y - click_size // 2,
        "width": click_size,
        "height": click_size,
    }


def _ax_chat_all_items(chat_left: float, right_sidebar_left: float = 0.0) -> list[dict]:
    items: list[dict] = []
    for item in _swift_ax("chat-all"):
        if _is_chat_pane_item(item, chat_left, right_sidebar_left):
            items.append(item)
    return items


def _image_row_anchor_x(items: list[dict], index: int) -> int | None:
    """Infer image bubble x from adjacent text bubbles from the same sender."""
    item = items[index]
    row_index = int(item.get("index") or 0)
    max_gap = int(os.environ.get("WECOM_GUI_IMAGE_ANCHOR_ROW_GAP", "2"))
    neighbor_indexes = list(range(index + 1, min(len(items), index + 4)))
    neighbor_indexes.extend(range(index - 1, max(-1, index - 4), -1))
    for neighbor_index in neighbor_indexes:
        neighbor = items[neighbor_index]
        neighbor_row = int(neighbor.get("index") or 0)
        if row_index and neighbor_row and abs(neighbor_row - row_index) > max_gap:
            continue
        _content, content_parts, _stamp = _meaningful_chat_texts(neighbor)
        if not content_parts:
            continue
        bubble_x = neighbor.get("bubbleX")
        if bubble_x is not None:
            return int(_float_value(bubble_x))
        return int(_float_value(neighbor.get("x")))
    return None


def _hidden_image_rows(
    last: int,
    chat_left: float,
    *,
    right_sidebar_left: float = 0.0,
    include_media: bool = True,
    snapshot_items: list[dict] | None = None,
) -> list[dict]:
    """Return image rows that normal AX chat output omits or exposes as text."""
    candidates: list[dict] = []
    items = _ax_chat_all_items(chat_left, right_sidebar_left) if snapshot_items is None else [
        item for item in snapshot_items if _is_chat_pane_item(item, chat_left, right_sidebar_left)
    ]
    for index, item in enumerate(items):
        content, content_parts, _stamp = _meaningful_chat_texts(item)
        media_elements = item.get("mediaElements") if isinstance(item.get("mediaElements"), list) else []
        height = int(_float_value(item.get("height")))
        y = int(_float_value(item.get("y")))
        visible = y + height > 0
        is_placeholder = _is_image_placeholder_content(content, content_parts)
        if media_elements or not visible:
            continue
        if content_parts and not is_placeholder:
            continue
        rect = _chat_image_row_rect(item, anchor_x=_image_row_anchor_x(items, index))
        if not _is_chat_image_rect(rect):
            continue
        message = {
            "row": int(item.get("index") or 0),
            "role": "unknown",
            "text": "[图片]",
            "time": "",
            "x": rect["x"],
            "width": rect["width"],
            "right": rect["x"] + rect["width"],
            "source": "axuielement-chat-hidden-image-row",
        }
        if include_media:
            media = {"type": "image", "rect": rect, "source": "axuielement-chat-hidden-image-row", "row": message["row"]}
            if _media_capture_skip_cached(media, rect):
                media.update(
                    {
                        "type": "animated_sticker",
                        "skip_capture": True,
                        "error": "cached_media_capture_skipped",
                    }
                )
                message["text"] = "[动画表情]"
            message["media"] = [media]
        candidates.append(message)
    return candidates[-last:] if last > 0 else candidates


def _image_capture_dir() -> Path:
    configured = os.environ.get("WECOM_GUI_CAPTURE_IMAGE_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(__file__).resolve().parents[3] / ".codex-run" / "wecom-images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_image_file(path: str | Path) -> bool:
    image_path = Path(path)
    try:
        if not image_path.is_file() or image_path.stat().st_size < 16:
            return False
        head = image_path.read_bytes()[:8]
    except OSError:
        return False
    return any(head.startswith(magic) for magic in IMAGE_MAGIC)


def _screenshot_rect(rect: dict, output_path: Path) -> dict:
    x = int(_float_value(rect.get("x")))
    y = int(_float_value(rect.get("y")))
    width = int(_float_value(rect.get("width")))
    height = int(_float_value(rect.get("height")))
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "invalid_capture_rect", "rect": rect}
    timeout = float(os.environ.get("WECOM_GUI_SCREENSHOT_TIMEOUT", "5"))
    try:
        proc = subprocess.run(
            ["screencapture", "-x", "-R", f"{x},{y},{width},{height}", str(output_path)],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "screencapture_timeout",
            "rect": rect,
            "path": str(output_path),
            "timeout": timeout,
        }
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip() or "screencapture_failed", "rect": rect}
    if not validate_image_file(output_path):
        return {"ok": False, "error": "captured_file_invalid", "rect": rect, "path": str(output_path)}
    return {"ok": True, "path": str(output_path), "rect": {"x": x, "y": y, "width": width, "height": height}}


def _click_image_and_capture(rect: dict) -> dict:
    x = int(_float_value(rect.get("x")) + _float_value(rect.get("width")) / 2)
    y = int(_float_value(rect.get("y")) + _float_value(rect.get("height")) / 2)
    if x <= 0 or y <= 0:
        return {"ok": False, "error": "invalid_media_point", "rect": rect}
    _append_event({"type": "image_capture_start", "rect": rect, "point": {"x": x, "y": y}})
    clicked = _swift_ax(["doubleclick", str(x), str(y)])
    if not clicked or not clicked[-1].get("ok"):
        _append_event({"type": "image_capture_failed", "stage": "doubleclick", "rect": rect, "result": clicked[-1] if clicked else {}})
        return {"ok": False, "error": (clicked[-1].get("error") if clicked else "") or "double_click_failed", "rect": rect}
    time.sleep(float(os.environ.get("WECOM_GUI_IMAGE_PREVIEW_DELAY", "0.6")))
    preview: dict = {}
    attempts = max(1, int(os.environ.get("WECOM_GUI_IMAGE_PREVIEW_ATTEMPTS", "5")))
    delay = float(os.environ.get("WECOM_GUI_IMAGE_PREVIEW_RETRY_DELAY", "0.25"))
    for index in range(attempts):
        items = _swift_ax("preview")
        preview = items[-1] if items else {}
        if preview.get("ok"):
            break
        if index < attempts - 1:
            time.sleep(delay)
    if not preview.get("ok"):
        close_result = (_swift_ax("close-preview") or [{}])[-1]
        _append_event(
            {
                "type": "image_capture_failed",
                "stage": "preview",
                "rect": rect,
                "preview": preview,
                "close_preview": close_result,
            }
        )
        return {
            "ok": False,
            "error": preview.get("error") or "preview_not_found",
            "rect": rect,
            "close_preview": close_result,
        }
    image_rect = preview.get("image") if isinstance(preview.get("image"), dict) else {}
    output_path = _image_capture_dir() / f"wecom-image-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.png"
    captured = _screenshot_rect(image_rect, output_path)
    close_result = (_swift_ax("close-preview") or [{}])[-1]
    captured["close_preview"] = close_result
    _append_event(
        {
            "type": "image_capture_result",
            "rect": rect,
            "preview": preview,
            "capture": captured,
            "close_preview": close_result,
        }
    )
    return captured


def _media_capture_mode() -> str:
    mode = str(os.environ.get("WECOM_GUI_MEDIA_CAPTURE_MODE", "preview") or "preview").strip().lower()
    if mode in {"0", "false", "none", "off"}:
        return "off"
    if mode in {"preview", "open"}:
        return "preview"
    return "bubble"


def _capture_image_bubble(rect: dict) -> dict:
    output_path = _image_capture_dir() / f"wecom-bubble-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.png"
    captured = _screenshot_rect(rect, output_path)
    captured["capture_mode"] = "bubble"
    _append_event(
        {
            "type": "image_capture_result",
            "mode": "bubble",
            "rect": rect,
            "capture": captured,
        }
    )
    return captured


def _capture_mini_program_card(rect: dict) -> dict:
    output_path = _image_capture_dir() / f"wecom-mini-program-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.png"
    captured = _screenshot_rect(rect, output_path)
    captured["capture_mode"] = "mini_program_card"
    _append_event(
        {
            "type": "mini_program_capture_result",
            "mode": "mini_program_card",
            "rect": rect,
            "capture": captured,
        }
    )
    return captured


def capture_chat_images(messages: list[dict]) -> list[dict]:
    """Capture image bubbles only during formal queue processing."""
    if os.environ.get("WECOM_GUI_CAPTURE_IMAGES", "1") == "0":
        return messages
    mode = _media_capture_mode()
    enriched: list[dict] = []
    for message in messages:
        copied = {**message}
        media_items = []
        for media in copied.get("media") or []:
            media_copy = {**media}
            rect = media_copy.get("rect") if isinstance(media_copy.get("rect"), dict) else {}
            media_type = str(media_copy.get("type") or "image").strip().lower()
            if media_type == "mini_program":
                if mode == "off":
                    media_copy["capture_ok"] = False
                    media_copy["capture_mode"] = "off"
                    media_copy["error"] = "media_capture_disabled"
                    media_items.append(media_copy)
                    continue
                result = _capture_mini_program_card(rect)
                media_copy["capture_ok"] = bool(result.get("ok"))
                media_copy["capture_mode"] = result.get("capture_mode") or "mini_program_card"
                if result.get("ok"):
                    media_copy["capture_path"] = result.get("path", "")
                    media_copy["capture_rect"] = result.get("rect", {})
                else:
                    media_copy["error"] = result.get("error") or "capture_failed"
                media_items.append(media_copy)
                continue
            if (
                media_type in {"sticker", "emoji", "animated_sticker"}
                or media_copy.get("skip_capture")
                or _is_animated_sticker_media(media_copy)
                or _media_capture_skip_cached(media_copy, rect)
            ):
                media_copy["type"] = "animated_sticker"
                media_copy["skip_capture"] = True
                media_copy["capture_ok"] = False
                media_copy["capture_mode"] = "skipped"
                media_copy["error"] = media_copy.get("error") or "media_capture_skipped"
                media_items.append(media_copy)
                continue
            if mode == "off":
                media_copy["capture_ok"] = False
                media_copy["capture_mode"] = "off"
                media_copy["error"] = "media_capture_disabled"
                media_items.append(media_copy)
                continue
            result = _click_image_and_capture(rect) if mode == "preview" else _capture_image_bubble(rect)
            media_copy["capture_ok"] = bool(result.get("ok"))
            media_copy["capture_mode"] = result.get("capture_mode") or mode
            if result.get("ok"):
                media_copy["capture_path"] = result.get("path", "")
                media_copy["capture_rect"] = result.get("rect", {})
            else:
                media_copy["error"] = result.get("error") or "capture_failed"
                if media_copy["error"] in {"preview_image_not_found", "preview_not_found"}:
                    _remember_media_capture_skip(media_copy, rect)
            media_items.append(media_copy)
        if media_items:
            copied["media"] = media_items
        enriched.append(copied)
    return enriched


def _ax_chat_messages(
    last: int,
    *,
    include_hidden_images: bool = False,
    include_hidden_image_media: bool = True,
) -> list[dict]:
    messages: list[dict] = []
    snapshot_items = _swift_ax("chat")
    viewport = next((item.get("chatViewport") for item in snapshot_items
                     if isinstance(item.get("chatViewport"), dict) and _float_value(item["chatViewport"].get("width")) > 0), None)
    geometry = {}
    if viewport:
        chat_left = _float_value(viewport.get("x"))
        right_sidebar_left = chat_left + _float_value(viewport.get("width"))
        boundary_source = "chat-snapshot"
    else:
        geometry = window_geometry()
        chat_left, right_sidebar_left, boundary_source = _chat_pane_boundaries(geometry if isinstance(geometry, dict) else {})
    if boundary_source in {"default", "sidebar"}:
        _append_event(
            {
                "type": "wecom_chat_boundary_fallback",
                "source": boundary_source,
                "chat_left": chat_left,
                "right_sidebar_left": right_sidebar_left,
                "geometry_ok": bool(isinstance(geometry, dict) and geometry.get("ok")),
            }
        )
    for item in snapshot_items:
        x = float(item.get("x") or 0)
        width = float(item.get("width") or 0)
        if not _is_chat_pane_item(item, chat_left, right_sidebar_left):
            continue
        texts = [str(text).strip() for text in item.get("texts", []) if str(text).strip()]
        content, content_parts, stamp = _meaningful_chat_texts(item)
        identity_text, _identity_parts, identity_time = _meaningful_chat_texts(item, use_message_nodes=False)
        media_elements = item.get("mediaElements") if isinstance(item.get("mediaElements"), list) else []
        is_mini_program = _is_mini_program_card(content, content_parts)
        if not texts and not media_elements:
            continue
        has_media = bool(media_elements)
        if not content_parts and not has_media:
            continue
        if (not content or content in CHAT_NOISE_TEXTS) and not has_media:
            continue
        if not content and has_media:
            content = "[图片]"
        bubble_x = item.get("bubbleX")
        bubble_width = item.get("bubbleWidth")
        try:
            message_x = float(bubble_x) if bubble_x is not None else x
        except (TypeError, ValueError):
            message_x = x
        try:
            message_width = float(bubble_width) if bubble_width is not None else width
        except (TypeError, ValueError):
            message_width = width
        right = int(message_x + message_width)
        message = {
            "row": int(item.get("index") or len(messages) + 1),
            "role": "unknown",
            "text": content,
            "time": stamp,
            "x": int(message_x),
            "width": int(message_width),
            "right": right,
            "source": "axuielement-chat-table",
            # Preserve existing observation/spool IDs when stripping AX timestamp labels from the body.
            "identity_text": identity_text or content,
            "identity_time": identity_time,
            "direction_evidence": item.get("directionEvidence") or {
                "source": "screencapturekit", "status": "unavailable", "side": "unknown",
            },
        }
        if has_media:
            media_payload = []
            for media in media_elements:
                if not isinstance(media, dict):
                    continue
                rect = _rect_from_item(media)
                if not _is_chat_image_rect(rect):
                    continue
                media_type = str(media.get("type") or media.get("mediaType") or "image").strip() or "image"
                media_item = {
                    "type": "animated_sticker" if _is_animated_sticker_media(media) else media_type,
                    "rect": rect,
                    "source": "axuielement-chat-media",
                    "row": int(item.get("index") or len(messages) + 1),
                }
                texts = media.get("texts") if isinstance(media.get("texts"), list) else []
                if texts:
                    media_item["texts"] = texts
                if media.get("skip_capture") or media.get("skipCapture") or _is_animated_sticker_media(media):
                    media_item["skip_capture"] = True
                media_payload.append(media_item)
            if media_payload:
                message["media"] = media_payload
                if all(_is_animated_sticker_media(media) for media in media_payload):
                    message["text"] = "[动画表情]" if _is_image_placeholder_content(content, content_parts) else content
                else:
                    message["text"] = content or "[图片]"
                message["content"] = message["text"]
        if is_mini_program and include_hidden_image_media:
            rect = _mini_program_card_rect(item)
            if rect:
                message["media"] = [
                    *(message.get("media") or []),
                    {
                        "type": "mini_program",
                        "rect": rect,
                        "source": "axuielement-chat-mini-program-card",
                        "row": int(item.get("index") or len(messages) + 1),
                        "texts": content_parts,
                    },
                ]
                message["content"] = message["text"]
        messages.append(message)
    if include_hidden_images:
        by_row = {int(message.get("row") or 0): message for message in messages}
        for message in _hidden_image_rows(
            last,
            chat_left,
            right_sidebar_left=right_sidebar_left,
            include_media=include_hidden_image_media,
            snapshot_items=snapshot_items if snapshot_items and all(item.get("snapshotComplete") for item in snapshot_items) else None,
        ):
            row = int(message.get("row") or 0)
            if row and row not in by_row:
                messages.append(message)
            elif row and row in by_row and include_hidden_image_media and not by_row[row].get("media"):
                by_row[row]["media"] = message.get("media", [])
                by_row[row]["source"] = message.get("source", by_row[row].get("source"))
                by_row[row]["x"] = message.get("x", by_row[row].get("x"))
                by_row[row]["width"] = message.get("width", by_row[row].get("width"))
                by_row[row]["right"] = message.get("right", by_row[row].get("right"))
                by_row[row]["text"] = by_row[row].get("text") or "[图片]"
                by_row[row]["content"] = by_row[row]["text"]
        messages.sort(key=lambda message: int(message.get("row") or 0))
    for message in messages:
        message.setdefault("direction_evidence", {
            "source": "screencapturekit", "status": "unsupported_media", "side": "unknown",
        })
    return messages[-last:] if last > 0 else messages


def chat_messages(
    app_name: str | None = None,
    last: int = 10,
    *,
    capture_images: bool = False,
    include_image_media: bool | None = None,
) -> list[dict]:
    """Return visible messages from the opened right-side chat pane."""
    chosen = resolve_app_name(app_name)
    if not chosen:
        raise RuntimeError(
            "WeCom is not running or its app name differs. "
            "Start WeCom or set WECOM_GUI_APP_NAME."
        )
    activate_app(chosen)
    include_media = capture_images if include_image_media is None else include_image_media
    ax_messages = _ax_chat_messages(
        last,
        include_hidden_images=True,
        include_hidden_image_media=include_media,
    )
    if ax_messages:
        return capture_chat_images(ax_messages) if capture_images else ax_messages
    script = f'''
    tell application "System Events"
      tell process "{chosen}"
{MAIN_WINDOW_SCRIPT}
        set outText to ""
        try
          set pane to UI element 1 of UI element 11 of UI element 28 of splitter group 1 of targetWindow
          set chatTable to UI element 1 of UI element 1 of pane
          set rowCount to count of rows of chatTable
          set startRow to rowCount - {last} + 1
          if startRow < 1 then set startRow to 1
          repeat with r from startRow to rowCount
            try
              set cellObj to UI element 1 of row r of chatTable
              set msgText to ""
              set stampText to ""
              set xPos to ""
              set widthValue to ""
              set rightPos to ""
              repeat with j from 1 to count of UI elements of cellObj
                set elem to UI element j of cellObj
                try
                  set roleName to role of elem as text
                  if roleName is "AXStaticText" then
                    set valueText to ""
                    try
                      set valueText to value of elem as text
                    end try
                    if valueText is "" then
                      try
                        set valueText to name of elem as text
                      end try
                    end if
                    if valueText is not "" and valueText is not "missing value" then set stampText to valueText
                  end if
                  if roleName is "AXTextArea" or roleName is "AXTextField" then
                    set valueText to ""
                    try
                      set valueText to value of elem as text
                    end try
                    if valueText is "" then
                      try
                        set valueText to name of elem as text
                      end try
                    end if
                    if valueText is not "missing value" then set msgText to valueText
                    try
                      set p to position of elem
                      set s to size of elem
                      set xPos to item 1 of p as text
                      set widthValue to item 1 of s as text
                      set rightPos to ((item 1 of p) + (item 1 of s)) as text
                    end try
                  end if
                end try
              end repeat
              if msgText is not "" then
                set outText to outText & r & " ||| " & xPos & " ||| " & widthValue & " ||| " & rightPos & " ||| " & stampText & " ||| " & msgText & linefeed
              end if
            end try
          end repeat
        end try
        return outText
      end tell
    end tell
    '''
    raw = run_osascript(script)
    messages: list[dict] = []
    for line in raw.splitlines():
        parts = [part.strip() for part in line.split(" ||| ", 5)]
        if len(parts) != 6:
            continue
        row, x_pos, width, right, stamp, text = parts
        messages.append(
            {
                "row": int(row) if row.isdigit() else row,
                "role": "unknown",
                "text": text,
                "time": stamp,
                "x": int(x_pos) if x_pos.isdigit() else None,
                "width": int(width) if width.isdigit() else None,
                "right": int(right) if right.isdigit() else None,
                "source": "accessibility-chat-table",
                "direction_evidence": {
                    "source": "screencapturekit", "status": "ax_fallback_unverified", "side": "unknown",
                },
            }
        )
    return messages


def click_text(label: str, app_name: str | None = None) -> dict:
    """Click the first accessible UI element whose value/name/title equals label."""
    chosen = app_name or find_running_app()
    if not chosen:
        raise RuntimeError(
            "WeCom is not running or its app name differs. "
            "Start WeCom or set WECOM_GUI_APP_NAME."
        )
    activate_app(chosen)
    if _swift_ax_open(label):
        return {"ok": True, "clicked": label, "app_name": chosen, "source": "axuielement"}
    safe_label = label.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "System Events"
      tell process "{chosen}"
{MAIN_WINDOW_SCRIPT}
        set targetLabel to "{safe_label}"

        -- Prefer exact match against conversation-list rows and click the row.
        try
          set convTable to UI element 1 of UI element 4 of UI element 28 of splitter group 1 of targetWindow
          repeat with r from 1 to count of rows of convTable
            set rowObj to row r of convTable
            try
              set cellObj to UI element 1 of rowObj
              set combinedText to ""
              repeat with j from 1 to count of UI elements of cellObj
                set elem to UI element j of cellObj
                set candidate to ""
                try
                  set candidate to value of elem as text
                end try
                if candidate is "" then
                  try
                    set candidate to name of elem as text
                  end try
                end if
                if candidate is not "" then
                  if combinedText is "" then
                    set combinedText to candidate
                  else
                    set combinedText to combinedText & " " & candidate
                  end if
                end if
                if candidate is targetLabel or candidate starts with (targetLabel & " ") then
                  set selected of rowObj to true
                  return "clicked"
                end if
              end repeat
              if combinedText is targetLabel or combinedText starts with (targetLabel & " ") then
                set selected of rowObj to true
                return "clicked"
              end if
            end try
          end repeat
        end try

        try
          set tableList to every table of entire contents of targetWindow
          repeat with t in tableList
            try
              repeat with r from 1 to count of rows of t
                set rowObj to row r of t
                set rowText to ""
                try
                  set cellObj to UI element 1 of rowObj
                on error
                  set cellObj to rowObj
                end try
                repeat with elem in entire contents of cellObj
                  try
                    set roleName to role of elem as text
                    if roleName is "AXStaticText" or roleName is "AXTextField" or roleName is "AXTextArea" then
                      set candidate to ""
                      try
                        set candidate to value of elem as text
                      end try
                      if candidate is "" then
                        try
                          set candidate to name of elem as text
                        end try
                      end if
                      if candidate is not "" and candidate is not "missing value" then
                        if rowText is "" then
                          set rowText to candidate
                        else
                          set rowText to rowText & " " & candidate
                        end if
                        if candidate is targetLabel or candidate starts with (targetLabel & " ") then
                          set selected of rowObj to true
                          return "clicked"
                        end if
                      end if
                    end if
                  end try
                end repeat
                if rowText is targetLabel or rowText starts with (targetLabel & " ") then
                  set selected of rowObj to true
                  return "clicked"
                end if
              end repeat
            end try
          end repeat
        end try

        repeat with elem in UI elements of targetWindow
          try
            set candidate to ""
            try
              set candidate to value of elem as text
            end try
            if candidate is "" then
              try
                set candidate to name of elem as text
              end try
            end if
            if candidate is "" then
              try
                set candidate to title of elem as text
              end try
            end if
            if candidate is targetLabel or candidate starts with (targetLabel & " ") then
              perform action "AXPress" of elem
              return "clicked"
            end if
          end try
        end repeat
        error "No accessible element matched: {safe_label}"
      end tell
    end tell
    '''
    run_osascript(script)
    return {"ok": True, "clicked": label, "app_name": chosen}


def click_point(x: float, y: float, app_name: str | None = None) -> dict:
    """Click an absolute screen coordinate."""
    chosen = app_name or find_running_app()
    if chosen:
        activate_app(chosen)
    script = f'''
    tell application "System Events"
      click at {{{int(x)}, {int(y)}}}
    end tell
    '''
    run_osascript(script)
    return {"ok": True, "clicked": {"x": x, "y": y}, "app_name": chosen}


def paste_and_enter(text: str, *, submit: bool) -> dict:
    """Paste text into the focused input and optionally press Return."""
    from .clipboard import set_clipboard

    ready = ensure_input_ready()
    if not ready.get("ok"):
        raise RuntimeError(f"chat input not ready for clipboard paste: {ready}")
    set_clipboard(text)
    script = '''
    tell application "System Events"
      keystroke "v" using command down
      delay 0.1
    end tell
    '''
    run_osascript(script)
    if submit:
        run_osascript('tell application "System Events" to key code 36')
    return {"ok": True, "submitted": submit, "chars": len(text)}


def paste_file_and_enter(path: str | Path, *, submit: bool) -> dict:
    """Paste one image file into the focused input and optionally submit."""
    image_path = Path(path).expanduser()
    if not validate_image_file(image_path):
        raise RuntimeError(f"invalid image file: {image_path}")
    ready = ensure_input_ready()
    if not ready.get("ok"):
        raise RuntimeError(f"chat input not ready for file paste: {ready}")
    safe_path = str(image_path).replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    set imageFile to POSIX file "{safe_path}"
    tell application "Finder"
      set the clipboard to imageFile
    end tell
    tell application "System Events"
      keystroke "v" using command down
      delay 0.2
    end tell
    '''
    run_osascript(script)
    if submit:
        run_osascript('tell application "System Events" to key code 36')
    return {"ok": True, "submitted": submit, "path": str(image_path), "method": "clipboard_file"}
