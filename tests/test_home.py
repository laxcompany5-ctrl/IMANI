"""Public home (landing) page tests — Bit 4."""
from __future__ import annotations

from .conftest import login


def test_landing_page_is_public_and_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    # Hero + call to action for guests.
    assert b"Request access" in response.data
    assert b"Sign in" in response.data
    # Key sections are present.
    assert b'id="features"' in response.data
    assert b'id="how"' in response.data
    assert b'id="security"' in response.data


def test_landing_shows_console_link_when_signed_in(client):
    login(client, "admin", "Imani@2026")
    response = client.get("/")
    assert response.status_code == 200
    assert b"Open console" in response.data


def test_dashboard_lives_at_dashboard(client):
    login(client, "admin", "Imani@2026")
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"Dashboard" in response.data or b"Welcome" in response.data
