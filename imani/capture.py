"""CCTV capture ingestion and continuous re-identification (Bit 8).

How a camera deployment talks to IMANI
--------------------------------------
An edge device (or a CV worker sitting next to the recorder) does the heavy
lifting — frame capture, person detection, and turning a person into traits —
then POSTs each detection to ``/ingest/capture``::

    {"camera": "gate-3",
     "captured_at": "2026-10-14 07:31:02",
     "height_cm": 181, "build": "athletic", "posture": "upright",
     "gait": "brisk", "face_template": [0.12, 0.87, ...],
     "image_ref": "frames/2026-10-14/gate-3/004312.jpg"}

For every detection IMANI:

1. **stores the sighting** — the traits, camera and timestamp are kept as their
   own row, so nothing a camera saw is ever lost, even when we have no idea who
   the person is;
2. **re-identifies** them by scoring those traits against every active person's
   *identity profile* (the values on their record refined by what the cameras
   have learned about them);
3. **attaches the sighting to an existing person** when the score is strong
   enough, or **auto-enrols a new person** when it is not — so the next time
   they walk past, both sightings belong to the same record instead of
   spawning a second one;
4. **learns**: the sighting is folded into that person's profile (a running
   height mean, modal build/posture/gait, and a small face gallery), so
   recognition gets sharper with every visit.

The extractor itself is deliberately *outside* IMANI. Computer-vision models
(face embeddings, gait descriptors) bring their own heavy dependencies and
hardware, so the device hands them over as plain JSON and IMANI owns the
storage, matching and the identity decision.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime

from flask import current_app

from . import matching
from .models import (
    AuditRepository,
    IndividualRepository,
    ProfileRepository,
    SightingRepository,
)
from .validation import (
    BUILD_OPTIONS,
    GAIT_OPTIONS,
    MAX_HEIGHT_CM,
    MIN_HEIGHT_CM,
    POSTURE_OPTIONS,
)

# The trait keys a probe exposes to the matcher (note ``face`` is the vector).
PROBE_TRAITS = ("height_cm", "build", "posture", "gait", "face")

# The stored trait keys that make a capture identifiable.
STORED_TRAITS = ("height_cm", "build", "posture", "gait", "face_template")

# Identity decision outcomes.
IDENTIFIED = "identified"
REVIEW = "review"
NEW = "new"


class CaptureError(ValueError):
    """A capture payload that cannot be understood."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# --- Normalisation --------------------------------------------------------

def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def _choice(raw, options):
    """Return the lowercased value if it is one of ``options``, else ``None``."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    return text if text in options else None


def _format_number(value) -> str:
    return f"{float(value):g}"


def normalise_traits(payload: dict):
    """Turn a raw payload into storage traits plus a probe face vector.

    Returns ``(traits, problems)``. ``traits`` carries both ``face_template``
    (text, for storage) and ``face`` (parsed vector, for scoring).
    """
    problems: list[str] = []

    raw_height = payload.get("height_cm")
    height = _int_or_none(raw_height)
    if raw_height not in (None, "") and height is None:
        problems.append("height_cm must be a number")
    elif height is not None and not (MIN_HEIGHT_CM <= height <= MAX_HEIGHT_CM):
        problems.append(
            f"height_cm must be between {MIN_HEIGHT_CM} and {MAX_HEIGHT_CM}"
        )
        height = None

    build = _choice(payload.get("build"), BUILD_OPTIONS)
    if payload.get("build") and build is None:
        problems.append("build must be one of: " + ", ".join(BUILD_OPTIONS))

    posture = _choice(payload.get("posture"), POSTURE_OPTIONS)
    if payload.get("posture") and posture is None:
        problems.append("posture must be one of: " + ", ".join(POSTURE_OPTIONS))

    gait = _choice(payload.get("gait"), GAIT_OPTIONS)
    if payload.get("gait") and gait is None:
        problems.append("gait must be one of: " + ", ".join(GAIT_OPTIONS))

    raw_face = payload.get("face_template")
    if isinstance(raw_face, (list, tuple)):
        face_text = ", ".join(_format_number(value) for value in raw_face)
    else:
        face_text = str(raw_face or "").strip()
    face_vector, face_error = matching.parse_template(face_text)
    if face_error:
        problems.append(face_error)
        face_text, face_vector = None, None

    traits = {
        "height_cm": height,
        "build": build,
        "posture": posture,
        "gait": gait,
        "face_template": face_text or None,
        "face": face_vector,
    }
    return traits, problems


def has_traits(traits: dict) -> bool:
    """True when a capture carries at least one trait worth matching on."""
    return any(traits.get(key) for key in STORED_TRAITS)


def probe_from(traits: dict) -> dict:
    return {key: traits.get(key) for key in PROBE_TRAITS}


# --- Profile view + matching ---------------------------------------------

def _modal(tally: dict | None):
    if not tally:
        return None
    # ``max`` over a sorted key list gives a stable, alphabetical tie-break.
    return max(sorted(tally), key=lambda key: tally[key])


def _pick_face(gallery: list[str], probe_face):
    """Choose the gallery template that best matches the probe."""
    if not gallery:
        return None
    if not probe_face:
        return gallery[0]
    best, best_similarity = None, None
    for template in gallery:
        vector, _ = matching.parse_template(template)
        similarity = matching.cosine_similarity(probe_face, vector)
        if similarity is None:
            continue
        if best_similarity is None or similarity > best_similarity:
            best, best_similarity = template, similarity
    return best or gallery[0]


def merge_profile(person, profile: dict | None, probe_face=None) -> dict:
    """The view the matcher uses: register values refined by learned profile."""
    merged = {
        "id": person["id"],
        "full_name": person["full_name"],
        "reference": person["reference"],
        "height_cm": person["height_cm"],
        "build": person["build"],
        "posture": person["posture"],
        "gait": person["gait"],
        "face_template": person["face_template"],
    }
    if not profile:
        return merged

    if profile.get("height_mean") is not None and profile.get("height_count"):
        merged["height_cm"] = round(profile["height_mean"])

    tallies = json.loads(profile.get("tallies") or "{}")
    for key in ProfileRepository.TALLY_KEYS:
        modal = _modal(tallies.get(key))
        if modal:
            merged[key] = modal

    gallery = json.loads(profile.get("face_templates") or "[]")
    chosen = _pick_face(gallery, probe_face)
    if chosen:
        merged["face_template"] = chosen
    return merged


def candidates(probe: dict) -> list[dict]:
    """Every active person as a profile, ready to be scored."""
    people = IndividualRepository.all(status="active")
    profiles = ProfileRepository.all()
    probe_face = probe.get("face")
    return [
        merge_profile(person, profiles.get(person["id"]), probe_face)
        for person in people
    ]


def identify(probe: dict):
    """Resolve a probe against the register.

    Returns ``(best, ranked, status)`` where status is one of
    :data:`IDENTIFIED`, :data:`REVIEW` or :data:`NEW`.
    """
    accept = float(current_app.config["CAPTURE_ACCEPT_SCORE"])
    review = float(current_app.config["CAPTURE_REVIEW_SCORE"])

    ranked = matching.rank(probe, candidates(probe), min_score=review, limit=5)
    best = ranked[0] if ranked else None
    if best is None:
        return None, ranked, NEW
    return best, ranked, IDENTIFIED if best["score"] >= accept else REVIEW


# --- Ingestion ------------------------------------------------------------

def _parse_seen_at(raw) -> str | None:
    """Best-effort parse of a device timestamp into SQLite's UTC format."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _enroll(reference: str, traits: dict, actor: str) -> int:
    """Create a provisional person from a capture that matched nobody."""
    values = {
        "full_name": f"Unknown ({reference})",
        "alias": None,
        "date_of_birth": None,
        "gender": "unspecified",
        "national_id": None,
        "phone": None,
        "email": None,
        "nationality": None,
        "address": None,
        "height_cm": traits.get("height_cm"),
        "build": traits.get("build") or "average",
        "posture": traits.get("posture") or "neutral",
        "gait": traits.get("gait") or "normal",
        "distinguishing_marks": None,
        "face_template": traits.get("face_template"),
        "notes": "Auto-enrolled from a CCTV capture; confirm or merge it.",
        "status": "active",
    }
    return IndividualRepository.create(values, actor=actor, source="cctv")


def traits_from_sighting(sighting) -> dict:
    """Rebuild a storage-traits mapping from a stored sighting row."""
    face_text = sighting["face_template"]
    face_vector, _ = matching.parse_template(face_text)
    return {
        "height_cm": sighting["height_cm"],
        "build": sighting["build"],
        "posture": sighting["posture"],
        "gait": sighting["gait"],
        "face_template": face_text,
        "face": face_vector,
    }


def _max_faces() -> int:
    return int(current_app.config["PROFILE_MAX_FACE_TEMPLATES"])


def enroll_from_sighting(sighting, actor: str) -> int:
    """Create a brand-new person from a stored capture and attach it."""
    traits = traits_from_sighting(sighting)
    person_id = _enroll(sighting["reference"], traits, actor)
    ProfileRepository.upsert(person_id, traits, seen_at=sighting["captured_at"],
                             max_faces=_max_faces())
    SightingRepository.assign(sighting["id"], person_id, status=IDENTIFIED)
    AuditRepository.record(
        "INSERT", "individuals", person_id,
        f"enrolled from capture {sighting['reference']}", actor,
    )
    return person_id


def reassign(sighting, individual_id: int, actor: str,
             status: str = IDENTIFIED) -> None:
    """Point an existing capture at a different person, and learn from it."""
    traits = traits_from_sighting(sighting)
    SightingRepository.assign(sighting["id"], individual_id, status=status)
    ProfileRepository.upsert(individual_id, traits, seen_at=sighting["captured_at"],
                             max_faces=_max_faces())
    AuditRepository.record(
        "CAPTURE", "individuals", individual_id,
        f"capture {sighting['reference']} attached by {actor}", actor,
    )


def confirm(sighting, actor: str) -> None:
    """Mark a tentative match as reviewed by a human."""
    SightingRepository.set_resolved(sighting["id"], True)
    AuditRepository.record(
        "UPDATE", "sightings", sighting["id"],
        f"confirmed {sighting['reference']}", actor,
    )


def ingest(payload: dict, actor: str = "cctv") -> dict:
    """Store one capture, resolve who it is, and learn from it.

    Raises :class:`CaptureError` when the payload is malformed. Returns a
    JSON-friendly summary of the decision.
    """
    traits, problems = normalise_traits(payload)
    if problems:
        raise CaptureError(problems)

    camera = _clean(payload.get("camera"))
    captured_at = _parse_seen_at(payload.get("captured_at"))
    image_ref = _clean(payload.get("image_ref"))
    notes = _clean(payload.get("notes"))

    matchable = has_traits(traits)
    best = None
    ranked: list = []
    status = NEW
    person_id = None

    if matchable:
        best, ranked, status = identify(probe_from(traits))
        if best is not None:
            person_id = best["individual_id"]

    sighting_id = SightingRepository.create({
        "individual_id": person_id,
        "camera": camera,
        "captured_at": captured_at,
        "status": status,
        "score": best["score"] if best else None,
        "band": best["band"] if best else None,
        "breakdown": best["breakdown"] if best else {},
        "height_cm": traits["height_cm"],
        "build": traits["build"],
        "posture": traits["posture"],
        "gait": traits["gait"],
        "face_template": traits["face_template"],
        "image_ref": image_ref,
        "notes": notes,
    })
    reference = SightingRepository.get(sighting_id)["reference"]

    created_person = False
    if person_id is None and matchable:
        person_id = _enroll(reference, traits, actor)
        SightingRepository.assign(sighting_id, person_id)
        created_person = True

    person = None
    if person_id is not None:
        ProfileRepository.upsert(
            person_id, traits, seen_at=captured_at,
            max_faces=int(current_app.config["PROFILE_MAX_FACE_TEMPLATES"]),
        )
        person = IndividualRepository.get(person_id)
        if created_person:
            AuditRepository.record(
                "INSERT", "individuals", person_id,
                f"auto-enrolled from capture {reference}", actor,
            )
        else:
            AuditRepository.record(
                "CAPTURE", "individuals", person_id,
                f"re-identified at {camera or 'unknown camera'} "
                f"(score {best['score'] if best else 'n/a'})", actor,
            )

    AuditRepository.record(
        "CAPTURE", "sightings", sighting_id,
        f"{status} at {camera or 'unknown camera'}"
        + (f" -> {person['reference']}" if person else ""), actor,
    )

    return {
        "sighting_id": sighting_id,
        "reference": reference,
        "status": status,
        "score": best["score"] if best else None,
        "band": best["band"] if best else None,
        "breakdown": best["breakdown"] if best else {},
        "created_person": created_person,
        "individual": (
            {
                "id": person["id"],
                "reference": person["reference"],
                "name": person["full_name"],
                "source": person["source"],
            }
            if person
            else None
        ),
        "candidates": [
            {
                "id": item["individual_id"],
                "reference": item["individual_reference"],
                "name": item["individual_name"],
                "score": item["score"],
                "band": item["band"],
            }
            for item in ranked
        ],
    }


def api_key_ok(provided, expected) -> bool:
    """Constant-time check of the ingestion API key."""
    if not expected:
        return False
    return bool(provided) and hmac.compare_digest(str(provided), str(expected))
