"""The capture archive — all CCTV footage, kept for later review (Bit 11).

Every capture a camera makes is stored with the footage around it: a still and
a short clip. This console is the long-term store — browse everything that was
recorded, filter by camera or date, and watch a capture back with its timestamp
instead of relying on the moment it happened.

Footage is written to ``MEDIA_ROOT`` by :mod:`imani.surveillance`; this module
only reads it back. Retention is enforced by the admin-only prune action.
"""
from __future__ import annotations

import json
import math

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

from .. import auth, media
from ..models import AuditRepository, IndividualRepository, MediaRepository, SightingRepository

bp = Blueprint("archive", __name__, url_prefix="/archive")

PAGE_SIZE = 24
STATUSES = ("identified", "review", "new")


def _get_or_404(sighting_id: int):
    sighting = SightingRepository.get(sighting_id)
    if sighting is None:
        abort(404)
    return sighting


def _breakdown(sighting) -> dict:
    raw = sighting["breakdown"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


@bp.route("/")
@auth.login_required
def index():
    camera = request.args.get("camera") or None
    status = request.args.get("status") or None
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None
    query = request.args.get("q") or None
    page = max(1, request.args.get("page", type=int) or 1)

    rows, total = MediaRepository.archive(
        camera=camera, status=status, date_from=date_from, date_to=date_to,
        query=query, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )
    pages = max(1, math.ceil(total / PAGE_SIZE))
    return render_template(
        "archive/list.html",
        captures=rows, total=total, page=page, pages=pages,
        camera=camera or "", status=status or "", date_from=date_from or "",
        date_to=date_to or "", query=query or "",
        cameras=SightingRepository.distinct_cameras(), statuses=STATUSES,
        usage=media.usage(), media_count=MediaRepository.count(),
        retention_days=current_app.config["MEDIA_RETENTION_DAYS"],
    )


@bp.route("/<int:sighting_id>")
@auth.login_required
def detail(sighting_id: int):
    sighting = _get_or_404(sighting_id)
    person = (IndividualRepository.get(sighting["individual_id"])
              if sighting["individual_id"] else None)
    assets = MediaRepository.for_sighting(sighting_id)
    clip = MediaRepository.clip_for(sighting_id)
    poster = MediaRepository.poster_for(sighting_id)
    return render_template(
        "archive/detail.html",
        sighting=sighting, person=person, assets=assets,
        clip=clip, poster=poster, breakdown=_breakdown(sighting),
    )


@bp.route("/<int:sighting_id>/frame")
@auth.login_required
def frame(sighting_id: int):
    """The still frame for a capture."""
    _get_or_404(sighting_id)
    poster = MediaRepository.poster_for(sighting_id)
    if poster is None:
        abort(404)
    try:
        data = media.read(poster["path"])
    except (ValueError, OSError):
        abort(404)
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@bp.route("/<int:sighting_id>/clip.json")
@auth.login_required
def clip_manifest(sighting_id: int):
    """The clip's frame list, for the in-browser replay."""
    _get_or_404(sighting_id)
    clip = MediaRepository.clip_for(sighting_id)
    manifest = media.manifest(clip["path"]) if clip else None
    if not manifest:
        abort(404)
    return jsonify({
        "fps": manifest.get("fps") or 5,
        "count": manifest.get("count") or 0,
        "duration_ms": manifest.get("duration_ms"),
        "frames": [
            {
                "index": entry["index"],
                "url": url_for("archive.clip_frame", sighting_id=sighting_id,
                               index=entry["index"]),
            }
            for entry in manifest.get("frames", [])
        ],
    })


@bp.route("/<int:sighting_id>/clip/<int:index>")
@auth.login_required
def clip_frame(sighting_id: int, index: int):
    """One frame of a stored clip."""
    _get_or_404(sighting_id)
    clip = MediaRepository.clip_for(sighting_id)
    manifest = media.manifest(clip["path"]) if clip else None
    if not manifest:
        abort(404)
    entry = next(
        (item for item in manifest.get("frames", []) if item["index"] == index),
        None,
    )
    if entry is None:
        abort(404)
    try:
        data = media.read(f"{clip['path']}/{entry['file']}")
    except (ValueError, OSError):
        abort(404)
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@bp.route("/prune", methods=("POST",))
@auth.role_required("admin")
def prune():
    result = media.prune()
    if result.get("kept"):
        flash("Retention is set to keep footage forever, so nothing was removed.", "info")
    else:
        freed = result["bytes"] / 1024 / 1024
        AuditRepository.record(
            "PRUNE", "capture_media", None,
            f"removed {result['deleted']} assets ({freed:.1f} MB)",
            auth.current_user()["username"],
        )
        flash(
            f"Removed {result['deleted']} old assets ({freed:.1f} MB).",
            "success",
        )
    return redirect(url_for("archive.index"))
