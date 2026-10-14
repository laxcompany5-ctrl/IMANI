"""The matching engine: run a probe and review ranked candidates.

Running a match is an investigative action, so it requires the *analyst* role
(or above). Reading saved runs is open to any signed-in user.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import auth, matching
from ..models import AuditRepository, IndividualRepository, MatchRepository
from ..reports import csv_response, timestamp
from ..validation import BUILD_OPTIONS, GAIT_OPTIONS, POSTURE_OPTIONS

bp = Blueprint("match", __name__, url_prefix="/match")

ENTITY = "match_runs"
MAX_RESULTS = 25

PROBE_KEYS = ("height_cm", "build", "posture", "gait", "face")

# Trait columns included in the CSV export, in display order.
EXPORT_TRAITS = ("height_cm", "gait", "face", "build", "posture")


def _enum(value, options, default):
    value = (value or "").strip().lower()
    return value if value in options else default


def _int_or_none(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _probe_from_form(form):
    """Build a normalised probe plus a list of input problems."""
    errors: list[str] = []

    height = _int_or_none(form.get("height_cm"))
    if height is not None and not (50 <= height <= 260):
        errors.append("Enter a height between 50 and 260 cm.")

    face_text = (form.get("face_template") or "").strip()
    face, face_error = matching.parse_template(face_text)
    if face_error:
        errors.append(face_error)

    probe = {
        "height_cm": height,
        "build": _enum(form.get("build"), BUILD_OPTIONS, "") or None,
        "posture": _enum(form.get("posture"), POSTURE_OPTIONS, "") or None,
        "gait": _enum(form.get("gait"), GAIT_OPTIONS, "") or None,
        "face": face,
    }
    return probe, face_text, errors


def _form_values(form) -> dict:
    return {
        "title": form.get("title", ""),
        "height_cm": form.get("height_cm", ""),
        "build": form.get("build", ""),
        "posture": form.get("posture", ""),
        "gait": form.get("gait", ""),
        "face_template": form.get("face_template", ""),
        "min_score": form.get("min_score", "0"),
        "notes": form.get("notes", ""),
    }


def _render_form(values, status=200):
    return render_template(
        "match/form.html",
        values=values,
        builds=BUILD_OPTIONS,
        postures=POSTURE_OPTIONS,
        gaits=GAIT_OPTIONS,
    ), status


@bp.route("/", methods=("GET", "POST"))
@auth.role_required("analyst")
def run():
    if request.method == "POST":
        probe, face_text, errors = _probe_from_form(request.form)

        if not any(probe[key] for key in PROBE_KEYS):
            errors.append("Provide at least one trait to match on.")

        min_score = _float_or_none(request.form.get("min_score"))
        min_score = 0.0 if min_score is None else min_score
        if not (0 <= min_score <= 100):
            errors.append("Choose a minimum score between 0 and 100.")

        if errors:
            for message in errors:
                flash(message, "danger")
            return _render_form(_form_values(request.form), status=400)

        # Only active individuals take part in matching.
        people = IndividualRepository.all(status="active")
        results = matching.rank(probe, people, min_score=min_score,
                                limit=MAX_RESULTS)

        traits_used = sorted({trait for r in results for trait in r["traits_used"]})
        actor = auth.current_user()["username"]
        values = {
            "title": (request.form.get("title") or "").strip() or "Untitled probe",
            "probe_height_cm": probe["height_cm"],
            "probe_build": probe["build"],
            "probe_posture": probe["posture"],
            "probe_gait": probe["gait"],
            "probe_face": face_text or None,
            "min_score": min_score,
            "notes": (request.form.get("notes") or "").strip() or None,
            "traits_used": ", ".join(traits_used),
            "result_count": len(results),
        }
        run_id = MatchRepository.create_run(values, actor=actor)
        MatchRepository.add_results(run_id, results)
        AuditRepository.record(
            "MATCH", ENTITY, run_id,
            f"ran match '{values['title']}' — {len(results)} candidate(s)", actor,
        )
        flash(f"Match complete — {len(results)} candidate(s) ranked.", "success")
        return redirect(url_for("match.results", run_id=run_id))

    return _render_form(_form_values({}))


@bp.route("/history")
@auth.login_required
def history():
    return render_template("match/history.html", runs=MatchRepository.recent_runs())


@bp.route("/<int:run_id>")
@auth.login_required
def results(run_id: int):
    run_row = MatchRepository.get_run(run_id)
    if run_row is None:
        abort(404)
    return render_template(
        "match/results.html",
        run=run_row,
        results=MatchRepository.results_for(run_id),
        labels=matching.TRAIT_LABELS,
    )


@bp.route("/<int:run_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(run_id: int):
    run_row = MatchRepository.get_run(run_id)
    if run_row is None:
        abort(404)
    MatchRepository.delete_run(run_id)
    AuditRepository.record(
        "DELETE", ENTITY, run_id,
        f"deleted match '{run_row['reference']}'",
        auth.current_user()["username"],
    )
    flash("Match run deleted.", "success")
    return redirect(url_for("match.history"))


# --- Reporting & export ---------------------------------------------------

def _trait_percent(breakdown: dict, trait: str):
    if trait not in breakdown:
        return ""
    return round(breakdown[trait]["similarity"] * 100)


@bp.route("/<int:run_id>/export.csv")
@auth.login_required
def export_csv(run_id: int):
    run_row = MatchRepository.get_run(run_id)
    if run_row is None:
        abort(404)

    header = ["rank", "reference", "name", "score", "band"] + [
        matching.TRAIT_LABELS[trait] for trait in EXPORT_TRAITS
    ]
    rows = MatchRepository.results_for(run_id)
    data = [
        [
            rank, r["individual_reference"], r["individual_name"],
            r["score"], r["band"],
            *[_trait_percent(r["breakdown"], trait) for trait in EXPORT_TRAITS],
        ]
        for rank, r in enumerate(rows, start=1)
    ]
    return csv_response(f"match-{run_row['reference']}.csv", header, data)


@bp.route("/<int:run_id>/report")
@auth.login_required
def report(run_id: int):
    run_row = MatchRepository.get_run(run_id)
    if run_row is None:
        abort(404)
    results = MatchRepository.results_for(run_id)
    for result in results:
        result["summary"] = matching.breakdown_text(result["breakdown"])
    return render_template(
        "match/report.html",
        run=run_row,
        results=results,
        generated=timestamp(),
        back_url=url_for("match.results", run_id=run_id),
    )
