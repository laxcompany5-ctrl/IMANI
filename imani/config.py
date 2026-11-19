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
    # The QR scan endpoints authenticate with a signed, camera-scoped token.
    CSRF_EXEMPT_ENDPOINTS = {
        "ingest.capture",
        "cameras.scan_frame",
        "cameras.scan_audio",
        "cameras.scan_flush",
    }

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

    # --- Automatic recording (Bit 13) ---------------------------------
    # Seconds between automatic snapshots while a camera is monitored.
    SNAPSHOT_INTERVAL_SECONDS = float(
        os.environ.get("IMANI_SNAPSHOT_INTERVAL", "30")
    )
    # Continuous video is written in segments of this many seconds.
    VIDEO_SEGMENT_SECONDS = float(os.environ.get("IMANI_VIDEO_SEGMENT", "20"))
    # The first segment is flushed sooner so a newly paired phone appears in
    # the Videos tab quickly; later segments use VIDEO_SEGMENT_SECONDS.
    VIDEO_SEGMENT_FIRST_SECONDS = float(
        os.environ.get("IMANI_VIDEO_SEGMENT_FIRST", "5")
    )
    VIDEO_SEGMENT_ENABLED = (
        os.environ.get("IMANI_VIDEO_SEGMENT_ENABLED", "1") == "1"
    )
    # If every recent live frame fails the quality gate, save the best one
    # after this long instead of leaving the Snapshots tab empty.
    SNAPSHOT_FALLBACK_SECONDS = float(
        os.environ.get("IMANI_SNAPSHOT_FALLBACK", "15")
    )
    # Lifetime of a camera pairing QR link.
    QR_TOKEN_MAX_AGE = int(os.environ.get("IMANI_QR_MAX_AGE", "2592000"))
    # Window in which an operator-generated "connect a device" QR can be
    # scanned. Kept short because each scan registers a new phone camera.
    CONNECT_TOKEN_MAX_AGE = int(os.environ.get("IMANI_CONNECT_MAX_AGE", "3600"))
    # How long a phone-source monitor waits for the next frame before it pauses.
    # Long enough to survive a brief tab switch; a new frame restarts it.
    PHONE_IDLE_SECONDS = float(os.environ.get("IMANI_PHONE_IDLE", "30"))

    # --- HTTPS / public URLs (needed for phone cameras) ---------------
    # Mobile browsers block getUserMedia on plain http:// LAN addresses, so
    # pair phones over HTTPS. `app.py` creates a self-signed certificate when
    # IMANI_HTTPS=1.
    HTTPS_ENABLED = os.environ.get("IMANI_HTTPS", "0").lower() in (
        "1", "true", "yes", "on",
    )
    SSL_CERT = os.environ.get(
        "IMANI_SSL_CERT", str(BASE_DIR / "instance" / "certs" / "imani.crt")
    )
    SSL_KEY = os.environ.get(
        "IMANI_SSL_KEY", str(BASE_DIR / "instance" / "certs" / "imani.key")
    )
    # Optional overrides when HTTPS is terminated by a tunnel/reverse proxy.
    # e.g. IMANI_PUBLIC_URL=https://imani.example.com
    PUBLIC_BASE_URL = os.environ.get("IMANI_PUBLIC_URL", "").rstrip("/")
    PUBLIC_SCHEME = os.environ.get("IMANI_PUBLIC_SCHEME", "")
    PUBLIC_PORT = os.environ.get("IMANI_PUBLIC_PORT", "")

    # --- Video URL import → 4K snapshots (Bit 14) ---------------------
    # Where downloaded source video and extracted snapshots are stored.
    VIDEO_IMPORT_ROOT = os.environ.get(
        "IMANI_VIDEO_IMPORT_ROOT", str(BASE_DIR / "instance" / "video_imports")
    )
    # Refuse to download a file larger than this (bytes). Default 2 GiB.
    VIDEO_IMPORT_MAX_BYTES = int(
        os.environ.get("IMANI_VIDEO_IMPORT_MAX_BYTES", str(2 * 1024 ** 3))
    )
    # Candidate frames sampled from the video before the selector chooses.
    VIDEO_IMPORT_MAX_CANDIDATES = int(
        os.environ.get("IMANI_VIDEO_IMPORT_MAX_CANDIDATES", "600")
    )
    # Target resolution for the upscaled snapshots.
    VIDEO_IMPORT_4K_WIDTH = int(os.environ.get("IMANI_VIDEO_4K_WIDTH", "3840"))
    VIDEO_IMPORT_4K_HEIGHT = int(os.environ.get("IMANI_VIDEO_4K_HEIGHT", "2160"))
    # Optional path to an ffmpeg binary. If empty, imageio-ffmpeg / PATH is used.
    VIDEO_IMPORT_FFMPEG = os.environ.get("IMANI_FFMPEG", "")

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
