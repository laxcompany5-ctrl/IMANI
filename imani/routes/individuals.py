"""The individual register — search, insert, update and delete.

Read access is open to every signed-in user; mutations require the *analyst*
role (or above). Every change is written to the audit trail.
"""
from __future__ import annotations

import math

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import auth
from ..models import AuditRepository, IndividualRepository
from ..validation import (
    BUILD_OPTIONS,
    GAIT_OPTIONS,
    GENDER_OPTIONS,
    INDIVIDUAL_STATUSES,
    POSTURE_OPTIONS,
    individual_problems,
)

bp = Blueprint("individuals", __name__, url_prefix="/individuals")

ENTITY = "individuals"
PAGE_SIZE = 20


def _enum(value: str | None, options: tuple[str, ...], default: str) -> str:
    value = (value or "").strip().lower()
    return value if value in options else default


def _int_or_none(value: str | None):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _collect(form) -> dict:
    """Normalise raw form input into a column → value mapping."""
    return {
        "full_name": (form.get("full_name") or "").strip(),
        "alias": (form.get("alias") or "").strip() or None,
        "date_of_birth": (form.get("date_of_birth") or "").strip() or None,
        "gender": _enum(form.get("gender"), GENDER_OPTIONS, "unspecified"),
        "national_id": (form.get("national_id") or "").strip() or None,
        "phone": (form.get("phone") or "").strip() or None,
        "email": (form.get("email") or "").strip() or None,
        "nationality": (form.get("nationality") or "").strip() or None,
        "address": (form.get("address") or "").strip() or None,
        "height_cm": _int_or_none(form.get("height_cm")),
        "build": _enum(form.get("build"), BUILD_OPTIONS, "average"),
        "posture": _enum(form.get("posture"), POSTURE_OPTIONS, "neutral"),
        "gait": _enum(form.get("gait"), GAIT_OPTIONS, "normal"),
        "distinguishing_marks": (form.get("distinguishing_marks") or "").strip() or None,
        "face_template": (form.get("face_template") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
        "status": _enum(form.get("status"), INDIVIDUAL_STATUSES, "active"),
    }


def _row_to_values(person) -> dict:
    """Adapt a database row into the same mapping ``_collect`` produces."""
    return {key: person[key] for key in person.keys()}


def _national_id_conflict(national_id: str | None, exclude_id: int | None = None):
    """Return an existing record that already uses ``national_id``, if any."""
    if not national_id:
        return None
    existing = IndividualRepository.get_by_national_id(national_id)
    if existing is not None and existing["id"] != exclude_id:
        return existing
    return None


def _get_or_404(individual_id: int):
    person = IndividualRepository.get(individual_id)
    if person is None:
        abort(404)
    return person


def _render_form(mode, values, action, individual_id=None, status=200):
    return render_template(
        "individuals/form.html",
        mode=mode,
        values=values,
        action=action,
        individual_id=individual_id,
        builds=BUILD_OPTIONS,
        postures=POSTURE_OPTIONS,
        gaits=GAIT_OPTIONS,
        genders=GENDER_OPTIONS,
        statuses=INDIVIDUAL_STATUSES,
    ), status


@bp.route("/")
@auth.login_required
def index():
    q = request.args.get("q", "").strip()
    status = _enum(request.args.get("status"), INDIVIDUAL_STATUSES, "") or None
    build = _enum(request.args.get("build"), BUILD_OPTIONS, "") or None
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    rows, total = IndividualRepository.search(
        q=q or None, status=status, build=build,
        limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )
    pages = max(1, math.ceil(total / PAGE_SIZE))
    return render_template(
        "individuals/list.html",
        individuals=rows, total=total, page=page, pages=pages,
        q=q, status=status or "", build=build or "",
        builds=BUILD_OPTIONS, statuses=INDIVIDUAL_STATUSES,
    )


@bp.route("/new", methods=("GET", "POST"))
@auth.role_required("analyst")
def create():
    if request.method == "POST":
        values = _collect(request.form)
        errors = individual_problems(values)
        if _national_id_conflict(values["national_id"]):
            errors.append("use a national ID that is not already on file")
        if errors:
            for message in errors:
                flash(f"Please {message}.", "danger")
            return _render_form("create", values, url_for("individuals.create"),
                                status=400)

        actor = auth.current_user()["username"]
        individual_id = IndividualRepository.create(values, actor=actor)
        AuditRepository.record(
            "INSERT", ENTITY, individual_id,
            f"registered '{values['full_name']}'", actor,
        )
        flash(f"Registered {values['full_name']}.", "success")
        return redirect(url_for("individuals.detail", individual_id=individual_id))

    return _render_form("create", _collect({}), url_for("individuals.create"))


@bp.route("/<int:individual_id>")
@auth.login_required
def detail(individual_id: int):
    person = _get_or_404(individual_id)
    activity = AuditRepository.for_entity(ENTITY, individual_id, limit=12)
    return render_template("individuals/detail.html", person=person,
                           activity=activity)


@bp.route("/<int:individual_id>/edit", methods=("GET", "POST"))
@auth.role_required("analyst")
def edit(individual_id: int):
    person = _get_or_404(individual_id)

    if request.method == "POST":
        values = _collect(request.form)
        errors = individual_problems(values)
        if _national_id_conflict(values["national_id"], exclude_id=individual_id):
            errors.append("use a national ID that is not already on file")
        if errors:
            for message in errors:
                flash(f"Please {message}.", "danger")
            return _render_form("edit", values,
                                url_for("individuals.edit", individual_id=individual_id),
                                individual_id=individual_id, status=400)

        actor = auth.current_user()["username"]
        IndividualRepository.update(individual_id, values, actor=actor)
        AuditRepository.record(
            "UPDATE", ENTITY, individual_id,
            f"updated '{values['full_name']}'", actor,
        )
        flash("Changes saved.", "success")
        return redirect(url_for("individuals.detail", individual_id=individual_id))

    return _render_form("edit", _row_to_values(person),
                        url_for("individuals.edit", individual_id=individual_id),
                        individual_id=individual_id)


@bp.route("/<int:individual_id>/delete", methods=("POST",))
@auth.role_required("analyst")
def delete(individual_id: int):
    person = _get_or_404(individual_id)
    actor = auth.current_user()["username"]
    IndividualRepository.delete(individual_id)
    AuditRepository.record(
        "DELETE", ENTITY, individual_id,
        f"deleted '{person['full_name']}' ({person['reference']})", actor,
    )
    flash(f"Deleted {person['full_name']}.", "success")
    return redirect(url_for("individuals.index"))
