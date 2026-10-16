"""Turn a person's bounding box (and short track) into IMANI traits.

Everything here is pure: no OpenCV, no network, no filesystem. That keeps it
unit-testable and lets a device swap in its own extractor while reusing the
rest of the agent.

Honest caveat: these are deliberately simple, physically-motivated heuristics.
A production deployment should replace them with trained models (a face
embedding network, a gait descriptor, a pose estimator). IMANI re-identifies on
whatever traits are present, so any trait you cannot estimate can simply be
left out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# These mirror imani.validation; a test asserts they never drift apart.
BUILD_OPTIONS = ("slim", "average", "athletic", "heavy")
POSTURE_OPTIONS = ("upright", "neutral", "stooped", "hunched")
GAIT_OPTIONS = ("normal", "brisk", "slow", "limp", "shuffle")
MIN_HEIGHT_CM = 50
MAX_HEIGHT_CM = 260


@dataclass(frozen=True)
class Box:
    """An axis-aligned bounding box in pixels (origin: top-left of the frame)."""

    x: float
    y: float
    width: float
    height: float

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def iou(self, other: "Box") -> float:
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.x + self.width, other.x + other.width)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return 0.0
        intersection = (right - left) * (bottom - top)
        union = self.width * self.height + other.width * other.height - intersection
        return intersection / union if union else 0.0


@dataclass(frozen=True)
class Detection:
    """A detected person, optionally with two keypoints for the posture guess."""

    box: Box
    head: tuple[float, float] | None = None
    feet: tuple[float, float] | None = None
    face: list[float] | None = None


@dataclass(frozen=True)
class Calibration:
    """Single-camera calibration for the height estimate.

    ``camera_height_cm`` is how high the lens sits above the ground and
    ``principal_y`` is the image row of the optical centre (roughly the horizon
    line). For a roughly level camera the pinhole model on the ground plane
    gives::

        real_height = box_height_px * camera_height_cm / (feet_y - principal_y)

    so the further a person is (smaller ``feet_y - principal_y``), the smaller
    their box must be for the same real height.
    """

    camera_height_cm: float = 250.0
    principal_y: float = 360.0

    def height_cm(self, box: Box) -> int | None:
        denominator = box.bottom - self.principal_y
        if denominator <= 1:
            return None
        centimetres = box.height * self.camera_height_cm / denominator
        if not (MIN_HEIGHT_CM <= centimetres <= MAX_HEIGHT_CM):
            return None
        return int(round(centimetres))


# Body width-to-height ratio bands. A standing adult is roughly 2.5–3x taller
# than they are wide; a heavier build reads as a wider box.
_BUILD_BANDS = ((0.34, "slim"), (0.43, "average"), (0.50, "athletic"))


def build_from_aspect(aspect: float) -> str | None:
    """Classify build from the box's width/height ratio."""
    if aspect <= 0:
        return None
    for threshold, label in _BUILD_BANDS:
        if aspect < threshold:
            return label
    return "heavy"


def posture_from_axis(head: tuple[float, float] | None,
                      feet: tuple[float, float] | None) -> str | None:
    """Classify posture from the lean of the head-to-feet axis."""
    if head is None or feet is None:
        return None
    dy = feet[1] - head[1]
    if dy <= 0:
        return None
    lean = abs(head[0] - feet[0]) / dy
    if lean < 0.08:
        return "upright"
    if lean < 0.20:
        return "neutral"
    if lean < 0.35:
        return "stooped"
    return "hunched"


def gait_from_speed(body_heights_per_second: float | None) -> str | None:
    """Classify gait from horizontal speed measured in body-heights/second.

    Walking is very roughly 0.3–1.0 body-heights/s; a brisk walk is faster and
    a shuffle is slower. ``limp`` needs stride asymmetry we cannot see from a
    box, so it is never guessed here.
    """
    if body_heights_per_second is None:
        return None
    if body_heights_per_second >= 1.1:
        return "brisk"
    if body_heights_per_second >= 0.55:
        return "normal"
    if body_heights_per_second >= 0.22:
        return "slow"
    return "shuffle"


def format_timestamp(epoch_seconds: float) -> str:
    """UTC ``YYYY-MM-DD HH:MM:SS`` — the format IMANI stores."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def build_payload(camera: str, box: Box, epoch_seconds: float,
                  calibration: Calibration,
                  gait: str | None = None,
                  posture: str | None = None,
                  face: list[float] | None = None,
                  image_ref: str | None = None) -> dict:
    """Assemble the JSON body for one ``/ingest/capture`` call.

    Traits that could not be estimated are omitted, never faked.
    """
    payload: dict = {
        "camera": camera,
        "captured_at": format_timestamp(epoch_seconds),
    }

    height = calibration.height_cm(box)
    if height is not None:
        payload["height_cm"] = height

    build = build_from_aspect(box.aspect)
    if build is not None:
        payload["build"] = build
    if posture:
        payload["posture"] = posture
    if gait:
        payload["gait"] = gait
    if face:
        payload["face_template"] = face
    if image_ref:
        payload["image_ref"] = image_ref
    return payload
