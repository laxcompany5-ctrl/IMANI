"""Capture agent tests — trait maths, tracking, payloads and delivery (Bit 9).

These run without OpenCV: only the pure pieces are exercised, plus the HTTP
client with a stubbed transport.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from agent import traits
from agent.agent import AgentConfig, build_detection_payload, process, synthetic_frames
from agent.client import DeliveryError, ImaniClient
from agent.tracker import IouTracker, Track


# --- Trait maths ----------------------------------------------------------

def test_option_tuples_match_the_server():
    """The agent and the server must agree on the allowed trait values."""
    from imani import validation

    assert traits.BUILD_OPTIONS == validation.BUILD_OPTIONS
    assert set(traits.GAIT_OPTIONS) == set(validation.GAIT_OPTIONS)
    assert traits.POSTURE_OPTIONS == validation.POSTURE_OPTIONS
    assert traits.MIN_HEIGHT_CM == validation.MIN_HEIGHT_CM
    assert traits.MAX_HEIGHT_CM == validation.MAX_HEIGHT_CM


def test_box_geometry():
    box = traits.Box(10, 20, 30, 40)
    assert box.bottom == 60
    assert box.center == (25, 40)
    assert round(box.aspect, 4) == round(30 / 40, 4)


def test_box_iou():
    a = traits.Box(0, 0, 10, 10)
    assert a.iou(traits.Box(0, 0, 10, 10)) == 1.0
    assert a.iou(traits.Box(100, 100, 10, 10)) == 0.0
    assert 0 < a.iou(traits.Box(5, 0, 10, 10)) < 1


def test_height_estimate_roundtrips():
    """A box drawn from the pinhole model comes back as the real height."""
    calibration = traits.Calibration(camera_height_cm=250, principal_y=360)
    # A 180 cm person whose feet are 260 px below the horizon.
    box = traits.Box(0, 620 - 187.2, 80, 187.2)
    assert calibration.height_cm(box) == 180


def test_height_none_above_horizon():
    calibration = traits.Calibration(principal_y=400)
    box = traits.Box(0, 0, 50, 100)  # feet at 100, above the horizon
    assert calibration.height_cm(box) is None


def test_build_bands():
    assert traits.build_from_aspect(0.30) == "slim"
    assert traits.build_from_aspect(0.40) == "average"
    assert traits.build_from_aspect(0.46) == "athletic"
    assert traits.build_from_aspect(0.60) == "heavy"
    assert traits.build_from_aspect(0) is None


def test_posture_from_axis():
    assert traits.posture_from_axis((100, 0), (100, 200)) == "upright"
    assert traits.posture_from_axis((130, 0), (100, 200)) == "neutral"
    assert traits.posture_from_axis((150, 0), (100, 200)) == "stooped"
    assert traits.posture_from_axis((200, 0), (100, 200)) == "hunched"
    assert traits.posture_from_axis(None, (100, 200)) is None


def test_gait_from_speed():
    assert traits.gait_from_speed(1.5) == "brisk"
    assert traits.gait_from_speed(0.7) == "normal"
    assert traits.gait_from_speed(0.3) == "slow"
    assert traits.gait_from_speed(0.05) == "shuffle"
    assert traits.gait_from_speed(None) is None


def test_format_timestamp_is_utc():
    assert traits.format_timestamp(0) == "1970-01-01 00:00:00"


def test_build_payload_omits_unknown_traits():
    calibration = traits.Calibration(principal_y=360)
    payload = traits.build_payload("gate-3", traits.Box(0, 100, 50, 200),
                                   epoch_seconds=0, calibration=calibration)
    assert payload["camera"] == "gate-3"
    assert payload["captured_at"] == "1970-01-01 00:00:00"
    # height is out of range for this box, so it must be absent, not faked
    assert "height_cm" not in payload
    assert payload["build"] in traits.BUILD_OPTIONS
    assert "gait" not in payload
    assert "face_template" not in payload


# --- Tracking -------------------------------------------------------------

def test_tracker_associates_a_moving_box():
    tracker = IouTracker(iou_threshold=0.3)
    tracker.update([traits.Detection(box=traits.Box(100, 200, 80, 200))], 0.0)
    tracks = tracker.update([traits.Detection(box=traits.Box(110, 200, 80, 200))], 0.2)
    assert len(tracks) == 1
    assert tracks[0].hits == 2


def test_tracker_starts_a_new_track_for_a_distant_box():
    tracker = IouTracker(iou_threshold=0.3)
    tracker.update([traits.Detection(box=traits.Box(0, 0, 50, 100))], 0.0)
    tracks = tracker.update([traits.Detection(box=traits.Box(500, 0, 50, 100))], 0.2)
    assert len(tracks) == 2


def test_tracker_retires_missing_tracks():
    tracker = IouTracker(max_missed=2)
    tracker.update([traits.Detection(box=traits.Box(0, 0, 50, 100))], 0.0)
    for step in range(3):
        tracker.update([], 0.1 * (step + 1))
    assert tracker.tracks == []


def test_should_emit_confirms_then_cools_down():
    tracker = IouTracker()
    track = Track(id=1, detection=traits.Detection(box=traits.Box(0, 0, 50, 100)),
                  first_seen=0.0, last_seen=0.0, hits=3)
    assert tracker.should_emit(track, 0.0, confirm_hits=3, min_interval=20) is True
    tracker.mark_emitted(track, 0.0)
    assert tracker.should_emit(track, 5.0, 3, 20) is False
    assert tracker.should_emit(track, 25.0, 3, 20) is True

    unconfirmed = Track(id=2, detection=track.detection, first_seen=0.0,
                        last_seen=0.0, hits=1)
    assert tracker.should_emit(unconfirmed, 0.0, 3, 20) is False


def test_track_speed_is_positive_and_scaled():
    tracker = IouTracker()
    for step in range(6):
        tracker.update([traits.Detection(
            box=traits.Box(100 + 20 * step, 200, 80, 200))], 0.2 * step)
    speed = tracker.tracks[0].speed()
    assert speed is not None and speed > 0


# --- End-to-end (no camera) ----------------------------------------------

class FakeClient:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return {"status": "new"}


def test_process_emits_one_capture_per_person():
    client = FakeClient()
    config = AgentConfig(camera="gate-3")
    emitted = process(config, synthetic_frames(), client, emit=lambda _p: None)

    assert emitted == 1
    assert len(client.payloads) == 1

    payload = client.payloads[0]
    assert payload["camera"] == "gate-3"
    # The synthetic walk is built for a ~180 cm person under the defaults.
    assert 170 <= payload["height_cm"] <= 190
    assert payload["build"] in traits.BUILD_OPTIONS
    assert payload["gait"] in traits.GAIT_OPTIONS
    assert payload["posture"] == "upright"


def test_process_dry_run_needs_no_client():
    seen = []
    emitted = process(AgentConfig(dry_run=True), synthetic_frames(),
                      None, emit=seen.append)
    assert emitted == 1
    assert seen[0]["camera"] == "camera-1"


def test_build_detection_payload_shapes():
    tracker = IouTracker()
    for step in range(4):
        tracker.update([traits.Detection(
            box=traits.Box(100 + 15 * step, 100, 80, 200),
            head=(140 + 15 * step, 100), feet=(140 + 15 * step, 300),
        )], 0.2 * step)
    payload = build_detection_payload(AgentConfig(camera="c1"),
                                      tracker.tracks[0], 1.0)
    assert payload["camera"] == "c1"
    assert payload["posture"] == "upright"


# --- HTTP client ----------------------------------------------------------

class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_posts_json_with_api_key(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["key"] = request.get_header("X-api-key")
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse(b'{"status": "identified"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ImaniClient("http://imani.test/ingest/capture", "secret")

    result = client.send({"camera": "gate-3", "height_cm": 180})

    assert result == {"status": "identified"}
    assert captured["url"] == "http://imani.test/ingest/capture"
    assert captured["method"] == "POST"
    assert captured["key"] == "secret"
    assert captured["body"]["height_cm"] == 180


def test_client_does_not_retry_a_4xx(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {},
            io.BytesIO(b'{"error": "invalid or missing API key"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ImaniClient("http://imani.test/ingest/capture", "wrong", retries=3)

    with pytest.raises(DeliveryError) as excinfo:
        client.send({"height_cm": 180})
    assert excinfo.value.status == 403
    assert calls["n"] == 1  # gave up immediately


def test_client_retries_transient_errors(monkeypatch):
    from agent import client as client_module

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return FakeResponse(b'{"status": "new"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)

    client = ImaniClient("http://imani.test/ingest/capture", "k", retries=3)
    assert client.send({"height_cm": 180}) == {"status": "new"}
    assert calls["n"] == 3


def test_client_gives_up_after_retries(monkeypatch):
    from agent import client as client_module

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)

    client = ImaniClient("http://imani.test/ingest/capture", "k", retries=2)
    with pytest.raises(DeliveryError):
        client.send({"height_cm": 180})
