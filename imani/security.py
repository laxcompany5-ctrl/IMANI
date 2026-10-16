"""Cross-site request forgery (CSRF) protection.

A random token is stored in the session and injected into every form; any
state-changing request (POST/PUT/PATCH/DELETE) must echo it back.
"""
from __future__ import annotations

import hmac
import secrets

from flask import abort, current_app, request, session

CSRF_SESSION_KEY = "_csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def generate_csrf_token() -> str:
    """Return the session CSRF token, creating it on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf() -> None:
    """Abort the request if the CSRF token is missing or wrong."""
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    if request.method in SAFE_METHODS:
        return
    # Header-authenticated machine APIs (e.g. /ingest/capture) are configured
    # to bypass the session token entirely.
    if request.endpoint in current_app.config.get("CSRF_EXEMPT_ENDPOINTS", ()):
        return

    expected = session.get(CSRF_SESSION_KEY)
    provided = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")

    if not expected or not provided or not hmac.compare_digest(expected, provided):
        abort(400, "The form session expired or was tampered with. Please try again.")


def init_csrf(app) -> None:
    app.jinja_env.globals["csrf_token"] = generate_csrf_token
    app.before_request(validate_csrf)
