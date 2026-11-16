"""Device connection protocol: QR registration, auto-start and snapshots."""
from __future__ import annotations

import base64
import time
from io import BytesIO

from PIL import Image

from imani import qr
from imani.models import (
    CameraRepository,
    CameraSnapshotRepository,
)

from .conftest import login


def textured_jpeg(colour=(90, 120, 180)) -> bytes:
    """Noise has enough contrast and edge detail to pass the quality gate."""
    image = Image.effect_noise((160, 90), 64).convert("RGB")
    overlay = Image.new("RGB", image.size, colour)
    image = Image.blend(image, overlay, 0.15)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def phone_camera(app):
    with app.app_context():
        for camera in CameraRepository.all():
            if camera["source_type"] == "phone" and camera["name"].startswith("Phone "):
                return camera
    return None


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_connect_page_shows_qr_and_requires_login(app, client):
    response = client.get("/cameras/connect")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]

    login(client)
    page = client.get("/cameras/connect")
    assert page.status_code == 200
    assert b"Connect a phone" in page.data
    assert b"/cameras/connect/qr.png" in page.data

    with app.app_context():
        token = qr.make_connect_token()
    image = client.get(f"/cameras/connect/qr.png?token={token}")
    assert image.status_code == 200
    assert image.mimetype == "image/png"
    assert image.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_invalid_connect_token_is_rejected(client):
    assert client.get("/cameras/connect/not-a-real-token").status_code == 403


def test_scanning_registers_the_phone_and_starts_the_monitor(app, client):
    with app.app_context():
        token = qr.make_connect_token()
        before = len(CameraRepository.all())

    response = client.get(f"/cameras/connect/{token}")
    assert response.status_code == 200
    assert b"Start camera" in response.data
    assert b"Phone " in response.data

    with app.app_context():
        cameras = CameraRepository.all()
        assert len(cameras) == before + 1
        camera = phone_camera(app)
        assert camera is not None
        assert camera["source_type"] == "phone"
        assert camera["source_url"] == "phone"
        assert camera["location"] == "Paired by QR"
        camera_id = camera["id"]

    monitor = app.extensions["surveillance"].get(camera_id)
    assert monitor is not None
    assert monitor._use_phone_source is True

    # Re-opening the same link on the same phone must reuse the device.
    again = client.get(f"/cameras/connect/{token}")
    assert again.status_code == 200
    with app.app_context():
        assert len(CameraRepository.all()) == before + 1


def test_scan_page_starts_phone_monitor_before_the_first_frame(app, client):
    with app.app_context():
        camera_id = CameraRepository.create(
            {
                "name": "Scan Start Cam",
                "source_type": "phone",
                "source_url": "phone",
                "created_by": "admin",
            }
        )
        token = qr.make_token(camera_id)

    response = client.get(f"/cameras/scan/{token}")
    assert response.status_code == 200

    monitor = app.extensions["surveillance"].get(camera_id)
    assert monitor is not None
    assert monitor._use_phone_source is True


def test_connected_phone_records_filtered_snapshot(app, client):
    with app.app_context():
        token = qr.make_connect_token()

    assert client.get(f"/cameras/connect/{token}").status_code == 200
    with app.app_context():
        camera = phone_camera(app)
        assert camera is not None
        camera_id = camera["id"]
        camera_token = qr.make_token(camera_id)

    payload = "data:image/jpeg;base64," + base64.b64encode(
        textured_jpeg()
    ).decode()
    for _ in range(3):
        response = client.post(
            f"/cameras/scan/{camera_token}/frame",
            json={"frame": payload},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True

    def has_snapshot():
        with app.app_context():
            return CameraSnapshotRepository.count(camera_id) > 0

    assert wait_for(has_snapshot)
    with app.app_context():
        snapshot = CameraSnapshotRepository.all(camera_id=camera_id)[0]
        assert snapshot["source_type"] == "phone"
        assert snapshot["quality_json"]
        assert snapshot["features_json"]

    monitor = app.extensions["surveillance"].get(camera_id)
    assert monitor is not None


def test_connected_devices_page_lists_new_phone_and_status(app, client):
    login(client)
    with app.app_context():
        token = qr.make_connect_token()
    client.get(f"/cameras/connect/{token}")

    page = client.get("/cameras/live")
    assert page.status_code == 200
    assert b"Connect phone (QR)" in page.data

    status = client.get("/cameras/status.json")
    assert status.status_code == 200
    payload = status.get_json()["cameras"]
    phone = next(item for item in payload if item["source_type"] == "phone")
    assert phone["name"].startswith("Phone ")
    assert "frames_seen" in phone
