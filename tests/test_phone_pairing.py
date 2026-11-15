"""QR pairing must work for any camera source, not only ``phone``."""
from __future__ import annotations

import base64
import time
from io import BytesIO

from PIL import Image

from imani import qr
from imani.models import CameraRepository


def jpeg(colour=(20, 160, 90)):
    buffer = BytesIO()
    Image.new("RGB", (80, 60), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def make_camera(app, name, source_type, source_url):
    with app.app_context():
        return CameraRepository.create({
            "name": name,
            "location": "Test",
            "source_type": source_type,
            "source_url": source_url,
            "created_by": "admin",
        })


def scan_payload():
    return "data:image/jpeg;base64," + base64.b64encode(jpeg()).decode()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_scan_frame_takes_over_a_demo_camera(app, client):
    camera_id = make_camera(app, "Demo Pairing", "demo", "demo")
    with app.app_context():
        token = qr.make_token(camera_id)

    response = client.post(
        f"/cameras/scan/{token}/frame", json={"frame": scan_payload()}
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    monitor = app.extensions["surveillance"].get(camera_id)
    assert monitor is not None
    assert monitor._use_phone_source is True
    assert wait_for(lambda: monitor.latest_jpeg() is not None)
    assert monitor.status == "live"


def test_scan_frame_takes_over_an_rtsp_camera_without_opencv(app, client):
    camera_id = make_camera(
        app, "RTSP Pairing", "rtsp", "rtsp://example.invalid/stream"
    )
    with app.app_context():
        token = qr.make_token(camera_id)

    response = client.post(
        f"/cameras/scan/{token}/frame", json={"frame": scan_payload()}
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    monitor = app.extensions["surveillance"].get(camera_id)
    assert monitor is not None
    assert monitor._use_phone_source is True
    assert wait_for(lambda: monitor.status == "live")
    assert monitor.error is None


def test_phone_mode_survives_repeated_frames(app, client):
    camera_id = make_camera(app, "Repeated Pairing", "demo", "demo")
    with app.app_context():
        token = qr.make_token(camera_id)

    first = client.post(
        f"/cameras/scan/{token}/frame", json={"frame": scan_payload()}
    )
    monitor = app.extensions["surveillance"].get(camera_id)
    assert first.status_code == 200
    assert monitor is not None

    for _ in range(3):
        again = client.post(
            f"/cameras/scan/{token}/frame", json={"frame": scan_payload()}
        )
        assert again.status_code == 200

    # The running phone monitor must be reused, not restarted per frame.
    assert app.extensions["surveillance"].get(camera_id) is monitor
    assert wait_for(lambda: monitor.frames_seen >= 1)
