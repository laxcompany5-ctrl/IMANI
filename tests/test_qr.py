"""QR pairing for phones and token-authenticated scan endpoints (Bit 13)."""
from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

from imani import qr
from imani.models import CameraRepository, CameraVideoRepository

from .conftest import login


def jpeg(colour=(30, 30, 200)):
    buffer = BytesIO()
    Image.new("RGB", (64, 48), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def make_camera(app, name="Pairing Cam", source_type="phone"):
    with app.app_context():
        existing = CameraRepository.get_by_name(name)
        if existing:
            return existing["id"]
        return CameraRepository.create({
            "name": name, "location": "Lobby",
            "source_type": source_type, "source_url": "phone",
            "created_by": "admin",
        })


def make_token(app, camera_id):
    with app.app_context():
        return qr.make_token(camera_id)


def test_qr_requires_sign_in(client):
    assert client.get("/cameras/1/qr.png").status_code == 302


def test_qr_page_and_png(app, client):
    login(client)
    camera_id = make_camera(app)
    page = client.get(f"/cameras/{camera_id}/qr")
    assert page.status_code == 200
    assert b"Pair a phone" in page.data or b"Pair " in page.data

    image = client.get(f"/cameras/{camera_id}/qr.png")
    assert image.status_code == 200
    assert image.mimetype == "image/png"
    assert image.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_scan_page_needs_no_session(app, client):
    camera_id = make_camera(app, "Scan Cam")
    token = make_token(app, camera_id)
    response = client.get(f"/cameras/scan/{token}")
    assert response.status_code == 200
    assert b"Scan Cam" in response.data
    assert b"Start camera" in response.data


def test_invalid_token_is_rejected(client):
    assert client.get("/cameras/scan/not-a-real-token").status_code == 403


def test_public_base_url_can_be_overridden(app):
    with app.test_request_context("/", base_url="http://127.0.0.1:5055"):
        app.config["PUBLIC_BASE_URL"] = "https://imani.example.com"
        assert qr.public_base_url() == "https://imani.example.com"
        app.config["PUBLIC_BASE_URL"] = ""
        app.config["PUBLIC_SCHEME"] = "https"
        app.config["PUBLIC_PORT"] = "5443"
        assert qr.public_base_url().startswith("https://")
        assert qr.public_base_url().endswith(":5443")


def test_https_scan_page_offers_secure_link(app, client):
    camera_id = make_camera(app, "Secure Cam")
    token = make_token(app, camera_id)
    app.config["HTTPS_ENABLED"] = True
    app.config["PUBLIC_PORT"] = "5443"
    response = client.get(f"/cameras/scan/{token}", base_url="http://192.0.2.10:5055")
    assert response.status_code == 200
    assert b"https://192.0.2.10:5443/cameras/scan/" in response.data


def test_scan_frame_starts_monitor_and_accepts_frame(app, client):
    camera_id = make_camera(app, "Frame Cam")
    token = make_token(app, camera_id)
    payload = "data:image/jpeg;base64," + base64.b64encode(jpeg()).decode()
    response = client.post(
        f"/cameras/scan/{token}/frame",
        json={"frame": payload},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    with app.app_context():
        assert app.extensions["surveillance"].get(camera_id) is not None


def test_scan_audio_is_saved_and_indexed(app, client):
    camera_id = make_camera(app, "Audio Cam")
    token = make_token(app, camera_id)
    response = client.post(
        f"/cameras/scan/{token}/audio",
        data={"audio": (BytesIO(b"fake-ogg-audio"), "slice.webm")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    with app.app_context():
        row = CameraVideoRepository.get(payload["id"])

        assert row["kind"] == "audio"
        assert row["mime_type"] == "audio/webm"


def test_rejects_non_token_frame(client):
    assert client.post("/cameras/scan/bad/frame", json={}).status_code == 403
