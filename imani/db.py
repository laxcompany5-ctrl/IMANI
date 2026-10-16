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


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(reset_db_command)
    app.cli.add_command(seed_command)
    app.cli.add_command(migrate_command)
