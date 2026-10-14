"""The IMANI matching engine.

Ranks individuals from the register against an observed *probe* — the traits a
security analyst records for a subject. Each comparable trait yields a
similarity in ``[0, 1]``; the overall score is the weighted mean (rescaled to
0–100) mapped to a confidence band.

Weights follow the product spec:

    gait 25% · height 25% · face 20% · build 15% · posture 15%

Only traits present on *both* sides are compared, and the weights of the
compared traits are re-normalised — so a partial probe still produces a
comparable score. This is why a face template is optional: when neither side
has one, face simply drops out of the calculation.
"""
from __future__ import annotations

import math

# Trait weights (must sum to 1.0 when every trait is available).
WEIGHTS = {
    "height_cm": 0.25,
    "gait": 0.25,
    "face": 0.20,
    "build": 0.15,
    "posture": 0.15,
}

TRAIT_LABELS = {
    "height_cm": "Height",
    "gait": "Gait",
    "face": "Face",
    "build": "Build",
    "posture": "Posture",
}

# Height is scored on a tolerance: an exact match is 1.0 and anything
# HEIGHT_TOLERANCE_CM or further away scores 0.
HEIGHT_TOLERANCE_CM = 8.0

# Ordinal scales give partial credit to neighbouring categories.
BUILD_SCALE = ("slim", "average", "athletic", "heavy")
POSTURE_SCALE = ("upright", "neutral", "stooped", "hunched")

# Gait is not strictly ordinal, so we group comparable paces.
GAIT_GROUPS = {"normal": 0, "brisk": 0, "slow": 1, "shuffle": 1, "limp": 2}

# Confidence bands, highest threshold first.
_BANDS = ((85.0, "HIGH"), (70.0, "MODERATE"), (55.0, "LOW"))


def band_for(score: float) -> str:
    """Map a 0–100 score to a confidence band."""
    for threshold, name in _BANDS:
        if score >= threshold:
            return name
    return "VERY LOW"


def parse_template(text: str | None):
    """Parse a comma/space-separated numeric face template.

    Returns ``(vector, error)``. A blank string yields ``(None, None)``.
    """
    if not text or not text.strip():
        return None, None
    parts = [p for p in text.replace(",", " ").split() if p]
    vector: list[float] = []
    for part in parts:
        try:
            vector.append(float(part))
        except ValueError:
            return None, "Enter the face template as numbers separated by commas."
    if len(vector) < 2:
        return None, "A face template needs at least two numbers."
    return vector, None


def cosine_similarity(a: list[float] | None, b: list[float] | None):
    """Cosine similarity clamped to ``[0, 1]``; ``None`` if not comparable."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return None
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def height_similarity(probe_cm, record_cm) -> float:
    return max(0.0, 1.0 - abs(float(probe_cm) - float(record_cm)) / HEIGHT_TOLERANCE_CM)


def ordinal_similarity(probe, record, scale) -> float | None:
    if probe not in scale or record not in scale:
        return None
    distance = abs(scale.index(probe) - scale.index(record))
    return max(0.0, 1.0 - 0.5 * distance)


def gait_similarity(probe, record) -> float | None:
    if probe not in GAIT_GROUPS or record not in GAIT_GROUPS:
        return None
    if probe == record:
        return 1.0
    return 0.5 if GAIT_GROUPS[probe] == GAIT_GROUPS[record] else 0.0


def _trait_similarities(probe: dict, person) -> dict[str, float]:
    """Similarity for every trait the probe and record both provide."""
    sims: dict[str, float] = {}

    if probe.get("height_cm") is not None and person["height_cm"] is not None:
        sims["height_cm"] = height_similarity(probe["height_cm"], person["height_cm"])

    if probe.get("gait") and person["gait"]:
        sim = gait_similarity(probe["gait"], person["gait"])
        if sim is not None:
            sims["gait"] = sim

    if probe.get("build") and person["build"]:
        sim = ordinal_similarity(probe["build"], person["build"], BUILD_SCALE)
        if sim is not None:
            sims["build"] = sim

    if probe.get("posture") and person["posture"]:
        sim = ordinal_similarity(probe["posture"], person["posture"], POSTURE_SCALE)
        if sim is not None:
            sims["posture"] = sim

    if probe.get("face") and person["face_template"]:
        record_face, _ = parse_template(person["face_template"])
        sim = cosine_similarity(probe["face"], record_face)
        if sim is not None:
            sims["face"] = sim

    return sims


def score(probe: dict, person) -> dict | None:
    """Score one individual against the probe.

    Returns ``None`` when the probe and record share no comparable trait.
    """
    sims = _trait_similarities(probe, person)
    total_weight = sum(WEIGHTS[trait] for trait in sims)
    if total_weight == 0:
        return None

    weighted = sum(WEIGHTS[trait] * similarity for trait, similarity in sims.items())
    value = round(weighted / total_weight * 100, 1)
    breakdown = {
        trait: {"similarity": round(sims[trait], 4), "weight": WEIGHTS[trait]}
        for trait in sims
    }
    return {
        "score": value,
        "band": band_for(value),
        "breakdown": breakdown,
        "traits_used": list(sims),
    }


def rank(probe: dict, people, min_score: float = 0.0,
         limit: int | None = None) -> list[dict]:
    """Score, filter and rank a collection of individuals against a probe."""
    results: list[dict] = []
    for person in people:
        scored = score(probe, person)
        if scored is None or scored["score"] < min_score:
            continue
        results.append({
            "individual_id": person["id"],
            "individual_name": person["full_name"],
            "individual_reference": person["reference"],
            **scored,
        })

    results.sort(key=lambda item: (item["score"], item["individual_id"] or 0),
                 reverse=True)
    return results[:limit] if limit else results
