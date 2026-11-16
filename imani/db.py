"""SQLite connection handling and Flask CLI commands."""
from __future__ import annotations

import sqlite3

import click
from flask import current_app, g


def get_db() -> sqlite3.Connection:
    """Return the request-scoped SQLite connection."""
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # Better concurrency for multiple gunicorn workers.
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(_exception=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Create all tables from ``schema.sql``."""
    db = get_db()
    with current_app.open_resource("schema.sql") as fh:
        db.executescript(fh.read().decode("utf-8"))
    db.commit()


# --- CLI ------------------------------------------------------------------

@click.command("init-db")
def init_db_command() -> None:
    """Create the database tables."""
    init_db()
    click.echo("Initialised the IMANI database.")


@click.command("reset-db")
def reset_db_command() -> None:
    """Drop every table and recreate an empty database (development only)."""
    db = get_db()
    db.executescript(
        "DROP TABLE IF EXISTS capture_media; DROP TABLE IF EXISTS cameras; "
        "DROP TABLE IF EXISTS identity_profiles; DROP TABLE IF EXISTS sightings; "
        "DROP TABLE IF EXISTS match_results; DROP TABLE IF EXISTS match_runs; "
        "DROP TABLE IF EXISTS audit_log; DROP TABLE IF EXISTS individuals; "
        "DROP TABLE IF EXISTS users;"
    )
    db.commit()
    init_db()
    click.echo("Reset the IMANI database.")


@click.command("seed")
def seed_command() -> None:
    """Create the default administrator if no users exist yet."""
    from .auth import ensure_seed_admin

    if ensure_seed_admin(
        current_app.config["DEFAULT_ADMIN_USERNAME"],
        current_app.config["DEFAULT_ADMIN_PASSWORD"],
        email=current_app.config["DEFAULT_ADMIN_EMAIL"],
    ):
        click.echo(
            f"Created administrator "
            f"'{current_app.config['DEFAULT_ADMIN_USERNAME']}'."
        )
    else:
        click.echo("Users already exist — no seed account created.")


def _table_columns(db, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


@click.command("migrate")
def migrate_command() -> None:
    """Add columns/tables introduced by later 'bits' (safe to re-run)."""
    db = get_db()
    added: list[str] = []

    # 1. Columns added to `users` by Bit 2. These must exist before schema.sql
    #    runs, because its google_id index references the column.
    user_additions = (
        ("google_id", "ALTER TABLE users ADD COLUMN google_id TEXT"),
        ("avatar_url", "ALTER TABLE users ADD COLUMN avatar_url TEXT"),
        ("auth_provider",
         "ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'password'"),
    )
    for name, ddl in user_additions:
        if name not in _table_columns(db, "users"):
            db.execute(ddl)
            added.append(f"users.{name}")

    # 2. Create any tables/indexes introduced by later bits. Every statement in
    #    schema.sql is "IF NOT EXISTS", so this is safe to re-run.
    with current_app.open_resource("schema.sql") as fh:
        db.executescript(fh.read().decode("utf-8"))

    # 2b. The `cameras` CHECK constraint predates the 'phone' source. SQLite
    #     cannot alter a constraint, so rebuild the table once when needed.
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cameras'"
    ).fetchone()
    if row and "'phone'" not in (row["sql"] or ""):
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute(
            """CREATE TABLE cameras_rebuilt (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL UNIQUE,
                location        TEXT,
                source_type     TEXT    NOT NULL DEFAULT 'rtsp'
                                CHECK (source_type IN ('rtsp','http','file','device','demo','phone')),
                source_url      TEXT    NOT NULL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                group_id        INTEGER REFERENCES camera_groups(id) ON DELETE SET NULL,
                algorithm_json  TEXT,
                status          TEXT    NOT NULL DEFAULT 'stopped'
                                CHECK (status IN ('stopped','connecting','live','error','unavailable')),
                last_frame_at   TEXT,
                last_capture_at TEXT,
                last_error      TEXT,
                created_by      TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        columns = _table_columns(db, "cameras")
        wanted = [
            "id", "name", "location", "source_type", "source_url", "enabled",
            "group_id", "algorithm_json", "status", "last_frame_at",
            "last_capture_at", "last_error", "created_by", "created_at",
            "updated_at",
        ]
        shared = [name for name in wanted if name in columns]
        db.execute(
            f"INSERT INTO cameras_rebuilt ({', '.join(shared)}) "
            f"SELECT {', '.join(shared)} FROM cameras"
        )
        db.execute("DROP TABLE cameras")
        db.execute("ALTER TABLE cameras_rebuilt RENAME TO cameras")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cameras_status ON cameras(status)"
        )
        db.execute("PRAGMA foreign_keys = ON")
        added.append("cameras(source_type phone, groups, algorithm)")

    # 2c. Columns added by Bit 12/13 to media tables.
    media_additions = (
        ("camera_snapshots", "features_json",
         "ALTER TABLE camera_snapshots ADD COLUMN features_json TEXT"),
        ("camera_snapshots", "description",
         "ALTER TABLE camera_snapshots ADD COLUMN description TEXT"),
        ("camera_snapshots", "quality_json",
         "ALTER TABLE camera_snapshots ADD COLUMN quality_json TEXT"),
        ("camera_videos", "kind",
         "ALTER TABLE camera_videos ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'"),
        ("camera_videos", "mime_type",
         "ALTER TABLE camera_videos ADD COLUMN mime_type TEXT"),
        ("camera_videos", "features_json",
         "ALTER TABLE camera_videos ADD COLUMN features_json TEXT"),
        ("camera_videos", "description",
         "ALTER TABLE camera_videos ADD COLUMN description TEXT"),
    )
    for table, name, ddl in media_additions:
        if _table_columns(db, table) and name not in _table_columns(db, table):
            db.execute(ddl)
            added.append(f"{table}.{name}")
    if "kind" in _table_columns(db, "camera_videos"):
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_camera_videos_kind "
            "ON camera_videos(kind)"
        )

    # 2d. Columns added by Bit 15: published copies of imported media.
    library_additions = (
        ("video_imports", "library_video_id",
         "ALTER TABLE video_imports ADD COLUMN library_video_id INTEGER"),
        ("video_imports", "library_slug",
         "ALTER TABLE video_imports ADD COLUMN library_slug TEXT"),
        ("video_imports", "published_at",
         "ALTER TABLE video_imports ADD COLUMN published_at TEXT"),
        ("video_import_snapshots", "library_snapshot_id",
         "ALTER TABLE video_import_snapshots ADD COLUMN library_snapshot_id INTEGER"),
        ("video_imports", "rejected_count",
         "ALTER TABLE video_imports ADD COLUMN rejected_count INTEGER NOT NULL DEFAULT 0"),
        ("video_import_snapshots", "quality_json",
         "ALTER TABLE video_import_snapshots ADD COLUMN quality_json TEXT"),
    )
    for table, name, ddl in library_additions:
        if _table_columns(db, table) and name not in _table_columns(db, table):
            db.execute(ddl)
            added.append(f"{table}.{name}")

    # 3. Columns added to `individuals` by Bit 6 (only if the table existed
    #    before step 2; a fresh table already includes them).
    individual_additions = (
        ("face_template", "ALTER TABLE individuals ADD COLUMN face_template TEXT"),
        ("source", "ALTER TABLE individuals ADD COLUMN source TEXT NOT NULL "
                   "DEFAULT 'manual'"),
    )
    if _table_columns(db, "individuals"):
        for name, ddl in individual_additions:
            if name not in _table_columns(db, "individuals"):
                db.execute(ddl)
                added.append(f"individuals.{name}")

    db.commit()

    if added:
        click.echo("Added: " + ", ".join(added) + ".")
    else:
        click.echo("Database already up to date.")


@click.command("publish-imports")
def publish_imports_command() -> None:
    """Publish completed video imports into the Videos and Snapshots libraries."""
    from . import video_import

    app = current_app._get_current_object()
    results = video_import.publish_completed_imports(app)
    if not results:
        click.echo("No unpublished completed imports.")
        return
    for result in results:
        if result.get("error"):
            click.echo(
                f"Import {result['import_id']}: ERROR {result['error']}"
            )
        else:
            click.echo(
                f"Import {result['import_id']}: video "
                f"{result.get('video_id')}, {result.get('snapshots', 0)} snapshots."
            )


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(reset_db_command)
    app.cli.add_command(seed_command)
    app.cli.add_command(migrate_command)
    app.cli.add_command(publish_imports_command)
