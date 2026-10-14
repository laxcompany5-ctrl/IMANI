"""Input validation helpers."""
from __future__ import annotations

import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


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
