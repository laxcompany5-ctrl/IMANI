"""CCTV cameras — connect IMANI to a surveillance feed (Bit 10).

A camera is a registered video source. An analyst adds one and starts
monitoring; the server then reads that stream *itself*, detects the people in
frame, extracts their traits and feeds them into the same ingestion engine the
``/ingest/capture`` API uses. A person seen by a camera is therefore stored and
re-identified exactly as if an edge agent had posted the capture.

Two kinds of source:

* ``demo`` — a synthetic walk. Needs no hardware and no OpenCV, so the whole
  pipeline can be exercised immediately.
* ``rtsp`` / ``http`` / ``file`` / ``device`` — a real stream read with OpenCV.
"""
from __future__ import annotations

import time

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import auth, surveillance
from ..models import AuditRepository, CameraRepository, SightingRepository

bp = Blueprint("cameras", __name__, url_prefix="/cameras")

SOURCE_TYPES = ("demo", "rtsp", "http", "file", "device")
SOURCE_LABELS = {
    "demo": "Demo (synthetic walk)",
    "rtsp": "RTSP stream",
    "http": "HTTP / MJPEG stream",
    "file": "Video file",
    "device": "Local device (webcam)",
}
# Roles that may add, edit, start and stop cameras.
MUTATION_ROLES = ("analyst", "admin")


def _get_or_404(camera_id: int):
    camera = CameraRepository.get(camera_id)
    if camera is None:
        abort(404)
    return camera


def _read_form():
    """Pull the camera form into a values dict, with defaults."""
    source_type = request.form.get("source_type", "rtsp")
    if source_type not in SOURCE_TYPES:
        source_type = "rtsp"
    source_url = request.form.get("source_url", "").strip()
    if source_type == "demo" and not source_url:
        source_url = "demo"
    return {
        "name": request.form.get("name", "").strip(),
        "location": request.form.get("location", "").strip(),
        "source_type": source_type,
        "source_url": source_url,
    }


def _problems(values, ignore_id: int | None = None) -> list[str]:
    problems = []
    if not values["name"]:
        problems.append("Give the camera a name.")
    if not values["source_url"]:
        problems.append("Give the camera a source — a stream URL, a file, or a device.")
    if values["name"]:
        clash = CameraRepository.get_by_name(values["name"])
        if clash and clash["id"] != ignore_id:
            problems.append(f"A camera named '{values['name']}' already exists.")
    return problems


def _decorate(camera) -> dict:
    """Blend the stored row with the live monitor state and recent captures."""
    manager = surveillance.get_manager()
    status = manager.status(camera["id"]) or {}
    data = dict(camera)
    data["running"] = bool(status.get("running"))
    # No monitor running: fall back to what the row recorded last time, so a
    # camera that could not be started still explains itself.
    data["live"] = status.get("status") or camera["status"] or "stopped"
    data["live_error"] = status.get("error") or camera["last_error"]
    data["frames_seen"] = status.get("frames_seen", 0)
    data["captures"] = status.get("captures", 0)
    data["has_live_view"] = status.get("has_live_view", False)
    if status.get("last_frame_at"):
        data["last_frame_at"] = status["last_frame_at"]
    if status.get("last_capture_at"):
        data["last_capture_at"] = status["last_capture_at"]
    _rows, total = SightingRepository.search(
        camera=camera["name"], limit=3, offset=0,
    )
    data["capture_count"] = total
    data["recent"] = _rows
    return data


# --- Pages ----------------------------------------------------------------

@bp.route("/")
@auth.login_required
def index():
    cameras = [_decorate(camera) for camera in CameraRepository.all()]
    manager = surveillance.get_manager()
    engine_problem = manager.start_error({"source_type": "rtsp"})
    return render_template(
        "cameras/list.html",
        cameras=cameras,
        source_labels=SOURCE_LABELS,
        can_manage=auth.current_user()["role"] in MUTATION_ROLES,
        engine_problem=engine_problem,
        running=sum(1 for camera in cameras if camera["running"]),
    )


@bp.route("/new", methods=("GET", "POST"))
@auth.role_required("analyst")
def create():
    values = {"name": "", "location": "", "source_type": "demo", "source_url": "demo"}
    if request.method == "POST":
        values = _read_form()
        problems = _problems(values)
        if problems:
            for problem in problems:
                flash(problem, "danger")
            return render_template(
                "cameras/form.html", values=values, source_types=SOURCE_TYPES,
                source_labels=SOURCE_LABELS,
            ), 400

        camera_id = CameraRepository.create(values, auth.current_user()["username"])
        AuditRepository.record(
            "INSERT", "cameras", camera_id,
            f"connected camera '{values['name']}' ({values['source_type']})",
            auth.current_user()["username"],
        )
        flash(f"Camera '{values['name']}' connected.", "success")
        return redirect(url_for("cameras.detail", camera_id=camera_id))

    return render_template(
        "cameras/form.html", values=values, source_types=SOURCE_TYPES,
        source_labels=SOURCE_LABELS,
    )


@bp.route("/<int:camera_id>")
@auth.login_required
def detail(camera_id: int):
    camera = _decorate(_get_or_404(camera_id))
    return render_template(
        "cameras/detail.html",
        camera=camera,
        can_manage=auth.current_user()["role"] in MUTATION_ROLES,
        source_labels=SOURCE_LABELS,
    )


@bp.route("/<int:camera_id>/edit", methods=("GET", "POST"))
@auth.role_required("analyst")
def edit(camera_id: int):
    camera = _get_or_404(camera_id)
    values = {
        "name": camera["name"], "location": camera["location"] or "",
        "source_type": camera["source_type"], "source_url": camera["source_url"],
    }
    if request.method == "POST":
        values = _read_form()
        problems = _problems(values, ignore_id=camera_id)
        if problems:
            for problem in problems:
                flash(problem, "danger")
            return render_template(
                "cameras/form.html", values=values, source_types=SOURCE_TYPES,
                source_labels=SOURCE_LABELS, camera_id=camera_id,
            ), 400

        # Editing a running camera: stop it first so the thread picks up the
        # new source on the next start.
        if surveillance.get_manager().is_running(camera_id):
            surveillance.get_manager().stop(camera_id)
            CameraRepository.set_state(camera_id, "stopped", None)

        CameraRepository.update(camera_id, values)
        AuditRepository.record(
            "UPDATE", "cameras", camera_id,
            f"updated camera '{values['name']}'",
            auth.current_user()["username"],
        )
        flash("Camera updated.", "success")
        return redirect(url_for("cameras.detail", camera_id=camera_id))

    return render_template(
        "cameras/form.html", values=values, source_types=SOURCE_TYPES,
        source_labels=SOURCE_LABELS, camera_id=camera_id,
    )


@bp.route("/<int:camera_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(camera_id: int):
    camera = _get_or_404(camera_id)
    surveillance.get_manager().stop(camera_id)
    CameraRepository.delete(camera_id)
    AuditRepository.record(
        "DELETE", "cameras", camera_id,
        f"disconnected camera '{camera['name']}'",
        auth.current_user()["username"],
    )
    flash(f"Camera '{camera['name']}' disconnected.", "success")
    return redirect(url_for("cameras.index"))


@bp.route("/<int:camera_id>/start", methods=("POST",))
@auth.role_required("analyst")
def start(camera_id: int):
    camera = _get_or_404(camera_id)
    manager = surveillance.get_manager()
    problem = manager.start_error(camera)
    if problem:
        CameraRepository.set_state(camera_id, "unavailable", problem)
        flash(problem, "danger")
        return redirect(url_for("cameras.detail", camera_id=camera_id))

    if manager.is_running(camera_id):
        flash(f"'{camera['name']}' is already being monitored.", "info")
        return redirect(url_for("cameras.detail", camera_id=camera_id))

    manager.start(current_app, camera)
    AuditRepository.record(
        "START", "cameras", camera_id,
        f"started monitoring '{camera['name']}'",
        auth.current_user()["username"],
    )
    flash(f"Now monitoring '{camera['name']}'.", "success")
    return redirect(url_for("cameras.detail", camera_id=camera_id))


@bp.route("/<int:camera_id>/stop", methods=("POST",))
@auth.role_required("analyst")
def stop(camera_id: int):
    camera = _get_or_404(camera_id)
    if surveillance.get_manager().stop(camera_id):
        AuditRepository.record(
            "STOP", "cameras", camera_id,
            f"stopped monitoring '{camera['name']}'",
            auth.current_user()["username"],
        )
        flash(f"Stopped monitoring '{camera['name']}'.", "info")
    else:
        flash(f"'{camera['name']}' is not being monitored.", "info")
    CameraRepository.set_state(camera_id, "stopped", None)
    return redirect(url_for("cameras.detail", camera_id=camera_id))


# --- Live view ------------------------------------------------------------

@bp.route("/<int:camera_id>/snapshot.jpg")
@auth.login_required
def snapshot(camera_id: int):
    """A single frame from the live view, or 503 when there is none yet."""
    _get_or_404(camera_id)
    monitor = surveillance.get_manager().get(camera_id)
    frame = monitor.latest_jpeg() if monitor else None
    if frame is None:
        abort(503, description="No live view yet — start monitoring this camera.")
    return Response(frame, mimetype="image/jpeg")


def _mjpeg(monitor):
    boundary = b"--frame\r\n"
    while True:
        frame = monitor.latest_jpeg()
        if frame:
            yield (boundary
                   + b"Content-Type: image/jpeg\r\n"
                   + b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                   + frame + b"\r\n")
        else:
            # Nothing to show: stop once the camera has gone quiet, otherwise
            # just spin until the next frame arrives.
            if not monitor.is_running:
                break
        time.sleep(0.1)


@bp.route("/<int:camera_id>/stream")
@auth.login_required
def stream(camera_id: int):
    """MJPEG live view. Open it in a browser tab to watch the camera."""
    _get_or_404(camera_id)
    monitor = surveillance.get_manager().get(camera_id)
    if monitor is None or monitor.latest_jpeg() is None:
        abort(503, description="No live view yet — start monitoring this camera.")
    return Response(
        _mjpeg(monitor),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@bp.route("/status.json")
@auth.login_required
def status():
    """Poll-friendly snapshot of every camera's live state."""
    manager = surveillance.get_manager()
    payload = []
    for camera in CameraRepository.all():
        live = manager.status(camera["id"]) or {}
        payload.append({
            "id": camera["id"],
            "name": camera["name"],
            "status": live.get("status") or camera["status"] or "stopped",
            "running": bool(live.get("running")),
            "error": live.get("error") or camera["last_error"],
            "frames_seen": live.get("frames_seen", 0),
            "captures": live.get("captures", 0),
            "last_frame_at": live.get("last_frame_at") or camera["last_frame_at"],
            "last_capture_at": live.get("last_capture_at") or camera["last_capture_at"],
            "has_live_view": live.get("has_live_view", False),
        })
    return jsonify({"cameras": payload})
