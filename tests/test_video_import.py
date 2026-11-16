"""Video URL import and 4K snapshot extraction (Bit 14)."""
from __future__ import annotations

import http.server
import json
import random
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from imani import video_import
from imani.models import (
    CameraSnapshotRepository,
    CameraVideoRepository,
    VideoImportRepository,
    VideoImportSnapshotRepository,
)

from .conftest import login


def _make_jpeg(path: Path, image: Image.Image) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=90)
    return path


def test_upscale_to_4k_pads_and_records_dimensions(tmp_path):
    source = _make_jpeg(
        tmp_path / "source.jpg", Image.new("RGB", (640, 360), (30, 80, 160))
    )
    target = tmp_path / "out" / "snap_4k.jpg"
    thumb = tmp_path / "out" / "snap_thumb.jpg"
    info = video_import.upscale_to_4k(source, target, thumb, 3840, 2160)

    assert info["original_width"] == 640
    assert info["original_height"] == 360
    assert info["width"] == 3840
    assert info["height"] == 2160
    assert info["size_bytes"] == target.stat().st_size
    with Image.open(target) as image:
        assert image.size == (3840, 2160)
        assert image.format == "JPEG"
    with Image.open(thumb) as image:
        assert image.size == (480, 270)


def test_interval_selector_picks_evenly_spaced_frames(tmp_path):
    candidates = [
        {
            "index": index,
            "timestamp": float(index),
            "path": _make_jpeg(
                tmp_path / f"{index}.jpg",
                Image.new("RGB", (96, 54), (index * 20, 40, 90)),
            ),
        }
        for index in range(10)
    ]
    selected = video_import.select_snapshots(
        candidates, "interval", max_snapshots=3, min_gap=1.0
    )
    assert [item["index"] for item in selected] == [0, 4, 9]


def test_scene_selector_caps_frames_and_keeps_order(tmp_path):
    random.seed(14)
    candidates = []
    for index in range(8):
        image = Image.new("RGB", (96, 54))
        pixels = image.load()
        for x in range(96):
            for y in range(54):
                pixels[x, y] = (
                    random.randrange(256),
                    random.randrange(256),
                    random.randrange(256),
                )
        candidates.append(
            {
                "index": index,
                "timestamp": float(index),
                "path": _make_jpeg(tmp_path / f"scene-{index}.jpg", image),
            }
        )

    selected = video_import.select_snapshots(
        candidates, "scene", max_snapshots=4, min_gap=1.0
    )
    assert 1 <= len(selected) <= 4
    timestamps = [item["timestamp"] for item in selected]
    assert timestamps == sorted(timestamps)
    assert selected[0]["index"] == 0


def test_video_import_requires_login(client):
    response = client.get("/video-import/")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_video_import_rejects_non_http_url(app, client):
    login(client)
    response = client.post(
        "/video-import/start",
        data={"source_url": "ftp://example.com/video.mp4"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert VideoImportRepository.count() == 0


@pytest.mark.skipif(not video_import.ffmpeg_exe(), reason="ffmpeg is unavailable")
def test_video_import_end_to_end_extracts_separate_4k_snapshots(app, client, tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    served = tmp_path / "served"
    served.mkdir()
    source = served / "sample.mp4"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
            "-t", "4", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(source),
        ],
        check=True,
        capture_output=True,
    )

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(served), **kwargs
    )
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/sample.mp4"

    try:
        login(client)
        response = client.post(
            "/video-import/start",
            data={
                "source_url": url,
                "mode": "interval",
                "interval": "1",
                "max_snapshots": "4",
            },
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        import_id = int(location.rstrip("/").split("/")[-1])

        with app.app_context():
            deadline = time.time() + 30
            row = VideoImportRepository.get(import_id)
            while row["status"] not in ("complete", "failed") and time.time() < deadline:
                time.sleep(0.2)
                row = VideoImportRepository.get(import_id)
            assert row["status"] == "complete", row["error"]
            assert row["snapshot_count"] >= 1
            assert row["video_path"]
            assert row["width"] == 640 and row["height"] == 360
            snapshots = VideoImportSnapshotRepository.all(import_id)

            assert snapshots
            for snapshot in snapshots:
                original = video_import.resolve(snapshot["original_path"])
                upscaled = video_import.resolve(snapshot["upscaled_path"])
                thumb = video_import.resolve(snapshot["thumb_path"])
                assert original.is_file()
                assert upscaled.is_file()
                assert thumb.is_file()
                assert original != upscaled
                with Image.open(upscaled) as image:
                    assert image.size == (3840, 2160)
                assert snapshot["width"] == 3840
                assert snapshot["height"] == 2160

        detail = client.get(f"/video-import/{import_id}")
        assert detail.status_code == 200
        assert b"4K snapshots" in detail.data

        with app.app_context():
            first = VideoImportSnapshotRepository.all(import_id)[0]
        for variant in ("thumb", "original", "4k"):
            file_response = client.get(
                f"/video-import/{import_id}/snapshot/{first['id']}/{variant}"
            )
            assert file_response.status_code == 200
            assert file_response.mimetype == "image/jpeg"
            assert file_response.data[:2] == b"\xff\xd8"

        bundle = client.get(f"/video-import/{import_id}/snapshots-4k.zip")
        assert bundle.status_code == 200
        assert bundle.data[:2] == b"PK"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- Bit 15/16: quality filtering, lighting and library publication ---------

def _textured(size=(160, 90)) -> Image.Image:
    image = Image.effect_noise(size, 64).convert("RGB")
    overlay = Image.new("RGB", size, (80, 100, 150))
    return Image.blend(image, overlay, 0.15)


def _jpeg_bytes(image: Image.Image) -> bytes:
    buffer = __import__("io").BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _make_completed_import(app, title="Test Import"):
    """Create a finished import on disk without downloading a real video."""
    with app.app_context():
        import_id = VideoImportRepository.create({
            "source_url": "https://example.com/video.mp4",
            "mode": "interval",
            "interval_sec": 1,
            "max_snapshots": 1,
            "output_width": 3840,
            "output_height": 2160,
            "created_by": "admin",
        })
        base = video_import.root() / str(import_id)
        source = base / "source" / "source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"not-a-real-mp4")
        original = base / "snapshots" / "original" / "snap_0001.jpg"
        upscaled = base / "snapshots" / "4k" / "snap_0001_4k.jpg"
        thumb = base / "snapshots" / "thumbs" / "snap_0001_thumb.jpg"
        _make_jpeg(original, _textured())
        info = video_import.upscale_to_4k(
            original, upscaled, thumb, 3840, 2160,
        )
        quality = video_import.analyze_quality(original)
        VideoImportSnapshotRepository.create({
            "import_id": import_id,
            "timestamp_sec": 1.5,
            "original_path": video_import._stored(original),
            "upscaled_path": video_import._stored(upscaled),
            "thumb_path": video_import._stored(thumb),
            "original_width": info["original_width"],
            "original_height": info["original_height"],
            "width": info["width"],
            "height": info["height"],
            "size_bytes": info["size_bytes"],
            "quality_json": json.dumps(quality),
        })
        VideoImportRepository.update(
            import_id,
            title=title,
            status="complete",
            stage="Complete",
            progress=100,
            video_path=video_import._stored(source),
            mime_type="video/mp4",
            duration_sec=4.0,
            fps=25.0,
            width=640,
            height=360,
            snapshot_count=1,
            completed_at="2026-11-16 10:00:00",
        )
        return import_id


def test_quality_gate_rejects_black_and_accepts_detail():
    black = _jpeg_bytes(Image.new("RGB", (160, 90), (2, 2, 2)))
    metrics = video_import.analyze_quality_bytes(black)
    assert metrics["ok"] is False
    assert "too dark" in metrics["reasons"]

    textured = _jpeg_bytes(_textured())
    assert video_import.analyze_quality_bytes(textured)["ok"] is True


def test_lighting_correction_lifts_a_dark_frame():
    dark = Image.effect_noise((160, 90), 40).point(
        lambda value: int(value * 0.28)
    ).convert("RGB")
    before = video_import.analyze_quality_bytes(_jpeg_bytes(dark))["brightness"]
    corrected, actions, after = video_import.normalise_lighting_bytes(
        _jpeg_bytes(dark)
    )
    assert "shadow lift" in " ".join(actions)
    assert after > before + 0.1
    assert video_import.analyze_quality_bytes(corrected)["brightness"] > before


def test_upscale_can_normalise_lighting(tmp_path):
    dark = Image.effect_noise((160, 90), 40).point(
        lambda value: int(value * 0.28)
    ).convert("RGB")
    source = _make_jpeg(tmp_path / "dark.jpg", dark)
    info = video_import.upscale_to_4k(
        source, tmp_path / "dark-4k.jpg", tmp_path / "dark-thumb.jpg",
        1280, 720, normalise=True,
    )
    assert info["lighting"]
    with Image.open(tmp_path / "dark-4k.jpg") as image:
        assert image.size == (1280, 720)


def test_publish_moves_import_into_videos_and_snapshots(app, client):
    import_id = _make_completed_import(app)
    with app.app_context():
        result = video_import.publish_import(app, import_id)
        assert result["published"] is True
        assert result["snapshots"] == 1

        row = VideoImportRepository.get(import_id)
        assert row["library_video_id"]
        assert row["published_at"]
        assert row["library_slug"] == "test-import"

        library_root = Path(app.config["MEDIA_ROOT"]).parent
        video = CameraVideoRepository.get(row["library_video_id"])
        assert video["kind"] == "import"
        assert video["camera_name"] == "Test Import"
        video_dir = library_root / video["path"]
        assert (video_dir / video["filename"]).is_file()
        assert (video_dir / "manifest.json").is_file()
        assert video["path"].startswith("videos/")

        snapshot = VideoImportSnapshotRepository.all(import_id)[0]
        assert snapshot["library_snapshot_id"]
        library = CameraSnapshotRepository.get(snapshot["library_snapshot_id"])
        assert library["source_type"] == "import"
        assert library["path"].startswith("snapshots/")
        library_path = library_root / library["path"]
        assert library_path.is_file()
        with Image.open(library_path) as image:
            assert image.size == (3840, 2160)
        # The import gallery and the library point at the same moved file.
        assert video_import.resolve(snapshot["upscaled_path"]) == library_path

        again = video_import.publish_import(app, import_id)
        assert again["video_id"] == video["id"]
        video_id = video["id"]

    login(client)
    page = client.get(f"/video-import/{import_id}")
    assert page.status_code == 200
    assert b"Published to the library" in page.data
    assert client.get(f"/videos/{video_id}/source").status_code == 200


def test_publish_backfills_old_imports_and_skips_bad_frames(app):
    import_id = _make_completed_import(app, title="Old Import")
    with app.app_context():
        first = VideoImportSnapshotRepository.all(import_id)[0]
        base = video_import.root() / str(import_id)
        dark = base / "snapshots" / "original" / "snap_0002.jpg"
        upscaled = base / "snapshots" / "4k" / "snap_0002_4k.jpg"
        thumb = base / "snapshots" / "thumbs" / "snap_0002_thumb.jpg"
        _make_jpeg(dark, Image.new("RGB", (160, 90), (1, 1, 1)))
        info = video_import.upscale_to_4k(dark, upscaled, thumb, 3840, 2160)
        VideoImportSnapshotRepository.create({
            "import_id": import_id,
            "timestamp_sec": 3.0,
            "original_path": video_import._stored(dark),
            "upscaled_path": video_import._stored(upscaled),
            "thumb_path": video_import._stored(thumb),
            "original_width": info["original_width"],
            "original_height": info["original_height"],
            "width": info["width"],
            "height": info["height"],
            "size_bytes": info["size_bytes"],
            # Deliberately no quality_json: this is a pre-gate import.
        })
        VideoImportSnapshotRepository.update(first["id"], quality_json=None)
        VideoImportRepository.update(import_id, snapshot_count=2)

        result = video_import.publish_import(app, import_id)
        assert result["snapshots"] == 1
        rows = VideoImportSnapshotRepository.all(import_id)
        published = [row for row in rows if row["library_snapshot_id"]]
        assert len(published) == 1
        assert published[0]["quality_json"]
        quality = json.loads(published[0]["quality_json"])
        assert quality["lighting"]


def test_delete_import_removes_library_copies_and_files(app):
    import_id = _make_completed_import(app, title="Delete Import")
    with app.app_context():
        video_import.publish_import(app, import_id)
        row = VideoImportRepository.get(import_id)
        video = CameraVideoRepository.get(row["library_video_id"])
        snapshot = VideoImportSnapshotRepository.all(import_id)[0]
        library = CameraSnapshotRepository.get(snapshot["library_snapshot_id"])
        library_root = Path(app.config["MEDIA_ROOT"]).parent
        video_dir = library_root / video["path"]
        image_path = library_root / library["path"]
        import_dir = video_import.root() / str(import_id)
        assert video_dir.exists()
        assert image_path.exists()

        video_import.delete_import(import_id)

        assert VideoImportRepository.get(import_id) is None
        assert CameraVideoRepository.get(video["id"]) is None
        assert CameraSnapshotRepository.get(library["id"]) is None
        assert not video_dir.exists()
        assert not image_path.exists()
        assert not import_dir.exists()
