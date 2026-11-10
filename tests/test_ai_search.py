"""AI search over saved photos and recordings (Bit 13)."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from imani import media
from imani.models import CameraRepository, CameraSnapshotRepository, CameraVideoRepository

from .conftest import login


def jpeg(colour, size=(80, 60)):
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def make_photo(app, camera_name="Search Gate", colour=(200, 30, 30)):
    with app.app_context():
        cam = CameraRepository.get_by_name(camera_name)
        if cam is None:
            camera_id = CameraRepository.create({
                "name": camera_name, "location": "Test",
                "source_type": "demo", "source_url": "demo",
                "created_by": "admin",
            })
        else:
            camera_id = cam["id"]
        data = jpeg(colour)
        saved = media.save_snapshot(camera_name, data, camera_id=camera_id,
                                    source_type="demo")
        return CameraSnapshotRepository.create({
            "camera_id": camera_id,
            "camera_name": camera_name,
            "filename": saved["filename"],
            "path": saved["path"],
            "source_type": "demo",
            "captured_at": "2026-05-01 10:00:00",
            "features_json": saved["features_json"],
        })


def test_search_requires_sign_in(client):
    assert client.get("/ai-search/").status_code == 302


def test_search_page_renders(app, client):
    login(client)
    response = client.get("/ai-search/")
    assert response.status_code == 200
    assert b"AI Feature Search" in response.data


def test_description_search_finds_matching_colour(app, client):
    login(client)
    make_photo(app, "Red Gate", (205, 25, 25))
    response = client.post("/ai-search/", data={
        "search_type": "description",
        "query": "person in a red jacket",
    })
    assert response.status_code == 200
    assert b"Red Gate" in response.data
    assert b"% match" in response.data


def test_description_search_excludes_wrong_colour(app, client):
    login(client)
    make_photo(app, "Blue Gate", (25, 25, 205))
    response = client.post("/ai-search/", data={
        "search_type": "description",
        "query": "person in a red jacket",
    })
    assert response.status_code == 200
    assert b"No matches found" in response.data


def test_photo_upload_search_scores_similar_frames(app, client):
    login(client)
    make_photo(app, "Upload Match", (30, 200, 30))
    response = client.post(
        "/ai-search/",
        data={
            "search_type": "image",
            "photo": (BytesIO(jpeg((30, 200, 30))), "probe.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"Upload Match" in response.data
    assert b"100.0% match" in response.data


def test_annotations_are_used_by_search(app, client):
    login(client)
    snap_id = make_photo(app, "Annotated Gate", (30, 30, 30))
    response = client.post(
        f"/snapshots/{snap_id}/describe",
        data={"description": "delivery driver in a dark coat"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with app.app_context():
        assert CameraSnapshotRepository.get(snap_id)["description"] == (
            "delivery driver in a dark coat"
        )
    search = client.post("/ai-search/", data={
        "search_type": "description",
        "query": "delivery driver",
    })
    assert search.status_code == 200
    assert b"Annotated Gate" in search.data


def test_audio_recording_is_searchable_and_playable(app, client):
    login(client)
    with app.app_context():
        camera = CameraRepository.get_by_name("Audio Phone")
        if camera is None:
            camera_id = CameraRepository.create({
                "name": "Audio Phone", "location": "Test",
                "source_type": "phone", "source_url": "phone",
                "created_by": "admin",
            })
            camera = CameraRepository.get(camera_id)
        saved = media.save_audio("Audio Phone", b"OggS-not-really-audio",
                                 "audio/webm", camera_id=camera["id"],
                                 source_type="phone")
        video_id = CameraVideoRepository.create({
            "camera_id": camera["id"],
            "camera_name": "Audio Phone",
            "filename": saved["filename"],
            "path": saved["path"],
            "source_type": "phone",
            "size_bytes": saved["size_bytes"],
            "mime_type": "audio/webm",
            "kind": "audio",
            "description": "doorbell conversation",
            "captured_at": "2026-05-01 10:05:00",
        })
    listing = client.get("/videos/?kind=audio")
    assert listing.status_code == 200
    assert b"Audio Phone" in listing.data
    stream = client.get(f"/videos/{video_id}/audio")
    assert stream.status_code == 200
    assert stream.mimetype == "audio/webm"
    search = client.post("/ai-search/", data={
        "search_type": "description", "query": "doorbell",
    })
    assert b"Audio Phone" in search.data
