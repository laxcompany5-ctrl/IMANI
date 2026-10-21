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
from datetime import datetime

from flask import current_app

from . import capture
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

def _video_source(camera, stop_event, on_frame):
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


def _demo_source(camera, stop_event, on_frame):
    """Replay a synthetic walk on a loop, so the pipeline runs with no camera."""
    while not stop_event.is_set():
        for item in synthetic_frames():
            if stop_event.is_set():
                return
            on_frame([], None)
            yield item
        deadline = time.monotonic() + DEMO_PAUSE
        while time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(0.1)


def frame_source(camera, stop_event, on_frame):
    """Pick the right source for a camera and yield ``(timestamp, detections)``."""
    if (camera["source_type"] or "rtsp") == "demo":
        return _demo_source(camera, stop_event, on_frame)
    return _video_source(camera, stop_event, on_frame)


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
            config = AgentConfig(
                camera=self.camera["name"],
                source=self.camera["source_url"] or "demo",
                min_interval_s=MIN_INTERVAL,
                confirm_hits=CONFIRM_HITS,
            )
            client = _LocalIngest(self.app, on_result=self._on_capture)
            frames = frame_source(self.camera, self._stop, on_frame=self._on_frame)
            process(config, frames, client, emit=lambda _payload: None)
            # Only reachable when the source ended on its own (a finished file,
            # a closed stream, or a stop request).
            self._set_state("stopped", None)
        except Exception as exc:  # a dead stream must not take the server down
            self._set_state("error", str(exc))
        finally:
            self._flush(force=True)

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
        self._flush()

    def _on_capture(self, payload: dict, result: dict) -> None:
        self.captures += 1
        self.last_capture_at = _now()
        self._flush()

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

    def start(self, app, camera) -> Monitor:
        """Start (or return) the monitor for a camera."""
        with self._lock:
            monitor = self._monitors.get(camera["id"])
            if monitor is not None and monitor.is_running:
                return monitor
            monitor = Monitor(app, camera)
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
        if (camera["source_type"] or "rtsp") != "demo":
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


def get_manager() -> SurveillanceManager:
    """The manager for the current application."""
    return current_app.extensions["surveillance"]
