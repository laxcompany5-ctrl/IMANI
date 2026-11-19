"""CCTV cameras — connect IMANI to a surveillance feed (Bit 10+12).
"""
from __future__ import annotations

import json
import time
import socket
from urllib.parse import urlparse

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
    session,
    url_for,
)

from .. import auth, media, qr, surveillance
from ..models import (
    AuditRepository,
    CameraRepository,
    CameraGroupRepository,
    CameraVideoRepository,
    SightingRepository,
)

bp = Blueprint("cameras", __name__, url_prefix="/cameras")

SOURCE_TYPES = ("demo", "rtsp", "http", "file", "device", "phone")
SOURCE_LABELS = {
    "demo": "Demo (synthetic walk)",
    "rtsp": "RTSP stream",
    "http": "HTTP / MJPEG stream",
    "file": "Video file",
    "device": "Local device (webcam)",
    "phone": "Phone / browser capture (getUserMedia)",
}

# DVR/NVR templates
DVR_TEMPLATES = {
    "generic_rtsp": {"name": "Generic RTSP", "template": "rtsp://{user}:{pass}@{ip}:{port}/{path}"},
    "hikvision": {"name": "Hikvision", "template": "rtsp://{user}:{pass}@{ip}:554/Streaming/Channels/{channel}01"},
    "dahua": {"name": "Dahua", "template": "rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel={channel}&subtype=0"},
    "axis": {"name": "Axis", "template": "rtsp://{user}:{pass}@{ip}:554/axis-media/media.amp"},
    "reolink": {"name": "Reolink", "template": "rtsp://{user}:{pass}@{ip}:554/h264Preview_{channel}_main"},
    "uniview": {"name": "Uniview", "template": "rtsp://{user}:{pass}@{ip}:554/media/video1"},
    "amcrest": {"name": "Amcrest", "template": "rtsp://{user}:{pass}@{ip}:554/cam/realmonitor?channel={channel}&subtype=1"},
}

DEFAULT_ALGORITHM = {
    "every_n_frames": 5,
    "confirm_hits": 3,
    "min_interval_s": 15.0,
    "iou_threshold": 0.3,
    "camera_height_cm": 250,
    "principal_y": 360,
    "face": False,
}

# Roles that may add, edit, start and stop cameras.
MUTATION_ROLES = ("analyst", "admin")


def _row_get(row, key, default=None):
    """Read a key from a sqlite3.Row or a dict without raising."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _get_or_404(camera_id: int):
    camera = CameraRepository.get(camera_id)
    if camera is None:
        abort(404)
    return camera


def _get_algorithm(camera):
    try:
        if _row_get(camera, "algorithm_json"):
            alg = json.loads(camera["algorithm_json"])
            merged = dict(DEFAULT_ALGORITHM)
            merged.update(alg or {})
            return merged
    except Exception:
        pass
    return dict(DEFAULT_ALGORITHM)


def _read_form():
    """Pull the camera form into a values dict, with defaults."""
    source_type = request.form.get("source_type", "rtsp")
    if source_type not in SOURCE_TYPES:
        source_type = "rtsp"
    source_url = request.form.get("source_url", "").strip()
    if source_type == "demo" and not source_url:
        source_url = "demo"
    if source_type == "phone" and not source_url:
        source_url = "phone"
    group_id = request.form.get("group_id") or None
    try:
        group_id = int(group_id) if group_id else None
    except Exception:
        group_id = None
    algorithm = {}
    for key in DEFAULT_ALGORITHM:
        val = request.form.get(f"alg_{key}")
        if val is not None and val != "":
            try:
                if isinstance(DEFAULT_ALGORITHM[key], bool):
                    algorithm[key] = val in ("1", "true", "on", "yes")
                elif isinstance(DEFAULT_ALGORITHM[key], int):
                    algorithm[key] = int(float(val))
                elif isinstance(DEFAULT_ALGORITHM[key], float):
                    algorithm[key] = float(val)
                else:
                    algorithm[key] = val
            except Exception:
                pass
    return {
        "name": request.form.get("name", "").strip(),
        "location": request.form.get("location", "").strip(),
        "source_type": source_type,
        "source_url": source_url,
        "group_id": group_id,
        "algorithm_json": json.dumps(algorithm) if algorithm else None,
    }


def _problems(values, ignore_id: int | None = None) -> list[str]:
    problems = []
    if not values["name"]:
        problems.append("Give the camera a name.")
    if values["source_type"] != "phone" and not values["source_url"]:
        problems.append("Give the camera a source — a stream URL, a file, or a device.")
    if values["source_type"] == "phone" and not values["source_url"]:
        values["source_url"] = "phone"
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
    data["algorithm"] = _get_algorithm(camera)
    try:
        if _row_get(camera, "group_id"):
            data["group"] = CameraGroupRepository.get(camera["group_id"])
    except Exception:
        pass
    return data


# --- Pages ----------------------------------------------------------------

@bp.route("/")
@auth.login_required
def index():
    group_id = request.args.get("group_id") or None
    try:
        group_id = int(group_id) if group_id else None
    except Exception:
        group_id = None
    cameras = [_decorate(camera) for camera in CameraRepository.all()]
    if group_id:
        cameras = [c for c in cameras if c.get("group_id") == group_id]
    manager = surveillance.get_manager()
    engine_problem = manager.start_error({"source_type": "rtsp"})
    groups = CameraGroupRepository.all()
    return render_template(
        "cameras/list.html",
        cameras=cameras,
        source_labels=SOURCE_LABELS,
        can_manage=auth.current_user()["role"] in MUTATION_ROLES,
        engine_problem=engine_problem,
        running=sum(1 for camera in cameras if camera["running"]),
        groups=groups,
        selected_group=group_id,
    )


@bp.route("/new", methods=("GET", "POST"))
@auth.role_required("analyst")
def create():
    values = {"name": "", "location": "", "source_type": "demo", "source_url": "demo", "group_id": None, "algorithm_json": None}
    groups = CameraGroupRepository.all()
    if request.method == "POST":
        values = _read_form()
        problems = _problems(values)
        if problems:
            for problem in problems:
                flash(problem, "danger")
            return render_template(
                "cameras/form.html", values=values, source_types=SOURCE_TYPES,
                source_labels=SOURCE_LABELS, groups=groups, dvr_templates=DVR_TEMPLATES,
                default_algorithm=DEFAULT_ALGORITHM,
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
        source_labels=SOURCE_LABELS, groups=groups, dvr_templates=DVR_TEMPLATES,
        default_algorithm=DEFAULT_ALGORITHM,
    )


@bp.route("/<int:camera_id>")
@auth.login_required
def detail(camera_id: int):
    camera = _decorate(_get_or_404(camera_id))
    groups = CameraGroupRepository.all()
    return render_template(
        "cameras/detail.html",
        camera=camera,
        can_manage=auth.current_user()["role"] in MUTATION_ROLES,
        source_labels=SOURCE_LABELS,
        groups=groups,
    )


@bp.route("/<int:camera_id>/edit", methods=("GET", "POST"))
@auth.role_required("analyst")
def edit(camera_id: int):
    camera = _get_or_404(camera_id)
    groups = CameraGroupRepository.all()
    alg = _get_algorithm(camera)
    values = {
        "name": camera["name"], "location": camera["location"] or "",
        "source_type": camera["source_type"], "source_url": camera["source_url"],
        "group_id": _row_get(camera, "group_id"),
    }
    values.update({f"alg_{k}": alg[k] for k in alg})
    if request.method == "POST":
        values = _read_form()
        problems = _problems(values, ignore_id=camera_id)
        if problems:
            for problem in problems:
                flash(problem, "danger")
            return render_template(
                "cameras/form.html", values=values, source_types=SOURCE_TYPES,
                source_labels=SOURCE_LABELS, camera_id=camera_id, groups=groups,
                dvr_templates=DVR_TEMPLATES, default_algorithm=DEFAULT_ALGORITHM,
            ), 400

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
        source_labels=SOURCE_LABELS, camera_id=camera_id, groups=groups,
        dvr_templates=DVR_TEMPLATES, default_algorithm=DEFAULT_ALGORITHM,
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


# --- Connection test ------------------------------------------------------

@bp.route("/test-connection", methods=("POST",))
@auth.role_required("analyst")
def test_connection():
    data = request.get_json(silent=True) or request.form
    source_type = data.get("source_type") or "rtsp"
    source_url = (data.get("source_url") or "").strip()
    result = {"ok": False, "message": "", "details": {}}
    if source_type == "demo":
        result = {"ok": True, "message": "Demo source is ready (synthetic).", "details": {"type": "demo"}}
        return jsonify(result)
    if source_type == "phone":
        result = {"ok": True, "message": "Phone/browser capture is ready. Open the capture page on your phone.", "details": {"type": "phone"}}
        return jsonify(result)
    if not source_url:
        result["message"] = "No source URL provided."
        return jsonify(result), 400
    try:
        if source_type in ("http", "rtsp"):
            parsed = urlparse(source_url)
            host = parsed.hostname
            port = parsed.port or (554 if source_type == "rtsp" else 80)
            if host:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                try:
                    code = sock.connect_ex((host, port))
                    if code == 0:
                        result["ok"] = True
                        result["message"] = f"TCP connection to {host}:{port} succeeded."
                        result["details"] = {"host": host, "port": port, "reachable": True}
                    else:
                        result["message"] = f"Cannot reach {host}:{port} (connection refused/timeout)."
                        result["details"] = {"host": host, "port": port, "reachable": False}
                finally:
                    sock.close()
            else:
                result["message"] = "Could not parse host from URL."
        elif source_type == "file":
            import os
            if os.path.exists(source_url):
                result["ok"] = True
                result["message"] = f"File found: {source_url}"
                result["details"] = {"exists": True, "size": os.path.getsize(source_url)}
            else:
                result["message"] = f"File not found: {source_url}"
        elif source_type == "device":
            result["ok"] = True
            result["message"] = f"Device index {source_url} will be tested when monitoring starts."
            result["details"] = {"device": source_url}
        else:
            result["message"] = "Unsupported source type for testing."
    except Exception as e:
        result["message"] = f"Test failed: {e}"
    return jsonify(result)


# --- Phone/browser capture ------------------------------------------------

def _decode_frame(value) -> bytes | None:
    """Accept a data URL or a bare base64 JPEG."""
    if not value or not isinstance(value, str):
        return None
    import base64

    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        return base64.b64decode(payload)
    except Exception:
        return None


def _push_phone_frame(camera, raw: bytes) -> bool:
    """Feed a browser frame into this camera's monitor, starting it if needed.

    A phone can be paired to *any* camera, not only one created with the
    ``phone`` source. The first frame switches that camera's monitor into
    phone-frame mode, so QR pairing also works for demo/RTSP entries whose
    stream cannot be read by this server (for example when OpenCV is absent).
    """
    manager = surveillance.get_manager()
    monitor = manager.get(camera["id"])
    phone_mode = bool(getattr(monitor, "_use_phone_source", False))
    if monitor is None or not monitor.is_running or not phone_mode:
        monitor = manager.start(current_app, camera, phone=True)
        try:
            current_app.logger.info(
                "Phone paired with camera %s (%s); switched monitor to phone source",
                camera["name"], camera["id"],
            )
        except Exception:  # pragma: no cover - logging is best effort
            pass
    if monitor is None:
        return False
    monitor.push_frame(raw)
    return True


def _store_audio(camera, data: bytes, mime_type: str | None):
    """Persist one audio slice recorded by a phone and index it."""
    from datetime import datetime

    if not data:
        return jsonify({"ok": False, "error": "no audio"}), 400
    result = media.save_audio(
        camera["name"], data, mime_type,
        camera_id=camera["id"], source_type=camera["source_type"],
    )
    if not result:
        return jsonify({"ok": False, "error": "could not save audio"}), 400
    video_id = CameraVideoRepository.create({
        "camera_id": camera["id"],
        "camera_name": camera["name"],
        "filename": result["filename"],
        "path": result["path"],
        "source_type": camera["source_type"],
        "size_bytes": result["size_bytes"],
        "mime_type": result["mime_type"],
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "audio",
    })
    return jsonify({
        "ok": True, "id": video_id, "path": result["path"],
        "mime_type": result["mime_type"],
    })


@bp.route("/<int:camera_id>/capture", methods=("GET", "POST"))
@auth.login_required
def phone_capture(camera_id: int):
    camera = _get_or_404(camera_id)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        raw = _decode_frame(data.get("frame"))
        if raw is None:
            return jsonify({"ok": False, "error": "no frame"}), 400
        try:
            if not _push_phone_frame(camera, raw):
                return jsonify({"ok": False, "error": "monitor unavailable"}), 503
            monitor = surveillance.get_manager().get(camera["id"])
            payload = {"ok": True}
            if monitor is not None:
                payload.update({
                    "status": monitor.status,
                    "frames": monitor.frames_seen,
                    "captures": monitor.captures,
                })
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return render_template(
        "cameras/phone_capture.html", camera=camera,
        scan_url=qr.scan_url(camera_id),
    )


@bp.route("/<int:camera_id>/audio", methods=("POST",))
@auth.login_required
def phone_audio(camera_id: int):
    camera = _get_or_404(camera_id)
    uploaded = request.files.get("audio")
    if uploaded is not None:
        data, mime = uploaded.read(), uploaded.mimetype
    else:
        data, mime = request.get_data(), request.content_type
    return _store_audio(camera, data, mime)


# --- QR pairing -----------------------------------------------------------

@bp.route("/<int:camera_id>/qr")
@auth.login_required
def qr_page(camera_id: int):
    camera = _get_or_404(camera_id)
    return render_template(
        "cameras/qr.html", camera=camera, scan_url=qr.scan_url(camera_id),
    )


@bp.route("/<int:camera_id>/qr.png")
@auth.login_required
def qr_png(camera_id: int):
    _get_or_404(camera_id)
    try:
        payload = qr.png(qr.scan_url(camera_id))
    except Exception as exc:
        abort(503, description=f"QR generation is unavailable: {exc}")
    return Response(
        payload, mimetype="image/png",
        headers={"Cache-Control": "no-store"},
    )


def _scan_camera(token: str):
    camera_id = qr.load_token(token)
    if camera_id is None:
        abort(403, description="This pairing link is invalid or has expired.")
    return _get_or_404(camera_id)


def _secure_scan_url(token: str) -> str | None:
    """A best-effort HTTPS URL for the same scan page, when one is configured.

    Phones refuse ``getUserMedia`` on plain-HTTP LAN origins, so the page can
    offer a secure link when the operator runs the server with ``IMANI_HTTPS=1``
    (or a tunnel via ``IMANI_PUBLIC_URL``).
    """
    if not current_app.config.get("HTTPS_ENABLED"):
        return None
    override = (current_app.config.get("PUBLIC_BASE_URL") or "").strip()
    if override:
        return f"{override.rstrip('/')}/cameras/scan/{token}"
    host = (request.host or "").split(":")[0] or "127.0.0.1"
    port = str(current_app.config.get("PUBLIC_PORT") or "5443")
    return f"https://{host}:{port}/cameras/scan/{token}"


@bp.route("/scan/<token>")
def scan(token: str):
    """Landing page a phone opens after scanning a camera's QR code."""
    camera = _scan_camera(token)
    # Start the phone-source monitor as soon as the page is opened, so the
    # device is visible as connecting/capturing before the first frame lands.
    try:
        surveillance.get_manager().start(current_app, camera, phone=True)
    except Exception:  # pragma: no cover - the frame endpoint will retry
        current_app.logger.exception(
            "Could not start phone monitor for camera %s", camera["id"]
        )
    return render_template(
        "cameras/scan.html", camera=camera, token=token,
        secure_url=_secure_scan_url(token),
    )


@bp.route("/scan/<token>/frame", methods=("POST",))
def scan_frame(token: str):
    camera = _scan_camera(token)
    data = request.get_json(silent=True) or {}
    raw = _decode_frame(data.get("frame"))
    if raw is None:
        return jsonify({"ok": False, "error": "no frame"}), 400
    if not _push_phone_frame(camera, raw):
        return jsonify({"ok": False, "error": "monitor unavailable"}), 503
    monitor = surveillance.get_manager().get(camera["id"])
    payload = {"ok": True}
    if monitor is not None:
        payload.update({
            "status": monitor.status,
            "frames": monitor.frames_seen,
            "captures": monitor.captures,
        })
    return jsonify(payload)


@bp.route("/scan/<token>/audio", methods=("POST",))
def scan_audio(token: str):
    camera = _scan_camera(token)
    uploaded = request.files.get("audio")
    if uploaded is not None:
        data, mime = uploaded.read(), uploaded.mimetype
    else:
        data, mime = request.get_data(), request.content_type
    return _store_audio(camera, data, mime)


@bp.route("/scan/<token>/flush", methods=("POST",))
def scan_flush(token: str):
    """Flush a phone's buffered media when its page is hidden or closed."""
    camera = _scan_camera(token)
    manager = surveillance.get_manager()
    manager.flush(camera["id"])
    monitor = manager.get(camera["id"])
    status = monitor.status_payload() if monitor else {}
    return jsonify({"ok": True, "status": status.get("status")})


@bp.route("/<int:camera_id>/flush", methods=("POST",))
@auth.login_required
def flush(camera_id: int):
    """Authenticated flush for the signed-in phone capture page."""
    _get_or_404(camera_id)
    surveillance.get_manager().flush(camera_id)
    return jsonify({"ok": True})


# --- Device connection protocol (self-registering phone) ------------------

def _register_phone_device(payload: dict):
    """Return the phone camera for a connect token, creating it on first scan.

    The camera is created before the first frame arrives and its monitor is
    started immediately, so it shows up on Connected Devices and begins
    recording/snapshotting as soon as the phone's camera is running.
    """
    nonce = str(payload.get("nonce") or "")
    if not nonce:
        abort(403, description="This device pairing link is invalid.")
    key = f"imani_device_{nonce}"
    existing_id = session.get(key)
    if existing_id:
        existing = CameraRepository.get(int(existing_id))
        if existing is not None:
            return existing

    base = f"Phone {nonce[:6].upper()}"
    name = base
    suffix = 2
    while CameraRepository.get_by_name(name) is not None:
        name = f"{base}-{suffix}"
        suffix += 1
    camera_id = CameraRepository.create(
        {
            "name": name,
            "location": "Paired by QR",
            "source_type": "phone",
            "source_url": "phone",
            "enabled": True,
        },
        actor="QR pairing",
    )
    session[key] = camera_id
    AuditRepository.record(
        "DEVICE_CONNECT",
        "cameras",
        camera_id,
        f"Phone registered by QR scan: {name}",
        "QR pairing",
    )
    return CameraRepository.get(camera_id)


@bp.route("/connect")
@auth.login_required
def connect():
    """Operator page: show the QR a new phone scans to join IMANI."""
    token = qr.make_connect_token()
    return render_template(
        "cameras/connect.html",
        token=token,
        scan_url=qr.connect_device_url(token),
        max_age_minutes=max(
            1, int(current_app.config.get("CONNECT_TOKEN_MAX_AGE", 3600)) // 60
        ),
    )


@bp.route("/connect/qr.png")
@auth.login_required
def connect_qr_png():
    token = (request.args.get("token") or "").strip()
    if qr.load_connect_token(token) is None:
        abort(403, description="This device pairing link is invalid or has expired.")
    try:
        payload = qr.png(qr.connect_device_url(token))
    except Exception as exc:
        abort(503, description=f"QR generation is unavailable: {exc}")
    return Response(
        payload, mimetype="image/png",
        headers={"Cache-Control": "no-store"},
    )


@bp.route("/connect/<token>")
def connect_device(token: str):
    """Public landing page for a phone that scanned the device-connect QR."""
    payload = qr.load_connect_token(token)
    if payload is None:
        abort(
            403,
            description=(
                "This device pairing link is invalid or has expired. "
                "Ask the operator to show a fresh QR code."
            ),
        )
    camera = _register_phone_device(payload)
    try:
        surveillance.get_manager().start(current_app, camera, phone=True)
    except Exception:  # pragma: no cover - frames retry the start
        current_app.logger.exception(
            "Could not start phone monitor for new device %s", camera["id"]
        )
    camera_token = qr.make_token(camera["id"])
    return render_template(
        "cameras/scan.html", camera=camera, token=camera_token,
        secure_url=_secure_scan_url(camera_token),
    )


# --- Live view ------------------------------------------------------------

@bp.route("/<int:camera_id>/snapshot.jpg")
@auth.login_required
def snapshot(camera_id: int):
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
            if not monitor.is_running:
                break
        time.sleep(0.1)


@bp.route("/<int:camera_id>/stream")
@auth.login_required
def stream(camera_id: int):
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
    group_id = request.args.get("group_id") or None
    try:
        group_id = int(group_id) if group_id else None
    except Exception:
        group_id = None
    manager = surveillance.get_manager()
    payload = []
    for camera in CameraRepository.all():
        if group_id and _row_get(camera, "group_id") != group_id:
            continue
        live = manager.status(camera["id"]) or {}
        payload.append({
            "id": camera["id"],
            "name": camera["name"],
            "group_id": _row_get(camera, "group_id"),
            "source_type": camera["source_type"],
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


@bp.route("/live")
@auth.login_required
def live_connections():
    """Live view of all connected devices and their capture status."""
    from ..models import CameraRepository, CameraGroupRepository
    from .. import surveillance
    manager = surveillance.get_manager()
    cameras = []
    for cam in CameraRepository.all():
        decorated = {}
        status = manager.status(cam["id"]) or {}
        decorated.update(dict(cam))
        decorated["running"] = bool(status.get("running"))
        decorated["live"] = status.get("status") or cam["status"] or "stopped"
        decorated["error"] = status.get("error") or cam["last_error"]
        decorated["frames_seen"] = status.get("frames_seen", 0)
        decorated["captures"] = status.get("captures", 0)
        decorated["has_live_view"] = status.get("has_live_view", False)
        decorated["last_frame_at"] = status.get("last_frame_at") or cam["last_frame_at"]
        decorated["last_capture_at"] = status.get("last_capture_at") or cam["last_capture_at"]
        cameras.append(decorated)
    groups = CameraGroupRepository.all()
    return render_template("cameras/live_connections.html", cameras=cameras, groups=groups, source_labels=SOURCE_LABELS)
