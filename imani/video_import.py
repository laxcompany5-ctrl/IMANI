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
import io
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import current_app, has_app_context

from . import features
from .models import (
    AuditRepository,
    CameraSnapshotRepository,
    CameraVideoRepository,
    VideoImportRepository,
    VideoImportSnapshotRepository,
)


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


def instance_root() -> Path:
    """The instance directory that also holds the Videos and Snapshots libraries."""
    return Path(current_app.config["MEDIA_ROOT"]).parent


def _stored(path: Path) -> str:
    """Store a path relative to the instance root where possible."""
    try:
        return path.relative_to(instance_root()).as_posix()
    except ValueError:
        return path.relative_to(root()).as_posix()


def resolve(rel_path: str) -> Path:
    """Resolve a stored path inside the instance or import storage roots."""
    relative = Path(rel_path)
    missing: Path | None = None
    for base in (instance_root(), root()):
        base = base.resolve()
        target = (base / relative).resolve()
        if base != target and base not in target.parents:
            continue
        if target.exists():
            return target
        if missing is None:
            missing = target
    if missing is not None:
        return missing
    raise ValueError("path escapes the video import root")


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
        # imageio-ffmpeg names its binary ``ffmpeg-linux-x86_64-...``, so a
        # directory is not enough for yt-dlp to discover it. Hand over the
        # full executable path instead (supported by yt-dlp >= 2021.09.02).
        options["ffmpeg_location"] = str(ffmpeg)
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


# --- Quality and lighting ---------------------------------------------------

QUALITY_MIN_BRIGHTNESS = 0.08
QUALITY_MAX_BRIGHTNESS = 0.93
QUALITY_MIN_CONTRAST = 0.05
QUALITY_MIN_SHARPNESS = 0.025


def analyze_quality(source: Path) -> dict:
    """Measure exposure, contrast and edge detail before a frame is saved.

    The numbers are deliberately conservative: a night-time CCTV frame can be
    dim, but a frame that is almost black, blown out, flat or blurred is not
    worth publishing to the Snapshots library.
    """
    from PIL import Image

    with Image.open(source) as original:
        return analyze_quality_image(original)


def analyze_quality_bytes(jpeg: bytes) -> dict:
    """Same measurements as :func:`analyze_quality`, for in-memory frames."""
    from PIL import Image

    if not jpeg:
        return {"ok": False, "reasons": ["empty frame"]}
    with Image.open(io.BytesIO(jpeg)) as original:
        return analyze_quality_image(original)


def analyze_quality_image(image) -> dict:
    from PIL import Image, ImageFilter, ImageStat

    gray = image.convert("L")
    gray.thumbnail((512, 512), Image.Resampling.LANCZOS)
    stats = ImageStat.Stat(gray)
    brightness = stats.mean[0] / 255.0
    contrast = stats.stddev[0] / 255.0
    sharpness = (
        ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0] / 255.0
    )
    histogram = gray.histogram()
    total = sum(histogram) or 1
    dark_ratio = sum(histogram[:13]) / total
    bright_ratio = sum(histogram[243:]) / total

    reasons: list[str] = []
    if brightness < QUALITY_MIN_BRIGHTNESS:
        reasons.append("too dark")
    elif brightness > QUALITY_MAX_BRIGHTNESS:
        reasons.append("overexposed")
    if contrast < QUALITY_MIN_CONTRAST:
        reasons.append("flat / low contrast")
    if sharpness < QUALITY_MIN_SHARPNESS:
        reasons.append("blurred / too little detail")
    if dark_ratio > 0.94 and brightness < 0.2:
        reasons.append("almost entirely black")
    if bright_ratio > 0.9 and brightness > 0.85:
        reasons.append("almost entirely white")

    score = max(
        0.0,
        min(
            1.0,
            (1.0 - abs(brightness - 0.5) * 2.0) * 0.35
            + min(contrast / 0.25, 1.0) * 0.35
            + min(sharpness / 0.10, 1.0) * 0.30,
        ),
    )
    return {
        "brightness": round(brightness, 4),
        "contrast": round(contrast, 4),
        "sharpness": round(sharpness, 4),
        "dark_ratio": round(dark_ratio, 4),
        "bright_ratio": round(bright_ratio, 4),
        "score": round(score, 4),
        "ok": not reasons,
        "reasons": reasons,
    }


def normalise_lighting(image):
    """Lift shadows or recover highlights, then apply a gentle auto-contrast.

    The untouched frame is still kept on disk; this only changes the 4K copy
    written to the Snapshots library. Returns the image and a list of actions.
    """
    from PIL import Image, ImageOps, ImageStat

    actions: list[str] = []
    gray = image.convert("L")
    mean = ImageStat.Stat(gray).mean[0] / 255.0
    adjusted = image

    if mean < 0.30:
        gamma = max(0.60, min(1.0, math.log(0.48) / math.log(max(mean, 0.02))))
        adjusted = image.point(
            lambda value: int(round(255 * ((value / 255.0) ** gamma)))
        )
        actions.append(f"shadow lift (gamma {gamma:.2f})")
    elif mean > 0.70:
        gamma = max(1.0, min(1.80, math.log(0.55) / math.log(min(mean, 0.98))))
        adjusted = image.point(
            lambda value: int(round(255 * ((value / 255.0) ** gamma)))
        )
        actions.append(f"highlight recovery (gamma {gamma:.2f})")

    # Apply contrast to the value channel only. Autocontrasting RGB bands
    # independently neutralises colour casts and can turn a red scene grey,
    # which destroys evidence colours used by AI search.
    hsv = adjusted.convert("HSV")
    hue, saturation, value = hsv.split()
    value = ImageOps.autocontrast(value, cutoff=1)
    adjusted = Image.merge("HSV", (hue, saturation, value)).convert("RGB")
    actions.append("auto contrast")
    return adjusted, actions


def normalise_lighting_bytes(jpeg: bytes) -> tuple[bytes, list[str], float]:
    """Return a lighting-corrected JPEG plus the actions applied.

    Used for live device snapshots. The caller keeps the raw frame for the
    live view and recording; only the saved snapshot is adjusted.
    """
    from PIL import Image, ImageOps, ImageStat

    with Image.open(io.BytesIO(jpeg)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        adjusted, actions = normalise_lighting(image)
        brightness = ImageStat.Stat(adjusted.convert("L")).mean[0] / 255.0
        buffer = io.BytesIO()
        adjusted.save(
            buffer, "JPEG", quality=92, subsampling=0, progressive=True,
        )
    return buffer.getvalue(), actions, round(brightness, 4)


def _quality_gate(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split sampled frames into publishable and rejected ones."""
    kept: list[dict] = []
    rejected: list[dict] = []
    for candidate in candidates:
        metrics = analyze_quality(candidate["path"])
        candidate = {**candidate, "quality": metrics}
        (kept if metrics["ok"] else rejected).append(candidate)
    return kept, rejected


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
    source: Path, target: Path, thumb: Path, width: int, height: int,
    normalise: bool = False,
) -> dict:
    """Write a padded 4K JPEG and a small preview; return dimensions/size.

    ``normalise=True`` lifts shadows / recovers highlights and applies a gentle
    auto-contrast to the 4K copy. The source frame is never modified.
    """
    from PIL import Image, ImageOps, ImageStat

    target.parent.mkdir(parents=True, exist_ok=True)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        original_width, original_height = image.size
        lighting: list[str] = []
        if normalise:
            image, lighting = normalise_lighting(image)
        saved_brightness = (
            ImageStat.Stat(image.convert("L")).mean[0] / 255.0
        )
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
        "lighting": lighting,
        "saved_brightness": round(saved_brightness, 4),
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
                video_rel = _stored(video_path)
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
                # Auto-filter: drop almost-black, blown-out, flat or blurred
                # frames before anything is written to the library.
                kept, rejected = _quality_gate(candidates)
                if not kept:
                    # A genuinely dark scene should still produce evidence:
                    # fall back to the best-exposed frames.
                    kept = sorted(
                        rejected, key=lambda item: item["quality"]["score"],
                        reverse=True,
                    )[: int(row["max_snapshots"] or 24)]
                _update(
                    import_id,
                    candidate_count=len(candidates),
                    rejected_count=len(rejected),
                    stage="Selecting snapshots",
                    progress=55,
                )
                selected = select_snapshots(
                    kept, row["mode"], int(row["max_snapshots"] or 24),
                    min_gap=max(1.0, effective_interval * 1.5),
                )
                if not selected:
                    raise VideoImportError(
                        "No usable snapshots passed the quality filter."
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
                        original, upscaled, thumb, target_width, target_height,
                        normalise=True,
                    )
                    feature_record = features.extract_features(original)
                    quality_record = {
                        **candidate.get("quality", {}),
                        "lighting": info.get("lighting", []),
                        "saved_brightness": info.get("saved_brightness"),
                    }
                    VideoImportSnapshotRepository.create({
                        "import_id": import_id,
                        "timestamp_sec": candidate["timestamp"],
                        "original_path": _stored(original),
                        "upscaled_path": _stored(upscaled),
                        "thumb_path": _stored(thumb),
                        "original_width": info["original_width"],
                        "original_height": info["original_height"],
                        "width": info["width"],
                        "height": info["height"],
                        "size_bytes": info["size_bytes"],
                        "features_json": (
                            json.dumps(feature_record)
                            if feature_record else None
                        ),
                        "quality_json": json.dumps(quality_record),
                    })
                    _update(
                        import_id,
                        progress=60 + int(38 * number / total),
                        stage=f"Upscaling snapshot {number} of {total}",
                    )

                _update(
                    import_id,
                    status="complete",
                    stage="Publishing to libraries",
                    progress=99,
                    snapshot_count=total,
                    output_width=target_width,
                    output_height=target_height,
                    completed_at=_now(),
                )
                try:
                    publish_import(app, import_id)
                except Exception as exc:  # noqa: BLE001 - keep extracted evidence
                    app.logger.exception(
                        "Video import %s could not be published", import_id
                    )
                    _update(
                        import_id,
                        stage="Complete — library publish failed",
                        error=f"Snapshots were extracted but could not be "
                              f"published to the Videos/Snapshots libraries: {exc}",
                    )
                else:
                    _update(import_id, stage="Complete", progress=100)
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


def _slug(value: str | None, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-")
    return text[:60].strip("-").lower() or fallback


def _move(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))
    return dest


def publish_import(app, import_id: int) -> dict:
    """Move a finished import into the permanent Videos and Snapshots libraries.

    The source video goes to ``instance/videos/<date>/<slug>/import_<id>/`` and
    each snapshot goes to ``instance/snapshots/<date>/<slug>/import_<id>/``.
    The import page keeps working because its stored paths are rewritten.
    """
    from PIL import Image

    row = VideoImportRepository.get(import_id)
    if row is None:
        raise VideoImportError("Import not found.")
    if row["status"] != "complete":
        raise VideoImportError("Only completed imports can be published.")
    if row["library_video_id"]:
        return {"video_id": row["library_video_id"], "published": True}

    snapshots = VideoImportSnapshotRepository.all(import_id)
    library_root = instance_root()
    date = (row["created_at"] or _now())[:10]
    slug = _slug(row["title"], f"video-import-{import_id}")
    title = (row["title"] or f"Video import #{import_id}")[:120]
    video_dir = library_root / "videos" / date / slug / f"import_{import_id}"
    snap_dir = library_root / "snapshots" / date / slug / f"import_{import_id}"
    video_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)

    try:
        created = datetime.strptime(
            (row["created_at"] or "")[:19], "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        created = datetime.now(timezone.utc).replace(tzinfo=None)
    start = created - timedelta(seconds=float(row["duration_sec"] or 0))

    video_name = "video.mp4"
    source_video = resolve(row["video_path"]) if row["video_path"] else None
    if source_video and source_video.exists():
        video_name = f"video{(source_video.suffix or '.mp4').lower()}"
        _move(source_video, video_dir / video_name)

    poster_written = False
    if snapshots:
        poster_source = None
        for candidate in snapshots:
            try:
                poster_source = resolve(candidate["upscaled_path"])
                if poster_source.exists():
                    break
            except ValueError:
                poster_source = None
        if poster_source and poster_source.exists():
            try:
                with Image.open(poster_source) as image:
                    poster = image.convert("RGB")
                    poster.thumbnail((1280, 720), Image.Resampling.LANCZOS)
                    poster.save(video_dir / "poster.jpg", "JPEG", quality=88)
                poster_written = True
            except Exception:  # pragma: no cover - poster is best effort
                pass

    manifest = {
        "kind": "import",
        "camera": title,
        "source_url": row["source_url"],
        "import_id": import_id,
        "video": video_name,
        "poster": "poster.jpg" if poster_written else None,
        "duration_sec": row["duration_sec"],
        "width": row["width"],
        "height": row["height"],
        "fps": row["fps"],
        "snapshots": len(snapshots),
        "imported_at": row["created_at"],
    }
    (video_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    size_bytes = 0
    for name in (video_name, "poster.jpg"):
        path = video_dir / name
        if path.exists():
            size_bytes += path.stat().st_size

    video_id = CameraVideoRepository.create({
        "camera_id": None,
        "camera_name": title,
        "filename": video_name,
        "path": _stored(video_dir),
        "source_type": "import",
        "duration_sec": row["duration_sec"],
        "frame_count": int(round(
            float(row["duration_sec"] or 0) * float(row["fps"] or 0)
        )),
        "size_bytes": size_bytes,
        "started_at": row["created_at"],
        "ended_at": row["created_at"],
        "captured_at": row["created_at"],
        "kind": "import",
        "mime_type": row["mime_type"] or "video/mp4",
        "description": f"Imported from {row['source_url'][:180]}",
    })

    published = 0
    skipped = 0
    for number, snapshot in enumerate(snapshots, start=1):
        original = resolve(snapshot["original_path"])
        upscaled = resolve(snapshot["upscaled_path"])
        thumb = resolve(snapshot["thumb_path"]) if snapshot["thumb_path"] else None
        timestamp = float(snapshot["timestamp_sec"] or 0)
        stem = f"snapshot_{number:04d}_t{int(round(timestamp)):03d}s"

        if not snapshot["quality_json"]:
            # Older imports predate the quality gate. Check before publishing
            # and normalise the 4K copy now.
            metrics = analyze_quality(original)
            if not metrics["ok"]:
                skipped += 1
                continue
            thumb_target = thumb or (snap_dir / f"{stem}_thumb.jpg")
            info = upscale_to_4k(
                original, upscaled, thumb_target,
                int(row["output_width"] or 3840), int(row["output_height"] or 2160),
                normalise=True,
            )
            thumb = thumb_target
            quality = {**metrics, "lighting": info["lighting"],
                       "saved_brightness": info["saved_brightness"]}
            VideoImportSnapshotRepository.update(
                snapshot["id"], quality_json=json.dumps(quality),
            )
        else:
            try:
                quality = json.loads(snapshot["quality_json"])
            except Exception:
                quality = {}

        dest_original = _move(original, snap_dir / f"{stem}.jpg")
        dest_upscaled = _move(upscaled, snap_dir / f"{stem}_4k.jpg")
        dest_thumb = None
        if thumb and thumb.exists():
            dest_thumb = _move(thumb, snap_dir / f"{stem}_thumb.jpg")

        captured = start + timedelta(seconds=timestamp)
        library_id = CameraSnapshotRepository.create({
            "camera_id": None,
            "camera_name": title,
            "filename": dest_upscaled.name,
            "path": _stored(dest_upscaled),
            "source_type": "import",
            "captured_at": captured.strftime("%Y-%m-%d %H:%M:%S"),
            "features_json": snapshot["features_json"],
            "description": (
                f"Video import #{import_id} · t={timestamp:.1f}s · 4K UHD · "
                f"auto-filtered, lighting normalised"
            ),
        })
        VideoImportSnapshotRepository.update(
            snapshot["id"],
            original_path=_stored(dest_original),
            upscaled_path=_stored(dest_upscaled),
            thumb_path=_stored(dest_thumb) if dest_thumb else None,
            library_snapshot_id=library_id,
        )
        published += 1

    _update(
        import_id,
        library_video_id=video_id,
        library_slug=slug,
        published_at=_now(),
        rejected_count=(row["rejected_count"] or 0) + skipped,
    )
    AuditRepository.record(
        "VIDEO_IMPORT_PUBLISH",
        "camera_videos",
        video_id,
        f"Published import #{import_id} to Videos and Snapshots "
        f"({published} snapshots, 4K, lighting checked)",
        row["created_by"] or "system",
    )
    return {"video_id": video_id, "snapshots": published, "published": True}


def publish_completed_imports(app) -> list[dict]:
    """Backfill imports that finished before the library publish step existed."""
    results = []
    with app.app_context():
        for row in VideoImportRepository.unpublished_complete():
            try:
                results.append({
                    "import_id": row["id"],
                    **publish_import(app, row["id"]),
                })
            except Exception as exc:  # noqa: BLE001 - continue with the rest
                results.append({
                    "import_id": row["id"], "error": str(exc),
                })
    return results


def delete_import(import_id: int) -> None:
    """Delete an import, its library copies and every file on disk."""
    row = VideoImportRepository.get(import_id)
    if row is None:
        return
    library_root = instance_root()
    files: list[Path] = []
    directories: set[Path] = set()

    for snapshot in VideoImportSnapshotRepository.all(import_id):
        library_id = snapshot["library_snapshot_id"]
        if library_id:
            library = CameraSnapshotRepository.get(library_id)
            if library:
                files.append(library_root / library["path"])
                CameraSnapshotRepository.delete(library_id)
        for key in ("original_path", "upscaled_path", "thumb_path"):
            if snapshot[key]:
                try:
                    files.append(resolve(snapshot[key]))
                except ValueError:
                    pass

    video_id = row["library_video_id"]
    if video_id:
        library = CameraVideoRepository.get(video_id)
        if library:
            path = library_root / library["path"]
            if path.is_dir():
                directories.add(path)
            elif path.exists():
                files.append(path)
            CameraVideoRepository.delete(video_id)

    VideoImportRepository.delete(import_id)
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for path in directories:
        shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(root() / str(import_id), ignore_errors=True)


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
