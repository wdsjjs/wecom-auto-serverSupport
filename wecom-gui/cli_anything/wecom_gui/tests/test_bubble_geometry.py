from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

import pytest

from cli_anything.wecom_gui.core import chat, edge_worker
from cli_anything.wecom_gui.utils import macos_backend


def evidence(side="left", status="matched"):
    return {"source": "screencapturekit", "status": status, "side": side}


@pytest.mark.parametrize("side,expected", [("left", "用户"), ("right", "客服")])
def test_single_direction_does_not_need_an_opposite_bubble(side, expected):
    messages = chat.infer_roles([
        {"text": "同一文案", "direction_evidence": evidence(side)},
        {"text": "同一文案", "direction_evidence": evidence(side)},
    ])
    assert [item["role"] for item in messages] == [expected, expected]
    assert all(item["role_confidence"] == "high" for item in messages)


@pytest.mark.parametrize("status", ["capture_failed_or_timed_out", "chat_changed_during_capture",
                                   "outside_viewport_or_unlaid_out", "unsupported_message_layout"])
def test_failed_visual_check_never_falls_back_to_legacy_position_guess(status):
    messages = chat.infer_roles([
        {"text": "左边", "x": 10, "right": 50, "direction_evidence": evidence("left", status)},
        {"text": "右边", "x": 100, "right": 200, "direction_evidence": evidence("right", status)},
    ])
    assert all(item["role"] == "unknown" and item["role_confidence"] == "low" for item in messages)


def test_timestamp_is_separate_from_body_and_legacy_identity_is_preserved(monkeypatch):
    row = {"index": 8, "x": 411, "width": 698,
           "texts": ["星期日 13:38", "叶黄素有什么好处？"],
           "messageTexts": ["叶黄素有什么好处？"], "timestampText": "星期日 13:38",
           "bubbleX": 437, "bubbleWidth": 119, "directionEvidence": evidence()}
    monkeypatch.setattr(macos_backend, "window_geometry", lambda: {"ok": True, "chatLeft": 411})
    monkeypatch.setattr(macos_backend, "_swift_ax", lambda command: [row])
    message = macos_backend._ax_chat_messages(10)[0]
    assert message["text"] == "叶黄素有什么好处？"
    assert message["time"] == "星期日 13:38"
    old = {"text": "星期日 13:38 叶黄素有什么好处？", "time": ""}
    old_fingerprint = edge_worker._visible_observation_fingerprints([old])[0][0]
    assert edge_worker._visible_observation_fingerprints([message])[0][0] == old_fingerprint
    assert edge_worker._message_identity("uid:1", message) == edge_worker._message_identity("uid:1", old)


def test_actual_message_that_looks_like_time_is_not_removed():
    content, parts, stamp = macos_backend._meaningful_chat_texts(
        {"texts": ["10:00", "13:38"], "messageTexts": ["13:38"], "timestampText": "10:00"})
    assert (content, parts, stamp) == ("13:38", ["13:38"], "10:00")


def test_complete_snapshot_reuses_geometry_and_keeps_blank_image_rows(monkeypatch):
    calls = []
    viewport = {"x": 311, "y": 100, "width": 1159, "height": 500}
    rows = [
        {"index": 1, "x": 311, "y": 100, "width": 1159, "height": 52,
         "texts": ["正文"], "messageTexts": ["正文"], "bubbleX": 337, "bubbleWidth": 100,
         "chatViewport": viewport, "snapshotComplete": True, "directionEvidence": evidence()},
        {"index": 2, "x": 311, "y": 152, "width": 1159, "height": 160,
         "texts": [], "messageTexts": [], "chatViewport": viewport, "snapshotComplete": True},
    ]

    def read(command):
        calls.append(command)
        assert command == "chat", "One complete snapshot must not scan geometry or chat-all again"
        return rows

    monkeypatch.setattr(macos_backend, "_swift_ax", read)
    messages = macos_backend._ax_chat_messages(100, include_hidden_images=True)
    assert calls == ["chat"]
    assert [message["text"] for message in messages] == ["正文", "[图片]"]
    assert messages[1]["media"][0]["source"] == "axuielement-chat-hidden-image-row"


@pytest.fixture(scope="module")
def native_helper(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Native bubble geometry requires the macOS Swift SDK")
    binary = tmp_path_factory.mktemp("native-bubble-tests") / "ax_wecom"
    source = Path(macos_backend.__file__).resolve().parents[1] / "scripts" / "ax_wecom.swift"
    subprocess.run(["swiftc", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=90)
    return binary


def write_png(path, width, height, rectangles):
    # A deterministic image fixture with no Pillow/OpenCV runtime dependency.
    rows = [bytearray(b"\xff\xff\xff" * width) for _ in range(height)]
    for x, y, w, h, color in rectangles:
        for line in rows[y:y + h]:
            line[x * 3:(x + w) * 3] = bytes(color) * w

    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows)))
    path.write_bytes(data + chunk(b"IEND", b""))


@pytest.mark.parametrize("scale,origin", [(1, (0, 0)), (2, (-1100, 33))])
def test_native_pixels_distinguish_sides_and_reject_invalid_rects(native_helper, tmp_path, scale, origin):
    x0, y0 = origin
    window = {"x": x0, "y": y0, "width": 700, "height": 500}
    rectangles = [(16, 30, 180, 40, (232, 232, 233)),
                  (484, 130, 200, 40, (207, 230, 253)),
                  (16, 230, 668, 40, (232, 232, 233))]
    image = tmp_path / "bubbles.png"
    write_png(image, 700 * scale, 500 * scale,
              [(x * scale, y * scale, w * scale, h * scale, color) for x, y, w, h, color in rectangles])
    bodies = [{"x": x0 + x, "y": y0 + y, "width": w, "height": h} for x, y, w, h in
              [(26, 37, 160, 22), (494, 137, 180, 22), (26, 237, 648, 22),
               (411, 90, 0, 14), (26, 530, 160, 22)]]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"window": window, "viewport": window, "bodies": bodies}))
    completed = subprocess.run([str(native_helper), "bubble-fixture", str(fixture), str(image)],
                               check=True, capture_output=True, text=True, timeout=10)
    results = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["side"] for item in results] == ["left", "right", "unknown", "unknown", "unknown"]
    assert results[2]["status"] == "ambiguous_alignment"
    assert all(item["status"] == "outside_viewport_or_unlaid_out" for item in results[3:])
