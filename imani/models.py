"""Data-access repositories (thin wrappers over SQLite)."""
from __future__ import annotations

import json
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


class IndividualRepository:
    """Queries and mutations for the ``individuals`` register."""

    # The writable columns, in a stable order used to build INSERT/UPDATE.
    COLUMNS = (
        "full_name", "alias", "date_of_birth", "gender", "national_id",
        "phone", "email", "nationality", "address", "height_cm", "build",
        "posture", "gait", "distinguishing_marks", "face_template", "notes",
        "status",
    )

    @staticmethod
    def get(individual_id: int | None):
        if not individual_id:
            return None
        return get_db().execute(
            "SELECT * FROM individuals WHERE id = ?", (individual_id,)
        ).fetchone()

    @staticmethod
    def get_by_reference(reference: str):
        if not reference:
            return None
        return get_db().execute(
            "SELECT * FROM individuals WHERE reference = ?", (reference,)
        ).fetchone()

    @staticmethod
    def get_by_national_id(national_id: str | None):
        if not national_id:
            return None
        return get_db().execute(
            "SELECT * FROM individuals WHERE national_id = ?", (national_id,)
        ).fetchone()

    @staticmethod
    def create(values: dict, actor: str | None = None) -> int:
        db = get_db()
        columns = IndividualRepository.COLUMNS
        placeholders = ", ".join(f":{c}" for c in columns) + ", :actor"
        params = {c: values.get(c) for c in columns}
        params["actor"] = actor
        cur = db.execute(
            f"INSERT INTO individuals ({', '.join(columns)}, created_by) "
            f"VALUES ({placeholders})",
            params,
        )
        individual_id = cur.lastrowid
        # A friendly reference derived from the row id, e.g. "IM-00042".
        db.execute(
            "UPDATE individuals SET reference = ? WHERE id = ?",
            (f"IM-{individual_id:05d}", individual_id),
        )
        db.commit()
        return individual_id

    @staticmethod
    def update(individual_id: int, values: dict, actor: str | None = None) -> None:
        db = get_db()
        assignments = ", ".join(f"{c} = :{c}" for c in IndividualRepository.COLUMNS)
        params = {c: values.get(c) for c in IndividualRepository.COLUMNS}
        params.update({"id": individual_id, "actor": actor, "now": _now()})
        db.execute(
            f"UPDATE individuals SET {assignments}, "
            "updated_at = :now, updated_by = :actor WHERE id = :id",
            params,
        )
        db.commit()

    @staticmethod
    def delete(individual_id: int) -> None:
        db = get_db()
        db.execute("DELETE FROM individuals WHERE id = ?", (individual_id,))
        db.commit()

    @staticmethod
    def all(status: str | None = None):
        if status:
            return get_db().execute(
                "SELECT * FROM individuals WHERE status = ? "
                "ORDER BY full_name COLLATE NOCASE, id",
                (status,),
            ).fetchall()
        return get_db().execute(
            "SELECT * FROM individuals ORDER BY full_name COLLATE NOCASE, id"
        ).fetchall()

    @staticmethod
    def count() -> int:
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM individuals"
        ).fetchone()["n"]

    @staticmethod
    def search(q: str | None = None, status: str | None = None,
               build: str | None = None, limit: int = 20, offset: int = 0):
        """Return ``(rows, total)`` for the given search terms."""
        clauses: list[str] = []
        params: list = []
        if q:
            like = f"%{q}%"
            clauses.append(
                "(full_name LIKE ? OR alias LIKE ? OR reference LIKE ? "
                "OR national_id LIKE ? OR phone LIKE ? OR email LIKE ?)"
            )
            params.extend([like] * 6)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if build:
            clauses.append("build = ?")
            params.append(build)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        db = get_db()
        total = db.execute(
            f"SELECT COUNT(*) AS n FROM individuals {where}", params
        ).fetchone()["n"]
        rows = db.execute(
            f"SELECT * FROM individuals {where} "
            "ORDER BY full_name COLLATE NOCASE, id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return rows, total


class MatchRepository:
    """Persisted match runs (probes) and their ranked candidates."""

    @staticmethod
    def create_run(values: dict, actor: str | None = None) -> int:
        db = get_db()
        cur = db.execute(
            "INSERT INTO match_runs (title, probe_height_cm, probe_build, "
            "probe_posture, probe_gait, probe_face, min_score, notes, "
            "traits_used, result_count, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values.get("title"), values.get("probe_height_cm"),
                values.get("probe_build"), values.get("probe_posture"),
                values.get("probe_gait"), values.get("probe_face"),
                values.get("min_score", 0), values.get("notes"),
                values.get("traits_used"), values.get("result_count", 0), actor,
            ),
        )
        run_id = cur.lastrowid
        db.execute(
            "UPDATE match_runs SET reference = ? WHERE id = ?",
            (f"MR-{run_id:05d}", run_id),
        )
        db.commit()
        return run_id

    @staticmethod
    def add_results(run_id: int, results: list[dict]) -> None:
        if not results:
            return
        db = get_db()
        db.executemany(
            "INSERT INTO match_results (run_id, individual_id, individual_name, "
            "individual_reference, score, band, breakdown) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, r.get("individual_id"), r.get("individual_name"),
                    r.get("individual_reference"), r["score"], r["band"],
                    json.dumps(r.get("breakdown", {})),
                )
                for r in results
            ],
        )
        db.commit()

    @staticmethod
    def get_run(run_id: int | None):
        if not run_id:
            return None
        return get_db().execute(
            "SELECT * FROM match_runs WHERE id = ?", (run_id,)
        ).fetchone()

    @staticmethod
    def results_for(run_id: int) -> list[dict]:
        rows = get_db().execute(
            "SELECT * FROM match_results WHERE run_id = ? "
            "ORDER BY score DESC, id",
            (run_id,),
        ).fetchall()
        return [
            {**dict(row), "breakdown": json.loads(row["breakdown"] or "{}")}
            for row in rows
        ]

    @staticmethod
    def recent_runs(limit: int = 50):
        return get_db().execute(
            "SELECT * FROM match_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    @staticmethod
    def count() -> int:
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM match_runs"
        ).fetchone()["n"]

    @staticmethod
    def delete_run(run_id: int) -> None:
        db = get_db()
        db.execute("DELETE FROM match_runs WHERE id = ?", (run_id,))
        db.commit()


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

    @staticmethod
    def for_entity(entity: str, entity_id: int, limit: int = 20):
        """Event history for a single record (e.g. one individual)."""
        return get_db().execute(
            "SELECT * FROM audit_log WHERE entity = ? AND entity_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (entity, entity_id, limit),
        ).fetchall()
