"""Signed-in landing page."""
from __future__ import annotations

from flask import Blueprint, render_template

from .. import auth

bp = Blueprint("dashboard", __name__)


@bp.route("/dashboard")
@auth.login_required
def index():
    from ..models import (
        IndividualRepository,
        MatchRepository,
        SightingRepository,
        UserRepository,
    )

    counts = UserRepository.count_by_status()
    return render_template(
        "dashboard.html",
        counts=counts,
        pending=counts.get("pending", 0),
        active=counts.get("active", 0),
        individuals=IndividualRepository.count(),
        matches=MatchRepository.count(),
        sightings=SightingRepository.count(),
        sightings_today=SightingRepository.count_today(),
        sighting_review=SightingRepository.review_count(),
    )
