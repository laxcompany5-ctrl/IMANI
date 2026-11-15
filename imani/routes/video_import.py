"""Video URL import and 4K snapshot extraction (Bit 14)."""
from __future__ import annotations

import shutil
from pathlib import Path

from flask import (
    Blueprint,
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

from .. import auth, video_import
from ..models import (
    AuditRepository,
    VideoImportRepository,
    VideoImportSnapshotRepository,
)

bp = Blueprint("video_import", __name__, url_prefix="/video-import")

MUTATION_ROLES = ("analyst", "admin")
MODES = ("auto", "scene", "interval", "people")


def _get_or_404(import_id: int):
    item = VideoImportRepository.get(import_id)
    if item is None:
        abort(404)
    return item


def _safe_file(rel_path: str | None) -> Path:
    if not rel_path:
        abort(404)
    try:
        path = video_import.resolve(rel_path)
    except (ValueError, OSError):
        abort(404)
    if not path.is_file():
        abort(404)
    return path


@bp.route("/")
@auth.login_required
def index():
    page = max(1, request.args.get("page", type=int) or 1)
    per_page = 20
    imports = VideoImportRepository.all(
        limit=per_page, offset=(page - 1) * per_page
    )
    total = VideoImportRepository.count()
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "video_import/index.html",
        imports=imports,
        page=page,
        pages=pages,
        total=total,
        modes=MODES,
        ffmpeg_ready=bool(video_import.ffmpeg_exe()),
        opencv_ready=video_import.opencv_available(),
        max_mb=current_app.config.get("VIDEO_IMPORT_MAX_BYTES", 0)
        // (1024 * 1024),
    )


@bp.route("/start", methods=("POST",))
@auth.role_required("analyst")
def start():
    source_url = (request.form.get("source_url") or "").strip()
    mode = (request.form.get("mode") or "auto").strip().lower()
    interval = request.form.get("interval", type=float) or 2.0
    max_snapshots = request.form.get("max_snapshots", type=int) or 24

    if not source_url:
        flash("Paste a video URL first.", "warning")
        return redirect(url_for("video_import.index"))
    if len(source_url) > 2000:
        flash("That URL is too long.", "warning")
        return redirect(url_for("video_import.index"))
    if not source_url.lower().startswith(("http://", "https://")):
        flash("Only http:// and https:// URLs are supported.", "warning")
        return redirect(url_for("video_import.index"))
    if mode not in MODES:
        mode = "auto"
    interval = max(0.5, min(float(interval), 60.0))
    max_snapshots = max(1, min(int(max_snapshots), 200))
    if not video_import.ffmpeg_exe():
        flash(
            "ffmpeg is not available. Install imageio-ffmpeg "
            "(pip install imageio-ffmpeg) and try again.",
            "danger",
        )
        return redirect(url_for("video_import.index"))

    app = current_app._get_current_object()
    import_id = video_import.start_import(
        app,
        {
            "source_url": source_url,
            "mode": mode,
            "interval_sec": interval,
            "max_snapshots": max_snapshots,
            "output_width": app.config.get("VIDEO_IMPORT_4K_WIDTH", 3840),
            "output_height": app.config.get("VIDEO_IMPORT_4K_HEIGHT", 2160),
            "created_by": auth.current_user()["username"],
        },
    )
    AuditRepository.record(
        "VIDEO_IMPORT",
        "video_imports",
        import_id,
        f"Queued video import: {source_url[:180]}",
        auth.current_user()["username"],
    )
    flash("Video import queued. IMANI will download it and extract 4K snapshots.", "success")
    return redirect(url_for("video_import.detail", import_id=import_id))


@bp.route("/<int:import_id>")
@auth.login_required
def detail(import_id: int):
    item = _get_or_404(import_id)
    snapshots = VideoImportSnapshotRepository.all(import_id)
    return render_template(
        "video_import/detail.html",
        item=item,
        snapshots=snapshots,
        is_running=item["status"] in ("queued", "running"),
    )


@bp.route("/<int:import_id>/status.json")
@auth.login_required
def status(import_id: int):
    item = _get_or_404(import_id)
    return jsonify(
        {
            "id": item["id"],
            "status": item["status"],
            "stage": item["stage"],
            "progress": item["progress"],
            "error": item["error"],
            "candidate_count": item["candidate_count"],
            "snapshot_count": item["snapshot_count"],
            "duration_sec": item["duration_sec"],
            "width": item["width"],
            "height": item["height"],
            "fps": item["fps"],
        }
    )


@bp.route("/<int:import_id>/video")
@auth.login_required
def video_file(import_id: int):
    item = _get_or_404(import_id)
    path = _safe_file(item["video_path"])
    download = request.args.get("download") == "1"
    name = f"imani-import-{import_id}-source{path.suffix or '.mp4'}"
    return send_file(
        str(path),
        mimetype=item["mime_type"] or "video/mp4",
        conditional=True,
        as_attachment=download,
        download_name=name,
    )


@bp.route("/<int:import_id>/snapshot/<int:snapshot_id>/<variant>")
@auth.login_required
def snapshot_file(import_id: int, snapshot_id: int, variant: str):
    item = _get_or_404(import_id)
    snap = VideoImportSnapshotRepository.get(snapshot_id)
    if snap is None or snap["import_id"] != item["id"]:
        abort(404)
    paths = {
        "thumb": snap["thumb_path"],
        "original": snap["original_path"],
        "4k": snap["upscaled_path"],
        "upscaled": snap["upscaled_path"],
    }
    if variant not in paths:
        abort(404)
    path = _safe_file(paths[variant])
    download = request.args.get("download") == "1"
    name = f"imani-import-{import_id}-snapshot-{snapshot_id}"
    if variant in ("4k", "upscaled"):
        name += "-4k"
    return send_file(
        str(path),
        mimetype="image/jpeg",
        conditional=True,
        as_attachment=download,
        download_name=f"{name}.jpg",
    )


@bp.route("/<int:import_id>/snapshots-4k.zip")
@auth.login_required
def snapshots_zip(import_id: int):
    _get_or_404(import_id)
    try:
        path = video_import.ensure_zip(import_id)
    except video_import.VideoImportError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("video_import.detail", import_id=import_id))
    return send_file(
        str(path),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"imani-import-{import_id}-4k-snapshots.zip",
    )


@bp.route("/<int:import_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(import_id: int):
    item = _get_or_404(import_id)
    if item["status"] in ("queued", "running"):
        flash("This import is still running. Wait for it to finish before deleting it.", "warning")
        return redirect(url_for("video_import.detail", import_id=import_id))
    base = video_import.root() / str(item["id"])
    VideoImportRepository.delete(import_id)
    try:
        if base.is_dir():
            shutil.rmtree(base)
    except OSError:
        pass
    AuditRepository.record(
        "VIDEO_IMPORT_DELETE",
        "video_imports",
        import_id,
        f"Deleted video import: {item['source_url'][:180]}",
        auth.current_user()["username"],
    )
    flash("Video import and its snapshots were deleted.", "success")
    return redirect(url_for("video_import.index"))
