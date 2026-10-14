"""IMANI — configuration."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    """Base configuration shared by all environments."""

    # --- Branding -----------------------------------------------------
    APP_NAME = "IMANI"
    APP_TAGLINE = "Security Intelligence Console"

    # --- Security -----------------------------------------------------
    # Override in production:  export IMANI_SECRET_KEY="..."
    SECRET_KEY = os.environ.get("IMANI_SECRET_KEY", "dev-secret-change-me")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Toggle the CSRF guard (tests disable it).
    CSRF_ENABLED = True

    # Brute-force protection.
    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_MINUTES = 5

    # --- Google sign-in (OAuth 2.0 / OpenID Connect) ------------------
    # Create an OAuth 2.0 "Web application" client in the Google Cloud
    # Console and set these (e.g. in a .env file or your environment).
    GOOGLE_CLIENT_ID = os.environ.get("IMANI_GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("IMANI_GOOGLE_CLIENT_SECRET", "")
    # Optional: pin the exact redirect URI registered with Google. When
    # blank it is derived from the incoming request.
    GOOGLE_REDIRECT_URI = os.environ.get("IMANI_GOOGLE_REDIRECT_URI", "")
    # Optional: comma-separated allow-list of email domains.
    GOOGLE_ALLOWED_DOMAINS = os.environ.get("IMANI_GOOGLE_ALLOWED_DOMAINS", "")

    # --- Database -----------------------------------------------------
    DATABASE = os.environ.get(
        "IMANI_DATABASE", str(BASE_DIR / "instance" / "imani.sqlite3")
    )

    # --- Seed administrator (used only by `flask seed`) ---------------
    DEFAULT_ADMIN_USERNAME = os.environ.get("IMANI_ADMIN_USER", "admin")
    DEFAULT_ADMIN_EMAIL = os.environ.get("IMANI_ADMIN_EMAIL", "admin@imani.local")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("IMANI_ADMIN_PASS", "Imani@2026")


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    DATABASE = ":memory:"
