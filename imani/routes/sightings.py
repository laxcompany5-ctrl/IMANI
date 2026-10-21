"""The sightings console — review what the cameras captured (Bit 8).

Every capture is stored and re-identified automatically by :mod:`imani.capture`.
These pages let a human see the feed, confirm tentative matches, re-point a
capture at the right person, or split one off into a new record.
"""
from __future__ import annotations

import json
import math

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import auth, capture, matching, media
from ..models import AuditRepository, IndividualRepository, ProfileRepository, SightingRepository
from ..validation import BUILD_OPTIONS, GAIT_OPTIONS, POSTURE_OPTIONS

bp = Blueprint("sightings", __name__, url_prefix="/sightings")

ENTITY = "sightings"
PAGE_SIZE = 20
STATUSES = ("identified", "review", "new")


def _get_or_404(sighting_id: int):
    sighting = SightingRepository.get(sighting_id)
    if sighting is None:
        abort(404)
    return sighting


def _breakdown(sighting) -> dict:
    return json.loads(sighting["breakdown"] or "{}")


@bp.route("/")
@auth.login_required
def index():
    camera = request.args.get("camera", "").strip() or None
    status = request.args.get("status", "").strip()
    status = status if status in STATUSES else None
    individual_id = request.args.get("individual", type=int)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    rows, total = SightingRepository.search(
        camera=camera, status=status, individual_id=individual_id,
        limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )
    pages = max(1, math.ceil(total / PAGE_SIZE))
    individual = IndividualRepository.get(individual_id) if individual_id else None
    return render_template(
        "sightings/list.html",
        sightings=rows, total=total, page=page, pages=pages,
        camera=camera or "", status=status or "",
        individual_id=individual_id, individual=individual,
        cameras=SightingRepository.distinct_cameras(),
        statuses=STATUSES,
        review_count=SightingRepository.review_count(),
    )


@bp.route("/review")
@auth.role_required("analyst")
def review():
    queue = SightingRepository.review_queue(limit=100)
    return render_template("sightings/review.html", sightings=queue)


@bp.route("/simulate", methods=("GET", "POST"))
@auth.role_required("analyst")
def simulate():
    """Fire a synthetic capture — stands in for a camera during setup/demo."""
    if request.method == "POST":
        payload = {
            "camera": request.form.get("camera"),
            "captured_at": request.form.get("captured_at"),
            "height_cm": request.form.get("height_cm"),
            "build": request.form.get("build"),
            "posture": request.form.get("posture"),
            "gait": request.form.get("gait"),
            "face_template": request.form.get("face_template"),
            "image_ref": request.form.get("image_ref"),
            "notes": request.form.get("notes"),
        }
        try:
            result = capture.ingest(
                payload, actor=auth.current_user()["username"],
            )
        except capture.CaptureError as exc:
            for problem in exc.problems:
                flash(problem, "danger")
            return render_template(
                "sightings/simulate.html", values=payload,
                builds=BUILD_OPTIONS, postures=POSTURE_OPTIONS,
                gaits=GAIT_OPTIONS,
            ), 400

        flash(f"Capture {result['reference']} — {result['status']}.", "success")
        return redirect(url_for("sightings.detail",
                                sighting_id=result["sighting_id"]))

    return render_template(
        "sightings/simulate.html", values={},
        builds=BUILD_OPTIONS, postures=POSTURE_OPTIONS, gaits=GAIT_OPTIONS,
    )


@bp.route("/<int:sighting_id>")
@auth.login_required
def detail(sighting_id: int):
    sighting = _get_or_404(sighting_id)
    person = (IndividualRepository.get(sighting["individual_id"])
              if sighting["individual_id"] else None)
    profile = ProfileRepository.get(person["id"]) if person else None
    people = IndividualRepository.all(status="active")
    return render_template(
        "sightings/detail.html",
        sighting=sighting, person=person, profile=profile, people=people,
        breakdown=_breakdown(sighting), labels=matching.TRAIT_LABELS,
        traits=("height_cm", "gait", "face", "build", "posture"),
    )


@bp.route("/<int:sighting_id>/confirm", methods=("POST",))
@auth.role_required("analyst")
def confirm(sighting_id: int):
    sighting = _get_or_404(sighting_id)
    capture.confirm(sighting, auth.current_user()["username"])
    flash("Capture confirmed.", "success")
    return redirect(url_for("sightings.detail", sighting_id=sighting_id))


@bp.route("/<int:sighting_id>/reassign", methods=("POST",))
@auth.role_required("analyst")
def reassign(sighting_id: int):
    sighting = _get_or_404(sighting_id)
    target = request.form.get("individual_id", type=int)
    if not target or IndividualRepository.get(target) is None:
        flash("Choose a person to attach this capture to.", "danger")
        return redirect(url_for("sightings.detail", sighting_id=sighting_id))

    capture.reassign(sighting, target, auth.current_user()["username"])
    flash("Capture re-assigned.", "success")
    return redirect(url_for("sightings.detail", sighting_id=sighting_id))


@bp.route("/<int:sighting_id>/enroll", methods=("POST",))
@auth.role_required("analyst")
def enroll(sighting_id: int):
    sighting = _get_or_404(sighting_id)
    if not capture.has_traits(capture.traits_from_sighting(sighting)):
        flash("This capture carries no traits to enrol.", "danger")
        return redirect(url_for("sightings.detail", sighting_id=sighting_id))

    person_id = capture.enroll_from_sighting(
        sighting, auth.current_user()["username"],
    )
    flash("Enrolled as a new person.", "success")
    return redirect(url_for("individuals.detail", individual_id=person_id))


@bp.route("/<int:sighting_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(sighting_id: int):
    sighting = _get_or_404(sighting_id)
    media.delete_for_sighting(sighting_id)
    SightingRepository.delete(sighting_id)
    AuditRepository.record(
        "DELETE", ENTITY, sighting_id,
        f"deleted capture {sighting['reference']}",
        auth.current_user()["username"],
    )
    flash("Capture deleted.", "success")
    return redirect(url_for("sightings.index"))
