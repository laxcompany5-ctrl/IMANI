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
    def create(values: dict, actor: str | None = None,
               source: str = "manual") -> int:
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
        # ``source`` marks whether a person registered this record or a camera
        # enrolled it automatically (Bit 8).
        db.execute(
            "UPDATE individuals SET reference = ?, source = ? WHERE id = ?",
            (f"IM-{individual_id:05d}", source, individual_id),
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
    def count_by_status() -> dict[str, int]:
        rows = get_db().execute(
            "SELECT status, COUNT(*) AS n FROM individuals GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

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
    def scored_count() -> int:
        """Total candidate rows across all runs."""
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM match_results"
        ).fetchone()["n"]

    @staticmethod
    def band_distribution() -> dict[str, int]:
        rows = get_db().execute(
            "SELECT band, COUNT(*) AS n FROM match_results GROUP BY band"
        ).fetchall()
        return {row["band"]: row["n"] for row in rows}

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

    @staticmethod
    def search(action: str | None = None, entity: str | None = None,
               actor: str | None = None, q: str | None = None,
               limit: int = 50, offset: int = 0):
        """Filtered, paginated audit trail. Returns ``(rows, total)``."""
        clauses: list[str] = []
        params: list = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if entity:
            clauses.append("entity = ?")
            params.append(entity)
        if actor:
            clauses.append("lower(actor) LIKE lower(?)")
            params.append(f"%{actor}%")
        if q:
            clauses.append("details LIKE ?")
            params.append(f"%{q}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        db = get_db()
        total = db.execute(
            f"SELECT COUNT(*) AS n FROM audit_log {where}", params
        ).fetchone()["n"]
        rows = db.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return rows, total

    @staticmethod
    def distinct_actions() -> list[str]:
        return [row["action"] for row in get_db().execute(
            "SELECT DISTINCT action FROM audit_log ORDER BY action"
        ).fetchall()]

    @staticmethod
    def distinct_entities() -> list[str]:
        return [row["entity"] for row in get_db().execute(
            "SELECT DISTINCT entity FROM audit_log ORDER BY entity"
        ).fetchall()]


class SightingRepository:
    """Captures handed over by a CCTV deployment (Bit 8)."""

    @staticmethod
    def create(values: dict) -> int:
        db = get_db()
        cur = db.execute(
            "INSERT INTO sightings (individual_id, camera, captured_at, "
            "status, score, band, breakdown, height_cm, build, posture, gait, "
            "face_template, image_ref, notes) "
            "VALUES (:individual_id, :camera, :captured_at, :status, :score, "
            ":band, :breakdown, :height_cm, :build, :posture, :gait, "
            ":face_template, :image_ref, :notes)",
            {
                "individual_id": values.get("individual_id"),
                "camera": values.get("camera") or None,
                "captured_at": values.get("captured_at") or _now(),
                "status": values.get("status", "new"),
                "score": values.get("score"),
                "band": values.get("band"),
                "breakdown": json.dumps(values.get("breakdown") or {}),
                "height_cm": values.get("height_cm"),
                "build": values.get("build"),
                "posture": values.get("posture"),
                "gait": values.get("gait"),
                "face_template": values.get("face_template"),
                "image_ref": values.get("image_ref") or None,
                "notes": values.get("notes") or None,
            },
        )
        sighting_id = cur.lastrowid
        db.execute(
            "UPDATE sightings SET reference = ? WHERE id = ?",
            (f"SG-{sighting_id:05d}", sighting_id),
        )
        db.commit()
        return sighting_id

    @staticmethod
    def get(sighting_id: int | None):
        if not sighting_id:
            return None
        return get_db().execute(
            "SELECT * FROM sightings WHERE id = ?", (sighting_id,)
        ).fetchone()

    @staticmethod
    def assign(sighting_id: int, individual_id: int | None,
               status: str | None = None, score=None, band=None,
               breakdown: dict | None = None) -> None:
        """Attach (or re-attach) a capture to a person."""
        sets, params = ["individual_id = ?"], [individual_id]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if score is not None:
            sets.append("score = ?")
            params.append(score)
        if band is not None:
            sets.append("band = ?")
            params.append(band)
        if breakdown is not None:
            sets.append("breakdown = ?")
            params.append(json.dumps(breakdown))
        params.append(sighting_id)
        db = get_db()
        db.execute(
            f"UPDATE sightings SET {', '.join(sets)} WHERE id = ?", params
        )
        db.commit()

    @staticmethod
    def set_resolved(sighting_id: int, resolved: bool = True) -> None:
        db = get_db()
        db.execute(
            "UPDATE sightings SET resolved = ? WHERE id = ?",
            (1 if resolved else 0, sighting_id),
        )
        db.commit()

    @staticmethod
    def delete(sighting_id: int) -> None:
        db = get_db()
        db.execute("DELETE FROM sightings WHERE id = ?", (sighting_id,))
        db.commit()

    @staticmethod
    def _select() -> str:
        return (
            "SELECT s.*, i.full_name AS individual_name, "
            "i.reference AS individual_reference "
            "FROM sightings s LEFT JOIN individuals i ON i.id = s.individual_id"
        )

    @staticmethod
    def search(camera: str | None = None, status: str | None = None,
               individual_id: int | None = None, resolved: bool | None = None,
               limit: int = 20, offset: int = 0):
        """Return ``(rows, total)`` — newest first, joined to the person."""
        clauses: list[str] = []
        params: list = []
        if camera:
            clauses.append("s.camera = ?")
            params.append(camera)
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        if individual_id:
            clauses.append("s.individual_id = ?")
            params.append(individual_id)
        if resolved is not None:
            clauses.append("s.resolved = ?")
            params.append(1 if resolved else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        db = get_db()
        total = db.execute(
            f"SELECT COUNT(*) AS n FROM sightings s {where}", params
        ).fetchone()["n"]
        rows = db.execute(
            f"{SightingRepository._select()} {where} "
            "ORDER BY s.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return rows, total

    @staticmethod
    def for_individual(individual_id: int, limit: int = 20):
        """Every capture attributed to one person, newest first."""
        return get_db().execute(
            f"{SightingRepository._select()} "
            "WHERE s.individual_id = ? "
            "ORDER BY s.captured_at DESC, s.id DESC LIMIT ?",
            (individual_id, limit),
        ).fetchall()

    @staticmethod
    def review_queue(limit: int = 50):
        """Tentative matches an analyst should confirm."""
        return get_db().execute(
            f"{SightingRepository._select()} "
            "WHERE s.status = 'review' AND s.resolved = 0 "
            "ORDER BY s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    @staticmethod
    def review_count() -> int:
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM sightings "
            "WHERE status = 'review' AND resolved = 0"
        ).fetchone()["n"]

    @staticmethod
    def count() -> int:
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM sightings"
        ).fetchone()["n"]

    @staticmethod
    def count_today() -> int:
        return get_db().execute(
            "SELECT COUNT(*) AS n FROM sightings "
            "WHERE captured_at >= datetime('now', 'start of day')"
        ).fetchone()["n"]

    @staticmethod
    def count_by_status() -> dict[str, int]:
        rows = get_db().execute(
            "SELECT status, COUNT(*) AS n FROM sightings GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    @staticmethod
    def distinct_cameras() -> list[str]:
        return [row["camera"] for row in get_db().execute(
            "SELECT DISTINCT camera FROM sightings "
            "WHERE camera IS NOT NULL ORDER BY camera"
        ).fetchall()]


class ProfileRepository:
    """The rolling identity profile the engine learns for each person."""

    TALLY_KEYS = ("build", "posture", "gait")

    @staticmethod
    def get(individual_id: int):
        row = get_db().execute(
            "SELECT * FROM identity_profiles WHERE individual_id = ?",
            (individual_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def all() -> dict[int, dict]:
        return {
            row["individual_id"]: dict(row)
            for row in get_db().execute("SELECT * FROM identity_profiles")
        }

    @staticmethod
    def upsert(individual_id: int, traits: dict, seen_at: str | None = None,
               max_faces: int = 5) -> None:
        """Fold one observation into the person's profile.

        Height converges on a running mean, build/posture/gait accumulate
        vote counts (we match on the modal value), and the face gallery keeps
        up to ``max_faces`` near-orthogonal templates for the cosine matcher.
        """
        from . import matching  # local: avoids an import cycle at package load

        db = get_db()
        now = _now()
        seen = seen_at or now
        row = db.execute(
            "SELECT * FROM identity_profiles WHERE individual_id = ?",
            (individual_id,),
        ).fetchone()

        if row is None:
            height_mean, height_count = None, 0
            tallies: dict = {}
            gallery: list = []
            first_seen, sample_count = seen, 0
        else:
            height_mean, height_count = row["height_mean"], row["height_count"]
            tallies = json.loads(row["tallies"] or "{}")
            gallery = json.loads(row["face_templates"] or "[]")
            first_seen = row["first_seen"] or seen
            sample_count = row["sample_count"]

        observed = False

        height = traits.get("height_cm")
        if height is not None:
            height_mean = ((height_mean or 0.0) * height_count + float(height))                 / (height_count + 1)
            height_count += 1
            observed = True

        for key in ProfileRepository.TALLY_KEYS:
            value = traits.get(key)
            if value:
                bucket = tallies.setdefault(key, {})
                bucket[value] = bucket.get(value, 0) + 1
                observed = True

        face_text = traits.get("face_template")
        if face_text and _gallery_add(gallery, face_text, max_faces,
                                      matching):
            observed = True

        if observed:
            sample_count += 1

        last_seen = seen
        if row is not None and row["last_seen"]:
            last_seen = max(row["last_seen"], seen)

        tallies_json = json.dumps(tallies)
        faces_json = json.dumps(gallery)

        if row is None:
            db.execute(
                "INSERT INTO identity_profiles (individual_id, height_mean, "
                "height_count, tallies, face_templates, sample_count, "
                "first_seen, last_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (individual_id, height_mean, height_count, tallies_json,
                 faces_json, sample_count, first_seen, last_seen, now),
            )
        else:
            db.execute(
                "UPDATE identity_profiles SET height_mean = ?, height_count = ?, "
                "tallies = ?, face_templates = ?, sample_count = ?, "
                "last_seen = ?, updated_at = ? WHERE individual_id = ?",
                (height_mean, height_count, tallies_json, faces_json,
                 sample_count, last_seen, now, individual_id),
            )
        db.commit()


def _gallery_add(gallery: list, template: str, max_faces: int, matching) -> bool:
    """Add a face template unless it duplicates one we already keep.

    Returns ``True`` when the gallery changed. Near-identical vectors
    (cosine >= 0.98) are skipped; once full, the oldest template is retired.
    """
    vector, _ = matching.parse_template(template)
    if vector is None:
        return False

    for existing in gallery:
        other, _ = matching.parse_template(existing)
        similarity = matching.cosine_similarity(vector, other)
        if similarity is not None and similarity >= 0.98:
            return False

    gallery.append(template)
    if len(gallery) > max_faces:
        del gallery[0]
    return True
