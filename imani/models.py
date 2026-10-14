"""Data-access repositories (thin wrappers over SQLite)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class UserRepository:
    """Queries and mutations for the ``users`` table."""

    @staticmethod
    def get(user_id: int | None):
        if not user_id:
            return None
        return get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()

    @staticmethod
    def get_by_username(username: str):
        return get_db().execute(
            "SELECT * FROM users WHERE lower(username) = lower(?)", (username or "",)
        ).fetchone()

    @staticmethod
    def get_by_email(email: str):
        return get_db().execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email or "",)
        ).fetchone()

    @staticmethod
    def get_by_identifier(identifier: str):
        """Look a user up by username *or* email."""
        return get_db().execute(
            "SELECT * FROM users WHERE lower(username) = lower(?) "
            "OR lower(email) = lower(?)",
            (identifier or "", identifier or ""),
        ).fetchone()

    @staticmethod
    def get_by_google_id(google_id: str):
        if not google_id:
            return None
        return get_db().execute(
            "SELECT * FROM users WHERE google_id = ?", (google_id,)
        ).fetchone()

    @staticmethod
    def suggest_username(base: str) -> str:
        """Build a unique username from a preferred base string."""
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "", base or "")[:28]
        if len(cleaned) < 3:
            cleaned = f"user{cleaned}"
        candidate = cleaned
        suffix = 0
        while UserRepository.get_by_username(candidate):
            suffix += 1
            candidate = f"{cleaned}{suffix}"[:32]
        return candidate

    @staticmethod
    def all():
        return get_db().execute(
            "SELECT * FROM users ORDER BY status, username"
        ).fetchall()

    @staticmethod
    def by_status(status: str):
        return get_db().execute(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at", (status,)
        ).fetchall()

    @staticmethod
    def count_by_status() -> dict[str, int]:
        rows = get_db().execute(
            "SELECT status, COUNT(*) AS n FROM users GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    @staticmethod
    def count() -> int:
        return get_db().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    @staticmethod
    def create(username: str, email: str, full_name: str, password_hash: str,
               role: str = "viewer", status: str = "pending",
               requested_role: str | None = None, google_id: str | None = None,
               avatar_url: str | None = None,
               auth_provider: str = "password") -> int:
        db = get_db()
        cur = db.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, "
            "status, requested_role, google_id, avatar_url, auth_provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (username, email, full_name, password_hash, role, status,
             requested_role, google_id, avatar_url, auth_provider),
        )
        db.commit()
        return cur.lastrowid

    @staticmethod
    def link_google(user_id: int, google_id: str, avatar_url: str | None = None) -> None:
        """Attach a Google identity to an existing account."""
        db = get_db()
        db.execute(
            "UPDATE users SET google_id = ?, avatar_url = COALESCE(?, avatar_url) "
            "WHERE id = ?",
            (google_id, avatar_url, user_id),
        )
        db.commit()

    @staticmethod
    def decide(user_id: int, status: str, role: str | None, decided_by: int) -> None:
        """Approve (status='active') or reject a pending account."""
        db = get_db()
        if role:
            db.execute(
                "UPDATE users SET status = ?, role = ?, decided_at = ?, decided_by = ? "
                "WHERE id = ?",
                (status, role, _now(), decided_by, user_id),
            )
        else:
            db.execute(
                "UPDATE users SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                (status, _now(), decided_by, user_id),
            )
        db.commit()

    @staticmethod
    def set_active(user_id: int, active: bool) -> None:
        db = get_db()
        db.execute(
            "UPDATE users SET status = ? WHERE id = ?",
            ("active" if active else "disabled", user_id),
        )
        db.commit()

    @staticmethod
    def update_password(user_id: int, password_hash: str) -> None:
        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )
        db.commit()

    # --- Brute-force protection --------------------------------------
    @staticmethod
    def register_failure(user_id: int, max_attempts: int, lock_minutes: int) -> None:
        db = get_db()
        row = db.execute(
            "SELECT failed_attempts FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        attempts = (row["failed_attempts"] if row else 0) + 1
        locked_until = None
        if attempts >= max_attempts:
            locked_until = (
                datetime.now(timezone.utc) + timedelta(minutes=lock_minutes)
            ).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, locked_until, user_id),
        )
        db.commit()

    @staticmethod
    def reset_failures(user_id: int) -> None:
        db = get_db()
        db.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (user_id,),
        )
        db.commit()

    @staticmethod
    def is_locked(user) -> bool:
        locked_until = user["locked_until"]
        if not locked_until:
            return False
        try:
            until = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return until > datetime.now(timezone.utc).replace(tzinfo=None)


class AuditRepository:
    """Append-only event log."""

    @staticmethod
    def record(action: str, entity: str, entity_id: int | None, details: str | None,
               actor: str | None) -> None:
        db = get_db()
        db.execute(
            "INSERT INTO audit_log (action, entity, entity_id, details, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, entity, entity_id, details, actor),
        )
        db.commit()

    @staticmethod
    def recent(limit: int = 50):
        return get_db().execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    @staticmethod
    def count_since(action: str, since_modifier: str) -> int:
        """Count events of ``action`` newer than ``datetime('now', ?)``.

        Example: ``count_since("LOGIN", "-1 day")``.
        """
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE action = ? AND created_at >= datetime('now', ?)",
            (action, since_modifier),
        ).fetchone()["n"]

    @staticmethod
    def recent_actions(limit: int = 8):
        """A compact, human-readable activity feed."""
        return get_db().execute(
            "SELECT action, entity, details, actor, created_at "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
