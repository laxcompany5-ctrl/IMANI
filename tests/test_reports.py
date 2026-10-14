"""Reporting & exports tests — reports overview, CSVs, case reports, activity log (Bit 7)."""
from __future__ import annotations

from imani.models import MatchRepository

from .conftest import login
from .test_individuals import SAMPLE, make_user, register


def run_match(client, **overrides):
    data = {"title": "Probe", "height_cm": "180", "build": "athletic",
            "posture": "upright", "gait": "brisk", "min_score": "0"}
    data.update(overrides)
    return client.post("/match/", data=data, follow_redirects=False)


def latest_run_id(app):
    with app.app_context():
        return MatchRepository.recent_runs(1)[0]["id"]


# --- Reports overview -----------------------------------------------------

def test_reports_requires_login(client):
    response = client.get("/reports", follow_redirects=False)
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_viewer_cannot_see_reports(app, client):
    make_user(app, username="vic", role="viewer")
    login(client, "vic", "Passw0rd1")
    response = client.get("/reports", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_analyst_overview_shows_metrics(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)

    response = client.get("/reports")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Reports" in body
    assert "Individuals on file" in body
    assert "Match bands" in body


# --- Register CSV ---------------------------------------------------------

def test_register_csv_export(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    register(client, full_name="Mary Wanjiku", national_id="999", reference=None)

    response = client.get("/individuals/export.csv")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
    assert "attachment" in response.headers["Content-Disposition"]

    text = response.get_data(as_text=True)
    header = text.splitlines()[0]
    assert "full_name" in header
    assert "face_template" not in header  # never exported
    assert "John Kamau" in text
    assert "Mary Wanjiku" in text


def test_register_csv_honours_search(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)  # John Kamau
    register(client, full_name="Mary Wanjiku", national_id="999")

    text = client.get("/individuals/export.csv?q=Kamau").get_data(as_text=True)
    assert "John Kamau" in text
    assert "Mary Wanjiku" not in text


# --- Match CSV + case report ---------------------------------------------

def test_match_csv_export(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    run_match(client)

    run_id = latest_run_id(app)
    response = client.get(f"/match/{run_id}/export.csv")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
    text = response.get_data(as_text=True)
    assert "score" in text.splitlines()[0]
    assert "John Kamau" in text


def test_match_case_report_is_printable(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    run_match(client)

    run_id = latest_run_id(app)
    response = client.get(f"/match/{run_id}/report")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Print / Save as PDF" in body
    assert "John Kamau" in body
    assert "report.css" in body
    assert "window.print()" in body


def test_match_report_missing_run_is_404(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    assert client.get("/match/9999/report").status_code == 404
    assert client.get("/match/9999/export.csv").status_code == 404


# --- Individual profile report -------------------------------------------

def test_individual_profile_report(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)

    with app.app_context():
        from imani.models import IndividualRepository
        person = IndividualRepository.all()[0]

    response = client.get(f"/individuals/{person['id']}/report")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "John Kamau" in body
    assert "Physical traits" in body
    assert "Print / Save as PDF" in body


# --- Activity log ---------------------------------------------------------

def test_activity_is_admin_only(app, client):
    make_user(app, username="ana", role="analyst")
    login(client, "ana", "Passw0rd1")
    response = client.get("/activity", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_admin_activity_log_and_export(app, client):
    make_user(app, username="ana", role="analyst")
    login(client, "admin", "Imani@2026")

    response = client.get("/activity")
    assert response.status_code == 200
    assert "Activity log" in response.get_data(as_text=True)

    filtered = client.get("/activity?action=LOGIN")
    assert filtered.status_code == 200

    export = client.get("/activity/export.csv")
    assert export.status_code == 200
    assert export.headers["Content-Type"].startswith("text/csv")
    header = export.get_data(as_text=True).splitlines()[0]
    assert header == "created_at,action,entity,entity_id,actor,details"
