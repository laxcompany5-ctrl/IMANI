"""Auto-captured snapshots from CCTV, cameras and phones (Bit 12b)."""
from __future__ import annotations

import os
from pathlib import Path

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
    send_file,
    url_for,
)

from .. import auth, media, surveillance
from ..models import AuditRepository, CameraRepository, CameraSnapshotRepository

bp = Blueprint("snapshots", __name__, url_prefix="/snapshots")

MUTATION_ROLES = ("analyst", "admin")


def _get_or_404(snapshot_id: int):
    snap = CameraSnapshotRepository.get(snapshot_id)
    if snap is None:
        abort(404)
    return snap


@bp.route("/")
@auth.login_required
def index():
    page = max(1, request.args.get("page", type=int) or 1)
    camera_id = request.args.get("camera_id", type=int)
    per_page = 30
    snaps = CameraSnapshotRepository.all(limit=per_page, offset=(page - 1) * per_page, camera_id=camera_id)
    total = CameraSnapshotRepository.count(camera_id=camera_id)
    pages = max(1, (total + per_page - 1) // per_page)
    cameras = CameraRepository.all()
    return render_template(
        "snapshots/index.html",
        snapshots=snaps,
        cameras=cameras,
        camera_id=camera_id,
        page=page,
        pages=pages,
        total=total,
    )


@bp.route("/<int:snapshot_id>/image")
@auth.login_required
def image(snapshot_id: int):
    snap = _get_or_404(snapshot_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        path = (base / snap["path"]).resolve()
        if base.resolve() not in path.parents and base.resolve() != path.parent:
            abort(404)
        return send_file(str(path), mimetype="image/jpeg")
    except Exception:
        abort(404)


@bp.route("/capture/<int:camera_id>", methods=("POST",))
@auth.role_required("analyst")
def capture_now(camera_id: int):
    camera = CameraRepository.get(camera_id)
    if camera is None:
        abort(404)
    manager = surveillance.get_manager()
    mon = manager.get(camera_id)
    jpeg = None
    if mon:
        jpeg = mon.latest_jpeg()
    if jpeg is None:
        flash("No frame available from this camera yet. Start monitoring or wait for frames.", "warning")
        return redirect(url_for("cameras.detail", camera_id=camera_id))
    result = media.save_snapshot(camera["name"], jpeg, camera_id=camera_id, source_type=camera["source_type"])
    if result:
        snap_id = CameraSnapshotRepository.create({
            "camera_id": camera_id,
            "camera_name": camera["name"],
            "filename": result["filename"],
            "path": result["path"],
            "source_type": camera["source_type"],
            "captured_at": __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "features_json": result.get("features_json"),
        })
        AuditRepository.record("CAPTURE", "camera_snapshots", snap_id,
                               f"Auto-captured snapshot from {camera['name']}",
                               auth.current_user()["username"])
        flash("Snapshot captured and saved.", "success")
    else:
        flash("Failed to save snapshot.", "danger")
    return redirect(url_for("cameras.detail", camera_id=camera_id))


@bp.route("/capture-all", methods=("POST",))
@auth.role_required("analyst")
def capture_all():
    manager = surveillance.get_manager()
    count = 0
    for camera in CameraRepository.all():
        mon = manager.get(camera["id"])
        if mon:
            jpeg = mon.latest_jpeg()
            if jpeg:
                result = media.save_snapshot(camera["name"], jpeg, camera_id=camera["id"], source_type=camera["source_type"])
                if result:
                    CameraSnapshotRepository.create({
                        "camera_id": camera["id"],
                        "camera_name": camera["name"],
                        "filename": result["filename"],
                        "path": result["path"],
                        "source_type": camera["source_type"],
                        "captured_at": __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "features_json": result.get("features_json"),
                    })
                    count += 1
    AuditRepository.record("CAPTURE", "camera_snapshots", None, f"Captured snapshots from {count} camera(s)", auth.current_user()["username"])
    flash(f"Captured {count} snapshot(s) from active cameras.", "success")
    return redirect(url_for("snapshots.index"))


@bp.route("/<int:snapshot_id>/describe", methods=("POST",))
@auth.login_required
def describe(snapshot_id: int):
    """Attach a written description to a saved photo for later AI search."""
    _get_or_404(snapshot_id)
    description = (request.form.get("description") or "").strip()
    CameraSnapshotRepository.update(snapshot_id, description=description or None)
    AuditRepository.record(
        "ANNOTATE", "camera_snapshots", snapshot_id,
        f"Described snapshot: {description[:120]}",
        auth.current_user()["username"],
    )
    flash("Description saved. It will be used by AI search.", "success")
    return redirect(request.referrer or url_for("snapshots.index"))


@bp.route("/<int:snapshot_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(snapshot_id: int):
    snap = _get_or_404(snapshot_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        path = (base / snap["path"])
        if path.exists():
            path.unlink()
    except Exception:
        pass
    CameraSnapshotRepository.delete(snapshot_id)
    flash("Snapshot deleted.", "success")
    return redirect(url_for("snapshots.index"))
