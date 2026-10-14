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
        "DROP TABLE IF EXISTS audit_log; DROP TABLE IF EXISTS users;"
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


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(reset_db_command)
    app.cli.add_command(seed_command)
