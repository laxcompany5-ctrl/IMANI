"""Input validation helpers."""
from __future__ import annotations

import datetime
import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# --- Individual register (Bit 5) -----------------------------------------
BUILD_OPTIONS = ("slim", "average", "athletic", "heavy")
POSTURE_OPTIONS = ("upright", "neutral", "stooped", "hunched")
GAIT_OPTIONS = ("normal", "brisk", "slow", "limp", "shuffle")
GENDER_OPTIONS = ("female", "male", "other", "unspecified")
INDIVIDUAL_STATUSES = ("active", "archived")

MAX_NAME_LENGTH = 120
MIN_HEIGHT_CM = 50
MAX_HEIGHT_CM = 260


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match((username or "").strip()))


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def password_problems(password: str, confirm: str | None = None) -> list[str]:
    """Return human-readable password-policy violations."""
    problems: list[str] = []
    if len(password or "") < 8:
        problems.append("be at least 8 characters long")
    if not re.search(r"[A-Za-z]", password or ""):
        problems.append("contain at least one letter")
    if not re.search(r"\d", password or ""):
        problems.append("contain at least one number")
    if confirm is not None and password != confirm:
        problems.append("match the confirmation")
    return problems


def individual_problems(values: dict) -> list[str]:
    """Return human-readable problems with a normalised individual record.

    ``values`` is expected to already be the normalised mapping produced by the
    register routes (see ``routes/individuals.py``): ``height_cm`` is an int or
    ``None`` and blank optional fields are ``None``.
    """
    problems: list[str] = []

    name = (values.get("full_name") or "").strip()
    if not name:
        problems.append("provide the individual's full name")
    elif len(name) > MAX_NAME_LENGTH:
        problems.append(f"keep the full name under {MAX_NAME_LENGTH} characters")

    dob = values.get("date_of_birth")
    if dob:
        try:
            parsed = datetime.datetime.strptime(dob, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            problems.append("use a valid date of birth (YYYY-MM-DD)")
        else:
            if parsed > datetime.date.today():
                problems.append("enter a date of birth in the past")

    height = values.get("height_cm")
    if height is not None and not (MIN_HEIGHT_CM <= height <= MAX_HEIGHT_CM):
        problems.append(
            f"keep the height between {MIN_HEIGHT_CM} and {MAX_HEIGHT_CM} cm"
        )

    email = values.get("email")
    if email and not valid_email(email):
        problems.append("enter a valid email address")

    phone = values.get("phone")
    if phone and not (5 <= len(phone) <= 32):
        problems.append("enter a valid phone number")

    face = values.get("face_template")
    if face:
        from .matching import parse_template

        _, error = parse_template(face)
        if error:
            problems.append("use a face template of two or more numbers")

    return problems
