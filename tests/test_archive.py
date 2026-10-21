"""The capture archive — footage stored, served and pruned (Bit 11)."""
from __future__ import annotations

import time

import pytest

from imani import capture, media
from imani.db import get_db
from imani.models import MediaRepository

from .conftest import login
from .test_cameras import (
    camera_id_for,
    create_camera,
    start_camera,
    stop_camera,
    wait_for_sighting,
)
from .test_individuals import make_user

CAPTURE = {
    "camera": "gate-3",
    "height_cm": 180,
    "build": "athletic",
    "posture": "upright",
    "gait": "brisk",
}


def record_a_capture(app, camera="gate-3", frames=None):
    """Create a sighting with footage and return (sighting_id, manifest)."""
    frames = frames or [(1.0, b"\xff\xd8first"), (2.0, b"\xff\xd8second")]
    with app.app_context():
        result = capture.ingest(dict(CAPTURE, camera=camera))
        manifest = media.save_capture(
            camera, "2026-10-21 12:00:00", result["reference"], frames, 5.0,
        )
        media.attach(result["sighting_id"], manifest)
        return result["sighting_id"], manifest


def test_archive_requires_sign_in(client):
    assert client.get("/archive/").status_code == 302
    assert client.get("/archive/1").status_code == 302


# --- The media store itself ----------------------------------------------

def test_footage_is_written_and_read_back(app):
    with app.app_context():
        manifest = media.save_capture(
            "gate-3", "2026-10-21 10:00:00", "SG-00042",
            [(1.0, b"\xff\xd8one"), (2.0, b"\xff\xd8two")], 5.0,
        )
        assert manifest["count"] == 2
        assert manifest["duration_ms"] == 400
        # The poster is the last frame.
        assert media.read(manifest["poster"]) == b"\xff\xd8two"
        stored = media.manifest(manifest["dir"])
        assert stored["count"] == 2
        assert [f["index"] for f in stored["frames"]] == [1, 2]


def test_media_paths_cannot_escape_the_root(app):
    with app.app_context():
        with pytest.raises(ValueError):
            media.read("../../../../etc/passwd")


def test_usage_counts_files(app):
    record_a_capture(app)
    with app.app_context():
        usage = media.usage()
    assert usage["files"] >= 3  # two frames + poster + manifest
    assert usage["bytes"] > 0


# --- Recording from a live camera ---------------------------------------

def test_demo_camera_records_replayable_footage(app, client):
    login(client)
    create_camera(client, name="recorder", source_type="demo")
    camera_id = camera_id_for(app, "recorder")

    start_camera(client, camera_id)
    rows = wait_for_sighting(app, "recorder")
    assert rows, "the demo camera stored no capture"
    sighting_id = rows[0]["id"]

    deadline = time.time() + 8
    assets = []
    while time.time() < deadline:
        with app.app_context():
            assets = MediaRepository.for_sighting(sighting_id)
        if len(assets) >= 2:
            break
        time.sleep(0.2)

    assert {asset["kind"] for asset in assets} == {"frame", "clip"}
    with app.app_context():
        poster = MediaRepository.poster_for(sighting_id)
        clip = MediaRepository.clip_for(sighting_id)
        assert media.absolute(poster["path"]).exists()
        assert media.manifest(clip["path"])["count"] >= 1

    # The archive serves the still and every clip frame.
    still = client.get(f"/archive/{sighting_id}/frame")
    assert still.status_code == 200
    assert still.mimetype == "image/jpeg"

    manifest = client.get(f"/archive/{sighting_id}/clip.json").get_json()
    assert manifest["fps"] == 5
    assert len(manifest["frames"]) >= 1
    first = client.get(f"/archive/{sighting_id}/clip/1")
    assert first.status_code == 200
    assert first.mimetype == "image/jpeg"

    stop_camera(client, camera_id)


# --- Browsing the archive ------------------------------------------------

def test_archive_lists_and_opens_a_capture(app, client):
    sighting_id, _manifest = record_a_capture(app, camera="north-gate")
    login(client)

    listing = client.get("/archive/")
    assert listing.status_code == 200
    assert b"north-gate" in listing.data
    assert b"Capture archive" in listing.data

    detail = client.get(f"/archive/{sighting_id}")
    assert detail.status_code == 200
    assert b"Footage" in detail.data
    assert b"Play recording" in detail.data


def test_archive_filters_by_camera(app, client):
    record_a_capture(app, camera="east-gate")
    record_a_capture(app, camera="west-gate")
    login(client)

    response = client.get("/archive/?camera=east-gate")
    body = response.data
    assert response.status_code == 200
    assert b"east-gate" in body
    # One card only — the other camera's capture is filtered out.
    assert body.count(b"archive-card") == 1


def test_capture_without_footage_is_still_listed(app, client):
    with app.app_context():
        capture.ingest(dict(CAPTURE, camera="no-footage"))
    login(client)

    response = client.get("/archive/")
    assert response.status_code == 200
    assert b"no-footage" in response.data
    assert b"no footage" in response.data


def test_missing_frames_return_404(app, client):
    with app.app_context():
        result = capture.ingest(dict(CAPTURE, camera="empty"))
    login(client)

    assert client.get(f"/archive/{result['sighting_id']}/frame").status_code == 404
    assert client.get(f"/archive/{result['sighting_id']}/clip.json").status_code == 404


# --- Retention -----------------------------------------------------------

def test_prune_removes_only_old_footage(app):
    old_id, _ = record_a_capture(app, camera="old")
    new_id, _ = record_a_capture(app, camera="new")

    with app.app_context():
        get_db().execute(
            "UPDATE capture_media SET created_at = '2000-01-01 00:00:00' "
            "WHERE sighting_id = ?", (old_id,),
        )
        get_db().commit()
        before = media.usage()["files"]
        removed = media.prune(days=90)
        after = media.usage()["files"]
        assert removed["deleted"] == 2
        assert after < before
        assert MediaRepository.for_sighting(old_id) == []
        assert len(MediaRepository.for_sighting(new_id)) == 2


def test_prune_is_admin_only(app, client):
    make_user(app, "vic", role="viewer")
    login(client, identifier="vic", password="Passw0rd1")
    response = client.post("/archive/prune")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


# --- Lifecycle -----------------------------------------------------------

def test_deleting_a_sighting_deletes_its_footage(app, client):
    sighting_id, manifest = record_a_capture(app, camera="doomed")
    login(client)

    with app.app_context():
        assert media.absolute(manifest["poster"]).exists()

    assert client.post(f"/sightings/{sighting_id}/delete").status_code == 302
    with app.app_context():
        assert MediaRepository.for_sighting(sighting_id) == []
