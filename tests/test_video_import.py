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
from imani.models import VideoImportRepository, VideoImportSnapshotRepository

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
