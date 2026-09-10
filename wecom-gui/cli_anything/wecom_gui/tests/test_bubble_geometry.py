from __future__ import annotations

import copy
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
        assert command == ["chat", "20"], "The native reader must receive the requested window"
        return rows

    monkeypatch.setattr(macos_backend, "_swift_ax", read)
    messages = macos_backend._ax_chat_messages(20, include_hidden_images=True)
    assert calls == [["chat", "20"]]
    assert [message["text"] for message in messages] == ["正文", "[图片]"]
    assert messages[1]["media"][0]["source"] == "axuielement-chat-hidden-image-row"
    assert messages[1]["direction_evidence"]["side"] == "unknown"


@pytest.mark.parametrize("side,role", [("left", "用户"), ("right", "客服")])
def test_hidden_image_keeps_verified_pixels_and_uses_actual_bubble_rect(monkeypatch, side, role):
    viewport = {"x": 300, "y": 100, "width": 700, "height": 500}
    rect = {"x": 316 if side == "left" else 784, "y": 120, "width": 200, "height": 160}
    row = {"index": 5, "x": 300, "y": 100, "width": 700, "height": 200,
           "texts": [], "messageTexts": [], "chatViewport": viewport, "snapshotComplete": True,
           "directionEvidence": {**evidence(side), "method": "image_pixels", "bubbleRect": rect}}
    monkeypatch.setattr(macos_backend, "_swift_ax", lambda command: [row])
    messages = chat.infer_roles(macos_backend._ax_chat_messages(20, include_hidden_images=True))
    assert len(messages) == 1
    assert messages[0]["role"] == role
    assert messages[0]["role_confidence"] == "high"
    assert messages[0]["media"][0]["rect"] == rect


@pytest.fixture(scope="module")
def native_helper(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Native bubble geometry requires the macOS Swift SDK")
    binary = tmp_path_factory.mktemp("native-bubble-tests") / "ax_wecom"
    source = Path(macos_backend.__file__).resolve().parents[1] / "scripts" / "ax_wecom.swift"
    subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=90)
    return binary


def test_native_input_preflight_accepts_empty_but_rejects_drafts_and_unreadable_values(native_helper, tmp_path):
    fixture = tmp_path / "inputs.json"
    fixture.write_text(json.dumps([
        {"value": ""}, {"value": "", "attributed": True},
        {"value": "draft"}, {"value": "draft", "attributed": True},
        {"value": " "}, {"value": "\u200b"},
        {}, {"value": None}, {"value": 123},
        {"value": "", "status": -25212}, {"value": "", "status": -25200},
    ]))
    completed = subprocess.run([str(native_helper), "input-fixture", str(fixture)],
                               check=True, capture_output=True, text=True, timeout=10)
    results = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(results) == 11
    assert [result["ok"] for result in results] == [True, True] + [False] * 9
    assert [result["input"]["valueLength"] for result in results[:6]] == [0, 0, 5, 5, 1, 1]
    assert all(result["error"] == "chat_input_not_empty" for result in results[2:6])
    assert all(result["error"] == "chat_input_value_unavailable" for result in results[6:])
    assert all(result["submitted"] is False for result in results)
    assert "draft" not in completed.stdout


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


def test_native_snapshot_preserves_body_timestamp_media_and_legacy_identity(native_helper, tmp_path):
    def node(role, texts=(), *, depth=2, x=326, y=140, width=150, height=22, has_rect=True):
        return {"role": role, "subrole": "", "texts": list(texts), "depth": depth,
                "x": x, "y": y, "width": width, "height": height,
                "hasRect": has_rect, "selected": False}

    row = node("AXRow", depth=0, x=300, y=100, width=700, height=100)
    viewport = {"x": 300, "y": 100, "width": 700, "height": 500}
    fixture = tmp_path / "chat.json"
    fixture.write_text(json.dumps({"viewport": viewport, "rows": [
        [row, node("AXStaticText", ["10:00"]), node("AXTextArea", ["  13:38  "])],
        [row, node("AXTextArea", ["hello"], width=0, has_rect=False)],
        [row, node("AXImage", ["图片"], width=160, height=160)],
        [row],
        [row, node("AXTextArea", ["hello"]), node("AXTextArea", ["hello"], depth=9)],
    ]}))
    completed = subprocess.run([str(native_helper), "chat-fixture", str(fixture)],
                               check=True, capture_output=True, text=True, timeout=10)
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(rows) == 5
    assert rows[0]["texts"] == ["10:00", "  13:38  "]
    assert rows[0]["messageTexts"] == ["13:38"]
    assert rows[0]["timestampText"] == "10:00"
    assert rows[0]["bubbleTextSupported"] is True
    assert rows[0]["bubbleImageSupported"] is False
    assert rows[0]["chatViewport"] == viewport
    assert rows[1]["messageTexts"] == ["hello"]
    assert rows[1]["bubbleWidth"] == 0
    assert rows[2]["mediaElements"][0]["mediaType"] == "image"
    assert rows[2]["bubbleTextSupported"] is False
    assert rows[2]["bubbleImageSupported"] is True
    assert rows[2]["messageTexts"] == []
    assert rows[3]["texts"] == rows[3]["messageTexts"] == []
    assert rows[3]["bubbleImageSupported"] is True
    assert rows[4]["texts"] == ["hello"]
    assert rows[4]["messageTexts"] == ["hello", "hello"]
    assert rows[4]["bubbleTextSupported"] is False


@pytest.mark.parametrize("change,expected", [
    ("offscreen_layout", True), ("visible_layout", False), ("offscreen_text", False),
    ("enters_viewport", False), ("leaves_viewport", False), ("new_message", False),
])
def test_native_snapshot_validation_ignores_only_offscreen_body_geometry(native_helper, tmp_path, change, expected):
    row = {"role": "AXRow", "subrole": "", "texts": [], "depth": 0,
           "x": 300, "y": 100, "width": 700, "height": 100, "hasRect": True, "selected": False}
    body = {**row, "role": "AXTextArea", "texts": ["hello"], "depth": 2,
            "x": 326, "y": 140, "width": 150, "height": 22}
    before = [[row, body], [row, {**body, "texts": ["previous"], "y": -800}]]
    after = copy.deepcopy(before)
    if change == "offscreen_layout":
        after[1][1].update(x=300, y=-763, width=0, height=14)
    elif change == "visible_layout":
        after[0][1]["height"] = 23
    elif change == "offscreen_text":
        after[1][1]["texts"] = ["updated"]
    elif change == "enters_viewport":
        after[1][1]["y"] = 180
    elif change == "leaves_viewport":
        after[0][1]["y"] = -800
    elif change == "new_message":
        after.append(copy.deepcopy(after[0]))
    fixture = tmp_path / "validation.json"
    fixture.write_text(json.dumps({"rows": before, "afterRows": after,
                                   "viewport": {"x": 300, "y": 100, "width": 700, "height": 500}}))
    completed = subprocess.run([str(native_helper), "chat-fixture", str(fixture)],
                               check=True, capture_output=True, text=True, timeout=10)
    assert json.loads(completed.stdout)["snapshotsMatch"] is expected


@pytest.mark.parametrize("row_count", [0, 7, 25])
def test_native_read_window_keeps_recent_rows_and_original_indices(native_helper, tmp_path, row_count):
    row = {"role": "AXRow", "subrole": "", "texts": [], "depth": 0,
           "x": 300, "y": 100, "width": 700, "height": 100, "hasRect": True, "selected": False}
    rows = [[row, {**row, "role": "AXTextArea", "texts": [f"message-{i + 1}"], "depth": 2}]
            for i in range(row_count)]
    fixture = tmp_path / "window.json"
    fixture.write_text(json.dumps({"rows": rows, "last": 20}))
    completed = subprocess.run([str(native_helper), "chat-fixture", str(fixture)],
                               check=True, capture_output=True, text=True, timeout=10)
    payloads = [json.loads(line) for line in completed.stdout.splitlines()]
    expected_indices = list(range(max(1, row_count - 19), row_count + 1))
    assert [item["index"] for item in payloads] == expected_indices
    assert [item["messageTexts"] for item in payloads] == [[f"message-{i}"] for i in expected_indices]
    if rows:
        fixture.write_text(json.dumps({"rows": rows, "afterRows": rows + [[row]], "last": 20}))
        checked = subprocess.run([str(native_helper), "chat-fixture", str(fixture)],
                                 check=True, capture_output=True, text=True, timeout=10)
        assert json.loads(checked.stdout)["snapshotsMatch"] is False


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


@pytest.mark.parametrize("scale,origin", [(1, (0, 0)), (2, (-900, 40))])
def test_native_image_pixels_distinguish_sides_and_reject_ambiguous_clipped_or_blank_rows(native_helper, tmp_path, scale, origin):
    x0, y0 = origin
    window = {"x": x0, "y": y0, "width": 700, "height": 1000}
    rectangles = [(16, 20, 200, 130, (80, 100, 140)),
                  (484, 200, 200, 130, (60, 170, 80)),
                  (16, 380, 200, 130, (100, 60, 130)), (484, 380, 200, 130, (50, 170, 150)),
                  (250, 560, 200, 130, (150, 100, 40)),
                  (16, 840, 200, 160, (60, 100, 130)),
                  (10, 730, 36, 36, (90, 90, 90))]
    # Different colors inside one connected photo must stay one image, unlike text bubble colors.
    rectangles += [(36, 40, 70, 70, (170, 50, 80)), (510, 230, 80, 80, (20, 80, 200))]
    image = tmp_path / "images.png"
    write_png(image, 700 * scale, 1000 * scale,
              [(x * scale, y * scale, w * scale, h * scale, color) for x, y, w, h, color in rectangles])
    bodies = [{"x": x0, "y": y0 + y, "width": 700, "height": height}
              for y, height in [(0, 180), (180, 180), (360, 180), (540, 180), (720, 110), (830, 180), (1100, 180)]]
    fixture = tmp_path / "images.json"
    fixture.write_text(json.dumps({"window": window, "viewport": window, "bodies": bodies, "images": True}))
    completed = subprocess.run([str(native_helper), "bubble-fixture", str(fixture), str(image)],
                               check=True, capture_output=True, text=True, timeout=10)
    results = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["side"] for item in results] == ["left", "right", "unknown", "unknown", "unknown", "unknown", "unknown"]
    assert results[0]["method"] == "image_pixels"
    assert results[-1]["status"] == "outside_viewport_or_unlaid_out"


@pytest.mark.parametrize("scale,origin", [(1, (0, 0)), (2, (-1100, 35))])
def test_native_short_text_bubbles_survive_large_colorful_photo(native_helper, tmp_path, scale, origin):
    x0, y0 = origin
    window = {"x": x0, "y": y0, "width": 700, "height": 676}
    # Each of these 16 photo colors outranks a one- or two-character bubble.
    rectangles = [(180, 10 + i * 25, 480, 25, (80 + i * 5, 100, 70)) for i in range(16)]
    rectangles += [(16, 500, 40, 32, (232, 232, 233)),
                   (630, 550, 54, 32, (201, 231, 255)),
                   (326, 600, 48, 32, (232, 232, 233))]
    image = tmp_path / "photo-and-short-replies.png"
    write_png(image, 700 * scale, 676 * scale,
              [(x * scale, y * scale, w * scale, h * scale, color) for x, y, w, h, color in rectangles])
    bodies = [{"x": x0 + x, "y": y0 + y, "width": w, "height": 18}
              for x, y, w in [(26, 507, 20), (640, 557, 34), (336, 607, 28), (100, 480, 30)]]
    fixture = tmp_path / "short-replies.json"
    fixture.write_text(json.dumps({"window": window, "viewport": window, "bodies": bodies}))
    completed = subprocess.run([str(native_helper), "bubble-fixture", str(fixture), str(image)],
                               check=True, capture_output=True, text=True, timeout=10)
    results = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["side"] for item in results] == ["left", "right", "unknown", "unknown"]
    assert results[2]["status"] == "ambiguous_alignment"
    assert results[3]["status"] == "no_matching_bubble"


def test_native_text_background_is_not_taken_from_largest_photo(native_helper, tmp_path):
    window = {"x": 0, "y": 0, "width": 700, "height": 676}
    image = tmp_path / "blue-photo.png"
    write_png(image, 700, 676, [(16, 10, 668, 500, (201, 231, 255)),
                               (630, 600, 54, 32, (201, 231, 255))])
    fixture = tmp_path / "blue-photo.json"
    fixture.write_text(json.dumps({"window": window, "viewport": window,
                                   "bodies": [{"x": 640, "y": 607, "width": 34, "height": 18}]}))
    completed = subprocess.run([str(native_helper), "bubble-fixture", str(fixture), str(image)],
                               check=True, capture_output=True, text=True, timeout=10)
    result = json.loads(completed.stdout)
    assert result["status"] == "matched"
    assert result["side"] == "right"
