"""The capture archive — footage persisted to disk for later review (Bit 11).

Nothing here touches the database. This module only knows how to write frames
somewhere safe, read them back, and throw old ones away; :class:`MediaRepository`
in :mod:`imani.models` keeps the index.

Layout under ``MEDIA_ROOT``::

    <YYYY-MM-DD>/<camera>/<reference>/
        frame-0001.jpg      the clip, oldest first
        frame-0002.jpg
        ...
        poster.jpg          the still shown in the archive grid
        manifest.json       fps, counts, timestamps

The path components are sanitised, and reads are resolved back under the root,
so a crafted path cannot escape the archive.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import current_app

from .models import MediaRepository

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str | None, fallback: str = "unknown") -> str:
    text = _SAFE.sub("-", (value or "").strip()).strip("-")
    return text[:80] or fallback


def root() -> Path:
    """The archive root, created on first use."""
    path = Path(current_app.config["MEDIA_ROOT"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _date_dir(captured_at: str | None) -> str:
    text = (captured_at or "").strip()
    if len(text) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return datetime.now().strftime("%Y-%m-%d")


def capture_dir(camera: str | None, captured_at: str | None,
                reference: str | None) -> Path:
    """Where one capture's footage lives, created if needed."""
    path = root() / _date_dir(captured_at) / _slug(camera, "camera") / _slug(reference)
    path.mkdir(parents=True, exist_ok=True)
    return path


def relative(path: Path) -> str:
    return path.relative_to(root()).as_posix()


def absolute(rel_path: str) -> Path:
    """Resolve a stored path, refusing anything that escapes the archive."""
    base = root().resolve()
    target = (base / rel_path).resolve()
    if base != target and base not in target.parents:
        raise ValueError("path escapes the media root")
    return target


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    try:  # Pillow is present for the demo renderer; do not require it.
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            return image.width, image.height
    except Exception:
        return None, None


def save_capture(camera: str | None, captured_at: str | None,
                 reference: str | None, frames: list[tuple[float, bytes]],
                 fps: float) -> dict | None:
    """Write a capture's still + clip. Returns the manifest, or None if empty."""
    frames = [(stamp, data) for stamp, data in frames if data]
    if not frames:
        return None

    directory = capture_dir(camera, captured_at, reference)
    manifest_frames = []
    for index, (stamp, data) in enumerate(frames, start=1):
        name = f"frame-{index:04d}.jpg"
        (directory / name).write_bytes(data)
        manifest_frames.append({"index": index, "file": name, "ts": round(stamp, 3)})

    poster = frames[-1][1]
    (directory / "poster.jpg").write_bytes(poster)

    width, height = _jpeg_size(poster)
    duration_ms = int(len(frames) / fps * 1000) if fps else len(frames) * 200
    manifest = {
        "camera": camera,
        "captured_at": captured_at,
        "reference": reference,
        "fps": fps,
        "count": len(frames),
        "duration_ms": duration_ms,
        "width": width,
        "height": height,
        "frames": manifest_frames,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    manifest["dir"] = relative(directory)
    manifest["poster"] = relative(directory / "poster.jpg")
    return manifest


def attach(sighting_id: int, manifest: dict) -> None:
    """Index a just-written capture: its poster and its clip."""
    MediaRepository.add(
        sighting_id, "frame", manifest["poster"],
        width=manifest.get("width"), height=manifest.get("height"),
    )
    MediaRepository.add(
        sighting_id, "clip", manifest["dir"],
        frame_count=manifest.get("count"),
        duration_ms=manifest.get("duration_ms"),
        width=manifest.get("width"), height=manifest.get("height"),
        fps=manifest.get("fps"),
    )
    MediaRepository.set_image_ref(sighting_id, manifest["poster"])


def read(rel_path: str) -> bytes:
    return absolute(rel_path).read_bytes()


def manifest(rel_dir: str) -> dict | None:
    path = absolute(rel_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def delete_media(rel_path: str) -> None:
    """Remove a stored asset: a clip directory or a single file."""
    try:
        target = absolute(rel_path)
    except ValueError:
        return
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        target.unlink(missing_ok=True)


def delete_for_sighting(sighting_id: int) -> int:
    """Drop every asset attached to a sighting. Returns how many were removed."""
    rows = MediaRepository.for_sighting(sighting_id)
    for row in rows:
        delete_media(row["path"])
    MediaRepository.delete_for_sighting(sighting_id)
    return len(rows)


def prune(days: int | None = None) -> dict:
    """Delete footage older than ``days``. Returns what was removed."""
    if days is None:
        days = int(current_app.config["MEDIA_RETENTION_DAYS"])
    if days <= 0:
        return {"deleted": 0, "bytes": 0, "kept": True}

    cutoff = datetime.now() - timedelta(days=days)
    rows = MediaRepository.older_than(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    removed = 0
    freed = 0
    for row in rows:
        try:
            target = absolute(row["path"])
            if target.is_file():
                freed += target.stat().st_size
            elif target.is_dir():
                for item in target.rglob("*"):
                    if item.is_file():
                        freed += item.stat().st_size
        except (ValueError, OSError):
            pass
        if row["kind"] == "clip":
            delete_media(row["path"])
        else:
            delete_media(row["path"])
        MediaRepository.delete(row["id"])
        removed += 1
    return {"deleted": removed, "bytes": freed, "kept": False}


# --- Synthetic footage for the demo camera --------------------------------

def render_demo_frame(detection, width: int = 1280, height: int = 720,
                      camera: str = "demo", label: str = "person",
                      source_size: tuple[int, int] = (1280, 720)) -> bytes | None:
    """Draw a clearly-synthetic frame so the archive has something to replay.

    This is deliberately stylised — a purple scene with a labelled box — so
    nobody mistakes demo footage for a real recording.
    """
    try:
        from io import BytesIO

        from PIL import Image, ImageDraw
    except Exception:
        return None

    image = Image.new("RGB", (width, height), (24, 13, 43))
    draw = ImageDraw.Draw(image)

    # Scan lines, for the CCTV feel.
    for y in range(0, height, 4):
        draw.line((0, y, width, y), fill=(33, 18, 58))

    box = getattr(detection, "box", None)
    if box is not None:
        # Detection coordinates are in the source frame; scale to the canvas
        # and clamp, so a box that runs off the edge is still drawable.
        scale_x = width / max(source_size[0], 1)
        scale_y = height / max(source_size[1], 1)
        x1 = max(2, min(width - 3, int(box.x * scale_x)))
        y1 = max(2, min(height - 3, int(box.y * scale_y)))
        x2 = max(x1 + 1, min(width - 2, int((box.x + box.width) * scale_x)))
        y2 = max(y1 + 1, min(height - 2, int((box.y + box.height) * scale_y)))
        draw.rectangle((x1, y1, x2, y2), outline=(232, 121, 249), width=2)
        draw.text((x1 + 4, max(2, y1 - 12)), label, fill=(232, 121, 249))
        # A simple head-and-body shape inside the box.
        cx = (x1 + x2) // 2
        head_r = max(4, (y2 - y1) // 10)
        draw.ellipse((cx - head_r, y1 + head_r, cx + head_r, y1 + head_r * 3),
                     outline=(239, 233, 252), width=2)
        draw.line((cx, y1 + head_r * 3, cx, y2 - (y2 - y1) // 3),
                  fill=(239, 233, 252), width=2)
        draw.line((cx, y2 - (y2 - y1) // 3, x1 + (x2 - x1) // 4, y2),
                  fill=(239, 233, 252), width=2)
        draw.line((cx, y2 - (y2 - y1) // 3, x2 - (x2 - x1) // 4, y2),
                  fill=(239, 233, 252), width=2)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draw.text((8, height - 16), f"{camera}  {stamp}", fill=(165, 150, 199))
    draw.text((8, 8), "SYNTHETIC FEED", fill=(165, 150, 199))

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def usage() -> dict:
    """How much footage is on disk, for the archive header."""
    files = 0
    total = 0
    for item in root().rglob("*"):
        if item.is_file():
            files += 1
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return {"files": files, "bytes": total}


def snapshots_root() -> Path:
    """Directory for auto-captured snapshots from CCTV/phone/camera feeds."""
    path = Path(current_app.config["MEDIA_ROOT"]).parent / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_snapshot(camera_name: str | None, jpeg_data: bytes, camera_id: int | None = None, source_type: str | None = None) -> dict | None:
    """Save a single frame snapshot from a video feed."""
    if not jpeg_data:
        return None
    from datetime import datetime
    import os
    snap_root = snapshots_root()
    date_dir = snap_root / datetime.now().strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    cam_dir = date_dir / _slug(camera_name, "camera")
    cam_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S_%f")[:-3]
    filename = f"snapshot_{ts}.jpg"
    filepath = cam_dir / filename
    filepath.write_bytes(jpeg_data)
    rel_path = filepath.relative_to(Path(current_app.config["MEDIA_ROOT"]).parent).as_posix()
    from . import features
    feat = features.extract_features(jpeg_data)
    return {
        "filename": filename,
        "path": rel_path,
        "full_path": str(filepath),
        "camera_name": camera_name,
        "camera_id": camera_id,
        "source_type": source_type,
        "features_json": json.dumps(feat) if feat else None,
        "features": feat,
    }


def videos_root() -> Path:
    """Directory for auto-saved video recordings from CCTV/phone/camera feeds for reference."""
    path = Path(current_app.config["MEDIA_ROOT"]).parent / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_video_recording(camera_name: str | None, frames: list[tuple[float, bytes]], 
                         camera_id: int | None = None, source_type: str | None = None,
                         started_at: str | None = None) -> dict | None:
    """Save a video recording as a frame sequence for reference."""
    frames = [(stamp, data) for stamp, data in frames if data]
    if not frames:
        return None
    from datetime import datetime
    vid_root = videos_root()
    date_dir = vid_root / datetime.now().strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    cam_dir = date_dir / _slug(camera_name, "camera")
    cam_dir.mkdir(parents=True, exist_ok=True)
    # Microseconds prevent collisions when an interval segment and a forced
    # shutdown flush land in the same second.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    vid_dir = cam_dir / f"rec_{ts}"
    vid_dir.mkdir(parents=True, exist_ok=True)
    
    manifest_frames = []
    for index, (stamp, data) in enumerate(frames, start=1):
        name = f"frame-{index:04d}.jpg"
        (vid_dir / name).write_bytes(data)
        manifest_frames.append({"index": index, "file": name, "ts": round(stamp, 3)})
    
    # Save poster/first frame as thumbnail
    (vid_dir / "poster.jpg").write_bytes(frames[0][1])
    
    duration_sec = frames[-1][0] - frames[0][0] if len(frames) > 1 else 0
    manifest = {
        "camera": camera_name,
        "camera_id": camera_id,
        "source_type": source_type,
        "started_at": started_at,
        "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "frame_count": len(frames),
        "duration_sec": round(duration_sec, 2),
        "frames": manifest_frames,
    }
    (vid_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    
    # Calculate size
    size_bytes = (
        sum((vid_dir / item["file"]).stat().st_size for item in manifest_frames)
        + (vid_dir / "poster.jpg").stat().st_size
        + (vid_dir / "manifest.json").stat().st_size
    )
    
    rel_path = vid_dir.relative_to(Path(current_app.config["MEDIA_ROOT"]).parent).as_posix()
    from . import features
    feat = features.extract_features(frames[0][1])
    return {
        "filename": f"rec_{ts}",
        "path": rel_path,
        "frame_count": len(frames),
        "duration_sec": round(duration_sec, 2),
        "size_bytes": size_bytes,
        "manifest": manifest,
        "features_json": json.dumps(feat) if feat else None,
        "features": feat,
    }


def audio_root() -> Path:
    """Directory for audio recorded alongside a phone camera feed."""
    path = Path(current_app.config["MEDIA_ROOT"]).parent / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


AUDIO_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


def save_audio(camera_name: str | None, data: bytes, mime_type: str | None,
               camera_id: int | None = None,
               source_type: str | None = None) -> dict | None:
    """Write one audio chunk from a phone microphone to disk.

    Browsers record with :js:class:`MediaRecorder` in short slices; each slice
    is stored as its own file and indexed as an ``audio`` recording so it can be
    played back with the footage it belongs to.
    """
    if not data:
        return None
    mime = (mime_type or "").split(";")[0].strip() or "audio/webm"
    # Browsers report an audio-only MediaRecorder stream as video/webm.
    if mime == "video/webm":
        mime = "audio/webm"
    elif mime == "video/ogg":
        mime = "audio/ogg"
    elif mime == "video/mp4":
        mime = "audio/mp4"
    ext = AUDIO_EXTENSIONS.get(mime, "webm")
    root_dir = audio_root()
    date_dir = root_dir / datetime.now().strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    cam_dir = date_dir / _slug(camera_name, "camera")
    cam_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
    filename = f"audio_{stamp}.{ext}"
    filepath = cam_dir / filename
    filepath.write_bytes(data)
    rel_path = filepath.relative_to(
        Path(current_app.config["MEDIA_ROOT"]).parent
    ).as_posix()
    return {
        "filename": filename,
        "path": rel_path,
        "size_bytes": len(data),
        "mime_type": mime,
        "camera_id": camera_id,
        "camera_name": camera_name,
        "source_type": source_type,
    }
