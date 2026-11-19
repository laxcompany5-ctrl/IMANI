"""Phone reliability fixes: retries, flush and best-frame snapshot fallback."""
from __future__ import annotations

import base64
import json
import time
from io import BytesIO

from PIL import Image

from imani import qr, surveillance
from imani.models import (
    CameraRepository,
    CameraSnapshotRepository,
    CameraVideoRepository,
)

from .conftest import login


def solid_jpeg(colour=(0, 0, 0)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (96, 72), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def textured_jpeg() -> bytes:
    image = Image.effect_noise((160, 90), 64).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def payload(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def make_camera(app, name: str) -> int:
    with app.app_context():
        return CameraRepository.create({
            "name": name,
            "location": "Test",
            "source_type": "phone",
            "source_url": "phone",
            "created_by": "admin",
        })


def test_best_available_snapshot_is_saved_when_no_frame_passes(app):
    app.config.update(
        SNAPSHOT_INTERVAL_SECONDS=9999,
        SNAPSHOT_FALLBACK_SECONDS=0,
        VIDEO_SEGMENT_SECONDS=9999,
    )
    camera_id = make_camera(app, "Fallback Cam")
    with app.app_context():
        camera = CameraRepository.get(camera_id)
    monitor = surveillance.Monitor(app, camera)
    monitor._on_frame([], solid_jpeg())
    monitor._flush(force=True)

    with app.app_context():
        rows = CameraSnapshotRepository.all(camera_id=camera_id)
    assert len(rows) == 1
    quality = json.loads(rows[0]["quality_json"])
    assert quality["fallback"] is True
    assert quality["reasons"]


def test_scan_frame_response_reports_monitor_state(app, client):
    camera_id = make_camera(app, "Status Cam")
    with app.app_context():
        token = qr.make_token(camera_id)
    assert client.get(f"/cameras/scan/{token}").status_code == 200
    response = client.post(
        f"/cameras/scan/{token}/frame", json={"frame": payload(textured_jpeg())}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert "status" in body
    assert "frames" in body


def test_scan_flush_writes_partial_segment_without_stopping(app, client):
    app.config.update(
        SNAPSHOT_INTERVAL_SECONDS=9999,
        VIDEO_SEGMENT_SECONDS=9999,
    )
    camera_id = make_camera(app, "Flush Cam")
    with app.app_context():
        token = qr.make_token(camera_id)
    assert client.get(f"/cameras/scan/{token}").status_code == 200
    for _ in range(3):
        client.post(f"/cameras/scan/{token}/frame", json={"frame": payload(textured_jpeg())})

    monitor = app.extensions["surveillance"].get(camera_id)
    assert wait_for(lambda: monitor.frames_seen >= 3)
    assert client.post(f"/cameras/scan/{token}/flush").status_code == 200

    with app.app_context():
        videos = CameraVideoRepository.all(camera_id=camera_id, kind="auto")
    assert videos
    assert videos[0]["frame_count"] >= 2
    assert monitor.status in ("live", "stopped")


def test_scan_page_contains_retry_and_flush_javascript(app, client):
    camera_id = make_camera(app, "JS Cam")
    with app.app_context():
        token = qr.make_token(camera_id)
    page = client.get(f"/cameras/scan/{token}")
    assert page.status_code == 200
    assert b"sendBeacon" in page.data
    assert b"Reconnecting to IMANI" in page.data
    assert b"/flush" in page.data


def test_phone_capture_page_has_retry_and_authenticated_flush(app, client):
    camera_id = make_camera(app, "Signed In Cam")
    login(client)
    page = client.get(f"/cameras/{camera_id}/capture")
    assert page.status_code == 200
    assert b"sendBeacon" in page.data
    assert b"Reconnecting" in page.data
    assert client.post(f"/cameras/{camera_id}/flush").status_code == 200
