"""Import a video from a URL and extract 4K snapshots from it (Bit 14).

The source footage is downloaded once and kept untouched. Candidate frames are
sampled from the footage, a deterministic selector chooses the useful ones
(scene changes, fixed intervals, or people), and every selected frame is saved
twice:

    <VIDEO_IMPORT_ROOT>/<import id>/
        source/                  the downloaded video (never modified)
        snapshots/original/      the frame at the source resolution
        snapshots/4k/            3840x2160 JPEG (Lanczos upscale, black padding)
        snapshots/thumbs/        480x270 preview used by the gallery
        snapshots-4k.zip         on-demand download bundle

Only ``ffmpeg`` is required. ``yt-dlp`` adds support for platform pages (e.g.
YouTube); OpenCV adds the optional "people" selector. Both are optional.

Important: upscaling to 4K adds pixels with high-quality interpolation. It
cannot recover detail that was never in the source. A true super-resolution
model is a separate, GPU-heavy step.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import current_app, has_app_context

from . import features
from .models import VideoImportRepository, VideoImportSnapshotRepository


class VideoImportError(RuntimeError):
    """A user-facing failure while downloading or processing a video."""


# One import at a time keeps CPU/disk use predictable on a small server.
_WORKER = threading.Semaphore(1)
_HOG = None
_HOG_LOCK = threading.Lock()

_STREAM_EXTENSIONS = {".m3u8", ".mpd"}
_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".ogv", ".3gp",
}
_MIME_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/mp2t": ".ts",
    "application/octet-stream": ".mp4",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def root() -> Path:
    """Create and return the video-import storage root."""
    path = Path(current_app.config["VIDEO_IMPORT_ROOT"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(rel_path: str) -> Path:
    """Resolve a stored path, refusing anything outside the import root."""
    base = root().resolve()
    target = (base / rel_path).resolve()
    if base != target and base not in target.parents:
        raise ValueError("path escapes the video import root")
    return target


def ffmpeg_exe() -> str:
    """Find ffmpeg from config, imageio-ffmpeg, or PATH."""
    configured = ""
    if has_app_context():
        configured = current_app.config.get("VIDEO_IMPORT_FFMPEG") or ""
    for candidate in (configured, os.environ.get("IMANI_FFMPEG", "")):
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg") or ""


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def _update(import_id: int, **values) -> None:
    VideoImportRepository.update(import_id, **values)


# --- Probe -----------------------------------------------------------------

def probe(path: Path) -> dict:
    """Return duration, dimensions and fps for a video."""
    try:
        import imageio_ffmpeg

        reader = imageio_ffmpeg.read_frames(str(path))
        meta = next(reader)
        try:
            reader.close()
        except Exception:
            pass
        width, height = meta.get("size") or (0, 0)
        if width and height:
            return {
                "duration": float(meta.get("duration") or 0.0),
                "width": int(width),
                "height": int(height),
                "fps": float(meta.get("fps") or 0.0),
            }
    except Exception:
        pass

    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VideoImportError(
            "ffmpeg is not available. Install imageio-ffmpeg or ffmpeg."
        )
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        raise VideoImportError(f"Could not inspect the video: {exc}") from exc
    text = result.stderr or ""
    duration = 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        duration = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )
    size_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    fps_match = re.search(r"([\d.]+)\s+fps", text)
    if not size_match:
        raise VideoImportError(
            "The downloaded file does not contain a readable video stream."
        )
    return {
        "duration": duration,
        "width": int(size_match.group(1)),
        "height": int(size_match.group(2)),
        "fps": float(fps_match.group(1)) if fps_match else 0.0,
    }


# --- Download ---------------------------------------------------------------

def _extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in _VIDEO_EXTENSIONS else ".mp4"


def _title_from_url(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name or "").strip()
    return name or "Imported video"


def _title_from_headers(headers) -> str:
    disposition = headers.get("Content-Disposition") or ""
    match = re.search(
        r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.IGNORECASE
    )
    return unquote(match.group(1)).strip() if match else ""


def _download_direct(url: str, directory: Path, max_bytes: int):
    """Download a direct media URL. Return None when the URL is an HTML page."""
    import requests

    extension = _extension_from_url(url)
    target = directory / f"source{extension}"
    part = directory / f"source{extension}.part"
    completed = False
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124 Safari/537.36 IMANI/1.0"
        )
    }
    try:
        with requests.get(
            url, stream=True, timeout=(20, 120), headers=headers,
            allow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                raise VideoImportError(
                    f"The video URL returned HTTP {response.status_code}."
                )
            content_type = (
                response.headers.get("Content-Type") or ""
            ).split(";")[0].strip().lower()
            if content_type.startswith("text/") or content_type in (
                "application/xhtml+xml", "application/json",
                "application/vnd.apple.mpegurl", "application/x-mpegurl",
                "audio/mpegurl",
            ):
                return None  # let yt-dlp/ffmpeg handle a page or HLS playlist

            extension = _MIME_EXTENSIONS.get(content_type) or extension
            target = directory / f"source{extension}"
            part = directory / f"source{extension}.part"
            chunks = response.iter_content(chunk_size=256 * 1024)
            first = next(chunks, b"")
            if first.lstrip().lower().startswith((b"<!doctype", b"<html")):
                return None  # an HTML player page, not a media file
            total = 0
            with part.open("wb") as handle:
                for chunk in itertools.chain((first,), chunks):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        raise VideoImportError(
                            "The video is larger than the configured download "
                            f"limit ({max_bytes // (1024 * 1024)} MiB)."
                        )
                    handle.write(chunk)
            if total == 0:
                raise VideoImportError("The video URL returned an empty file.")
            part.replace(target)
            completed = True
            return {
                "path": target,
                "title": _title_from_headers(response.headers)
                or _title_from_url(response.url or url),
                "mime_type": content_type or "video/mp4",
                "source_kind": "direct",
            }
    except requests.RequestException as exc:
        raise VideoImportError(f"Could not download the video: {exc}") from exc
    finally:
        if not completed:
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass


def _download_with_ytdlp(url: str, directory: Path, max_bytes: int):
    try:
        import yt_dlp
    except Exception as exc:
        raise VideoImportError(
            "This URL looks like a web page, not a direct video file. "
            "Install yt-dlp (pip install yt-dlp) or use a direct MP4 URL."
        ) from exc

    ffmpeg = ffmpeg_exe()
    options = {
        "outtmpl": str(directory / "source.%(ext)s"),
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
    }
    if ffmpeg:
        options["ffmpeg_location"] = str(Path(ffmpeg).parent)
    if max_bytes:
        options["max_filesize"] = max_bytes

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True) or {}
    except Exception as exc:
        raise VideoImportError(f"Could not download this video URL: {exc}") from exc

    files = [
        path for path in directory.glob("source.*")
        if path.is_file() and path.suffix not in (".part", ".ytdl")
    ]
    if not files:
        raise VideoImportError(
            "The downloader finished without producing a video file."
        )
    video = max(files, key=lambda item: item.stat().st_size)
    title = (info.get("title") or "").strip() or _title_from_url(url)
    return {
        "path": video,
        "title": title[:200],
        "mime_type": "video/mp4" if video.suffix.lower() == ".mp4" else "",
        "source_kind": "platform",
    }


def download(url: str, directory: Path, max_bytes: int) -> dict:
    """Fetch ``url`` into ``directory`` and return its metadata."""
    directory.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise VideoImportError("Only http:// and https:// video URLs are supported.")
    if not parsed.netloc:
        raise VideoImportError("The video URL is missing a host name.")
    if parsed.path.lower().endswith(tuple(_STREAM_EXTENSIONS)):
        result = _download_with_ytdlp(url, directory, max_bytes)
    else:
        result = _download_direct(url, directory, max_bytes)
    if result is None:
        result = _download_with_ytdlp(url, directory, max_bytes)
    if not result["mime_type"]:
        import mimetypes

        result["mime_type"] = mimetypes.guess_type(str(result["path"]))[0] or "video/mp4"
    return result


# --- Candidate frames -------------------------------------------------------

def extract_candidates(
    source: Path,
    directory: Path,
    duration: float,
    interval: float,
    max_candidates: int,
    on_progress=None,
) -> tuple[list[dict], float]:
    """Sample frames from the video and return ``(candidates, interval)``.

    The sampling interval grows automatically so a long video never creates
    more than ``max_candidates`` images.
    """
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VideoImportError(
            "ffmpeg is not available. Install imageio-ffmpeg or ffmpeg."
        )
    interval = max(0.5, min(float(interval or 2.0), 60.0))
    if duration and max_candidates:
        interval = max(interval, duration / float(max_candidates))
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass

    if on_progress:
        on_progress(30, "Sampling frames from the video")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-an", "-vf", f"fps=1/{interval:.6f}", "-q:v", "2",
        "-start_number", "0",
        str(directory / "%06d.jpg"),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-6:]
        raise VideoImportError(
            "ffmpeg could not decode the video"
            + (f": {' / '.join(tail)}" if tail else ".")
        )

    files = sorted(directory.glob("*.jpg"))
    if not files:
        # Very short clips (shorter than the sampling interval) still deserve
        # one snapshot: grab the first frame.
        first = directory / "000000.jpg"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-frames:v", "1", "-q:v", "2", str(first),
            ],
            capture_output=True,
        )
        files = [first] if first.exists() else []
    if not files:
        raise VideoImportError("No video frames could be extracted.")

    candidates = [
        {
            "index": index,
            "timestamp": round(index * interval, 3),
            "path": path,
        }
        for index, path in enumerate(files)
    ]
    if on_progress:
        on_progress(50, f"Sampled {len(candidates)} candidate frames")
    return candidates, interval


# --- Selection --------------------------------------------------------------

def _dhash(path: Path) -> int:
    from PIL import Image
    import numpy as np

    with Image.open(path) as image:
        small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = np.asarray(small, dtype=np.int16)
    diff = pixels[:, 1:] > pixels[:, :-1]
    value = 0
    for bit in diff.ravel():
        value = (value << 1) | int(bit)
    return value


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _evenly(candidates: list[dict], count: int) -> list[dict]:
    if count <= 0 or not candidates:
        return []
    if len(candidates) <= count:
        return list(candidates)
    if count == 1:
        return [candidates[0]]
    indexes = {
        round(position * (len(candidates) - 1) / (count - 1))
        for position in range(count)
    }
    return [candidates[index] for index in sorted(indexes)]


def _hog_detector():
    global _HOG
    with _HOG_LOCK:
        if _HOG is None:
            import cv2

            detector = cv2.HOGDescriptor()
            detector.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            _HOG = detector
    return _HOG


def _has_person(path: Path) -> bool:
    try:
        import cv2

        image = cv2.imread(str(path))
        if image is None:
            return False
        height, width = image.shape[:2]
        if width > 640:
            image = cv2.resize(image, (640, int(height * 640 / width)))
        boxes, _weights = _hog_detector().detectMultiScale(
            image, winStride=(8, 8), padding=(8, 8), scale=1.05,
        )
        return len(boxes) > 0
    except Exception:
        return False


def _select_scene(
    candidates: list[dict], max_snapshots: int, min_gap: float
) -> list[dict]:
    if not candidates:
        return []
    hashes = [_dhash(item["path"]) for item in candidates]
    target = min(3, max_snapshots, len(candidates))
    selected: list[dict] = []
    for threshold in (12, 8, 5):
        selected = [candidates[0]]
        previous = hashes[0]
        last_timestamp = candidates[0]["timestamp"]
        for item, digest in zip(candidates[1:], hashes[1:]):
            if item["timestamp"] - last_timestamp < min_gap:
                continue
            if _hamming(previous, digest) >= threshold:
                selected.append(item)
                previous = digest
                last_timestamp = item["timestamp"]
        if len(selected) >= target:
            break
    if candidates[-1] not in selected:
        selected.append(candidates[-1])
    return _evenly(selected, max_snapshots)


def _select_people(
    candidates: list[dict], max_snapshots: int, min_gap: float
) -> list[dict]:
    if not opencv_available():
        raise VideoImportError(
            "The 'people' selector needs OpenCV. Install it with "
            "`pip install opencv-python-headless`, or choose Auto/Scene."
        )
    selected: list[dict] = []
    last_timestamp = -1.0
    for item in candidates:
        if item["timestamp"] - last_timestamp < min_gap:
            continue
        if _has_person(item["path"]):
            selected.append(item)
            last_timestamp = item["timestamp"]
    return _evenly(selected, max_snapshots)


def select_snapshots(
    candidates: list[dict], mode: str, max_snapshots: int, min_gap: float
) -> list[dict]:
    """Choose which candidate frames become saved snapshots."""
    if not candidates:
        return []
    max_snapshots = max(1, min(int(max_snapshots or 24), len(candidates)))
    mode = (mode or "auto").lower()
    if mode == "interval":
        return _evenly(candidates, max_snapshots)
    if mode == "people":
        selected = _select_people(candidates, max_snapshots, min_gap)
        if selected:
            return selected
        mode = "scene"
    selected = _select_scene(candidates, max_snapshots, min_gap)
    if len(selected) < min(3, max_snapshots, len(candidates)):
        return _evenly(candidates, max_snapshots)
    return selected


# --- 4K upscale -------------------------------------------------------------

def upscale_to_4k(
    source: Path, target: Path, thumb: Path, width: int, height: int
) -> dict:
    """Write a padded 4K JPEG and a small preview; return dimensions/size."""
    from PIL import Image, ImageOps

    target.parent.mkdir(parents=True, exist_ok=True)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        original_width, original_height = image.size
        big = ImageOps.pad(
            image, (int(width), int(height)),
            method=Image.Resampling.LANCZOS,
            color=(0, 0, 0), centering=(0.5, 0.5),
        )
        big.save(
            target, "JPEG", quality=95, subsampling=0,
            progressive=True, optimize=False,
        )
        small = ImageOps.pad(
            image, (480, 270),
            method=Image.Resampling.LANCZOS,
            color=(0, 0, 0), centering=(0.5, 0.5),
        )
        small.save(thumb, "JPEG", quality=86, progressive=True)
    return {
        "original_width": original_width,
        "original_height": original_height,
        "width": int(width),
        "height": int(height),
        "size_bytes": target.stat().st_size,
    }


# --- Orchestration ----------------------------------------------------------

def process_import(app, import_id: int) -> None:
    """Download, select and upscale one import. Runs in a daemon thread."""
    with _WORKER:
        with app.app_context():
            row = VideoImportRepository.get(import_id)
            if row is None or row["status"] != "queued":
                return
            base = root() / str(import_id)
            (base / "source").mkdir(parents=True, exist_ok=True)
            (base / "snapshots" / "original").mkdir(parents=True, exist_ok=True)
            (base / "snapshots" / "4k").mkdir(parents=True, exist_ok=True)
            (base / "snapshots" / "thumbs").mkdir(parents=True, exist_ok=True)
            candidates_dir = base / "candidates"

            try:
                _update(
                    import_id, status="running", stage="Downloading video",
                    progress=3, error=None,
                )
                downloaded = download(
                    row["source_url"], base / "source",
                    int(app.config.get("VIDEO_IMPORT_MAX_BYTES", 0)),
                )
                video_path = downloaded["path"]
                video_rel = video_path.relative_to(root()).as_posix()
                _update(
                    import_id,
                    source_kind=downloaded.get("source_kind"),
                    title=downloaded.get("title"),
                    video_path=video_rel,
                    mime_type=downloaded.get("mime_type"),
                    stage="Inspecting video",
                    progress=20,
                )

                meta = probe(video_path)
                _update(
                    import_id,
                    duration_sec=meta["duration"],
                    width=meta["width"],
                    height=meta["height"],
                    fps=meta["fps"],
                    stage="Sampling frames",
                    progress=25,
                )

                def progress(value: int, stage: str) -> None:
                    _update(import_id, progress=value, stage=stage)

                candidates, effective_interval = extract_candidates(
                    video_path, candidates_dir, meta["duration"],
                    float(row["interval_sec"] or 2),
                    int(app.config.get("VIDEO_IMPORT_MAX_CANDIDATES", 600)),
                    on_progress=progress,
                )
                _update(
                    import_id,
                    candidate_count=len(candidates),
                    stage="Selecting snapshots",
                    progress=55,
                )
                selected = select_snapshots(
                    candidates, row["mode"], int(row["max_snapshots"] or 24),
                    min_gap=max(1.0, effective_interval * 1.5),
                )
                if not selected:
                    raise VideoImportError(
                        "No usable snapshots were found in this video."
                    )

                target_width = int(
                    app.config.get("VIDEO_IMPORT_4K_WIDTH", 3840)
                )
                target_height = int(
                    app.config.get("VIDEO_IMPORT_4K_HEIGHT", 2160)
                )
                total = len(selected)
                for number, candidate in enumerate(selected, start=1):
                    stem = f"snap_{number:04d}"
                    original = base / "snapshots" / "original" / f"{stem}.jpg"
                    upscaled = base / "snapshots" / "4k" / f"{stem}_4k.jpg"
                    thumb = base / "snapshots" / "thumbs" / f"{stem}_thumb.jpg"
                    shutil.copy2(candidate["path"], original)
                    info = upscale_to_4k(
                        original, upscaled, thumb, target_width, target_height
                    )
                    feature_record = features.extract_features(original)
                    VideoImportSnapshotRepository.create({
                        "import_id": import_id,
                        "timestamp_sec": candidate["timestamp"],
                        "original_path": original.relative_to(root()).as_posix(),
                        "upscaled_path": upscaled.relative_to(root()).as_posix(),
                        "thumb_path": thumb.relative_to(root()).as_posix(),
                        "original_width": info["original_width"],
                        "original_height": info["original_height"],
                        "width": info["width"],
                        "height": info["height"],
                        "size_bytes": info["size_bytes"],
                        "features_json": (
                            json.dumps(feature_record)
                            if feature_record else None
                        ),
                    })
                    _update(
                        import_id,
                        progress=60 + int(38 * number / total),
                        stage=f"Upscaling snapshot {number} of {total}",
                    )

                _update(
                    import_id,
                    status="complete",
                    stage="Complete",
                    progress=100,
                    snapshot_count=total,
                    output_width=target_width,
                    output_height=target_height,
                    completed_at=_now(),
                )
                app.logger.info(
                    "Video import %s complete: %s snapshots at %sx%s",
                    import_id, total, target_width, target_height,
                )
            except Exception as exc:  # noqa: BLE001 - surface the message to the UI
                message = str(exc) or exc.__class__.__name__
                _update(
                    import_id, status="failed", stage="Failed",
                    error=message[:2000],
                )
                app.logger.exception("Video import %s failed", import_id)
            finally:
                shutil.rmtree(candidates_dir, ignore_errors=True)


def start_import(app, values: dict) -> int:
    """Create the database row and start processing in the background."""
    import_id = VideoImportRepository.create(values)
    thread = threading.Thread(
        target=process_import,
        args=(app, import_id),
        name=f"imani-video-import-{import_id}",
        daemon=True,
    )
    thread.start()
    return import_id


# --- Download bundle --------------------------------------------------------

def ensure_zip(import_id: int) -> Path:
    """Build (or reuse) a ZIP containing every 4K snapshot for an import."""
    base = root() / str(import_id)
    target = base / "snapshots-4k.zip"
    snapshots = VideoImportSnapshotRepository.all(import_id)
    if not snapshots:
        raise VideoImportError("This import has no snapshots.")
    newest = max(
        (resolve(snap["upscaled_path"]).stat().st_mtime for snap in snapshots),
        default=0,
    )
    if target.exists() and target.stat().st_mtime >= newest:
        return target
    manifest = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for number, snap in enumerate(snapshots, start=1):
            path = resolve(snap["upscaled_path"])
            name = f"snapshot_{number:04d}_4k.jpg"
            archive.write(path, arcname=name)
            manifest.append({
                "file": name,
                "timestamp_sec": snap["timestamp_sec"],
                "width": snap["width"],
                "height": snap["height"],
            })
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "import_id": import_id,
                    "snapshots": manifest,
                    "note": (
                        "4K frames are Lanczos-upscaled from the source video; "
                        "upscaling does not create new detail."
                    ),
                },
                indent=2,
            ),
        )
    return target
