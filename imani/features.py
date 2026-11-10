"""Image features and similarity for IMANI's description/photo search.

Everything here is deterministic. Features are read from the actual pixels with
Pillow + NumPy, so the percentage a result shows is a real measure of how much
the frame matches the prompt (colour) or the uploaded photo (colour histogram),
not a random number.

The feature record stored next to each photo/video looks like::

    {
      "width": 1280, "height": 720,
      "hist": [ ...108 floats... ],       # normalised hue/sat/value histogram
      "colours": {"red": 0.12, ...},      # fraction of the frame per colour name
      "dominant": [{"hex": "#a33", "ratio": 0.42}, ...],
      "brightness": 0.41
    }

Storing features means a search does not have to decode every JPEG again; but
:func:`extract_features` is cheap enough to fall back to on demand.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path

# Hue is in degrees; saturation/value are 0..1.
# Each entry is (hue ranges, minimmum saturation, minimum value, maximum value).
COLOUR_RANGES: dict[str, dict] = {
    "red":     {"hue": [(0, 14), (346, 360)], "smin": 0.35, "vmin": 0.18},
    "orange":  {"hue": [(15, 42)], "smin": 0.40, "vmin": 0.25},
    "yellow":  {"hue": [(43, 70)], "smin": 0.35, "vmin": 0.35},
    "green":   {"hue": [(71, 165)], "smin": 0.25, "vmin": 0.15},
    "cyan":    {"hue": [(166, 195)], "smin": 0.25, "vmin": 0.20},
    "blue":    {"hue": [(196, 258)], "smin": 0.25, "vmin": 0.15},
    "purple":  {"hue": [(259, 292)], "smin": 0.22, "vmin": 0.12},
    "pink":    {"hue": [(293, 345)], "smin": 0.20, "vmin": 0.30},
    "brown":   {"hue": [(12, 48)], "smin": 0.18, "vmin": 0.08, "vmax": 0.72},
    "black":   {"hue": [], "smin": 0.0, "vmin": 0.0, "vmax": 0.20},
    "white":   {"hue": [], "smin": 0.0, "smax": 0.16, "vmin": 0.82},
    "grey":    {"hue": [], "smin": 0.0, "smax": 0.16, "vmin": 0.20, "vmax": 0.82},
    "beige":   {"hue": [(25, 52)], "smin": 0.06, "smax": 0.30, "vmin": 0.62},
}

# Words that mean one of the base colours above.
SYNONYMS = {
    "gray": "grey", "silver": "grey", "charcoal": "black",
    "violet": "purple", "indigo": "blue", "navy": "blue",
    "maroon": "red", "crimson": "red", "scarlet": "red", "burgundy": "red",
    "tan": "beige", "khaki": "beige", "cream": "white", "ivory": "white",
    "teal": "cyan", "turquoise": "cyan", "aqua": "cyan",
    "magenta": "pink", "rose": "pink", "olive": "green", "lime": "green",
    "gold": "yellow", "golden": "yellow", "amber": "orange",
    "skin": "beige", "complexion": "beige",
}

_WORD = re.compile(r"[a-z]+")
_STOP = {
    "the", "and", "with", "wearing", "wear", "wore", "has", "have", "had",
    "person", "people", "man", "woman", "someone", "somebody", "photo",
    "video", "picture", "image", "from", "near", "around", "this", "that",
    "who", "was", "were", "are", "is", "of", "in", "on", "at", "to", "for",
    "by", "a", "an", "or", "very", "looking", "seen", "tall", "short",
}

# A colour that covers this much of the frame counts as fully present.
COLOUR_FULL = 0.12


def _load(source):
    """Open ``source`` (bytes, path or Path) as an RGB PIL image."""
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        image = Image.open(io.BytesIO(source))
    else:
        image = Image.open(source)
    return image.convert("RGB")


def _colour_mask(spec: dict, hue, sat, val):
    import numpy as np

    mask = np.ones(hue.shape, dtype=bool)
    ranges = spec.get("hue") or []
    if ranges:
        hue_mask = np.zeros(hue.shape, dtype=bool)
        for low, high in ranges:
            if low <= high:
                hue_mask |= (hue >= low) & (hue <= high)
            else:  # wraps past 360
                hue_mask |= (hue >= low) | (hue <= high)
        mask &= hue_mask
    if "smin" in spec:
        mask &= sat >= spec["smin"]
    if "smax" in spec:
        mask &= sat <= spec["smax"]
    if "vmin" in spec:
        mask &= val >= spec["vmin"]
    if "vmax" in spec:
        mask &= val <= spec["vmax"]
    return mask


def _dominant_colours(rgb, hue, sat, val, total: int) -> list[dict]:
    """A handful of representative swatches, largest share first."""
    import numpy as np

    buckets: list[tuple[int, object]] = []
    hue_bin = np.clip((hue / 30).astype(int), 0, 11)
    for index in range(12):
        mask = (hue_bin == index) & (sat > 0.22) & (val > 0.18)
        count = int(mask.sum())
        if count:
            buckets.append((count, rgb[mask]))
    grey = sat < 0.16
    if int(grey.sum()):
        buckets.append((int(grey.sum()), rgb[grey]))

    swatches = []
    for count, pixels in buckets:
        ratio = count / max(total, 1)
        if ratio < 0.03:
            continue
        mean = pixels.mean(axis=0)
        swatches.append({
            "hex": "#%02x%02x%02x" % tuple(int(max(0, min(255, c))) for c in mean),
            "ratio": round(ratio, 3),
        })
    swatches.sort(key=lambda item: item["ratio"], reverse=True)
    return swatches[:5]


def extract_features(source, size: tuple[int, int] = (48, 48)) -> dict | None:
    """Read pixels from ``source`` and return the feature record."""
    try:
        import numpy as np

        image = _load(source)
    except Exception:
        return None

    width, height = image.size
    small = image.resize(size)
    rgb = np.asarray(small, dtype=np.float32)
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32)
    hue = hsv[..., 0] * (360.0 / 255.0)
    sat = hsv[..., 1] / 255.0
    val = hsv[..., 2] / 255.0
    total = hue.size

    # 12 hue bins x 3 saturation x 3 value = 108 dimensions.
    hbin = np.clip((hue / 30).astype(int), 0, 11)
    sbin = np.clip((sat * 3).astype(int), 0, 2)
    vbin = np.clip((val * 3).astype(int), 0, 2)
    index = (hbin * 9 + sbin * 3 + vbin).ravel()
    hist = np.bincount(index, minlength=108).astype(np.float64)
    hist /= hist.sum() or 1.0

    colours = {}
    for name, spec in COLOUR_RANGES.items():
        mask = _colour_mask(spec, hue, sat, val)
        colours[name] = round(float(mask.sum()) / total, 5)

    return {
        "width": width,
        "height": height,
        "hist": [round(float(value), 6) for value in hist],
        "colours": colours,
        "dominant": _dominant_colours(rgb, hue, sat, val, total),
        "brightness": round(float(val.mean()), 4),
    }


# --- Prompt matching ------------------------------------------------------

def colours_in(text: str) -> list[str]:
    """Base colour names mentioned in a description, in order, no duplicates."""
    found: list[str] = []
    for word in _WORD.findall((text or "").lower()):
        colour = SYNONYMS.get(word, word)
        if colour in COLOUR_RANGES and colour not in found:
            found.append(colour)
    return found


def keyword_score(query: str, text: str) -> float:
    """Share of meaningful query words that appear in ``text`` (0..1)."""
    wanted = {w for w in _WORD.findall((query or "").lower()) if w not in _STOP}
    if not wanted:
        return 0.0
    present = set(_WORD.findall((text or "").lower()))
    return len(wanted & present) / len(wanted)


def colour_score(description: str, features: dict) -> float | None:
    """How much of the frame carries the colours the prompt asks for.

    Returns ``None`` when the prompt names no colours, so callers can fall back
    to metadata keywords instead of pretending every frame matches.
    """
    wanted = colours_in(description)
    if not wanted:
        return None
    ratios = (features or {}).get("colours") or {}
    presence = []
    for colour in wanted:
        ratio = float(ratios.get(colour, 0.0))
        presence.append(min(1.0, ratio / COLOUR_FULL))
    return sum(presence) / len(presence)


def description_match(description: str, features: dict,
                      meta_text: str = "") -> tuple[float, dict]:
    """Score a frame against a written prompt.

    Returns ``(percentage 0-100, detail)``. Colour words in the prompt are
    matched against the measured colour makeup of the frame; the remaining
    words are matched against annotations and camera metadata. When the prompt
    names a colour, at least 40% is reserved for colour so every result is
    comparable.
    """
    colour = colour_score(description, features)
    keywords = keyword_score(description, meta_text)
    detail = {
        "colours": colours_in(description),
        "colour_presence": None if colour is None else round(colour, 3),
        "keywords": round(keywords, 3),
    }
    if colour is None:
        percentage = round(keywords * 100.0, 1)
        detail["mode"] = "keywords"
    else:
        combined = 0.8 * colour + 0.2 * keywords
        percentage = 40.0 + 60.0 * combined if combined > 0 else 0.0
        detail["mode"] = "colour+keywords"
    detail["percentage"] = round(percentage, 1)
    return round(percentage, 1), detail


def image_similarity(left: dict, right: dict) -> tuple[float, dict]:
    """Compare two feature records. 100 means identical colour makeup."""
    import numpy as np

    a = np.asarray((left or {}).get("hist") or [], dtype=np.float64)
    b = np.asarray((right or {}).get("hist") or [], dtype=np.float64)
    if a.size == 0 or a.size != b.size:
        return 0.0, {"cosine": 0.0, "overlap": 0.0}
    denominator = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    cosine = float(np.dot(a, b) / denominator)
    overlap = float(np.minimum(a, b).sum())
    score = max(0.0, min(100.0, (0.6 * cosine + 0.4 * overlap) * 100.0))
    return round(score, 1), {"cosine": round(cosine, 3), "overlap": round(overlap, 3)}


def summarise(features: dict) -> str:
    """A one-line human description of the dominant colours, for the UI."""
    if not features:
        return ""
    parts = [item["hex"] for item in features.get("dominant", [])[:3]]
    return " · ".join(parts)
