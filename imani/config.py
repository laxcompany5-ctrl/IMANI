"""IMANI — configuration.

Values are read from the environment so nothing secret is hard-coded. In
development a local ``.env`` file is loaded automatically (if ``python-dotenv``
is installed); in production, use real environment variables / a systemd
``EnvironmentFile``.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local .env for development. Harmless if the file or package is absent.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass

DEFAULT_SECRET_KEY = "dev-secret-change-me"


class Config:
    """Base configuration shared by all environments."""

    # Which environment we are in: "development" or "production".
    ENVIRONMENT = os.environ.get("IMANI_ENV", "development")

    # --- Branding -----------------------------------------------------
    APP_NAME = "IMANI"
    APP_TAGLINE = "Security Intelligence Console"

    # --- Security -----------------------------------------------------
    SECRET_KEY = os.environ.get("IMANI_SECRET_KEY", DEFAULT_SECRET_KEY)
    SESSION_COOKIE_NAME = "imani_session"        # don't advertise "session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False                # HTTPS-only; on in production
    PREFERRED_URL_SCHEME = "http"

    # Trust X-Forwarded-* headers from an upstream reverse proxy (nginx).
    BEHIND_PROXY = os.environ.get("IMANI_BEHIND_PROXY", "0") == "1"

    # Toggle the CSRF guard (tests disable it).
    CSRF_ENABLED = True

    # Brute-force protection.
    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_MINUTES = 5

    # --- Google sign-in (OAuth 2.0 / OpenID Connect) ------------------
    GOOGLE_CLIENT_ID = os.environ.get("IMANI_GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("IMANI_GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.environ.get("IMANI_GOOGLE_REDIRECT_URI", "")
    GOOGLE_ALLOWED_DOMAINS = os.environ.get("IMANI_GOOGLE_ALLOWED_DOMAINS", "")

    # --- Database -----------------------------------------------------
    DATABASE = os.environ.get(
        "IMANI_DATABASE", str(BASE_DIR / "instance" / "imani.sqlite3")
    )

    # --- Seed administrator (used only by `flask seed`) ---------------
    DEFAULT_ADMIN_USERNAME = os.environ.get("IMANI_ADMIN_USER", "admin")
    DEFAULT_ADMIN_EMAIL = os.environ.get("IMANI_ADMIN_EMAIL", "admin@imani.local")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("IMANI_ADMIN_PASS", "Imani@2026")


class ProductionConfig(Config):
    """HTTPS + reverse-proxy configuration for a real domain."""

    ENVIRONMENT = "production"
    SESSION_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"
    BEHIND_PROXY = True


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    DATABASE = ":memory:"
