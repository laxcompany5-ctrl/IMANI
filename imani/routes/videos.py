"""Auto-saved video recordings from CCTV, cameras and phones for reference (Bit 12c)."""
from __future__ import annotations

import json
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
from ..models import (
    AuditRepository,
    CameraRepository,
    CameraVideoRepository,
    VideoImportRepository,
)

bp = Blueprint("videos", __name__, url_prefix="/videos")

MUTATION_ROLES = ("analyst", "admin")


def _get_or_404(video_id: int):
    vid = CameraVideoRepository.get(video_id)
    if vid is None:
        abort(404)
    return vid


@bp.route("/")
@auth.login_required
def index():
    page = max(1, request.args.get("page", type=int) or 1)
    camera_id = request.args.get("camera_id", type=int)
    kind = request.args.get("kind") or None
    if kind not in (None, "manual", "auto", "audio", "import"):
        kind = None
    per_page = 24
    videos = CameraVideoRepository.all(
        limit=per_page, offset=(page - 1) * per_page,
        camera_id=camera_id, kind=kind,
    )
    total = CameraVideoRepository.count(camera_id=camera_id, kind=kind)
    pages = max(1, (total + per_page - 1) // per_page)
    cameras = CameraRepository.all()
    return render_template(
        "videos/index.html",
        videos=videos,
        cameras=cameras,
        camera_id=camera_id,
        kind=kind,
        page=page,
        pages=pages,
        total=total,
    )


@bp.route("/<int:video_id>")
@auth.login_required
def detail(video_id: int):
    vid = _get_or_404(video_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        manifest_path = (base / vid["path"] / "manifest.json")
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    except Exception:
        manifest = None
    return render_template(
        "videos/detail.html", video=vid, manifest=manifest,
        is_import=(vid["kind"] == "import"),
    )


@bp.route("/<int:video_id>/source")
@auth.login_required
def source(video_id: int):
    """Stream the original file behind an imported video."""
    vid = _get_or_404(video_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        directory = (base / vid["path"]).resolve()
        path = (directory / (vid["filename"] or "video.mp4")).resolve()
        if base.resolve() not in path.parents or not path.is_file():
            abort(404)
        return send_file(
            str(path),
            mimetype=vid["mime_type"] or "video/mp4",
            conditional=True,
        )
    except Exception:
        abort(404)


@bp.route("/<int:video_id>/poster.jpg")
@auth.login_required
def poster(video_id: int):
    vid = _get_or_404(video_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        path = (base / vid["path"] / "poster.jpg")
        return send_file(str(path), mimetype="image/jpeg")
    except Exception:
        abort(404)


@bp.route("/<int:video_id>/audio")
@auth.login_required
def audio(video_id: int):
    """Stream an audio recording captured by a phone microphone."""
    vid = _get_or_404(video_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        path = (base / vid["path"]).resolve()
        if base.resolve() not in path.parents:
            abort(404)
        return send_file(
            str(path),
            mimetype=vid["mime_type"] or "audio/webm",
            conditional=True,
        )
    except Exception:
        abort(404)


@bp.route("/<int:video_id>/describe", methods=("POST",))
@auth.login_required
def describe(video_id: int):
    """Attach a written description to a recording for AI search."""
    _get_or_404(video_id)
    description = (request.form.get("description") or "").strip()
    CameraVideoRepository.update(video_id, description=description or None)
    AuditRepository.record(
        "ANNOTATE", "camera_videos", video_id,
        f"Described recording: {description[:120]}",
        auth.current_user()["username"],
    )
    flash("Description saved. It will be used by AI search.", "success")
    return redirect(request.referrer or url_for("videos.detail", video_id=video_id))


@bp.route("/<int:video_id>/frame/<int:index>.jpg")
@auth.login_required
def frame(video_id: int, index: int):
    vid = _get_or_404(video_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        path = (base / vid["path"] / f"frame-{index:04d}.jpg")
        return send_file(str(path), mimetype="image/jpeg")
    except Exception:
        abort(404)


@bp.route("/<int:video_id>/manifest.json")
@auth.login_required
def manifest(video_id: int):
    vid = _get_or_404(video_id)
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        manifest_path = (base / vid["path"] / "manifest.json")
        data = json.loads(manifest_path.read_text())
        # Rewrite frame URLs
        for f in data.get("frames", []):
            f["url"] = url_for("videos.frame", video_id=video_id, index=f["index"])
        return jsonify(data)
    except Exception:
        abort(404)


@bp.route("/record/<int:camera_id>", methods=("POST",))
@auth.role_required("analyst")
def record_now(camera_id: int):
    camera = CameraRepository.get(camera_id)
    if camera is None:
        abort(404)
    manager = surveillance.get_manager()
    mon = manager.get(camera_id)
    if mon is None or not mon.is_running:
        flash("Start monitoring the camera first to record video.", "warning")
        return redirect(url_for("cameras.detail", camera_id=camera_id))
    # Get frames from buffer
    frames = list(mon._buffer)
    if not frames:
        flash("No frames available in buffer yet. Wait a moment and try again.", "warning")
        return redirect(url_for("cameras.detail", camera_id=camera_id))
    result = media.save_video_recording(camera["name"], frames, camera_id=camera_id, source_type=camera["source_type"], started_at=__import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if result:
        vid_id = CameraVideoRepository.create({
            "camera_id": camera_id,
            "camera_name": camera["name"],
            "filename": result["filename"],
            "path": result["path"],
            "source_type": camera["source_type"],
            "duration_sec": result["duration_sec"],
            "frame_count": result["frame_count"],
            "size_bytes": result["size_bytes"],
            "started_at": result["manifest"].get("started_at"),
            "ended_at": result["manifest"].get("ended_at"),
            "captured_at": __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "manual",
            "mime_type": "image/jpeg",
            "features_json": result.get("features_json"),
        })
        AuditRepository.record("RECORD", "camera_videos", vid_id,
                               f"Saved video recording from {camera['name']} ({result['frame_count']} frames)",
                               auth.current_user()["username"])
        flash(f"Video recording saved ({result['frame_count']} frames, {result['duration_sec']}s).", "success")
    else:
        flash("Failed to save video recording.", "danger")
    return redirect(url_for("cameras.detail", camera_id=camera_id))


@bp.route("/record-all", methods=("POST",))
@auth.role_required("analyst")
def record_all():
    manager = surveillance.get_manager()
    count = 0
    for camera in CameraRepository.all():
        mon = manager.get(camera["id"])
        if mon and mon.is_running:
            frames = list(mon._buffer)
            if frames:
                result = media.save_video_recording(camera["name"], frames, camera_id=camera["id"], source_type=camera["source_type"])
                if result:
                    CameraVideoRepository.create({
                        "camera_id": camera["id"],
                        "camera_name": camera["name"],
                        "filename": result["filename"],
                        "path": result["path"],
                        "source_type": camera["source_type"],
                        "duration_sec": result["duration_sec"],
                        "frame_count": result["frame_count"],
                        "size_bytes": result["size_bytes"],
                        "started_at": result["manifest"].get("started_at"),
                        "ended_at": result["manifest"].get("ended_at"),
                        "captured_at": __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": "manual",
                        "mime_type": "image/jpeg",
                        "features_json": result.get("features_json"),
                    })
                    count += 1
    AuditRepository.record("RECORD", "camera_videos", None, f"Saved {count} video recording(s)", auth.current_user()["username"])
    flash(f"Saved {count} video recording(s) from active cameras.", "success")
    return redirect(url_for("videos.index"))


@bp.route("/<int:video_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(video_id: int):
    vid = _get_or_404(video_id)
    if vid["kind"] == "import":
        # Removing an imported video also removes its import record and the
        # snapshots that were published with it.
        from .. import video_import

        linked = VideoImportRepository.find_by_library_video(video_id)
        if linked:
            video_import.delete_import(linked["id"])
        else:
            CameraVideoRepository.delete(video_id)
        AuditRepository.record(
            "DELETE", "camera_videos", video_id,
            f"Deleted imported video: {vid['camera_name']}",
            auth.current_user()["username"],
        )
        flash("Imported video, its source file and its library snapshots were deleted.", "success")
        return redirect(url_for("videos.index"))
    try:
        base = Path(current_app.config["MEDIA_ROOT"]).parent
        import shutil
        path = (base / vid["path"])
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception:
        pass
    CameraVideoRepository.delete(video_id)
    flash("Video recording deleted.", "success")
    return redirect(url_for("videos.index"))
