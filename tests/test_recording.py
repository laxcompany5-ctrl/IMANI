"""Automatic snapshots and continuous video segments (Bit 13)."""
from __future__ import annotations

import json
from io import BytesIO

from PIL import Image

from imani import surveillance
from imani.models import (
    CameraRepository,
    CameraSnapshotRepository,
    CameraVideoRepository,
)


def jpeg(colour=(200, 40, 40)):
    buffer = BytesIO()
    Image.new("RGB", (96, 72), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def monitor_for(app, name="Recorder Cam"):
    with app.app_context():
        camera = CameraRepository.get_by_name(name)
        if camera is None:
            camera_id = CameraRepository.create({
                "name": name, "location": "Test",
                "source_type": "demo", "source_url": "demo",
                "created_by": "admin",
            })
            camera = CameraRepository.get(camera_id)
    return surveillance.Monitor(app, camera)


def test_forced_flush_saves_snapshot_with_features(app):
    app.config.update(SNAPSHOT_INTERVAL_SECONDS=9999, VIDEO_SEGMENT_SECONDS=9999)
    monitor = monitor_for(app)
    monitor._on_frame([], jpeg())
    monitor._flush(force=True)
    with app.app_context():
        snapshots = CameraSnapshotRepository.all()
    assert len(snapshots) == 1
    features = json.loads(snapshots[0]["features_json"])
    assert features["colours"]["red"] > 0.9


def test_forced_flush_writes_continuous_video_segment(app):
    app.config.update(SNAPSHOT_INTERVAL_SECONDS=9999, VIDEO_SEGMENT_SECONDS=9999)
    monitor = monitor_for(app, "Segment Cam")
    monitor._on_frame([], jpeg())
    monitor._on_frame([], jpeg())
    monitor._flush(force=True)
    with app.app_context():
        rows = CameraVideoRepository.all(kind="auto")
    assert rows
    assert rows[0]["frame_count"] == 2
    assert rows[0]["kind"] == "auto"


def test_snapshot_interval_triggers_automatically(app):
    app.config.update(SNAPSHOT_INTERVAL_SECONDS=0, VIDEO_SEGMENT_SECONDS=9999)
    monitor = monitor_for(app, "Interval Cam")
    monitor._on_frame([], jpeg((40, 40, 200)))
    with app.app_context():
        snapshots = CameraSnapshotRepository.all()
    assert snapshots
    assert snapshots[0]["camera_name"] == "Interval Cam"
