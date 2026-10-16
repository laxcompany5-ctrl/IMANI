"""Capture agent orchestration and command line.

Reads a video source (or a synthetic walk-in), tracks each person, turns the
track into traits and posts one capture per person to IMANI. Run it with::

    python -m agent --source rtsp://camera.local/stream \\
        --endpoint https://imani.example/ingest/capture \\
        --api-key "$IMANI_INGEST_API_KEY" --camera gate-3 \\
        --camera-height 250 --principal-y 360

Use ``--source synthetic`` to exercise the whole path with no camera and no
OpenCV, and ``--dry-run`` to print the captures instead of sending them.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, replace

from .traits import (
    Calibration,
    Box,
    Detection,
    build_payload,
    gait_from_speed,
    posture_from_axis,
)
from .tracker import IouTracker


@dataclass
class AgentConfig:
    endpoint: str = "http://127.0.0.1:5000/ingest/capture"
    api_key: str = ""
    camera: str = "camera-1"
    source: str = "0"
    every_n_frames: int = 5
    confirm_hits: int = 3
    min_interval_s: float = 20.0
    iou_threshold: float = 0.3
    dry_run: bool = False
    face: bool = False
    calibration: Calibration = field(default_factory=Calibration)


def build_detection_payload(config: AgentConfig, track, timestamp: float) -> dict:
    """Turn one confirmed track into a capture payload."""
    detection = track.detection
    return build_payload(
        camera=config.camera,
        box=detection.box,
        epoch_seconds=timestamp,
        calibration=config.calibration,
        gait=gait_from_speed(track.speed()),
        posture=posture_from_axis(detection.head, detection.feet),
        face=detection.face,
    )


def process(config: AgentConfig, frames, client, emit=print) -> int:
    """Consume ``(timestamp, [Detection])`` frames and post captures.

    ``client`` may be ``None`` when ``config.dry_run`` is set. Returns the
    number of captures emitted.
    """
    tracker = IouTracker(iou_threshold=config.iou_threshold)
    emitted = 0

    for timestamp, detections in frames:
        tracker.update(detections, timestamp)
        for track in tracker.tracks:
            if not tracker.should_emit(track, timestamp, config.confirm_hits,
                                       config.min_interval_s):
                continue
            payload = build_detection_payload(config, track, timestamp)
            if config.dry_run or client is None:
                emit(payload)
            else:
                emit(client.send(payload))
            tracker.mark_emitted(track, timestamp)
            emitted += 1

    return emitted


# --- Frame sources --------------------------------------------------------

def synthetic_frames(count: int = 24, start: float | None = None,
                     fps: float = 5.0):
    """A single person walking away from the camera — for demos and tests.

    The geometry is consistent with the *default* calibration (camera 250 cm
    high, horizon at row 360) for a person about 180 cm tall, so the height
    estimate comes back around 180 cm.
    """
    start = time.time() if start is None else start
    horizon, camera_height_cm, real_height_cm = 360.0, 250.0, 180.0
    ratio = real_height_cm / camera_height_cm
    for index in range(count):
        timestamp = start + index / fps
        progress = index / max(count - 1, 1)
        # Feet start near the camera and drift up toward the horizon as the
        # person walks away; the box shrinks to match.
        feet_y = 620.0 - 150.0 * progress
        height = ratio * (feet_y - horizon)
        width = height * 0.45
        x = 100.0 + 300.0 * progress
        box = Box(x, feet_y - height, width, height)
        center_x = x + width / 2
        yield timestamp, [Detection(
            box=box,
            head=(center_x, feet_y - height),
            feet=(center_x, feet_y),
        )]


def video_frames(config: AgentConfig):
    """Yield ``(timestamp, [Detection])`` read from a real video source."""
    from . import vision  # imported here so OpenCV is only needed for this path

    capture = vision.open_capture(config.source)
    analyzer = vision.FrameAnalyzer(face=config.face)
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            index += 1
            if index % max(config.every_n_frames, 1):
                continue

            detections = analyzer.detect(frame)
            if config.face and detections:
                enriched = []
                for detection in detections:
                    face = analyzer.face_embedding(frame, detection.box)
                    enriched.append(replace(detection, face=face))
                detections = enriched
            yield time.time(), detections
    finally:
        capture.release()


def run(config: AgentConfig, emit=print) -> int:
    """Wire a source to IMANI and run to completion. Returns captures emitted."""
    client = None
    if not config.dry_run:
        from .client import ImaniClient

        client = ImaniClient(config.endpoint, config.api_key)

    frames = (synthetic_frames() if config.source == "synthetic"
              else video_frames(config))
    return process(config, frames, client, emit=emit)


# --- CLI ------------------------------------------------------------------

def _default_emit(payload) -> None:
    print(json.dumps(payload, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Watch a video source and post people to IMANI.",
    )
    parser.add_argument("--source", default="0",
                        help='device index, file, RTSP/HTTP URL, or "synthetic"')
    parser.add_argument("--endpoint", default="http://127.0.0.1:5000/ingest/capture",
                        help="the full /ingest/capture URL")
    parser.add_argument("--api-key", default="",
                        help="value for the X-API-Key header (or IMANI_INGEST_API_KEY)")
    parser.add_argument("--camera", default="camera-1", help="camera name")
    parser.add_argument("--camera-height", type=float, default=250.0,
                        help="lens height above the ground, in cm")
    parser.add_argument("--principal-y", type=float, default=360.0,
                        help="image row of the optical centre (horizon), in px")
    parser.add_argument("--every-n-frames", type=int, default=5,
                        help="only analyse every Nth frame")
    parser.add_argument("--confirm-hits", type=int, default=3,
                        help="frames a person must be seen before a capture is sent")
    parser.add_argument("--min-interval", type=float, default=20.0,
                        help="minimum seconds between captures of the same track")
    parser.add_argument("--iou-threshold", type=float, default=0.3,
                        help="tracking IoU threshold")
    parser.add_argument("--face", action="store_true",
                        help="also compute the (weak) face descriptor")
    parser.add_argument("--dry-run", action="store_true",
                        help="print captures instead of sending them")
    return parser


def config_from_args(args: argparse.Namespace) -> AgentConfig:
    import os

    api_key = args.api_key or os.environ.get("IMANI_INGEST_API_KEY", "")
    return AgentConfig(
        endpoint=args.endpoint,
        api_key=api_key,
        camera=args.camera,
        source=args.source,
        every_n_frames=args.every_n_frames,
        confirm_hits=args.confirm_hits,
        min_interval_s=args.min_interval,
        iou_threshold=args.iou_threshold,
        dry_run=args.dry_run,
        face=args.face,
        calibration=Calibration(camera_height_cm=args.camera_height,
                                principal_y=args.principal_y),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)

    if not config.dry_run and not config.api_key:
        print("error: no API key. Pass --api-key or set IMANI_INGEST_API_KEY, "
              "or use --dry-run.", file=sys.stderr)
        return 2

    try:
        emitted = run(config, emit=_default_emit)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 130
    except Exception as error:  # surface a clean message rather than a traceback
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"emitted {emitted} capture(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
