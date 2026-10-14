"""Authentication & authorisation for IMANI."""
from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import current_app, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .models import AuditRepository, UserRepository

SESSION_USER_KEY = "_imani_user_id"

# Role hierarchy: admin can do everything the others can.
ROLE_LEVELS = {"viewer": 1, "analyst": 2, "admin": 3}

# Reasons returned by authenticate().
INVALID = "invalid"
LOCKED = "locked"
PENDING = "pending"
DISABLED = "disabled"
REJECTED = "rejected"


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        # Malformed hash (e.g. the sentinel used by Google-only accounts).
        return False


def authenticate(identifier: str, password: str):
    """Authenticate a user.

    Returns ``(user, None)`` on success, or ``(None, reason)`` where reason
    is one of :data:`INVALID`, :data:`LOCKED`, :data:`PENDING`,
    :data:`DISABLED` or :data:`REJECTED`.
    """
    user = UserRepository.get_by_identifier(identifier)

    # Wrong username, or wrong password for a real user: identical response.
    if not user or not verify_password(user["password_hash"], password):
        if user:
            UserRepository.register_failure(
                user["id"],
                current_app.config["MAX_FAILED_ATTEMPTS"],
                current_app.config["LOCKOUT_MINUTES"],
            )
        return None, INVALID

    if UserRepository.is_locked(user):
        return None, LOCKED

    status = user["status"]
    if status == "pending":
        return None, PENDING
    if status == "rejected":
        return None, REJECTED
    if status != "active":
        return None, DISABLED

    UserRepository.reset_failures(user["id"])
    return user, None


def change_password(user_id: int, new_password: str) -> None:
    UserRepository.update_password(user_id, hash_password(new_password))


def login(user) -> None:
    session.clear()
    session[SESSION_USER_KEY] = user["id"]


def logout() -> None:
    session.pop(SESSION_USER_KEY, None)


def current_user():
    """Load the logged-in user, cached on ``g`` for the request."""
    if "current_user" not in g:
        user_id = session.get(SESSION_USER_KEY)
        g.current_user = UserRepository.get(user_id) if user_id else None
    return g.current_user


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def role_required(minimum_role: str) -> Callable:
    """Decorator factory: require at least ``minimum_role``."""
    required = ROLE_LEVELS[minimum_role]

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if ROLE_LEVELS.get(user["role"], 0) < required:
                flash("You are not authorised to perform that action.", "danger")
                return redirect(url_for("dashboard.index"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def ensure_seed_admin(username: str, password: str,
                      email: str = "admin@imani.local",
                      full_name: str = "System Administrator") -> bool:
    """Create the default admin account if no users exist. Returns True if created."""
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if count:
        return False
    UserRepository.create(
        username, email, full_name, hash_password(password),
        role="admin", status="active",
    )
    AuditRepository.record("INSERT", "users", None, f"seeded admin '{username}'", "system")
    return True
