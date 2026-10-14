"""Reporting: a console overview and the global activity log."""
from __future__ import annotations

import math

from flask import Blueprint, render_template, request

from .. import auth
from ..models import (
    AuditRepository,
    IndividualRepository,
    MatchRepository,
    UserRepository,
)
from ..reports import csv_response

bp = Blueprint("reports", __name__)

PAGE_SIZE = 50
ACTIVITY_EXPORT_COLUMNS = ("created_at", "action", "entity", "entity_id", "actor",
                           "details")


@bp.route("/reports")
@auth.role_required("analyst")
def overview():
    individuals = IndividualRepository.count_by_status()
    users = UserRepository.count_by_status()
    return render_template(
        "reports/overview.html",
        individuals=individuals,
        individuals_total=sum(individuals.values()),
        users=users,
        matches=MatchRepository.count(),
        scored=MatchRepository.scored_count(),
        bands=MatchRepository.band_distribution(),
        runs=MatchRepository.recent_runs(6),
        activity=AuditRepository.recent_actions(8),
    )


def _activity_filters() -> dict:
    return {
        "action": request.args.get("action", "").strip(),
        "entity": request.args.get("entity", "").strip(),
        "actor": request.args.get("actor", "").strip(),
        "q": request.args.get("q", "").strip(),
    }


@bp.route("/activity")
@auth.role_required("admin")
def activity():
    filters = _activity_filters()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    rows, total = AuditRepository.search(
        action=filters["action"] or None,
        entity=filters["entity"] or None,
        actor=filters["actor"] or None,
        q=filters["q"] or None,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    pages = max(1, math.ceil(total / PAGE_SIZE))
    return render_template(
        "reports/activity.html",
        events=rows, total=total, page=page, pages=pages,
        filters=filters,
        actions=AuditRepository.distinct_actions(),
        entities=AuditRepository.distinct_entities(),
    )


@bp.route("/activity/export.csv")
@auth.role_required("admin")
def activity_export():
    filters = _activity_filters()
    rows, _ = AuditRepository.search(
        action=filters["action"] or None,
        entity=filters["entity"] or None,
        actor=filters["actor"] or None,
        q=filters["q"] or None,
        limit=100000,
        offset=0,
    )
    data = [
        ["" if row[column] is None else row[column]
         for column in ACTIVITY_EXPORT_COLUMNS]
        for row in rows
    ]
    return csv_response("activity.csv", ACTIVITY_EXPORT_COLUMNS, data)
