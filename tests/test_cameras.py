"""CCTV cameras — connect, monitor and capture (Bit 10)."""
from __future__ import annotations

import time

from imani.models import CameraRepository, SightingRepository

from .conftest import login
from .test_individuals import make_user


def create_camera(client, name="lobby", source_type="demo", source_url="",
                  location="North entrance"):
    return client.post(
        "/cameras/new",
        data={"name": name, "location": location,
              "source_type": source_type, "source_url": source_url},
        follow_redirects=False,
    )


def camera_id_for(app, name):
    with app.app_context():
        return CameraRepository.get_by_name(name)["id"]


def start_camera(client, camera_id):
    return client.post(f"/cameras/{camera_id}/start", data={},
                       follow_redirects=False)


def stop_camera(client, camera_id):
    return client.post(f"/cameras/{camera_id}/stop", data={},
                       follow_redirects=False)


def wait_for_sighting(app, camera_name, timeout=8.0):
    """Poll until the monitor has stored a capture from this camera."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            rows, total = SightingRepository.search(
                camera=camera_name, limit=5, offset=0,
            )
            if total >= 1:
                return rows
        time.sleep(0.2)
    return []


# --- Access control -------------------------------------------------------

def test_cameras_require_sign_in(client):
    assert client.get("/cameras/").status_code == 302


def test_viewer_can_see_but_not_manage(app, client):
    make_user(app, "val", role="viewer")
    login(client, identifier="val", password="Passw0rd1")

    assert client.get("/cameras/").status_code == 200

    # Registering a camera is analyst-only (role_required redirects them away).
    response = create_camera(client, name="nope")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    with app.app_context():
        assert CameraRepository.get_by_name("nope") is None


def test_analyst_can_manage(app, client):
    make_user(app, "ana", role="analyst")
    login(client, identifier="ana", password="Passw0rd1")

    assert create_camera(client, name="ana-cam").status_code == 302
    camera_id = camera_id_for(app, "ana-cam")
    assert client.post(f"/cameras/{camera_id}/delete", data={}).status_code == 302


# --- Registering a camera -------------------------------------------------

def test_create_demo_camera(app, client):
    login(client)
    response = create_camera(client, name="gate-1")
    assert response.status_code == 302

    with app.app_context():
        camera = CameraRepository.get_by_name("gate-1")
    assert camera["source_type"] == "demo"
    assert camera["source_url"] == "demo"  # blank source is filled in
    assert camera["location"] == "North entrance"
    assert camera["status"] == "stopped"


def test_duplicate_name_is_rejected(app, client):
    login(client)
    create_camera(client, name="dup")
    response = create_camera(client, name="dup")
    assert response.status_code == 400
    assert b"already exists" in response.data


def test_missing_fields_are_rejected(client):
    login(client)
    assert create_camera(client, name="").status_code == 400
    assert create_camera(client, name="blank", source_type="rtsp",
                         source_url="").status_code == 400


def test_delete_camera_removes_it(app, client):
    login(client)
    create_camera(client, name="gone")
    camera_id = camera_id_for(app, "gone")

    assert client.post(f"/cameras/{camera_id}/delete", data={}).status_code == 302
    with app.app_context():
        assert CameraRepository.get(camera_id) is None


# --- Monitoring ------------------------------------------------------------

def test_start_demo_camera_produces_captures(app, client):
    """The whole pipeline: start a camera, and it captures people by itself."""
    login(client)
    create_camera(client, name="lobby", source_type="demo")
    camera_id = camera_id_for(app, "lobby")
    manager = app.extensions["surveillance"]

    assert start_camera(client, camera_id).status_code == 302
    assert manager.is_running(camera_id)

    rows = wait_for_sighting(app, "lobby")
    assert rows, "the demo camera never stored a capture"
    assert rows[0]["camera"] == "lobby"
    # A capture must carry the traits the agent extracts.
    assert rows[0]["height_cm"] is not None
    assert rows[0]["build"] is not None

    stop_camera(client, camera_id)
    assert not manager.is_running(camera_id)


def test_status_json_reports_the_live_monitor(app, client):
    login(client)
    create_camera(client, name="gate-9", source_type="demo")
    camera_id = camera_id_for(app, "gate-9")

    payload = client.get("/cameras/status.json").get_json()
    entry = next(c for c in payload["cameras"] if c["id"] == camera_id)
    assert entry["running"] is False

    start_camera(client, camera_id)
    payload = client.get("/cameras/status.json").get_json()
    entry = next(c for c in payload["cameras"] if c["id"] == camera_id)
    assert entry["running"] is True

    stop_camera(client, camera_id)


def test_start_real_camera_without_opencv_is_unavailable(app, client, monkeypatch):
    import agent.vision as vision

    monkeypatch.setattr(vision, "available", lambda: False)
    login(client)
    create_camera(client, name="gate-3", source_type="rtsp",
                  source_url="rtsp://camera.local/stream")
    camera_id = camera_id_for(app, "gate-3")

    assert start_camera(client, camera_id).status_code == 302

    with app.app_context():
        camera = CameraRepository.get(camera_id)
    assert camera["status"] == "unavailable"
    assert "OpenCV" in (camera["last_error"] or "")
    assert not app.extensions["surveillance"].is_running(camera_id)


def test_delete_stops_the_monitor(app, client):
    login(client)
    create_camera(client, name="shortlived", source_type="demo")
    camera_id = camera_id_for(app, "shortlived")
    manager = app.extensions["surveillance"]

    start_camera(client, camera_id)
    assert manager.is_running(camera_id)

    client.post(f"/cameras/{camera_id}/delete", data={})
    assert not manager.is_running(camera_id)


# --- Live view -------------------------------------------------------------

def test_snapshot_without_frames_is_not_ready(app, client):
    login(client)
    create_camera(client, name="quiet", source_type="demo")
    camera_id = camera_id_for(app, "quiet")

    assert client.get(f"/cameras/{camera_id}/snapshot.jpg").status_code == 503
    assert client.get(f"/cameras/{camera_id}/stream").status_code == 503


def test_detail_page_renders_the_camera(app, client):
    login(client)
    create_camera(client, name="lobby", source_type="demo", location="Reception")
    camera_id = camera_id_for(app, "lobby")

    response = client.get(f"/cameras/{camera_id}")
    assert response.status_code == 200
    assert b"lobby" in response.data
    assert b"Reception" in response.data
