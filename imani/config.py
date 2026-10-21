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
    # Endpoints exempt from the session CSRF check — machine-to-machine APIs
    # that authenticate with a shared key in a header instead of the session.
    CSRF_EXEMPT_ENDPOINTS = {"ingest.capture"}

    # Brute-force protection.
    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_MINUTES = 5

    # --- Google sign-in (OAuth 2.0 / OpenID Connect) ------------------
    GOOGLE_CLIENT_ID = os.environ.get("IMANI_GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("IMANI_GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.environ.get("IMANI_GOOGLE_REDIRECT_URI", "")
    GOOGLE_ALLOWED_DOMAINS = os.environ.get("IMANI_GOOGLE_ALLOWED_DOMAINS", "")

    # --- CCTV capture ingestion (Bit 8) -------------------------------
    # Shared secret the capture devices send in the X-API-Key header. When
    # unset the ingestion API is disabled (every request is rejected).
    INGEST_API_KEY = os.environ.get("IMANI_INGEST_API_KEY", "")
    # A capture scoring at or above this is attached to a known person outright.
    CAPTURE_ACCEPT_SCORE = float(os.environ.get("IMANI_CAPTURE_ACCEPT_SCORE", "85"))
    # Between review and accept it is attached but flagged for confirmation.
    CAPTURE_REVIEW_SCORE = float(os.environ.get("IMANI_CAPTURE_REVIEW_SCORE", "70"))
    # How many face templates to keep in each identity profile gallery.
    PROFILE_MAX_FACE_TEMPLATES = int(os.environ.get("IMANI_PROFILE_MAX_FACES", "5"))

    # --- Capture archive (Bit 11) -------------------------------------
    # Where recorded footage is kept. Sits next to the database.
    MEDIA_ROOT = os.environ.get(
        "IMANI_MEDIA_ROOT", str(BASE_DIR / "instance" / "media")
    )
    # Frames held in memory per camera to build a clip when a person is caught.
    MEDIA_CLIP_FRAMES = int(os.environ.get("IMANI_MEDIA_CLIP_FRAMES", "24"))
    # Frame rate the clip is replayed at in the archive.
    MEDIA_CLIP_FPS = float(os.environ.get("IMANI_MEDIA_CLIP_FPS", "5"))
    # How long footage is kept. 0 keeps it forever.
    MEDIA_RETENTION_DAYS = int(os.environ.get("IMANI_MEDIA_RETENTION_DAYS", "90"))

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
