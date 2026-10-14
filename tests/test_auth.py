"""Authentication, approval and access-control tests (Bit 1)."""
from __future__ import annotations

from imani.models import UserRepository

from .conftest import login, signup


# --- Sign-up --------------------------------------------------------------

def test_signup_creates_pending_account(app, client):
    response = signup(client)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")

    with app.app_context():
        user = UserRepository.get_by_username("jane")
        assert user is not None
        assert user["status"] == "pending"
        assert user["requested_role"] == "analyst"
        # Least privilege until an admin decides.
        assert user["role"] == "viewer"


def test_signup_rejects_duplicate_username(app, client):
    signup(client)
    response = signup(client, email="other@imani.local")
    assert response.status_code == 400


def test_signup_rejects_weak_password(client):
    response = client.post(
        "/auth/signup",
        data={
            "full_name": "Weak Password",
            "username": "weakuser",
            "email": "weak@imani.local",
            "requested_role": "viewer",
            "password": "short",
            "confirm_password": "short",
        },
    )
    assert response.status_code == 400


# --- Login / approval gate ------------------------------------------------

def test_pending_user_cannot_sign_in(app, client):
    signup(client)
    response = login(client, "jane", "Passw0rd1")
    assert response.status_code == 200                 # stays on login page
    assert b"awaiting administrator approval" in response.data

    # Confirm no session was established.
    with client.session_transaction() as session:
        assert "_imani_user_id" not in session


def test_approved_user_can_sign_in(app, client):
    signup(client)
    with app.app_context():
        user = UserRepository.get_by_username("jane")
        UserRepository.decide(user["id"], "active", "analyst", decided_by=1)

    response = login(client, "jane", "Passw0rd1")
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["_imani_user_id"] == user["id"]


def test_login_accepts_email_identifier(app, client):
    signup(client)
    with app.app_context():
        user = UserRepository.get_by_username("jane")
        UserRepository.decide(user["id"], "active", "analyst", decided_by=1)

    response = login(client, "jane@imani.local", "Passw0rd1")
    assert response.status_code == 302


def test_wrong_password_is_rejected(client):
    response = login(client, "admin", "wrong-password")
    assert response.status_code == 200
    assert b"Invalid username/email or password" in response.data


# --- Brute-force protection ----------------------------------------------

def test_account_locks_after_repeated_failures(app, client):
    for _ in range(5):
        login(client, "admin", "wrong")
    response = login(client, "admin", "Imani@2026")
    assert b"temporarily locked" in response.data


# --- Access control -------------------------------------------------------

def test_dashboard_requires_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_non_admin_cannot_reach_approvals(app, client):
    signup(client)
    with app.app_context():
        user = UserRepository.get_by_username("jane")
        UserRepository.decide(user["id"], "active", "analyst", decided_by=1)

    login(client, "jane", "Passw0rd1")
    response = client.get("/admin/approvals", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_admin_can_approve_through_the_ui(app, client):
    signup(client)
    login(client, "admin", "Imani@2026")

    with app.app_context():
        user = UserRepository.get_by_username("jane")
        user_id = user["id"]

    response = client.post(
        f"/admin/approvals/{user_id}/approve",
        data={"role": "analyst"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        user = UserRepository.get(user_id)
        assert user["status"] == "active"
        assert user["role"] == "analyst"
