"""Prompt-to-identity retrieval for IMANI searches.

A written prompt is parsed into colours, physical traits and free-text
keywords. Media rows are scored from their measured colour/keyword features
(:mod:`imani.features`); sightings are scored with the same weighted trait
engine used by the manual matcher (:mod:`imani.matching`). Both result sets are
merged into one ranked list so the analyst sees the single best matching piece
of evidence — a photo, a recording, or a known identity.

This is deterministic and explainable: no external model or random score.
"""
from __future__ import annotations

from flask import url_for

from . import features, matching
from .models import SightingRepository

TRAIT_WEIGHT = 0.85
KEYWORD_WEIGHT = 0.15
MIN_KEYWORD = 0.25
MIN_SCORE = 25.0


def _sighting_meta(row) -> str:
    traits = " ".join(
        str(row[key]) for key in ("height_cm", "build", "posture", "gait")
        if row[key] is not None
    )
    return " ".join(filter(None, [
        row["camera"] or "",
        row["individual_name"] or "",
        row["individual_reference"] or "",
        row["reference"] or "",
        row["notes"] or "",
        traits,
    ]))


def identity_score(query: str, row) -> tuple[float, dict] | None:
    """Score one sighting against a prompt. ``None`` means no real evidence."""
    probe = features.traits_in(query)
    keyword = features.keyword_score(query, _sighting_meta(row))
    trait_result = matching.score(probe, row) if probe else None

    if trait_result:
        score = (TRAIT_WEIGHT * trait_result["score"]
                 + KEYWORD_WEIGHT * keyword * 100.0)
    else:
        if keyword < MIN_KEYWORD:
            return None
        score = keyword * 100.0

    if score < MIN_SCORE and keyword < MIN_KEYWORD:
        return None

    detail = {
        "mode": "identity+traits" if trait_result else "identity+keywords",
        "traits": probe,
        "trait_score": trait_result["score"] if trait_result else None,
        "band": trait_result["band"] if trait_result else None,
        "traits_used": trait_result["traits_used"] if trait_result else [],
        "keywords": round(keyword, 3),
        "percentage": round(score, 1),
    }
    return round(score, 1), detail


def _candidate_identity(row, score: float, detail: dict) -> dict:
    individual_id = row["individual_id"] if "individual_id" in row.keys() else None
    return {
        "type": "identity",
        "id": row["id"],
        "camera_name": row["camera"] or "Unknown camera",
        "timestamp": row["captured_at"],
        "kind": "identity",
        "description": row["notes"] if "notes" in row.keys() else None,
        "features": None,
        "thumb_url": url_for("archive.frame", sighting_id=row["id"]),
        "view_url": url_for("sightings.detail", sighting_id=row["id"]),
        "is_identity": True,
        "score": score,
        "detail": detail,
        "swatches": [],
        "identity": {
            "name": row["individual_name"] if "individual_name" in row.keys() else None,
            "reference": (row["individual_reference"]
                          if "individual_reference" in row.keys() else None),
            "sighting_reference": row["reference"] if "reference" in row.keys() else None,
            "individual_id": individual_id,
            "individual_url": (
                url_for("individuals.detail", individual_id=individual_id)
                if individual_id else None
            ),
            "status": row["status"] if "status" in row.keys() else None,
            "height_cm": row["height_cm"] if "height_cm" in row.keys() else None,
            "build": row["build"] if "build" in row.keys() else None,
            "posture": row["posture"] if "posture" in row.keys() else None,
            "gait": row["gait"] if "gait" in row.keys() else None,
        },
    }


def search_identities(query: str, limit: int = 500) -> list[dict]:
    """Rank saved sightings/identities against a written prompt."""
    rows, _ = SightingRepository.search(limit=limit)
    results = []
    for row in rows:
        scored = identity_score(query, row)
        if scored is None:
            continue
        score, detail = scored
        results.append(_candidate_identity(row, score, detail))
    results.sort(key=lambda item: item["score"], reverse=True)
    return results
