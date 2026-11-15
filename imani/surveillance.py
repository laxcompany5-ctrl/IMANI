"""Connect IMANI to CCTV and turn what the cameras see into captures (Bit 10).

This is the *in-app* version of the capture pipeline. Instead of running
``agent/`` as a separate program beside the recorder, the server itself opens
each registered camera's stream, detects the people in frame, extracts their
traits and hands them to :func:`imani.capture.ingest` — the same function the
``/ingest/capture`` API uses, so recognition behaviour is identical whether a
capture arrives from the edge agent over HTTP or from a monitor thread here.

Sources fall into two kinds:

* ``demo`` — replays a synthetic walk. Needs no hardware and no OpenCV, so the
  whole pipeline can be exercised on any machine.
* ``rtsp`` / ``http`` / ``file`` / ``device`` — a real stream read with OpenCV.

Every camera runs in its own daemon thread. The thread pushes a Flask app
context so it can read and write the database; state lives on the
:class:`Monitor` and is flushed to the ``cameras`` row on a throttle rather
than on every frame.

Traits and tracking are the capture agent's (``agent/traits.py``,
``agent/tracker.py``), shared rather than duplicated, so a monitor sees the
same weights, thresholds and identity decisions as a real edge deployment.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

from flask import current_app

from . import capture, media
from .models import CameraRepository

# How long to wait for a monitor thread to finish when stopping it.
JOIN_TIMEOUT = 5.0
# Seconds between writing last-seen values back to the cameras row.
FLUSH_INTERVAL = 5.0
# Seconds to pause between synthetic walks for a demo camera.
DEMO_PAUSE = 10.0
# Frame pacing for real cameras: analyse every Nth frame.
FRAME_STRIDE = 5
# A person must be seen in this many analysed frames before a capture fires.
CONFIRM_HITS = 3
# Seconds between captures of the same person at the same camera.
MIN_INTERVAL = 15.0
# Seconds of footage to keep recording after a capture so the clip shows the
# moment, not just the frames before it.
POST_ROLL = 1.5

# The trait engine is shared with the capture agent (agent/), which ships in
# this repository. Import defensively: if it is missing, monitoring a real
# camera is impossible and we degrade instead of crashing the server.
try:
    from agent.agent import AgentConfig, process, synthetic_frames
    _ENGINE_ERROR = None
except Exception as exc:  # pragma: no cover - only if agent/ is absent
    AgentConfig = process = synthetic_frames = None
    _ENGINE_ERROR = str(exc)


def engine_error() -> str | None:
    """Why the capture engine cannot run, or ``None`` when it can."""
    return _ENGINE_ERROR


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- Frame sources --------------------------------------------------------

def _video_source(camera, stop_event, on_frame, on_idle=None):
    """Read a real camera with OpenCV and yield ``(timestamp, detections)``."""
    from agent import vision

    if not vision.available():
        raise RuntimeError(
            "OpenCV is not installed on this server, so it cannot read a real "
            "video stream. Install it with `pip install -r "
            "agent/requirements.txt`, or switch this camera to the 'demo' "
            "source to try the pipeline without hardware."
        )

    capture_obj = vision.open_capture(camera["source_url"])
    analyzer = vision.FrameAnalyzer(face=False)
    index = 0
    try:
        while not stop_event.is_set():
            ok, frame = capture_obj.read()
            if not ok:
                break  # stream ended or was closed
            index += 1
            if index % FRAME_STRIDE:
                continue
            detections = analyzer.detect(frame)
            on_frame(detections, vision.encode_jpeg(frame))
            yield time.time(), detections
    finally:
        capture_obj.release()


def _demo_source(camera, stop_event, on_frame, on_idle=None):
    """Replay a synthetic walk on a loop, so the pipeline runs with no camera.

    The frames are drawn (and labelled SYNTHETIC FEED) rather than photographed,
    so the archive has footage to replay without pretending to be real video.
    """
    while not stop_event.is_set():
        for timestamp, detections in synthetic_frames():
            if stop_event.is_set():
                return
            jpeg = None
            if detections:
                jpeg = media.render_demo_frame(
                    detections[0], camera=camera["name"],
                )
            on_frame(detections, jpeg)
            yield timestamp, detections
        if on_idle is not None:
            on_idle()  # end of this walk: finish any clip in progress
        deadline = time.monotonic() + DEMO_PAUSE
        while time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(0.1)


def frame_source(camera, stop_event, on_frame, on_idle=None):
    """Pick the right source for a camera and yield ``(timestamp, detections)``."""
    if (camera["source_type"] or "rtsp") == "demo":
        return _demo_source(camera, stop_event, on_frame, on_idle)
    return _video_source(camera, stop_event, on_frame, on_idle)


# --- In-process ingestion -------------------------------------------------

class _LocalIngest:
    """Stands in for the HTTP capture client, calling the engine directly."""

    def __init__(self, app, on_result=None):
        self.app = app
        self.on_result = on_result

    def send(self, payload: dict) -> dict:
        with self.app.app_context():
            result = capture.ingest(payload, actor="cctv")
        if self.on_result is not None:
            self.on_result(payload, result)
        return result


# --- Monitor --------------------------------------------------------------

class Monitor:
    """Watches one camera in its own thread and feeds captures into IMANI."""

    def __init__(self, app, camera):
        # A background thread has no request context, so a `current_app` proxy
        # cannot be resolved there. Keep the real application object.
        self.app = (app._get_current_object() if hasattr(app, "_get_current_object")
                    else app)
        self.camera = camera
        self.camera_id = camera["id"]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._buffer: deque = deque(
            maxlen=max(2, int(app.config.get("MEDIA_CLIP_FRAMES", 24)))
        )
        # Clips still collecting their post-roll before being written.
        self._pending: list[dict] = []
        self._last_flush = time.monotonic()

        # Live state. These are the values shown on the cameras page while a
        # monitor is running; they are flushed to the database on a throttle.
        self.status = "stopped"
        self.error: str | None = None
        self.frames_seen = 0
        self.captures = 0
        self.last_frame_at = camera["last_frame_at"]
        self.last_capture_at = camera["last_capture_at"]

    # --- lifecycle ------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"imani-camera-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_TIMEOUT)
        self._finalize_pending()
        self._set_state("stopped", None)

    def latest_jpeg(self) -> bytes | None:
        return self._latest_jpeg

    def status_payload(self) -> dict:
        return {
            "running": self.is_running,
            "status": self.status,
            "error": self.error,
            "frames_seen": self.frames_seen,
            "captures": self.captures,
            "last_frame_at": self.last_frame_at,
            "last_capture_at": self.last_capture_at,
            "has_live_view": self._latest_jpeg is not None,
        }

    # --- worker ---------------------------------------------------------

    def _run(self) -> None:
        try:
            self._set_state("connecting", None)
            alg = getattr(self, 'algorithm', {}) or {}
            config = AgentConfig(
                camera=self.camera["name"],
                source=self.camera["source_url"] or "demo",
                min_interval_s=float(alg.get("min_interval_s", MIN_INTERVAL)),
                confirm_hits=int(alg.get("confirm_hits", CONFIRM_HITS)),
                iou_threshold=float(alg.get("iou_threshold", 0.3)),
                every_n_frames=int(alg.get("every_n_frames", 5)),
                face=bool(alg.get("face", False)),
            )
            try:
                from agent.traits import Calibration
                config = config.__class__(
                    **{**config.__dict__, 'calibration': Calibration(
                        camera_height_cm=float(alg.get("camera_height_cm", 250)),
                        principal_y=float(alg.get("principal_y", 360)),
                    )}
                )
            except Exception:
                pass
            client = _LocalIngest(self.app, on_result=self._on_capture)
            if getattr(self, '_use_phone_source', False):
                frames = self._phone_frame_source()
            else:
                frames = frame_source(
                    self.camera, self._stop,
                    on_frame=self._on_frame, on_idle=self._finalize_pending,
                )
            process(config, frames, client, emit=lambda _payload: None)
            self._set_state("stopped", None)
        except Exception as exc:
            self._set_state("error", str(exc))
        finally:
            self._finalize_pending()
            self._flush(force=True)

    def _phone_frame_source(self):
        """Yield frames pushed from phone/browser capture.

        If the phone stops sending (tab closed, screen locked, network lost),
        the monitor shuts itself down after a short idle period instead of
        sitting in an error loop. The next frame from the phone restarts it.
        """
        import time as _time

        idle_limit = float(self.app.config.get("PHONE_IDLE_SECONDS", 10.0)) or 10.0
        last_frame = _time.time()
        while not self._stop.is_set():
            if self._phone_frames:
                _ts, jpeg = self._phone_frames.popleft()
                self._on_frame([], jpeg)
                yield _time.time(), []
                last_frame = _time.time()
            else:
                if _time.time() - last_frame > idle_limit:
                    self._set_state("stopped", "Phone stopped streaming")
                    return
                _time.sleep(0.1)

    def _set_state(self, status: str, error: str | None) -> None:
        with self._lock:
            self.status = status
            self.error = error
        try:
            with self.app.app_context():
                CameraRepository.set_state(self.camera_id, status, error)
        except Exception:  # pragma: no cover - best effort during shutdown
            pass

    def _on_frame(self, detections, jpeg: bytes | None) -> None:
        self.frames_seen += 1
        self.last_frame_at = _now()
        if self.status != "live":
            self._set_state("live", None)
        if jpeg:
            self._latest_jpeg = jpeg
            self._buffer.append((time.time(), jpeg))
            for item in self._pending:
                item["frames"].append((time.time(), jpeg))
        self._finalize_pending(expired_only=True)
        self._flush()

    def _on_capture(self, payload: dict, result: dict) -> None:
        self.captures += 1
        self.last_capture_at = _now()
        sighting_id = result.get("sighting_id")
        if sighting_id and self._buffer:
            self._pending.append({
                "sighting_id": sighting_id,
                "reference": result.get("reference") or f"SG-{sighting_id}",
                "frames": list(self._buffer),
                "deadline": time.monotonic() + POST_ROLL,
            })
        self._flush()

    def _finalize_pending(self, expired_only: bool = False) -> None:
        """Write clips whose post-roll has finished.

        Archiving is best-effort: a full disk or an unreadable frame must never
        stop the camera from monitoring.
        """
        if not self._pending:
            return
        now = time.monotonic()
        ready = [
            item for item in self._pending
            if not expired_only or now >= item["deadline"]
        ]
        if not ready:
            return
        self._pending = [item for item in self._pending if item not in ready]
        fps = float(self.app.config.get("MEDIA_CLIP_FPS", 5.0)) or 5.0
        for item in ready:
            try:
                with self.app.app_context():
                    manifest = media.save_capture(
                        self.camera["name"], _now(), item["reference"],
                        item["frames"], fps,
                    )
                    if manifest:
                        media.attach(item["sighting_id"], manifest)
            except Exception:  # pragma: no cover - never take the camera down
                pass

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_flush) < FLUSH_INTERVAL:
            return
        self._last_flush = now
        try:
            with self.app.app_context():
                CameraRepository.update_seen(
                    self.camera_id, self.last_frame_at, self.last_capture_at,
                )
        except Exception:  # pragma: no cover - best effort
            pass


# --- Manager --------------------------------------------------------------

class SurveillanceManager:
    """Owns every :class:`Monitor` for one application instance.

    Lives in ``app.extensions["surveillance"]`` so each test (and each app
    instance) gets its own set of threads.
    """

    def __init__(self):
        self._monitors: dict[int, Monitor] = {}
        self._lock = threading.Lock()

    def start(self, app, camera, phone: bool = False) -> Monitor:
        """Start (or return) the monitor for a camera.

        ``phone=True`` forces the phone-frame source. Scanning a camera's QR
        code uses this so a phone can take over a camera that is otherwise an
        RTSP/demo source (for example when OpenCV is not installed).
        """
        monitor = self._monitors.get(camera["id"])
        if (
            monitor is not None
            and monitor.is_running
            and (not phone or getattr(monitor, "_use_phone_source", False))
        ):
            return monitor
        if monitor is not None:
            # A running RTSP/demo monitor must be stopped before replacing it
            # with a phone-source monitor.
            self.stop(camera["id"])
        with self._lock:
            monitor = self._monitors.get(camera["id"])
            if monitor is not None and monitor.is_running:
                return monitor
            monitor = Monitor(app, camera)
            if phone:
                monitor._use_phone_source = True
            self._monitors[camera["id"]] = monitor
        monitor.start()
        return monitor

    def stop(self, camera_id: int) -> bool:
        """Stop a camera's monitor. Returns False if it was not running."""
        with self._lock:
            monitor = self._monitors.pop(camera_id, None)
        if monitor is None:
            return False
        monitor.stop()
        return True

    def get(self, camera_id: int) -> Monitor | None:
        return self._monitors.get(camera_id)

    def is_running(self, camera_id: int) -> bool:
        monitor = self._monitors.get(camera_id)
        return bool(monitor and monitor.is_running)

    def status(self, camera_id: int) -> dict | None:
        monitor = self._monitors.get(camera_id)
        return monitor.status_payload() if monitor else None

    def stop_all(self) -> None:
        for camera_id in list(self._monitors):
            self.stop(camera_id)

    def start_error(self, camera) -> str | None:
        """A human-readable reason this camera cannot be monitored, if any."""
        if _ENGINE_ERROR is not None:
            return f"The capture engine is unavailable: {_ENGINE_ERROR}"
        source_type = camera["source_type"] or "rtsp"
        if source_type in ("demo", "phone"):
            # Demo is synthetic and phone frames arrive over HTTP, so neither
            # needs OpenCV on the server.
            return None
        if source_type:
            try:
                from agent import vision

                if not vision.available():
                    return (
                        "OpenCV is not installed on this server, so it cannot "
                        "read a real video stream. Install it with `pip install "
                        "-r agent/requirements.txt`, or switch this camera to "
                        "the 'demo' source."
                    )
            except Exception as exc:  # pragma: no cover
                return f"OpenCV could not be loaded: {exc}"
        return None


def init_app(app) -> None:
    """Attach a :class:`SurveillanceManager` to the application."""
    app.extensions["surveillance"] = SurveillanceManager()
    # Monitors live in the process, so a development-server restart leaves
    # stale `live` rows behind. Production workers are managed separately and
    # must not clear each other's state.
    if app.config.get("ENVIRONMENT") != "production":
        try:
            from .models import CameraRepository

            with app.app_context():
                CameraRepository.clear_runtime_states()
        except Exception:  # pragma: no cover - database may not exist yet
            pass


def get_manager() -> SurveillanceManager:
    """The manager for the current application."""
    return current_app.extensions["surveillance"]


# --- Bit 12/13 extensions -------------------------------------------------
# Per-camera algorithm settings, phone frame injection, automatic snapshots
# and continuous video segments. These wrap the methods above rather than
# editing them, so the core monitor stays small and the earlier behaviour is
# unchanged when the new settings are absent.
import json as _json

_orig_monitor_init = Monitor.__init__


def _cam(camera, key, default=None):
    """Read a key from a sqlite3.Row or a dict."""
    try:
        return camera[key]
    except (KeyError, IndexError, TypeError):
        return default


def _monitor_init_patched(self, app, camera):
    _orig_monitor_init(self, app, camera)
    # Load per-camera algorithm settings (Bit 12).
    self.algorithm = {}
    try:
        raw = _cam(camera, "algorithm_json")
        if raw:
            self.algorithm = _json.loads(raw) or {}
    except Exception:
        self.algorithm = {}
    self._phone_frames = deque(maxlen=30)
    self._use_phone_source = (_cam(camera, "source_type") == "phone")
    # Frames since the current recording segment began (Bit 13).
    self._segment_frames: deque = deque(maxlen=1200)
    self._segment_started: float | None = None
    self._last_auto_snapshot = 0.0


def _monitor_start_patched(self):
    if self.is_running:
        return
    self._stop.clear()
    self._thread = threading.Thread(
        target=self._run,
        name=f"imani-camera-{self.camera_id}",
        daemon=True,
    )
    self._thread.start()


def push_frame(self, jpeg):
    """Push a frame from phone/browser capture."""
    if jpeg:
        self._phone_frames.append((time.time(), jpeg))


Monitor.__init__ = _monitor_init_patched
Monitor.start = _monitor_start_patched
Monitor.push_frame = push_frame


def _save_auto_snapshot(self) -> None:
    """Persist the current frame, with measured features for AI search."""
    from .models import CameraSnapshotRepository

    jpeg = self._latest_jpeg
    if not jpeg:
        return
    with self.app.app_context():
        result = media.save_snapshot(
            self.camera["name"], jpeg,
            camera_id=self.camera_id,
            source_type=_cam(self.camera, "source_type"),
        )
        if not result:
            return
        CameraSnapshotRepository.create({
            "camera_id": self.camera_id,
            "camera_name": self.camera["name"],
            "filename": result["filename"],
            "path": result["path"],
            "source_type": _cam(self.camera, "source_type"),
            "captured_at": _now(),
            "features_json": result.get("features_json"),
        })


def _write_segment(self) -> None:
    """Flush the frames collected since the last segment to disk."""
    from .models import CameraVideoRepository

    frames = list(self._segment_frames)
    self._segment_frames.clear()
    self._segment_started = None
    if len(frames) < 2:
        return
    if not self.app.config.get("VIDEO_SEGMENT_ENABLED", True):
        return
    with self.app.app_context():
        result = media.save_video_recording(
            self.camera["name"], frames,
            camera_id=self.camera_id,
            source_type=_cam(self.camera, "source_type"),
        )
        if not result:
            return
        CameraVideoRepository.create({
            "camera_id": self.camera_id,
            "camera_name": self.camera["name"],
            "filename": result["filename"],
            "path": result["path"],
            "source_type": _cam(self.camera, "source_type"),
            "duration_sec": result["duration_sec"],
            "frame_count": result["frame_count"],
            "size_bytes": result["size_bytes"],
            "started_at": result["manifest"].get("started_at"),
            "ended_at": result["manifest"].get("ended_at"),
            "captured_at": _now(),
            "kind": "auto",
            "mime_type": "image/jpeg",
            "features_json": result.get("features_json"),
        })


def _record_frame(self, jpeg: bytes | None) -> None:
    """Keep a frame for the current continuous recording segment."""
    if not jpeg or not self.app.config.get("VIDEO_SEGMENT_ENABLED", True):
        return
    if self._segment_started is None:
        self._segment_started = time.time()
    self._segment_frames.append((time.time(), jpeg))


# Wrap _on_frame so every displayed frame is also a recording frame (Bit 13).
_orig_on_frame = Monitor._on_frame


def _on_frame_recording(self, detections, jpeg):
    result = _orig_on_frame(self, detections, jpeg)
    try:
        _record_frame(self, jpeg)
    except Exception:  # pragma: no cover - recording must never break capture
        pass
    return result


Monitor._on_frame = _on_frame_recording


# Wrap _flush: throttle snapshots and close recording segments on a schedule,
# and flush the last partial segment when the monitor stops.
_orig_flush = Monitor._flush


def _flush_with_snapshot(self, force=False):
    result = _orig_flush(self, force)
    try:
        now = time.monotonic()
        interval = float(self.app.config.get("SNAPSHOT_INTERVAL_SECONDS", 30))
        due = (now - self._last_auto_snapshot) >= interval
        if (due or force) and self._latest_jpeg:
            if force and self._last_auto_snapshot:
                pass  # already captured this run; don't duplicate on stop
            else:
                self._last_auto_snapshot = now
                _save_auto_snapshot(self)
    except Exception:  # pragma: no cover - best effort
        pass
    try:
        seconds = float(self.app.config.get("VIDEO_SEGMENT_SECONDS", 20))
        started = self._segment_started
        if force or (started is not None and (time.time() - started) >= seconds):
            _write_segment(self)
    except Exception:  # pragma: no cover - best effort
        pass
    return result


Monitor._flush = _flush_with_snapshot
