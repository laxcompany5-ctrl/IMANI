"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from imani import create_app
from imani.auth import ensure_seed_admin
from imani.config import TestConfig
from imani.db import init_db


@pytest.fixture
def app(tmp_path):
    class TestingConfig(TestConfig):
        # A file-backed DB so every request shares one store.
        DATABASE = str(tmp_path / "test.sqlite3")

    application = create_app(TestingConfig)
    with application.app_context():
        init_db()
        ensure_seed_admin("admin", "Imani@2026", email="admin@imani.local")
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


def signup(client, username="jane", email="jane@imani.local",
           password="Passw0rd1", role="analyst"):
    return client.post(
        "/auth/signup",
        data={
            "full_name": "Jane Wanjiru",
            "username": username,
            "email": email,
            "requested_role": role,
            "password": password,
            "confirm_password": password,
        },
        follow_redirects=False,
    )


def login(client, identifier="admin", password="Imani@2026"):
    return client.post(
        "/auth/login",
        data={"identifier": identifier, "password": password},
        follow_redirects=False,
    )
