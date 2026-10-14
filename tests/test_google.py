"""Google OAuth sign-in tests.

Google's network endpoints are mocked, so these run with no internet access.
"""
from __future__ import annotations

import pytest

from imani.models import UserRepository

from .conftest import login


USERINFO = {
    "sub": "google-sub-12345",
    "email": "grace@imani.local",
    "email_verified": True,
    "name": "Grace Achieng",
    "picture": "https://example.invalid/grace.png",
}


def prime_session(client, state="the-state"):
    with client.session_transaction() as session:
        session["_oauth_state"] = state
        session["_oauth_verifier"] = "the-verifier"
        session["_oauth_next"] = ""


def mock_google(monkeypatch, userinfo=None):
    monkeypatch.setattr(
        "imani.oauth.exchange_code",
        lambda *a, **k: {"access_token": "access-token"},
    )
    monkeypatch.setattr(
        "imani.oauth.fetch_userinfo",
        lambda *a, **k: dict(userinfo or USERINFO),
    )


# --- Button & initiation --------------------------------------------------

def test_google_button_is_shown(google_client):
    response = google_client.get("/auth/login")
    assert b"Continue with Google" in response.data
    assert b"/auth/google" in response.data


def test_google_login_redirects_to_google(google_client):
    response = google_client.get("/auth/google", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "client_id=test-client-id" in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "state=" in location

    with google_client.session_transaction() as session:
        assert session["_oauth_state"]
        assert session["_oauth_verifier"]


def test_google_login_unconfigured_is_safe(client):
    response = client.get("/auth/google", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")
    with client.session_transaction() as session:
        assert "_oauth_state" not in session


# --- Callback -------------------------------------------------------------

def test_callback_rejects_bad_state(google_client, monkeypatch):
    prime_session(google_client)
    called = {"n": 0}
    monkeypatch.setattr("imani.oauth.exchange_code",
                        lambda *a, **k: called.__setitem__("n", 1))

    response = google_client.get(
        "/auth/google/callback?code=abc&state=WRONG", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")
    assert called["n"] == 0
    with google_client.session_transaction() as session:
        assert "_imani_user_id" not in session


def test_callback_creates_pending_account(google_app, google_client, monkeypatch):
    prime_session(google_client)
    mock_google(monkeypatch)

    response = google_client.get(
        "/auth/google/callback?code=abc&state=the-state", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")

    with google_app.app_context():
        user = UserRepository.get_by_google_id("google-sub-12345")
        assert user is not None
        assert user["status"] == "pending"
        assert user["auth_provider"] == "google"
        assert user["email"] == "grace@imani.local"


def test_callback_signs_in_linked_active_user(google_app, google_client, monkeypatch):
    # An existing, approved password user with the same email.
    with google_app.app_context():
        user_id = UserRepository.create(
            "grace", "grace@imani.local", "Grace Achieng",
            "!oauth-no-password", role="analyst", status="active",
        )
    prime_session(google_client)
    mock_google(monkeypatch)

    response = google_client.get(
        "/auth/google/callback?code=abc&state=the-state", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")

    with google_app.app_context():
        user = UserRepository.get(user_id)
        assert user["google_id"] == "google-sub-12345"   # identity linked
    with google_client.session_transaction() as session:
        assert session["_imani_user_id"] == user_id


def test_callback_pending_user_is_not_signed_in(google_app, google_client, monkeypatch):
    with google_app.app_context():
        UserRepository.create(
            "grace", "grace@imani.local", "Grace Achieng",
            "!oauth-no-password", role="viewer", status="pending",
        )
    prime_session(google_client)
    mock_google(monkeypatch)

    response = google_client.get(
        "/auth/google/callback?code=abc&state=the-state", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")
    with google_client.session_transaction() as session:
        assert "_imani_user_id" not in session


def test_callback_rejects_unverified_email(google_app, google_client, monkeypatch):
    prime_session(google_client)
    mock_google(monkeypatch, {**USERINFO, "email_verified": False})

    response = google_client.get(
        "/auth/google/callback?code=abc&state=the-state", follow_redirects=False
    )
    assert response.status_code == 302
    with google_app.app_context():
        assert UserRepository.get_by_email("grace@imani.local") is None


def test_callback_enforces_allowed_domains(google_app, google_client, monkeypatch):
    google_app.config["GOOGLE_ALLOWED_DOMAINS"] = "imani.local"
    prime_session(google_client)
    mock_google(monkeypatch, {**USERINFO, "email": "grace@gmail.com"})

    response = google_client.get(
        "/auth/google/callback?code=abc&state=the-state", follow_redirects=False
    )
    assert response.status_code == 302
    with google_app.app_context():
        assert UserRepository.get_by_email("grace@gmail.com") is None
