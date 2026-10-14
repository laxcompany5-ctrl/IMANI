"""Individual register tests — search / insert / update / delete (Bit 5)."""
from __future__ import annotations

from imani import auth
from imani.models import AuditRepository, IndividualRepository, UserRepository

from .conftest import login


def make_user(app, username="ana", role="analyst", password="Passw0rd1",
              status="active"):
    """Create an account directly with a known password and role."""
    with app.app_context():
        UserRepository.create(
            username, f"{username}@imani.local", "Ana Analyst",
            auth.hash_password(password), role=role, status=status,
        )


SAMPLE = {
    "full_name": "John Kamau",
    "alias": "J",
    "date_of_birth": "1990-05-04",
    "gender": "male",
    "national_id": "12345678",
    "phone": "0712345678",
    "email": "john@example.com",
    "nationality": "Kenyan",
    "address": "Nairobi",
    "height_cm": "180",
    "build": "athletic",
    "posture": "upright",
    "gait": "brisk",
    "distinguishing_marks": "scar on left arm",
    "notes": "case 12",
    "status": "active",
}


def register(client, **overrides):
    data = dict(SAMPLE, **overrides)
    return client.post("/individuals/new", data=data, follow_redirects=False)


# --- Access control -------------------------------------------------------

def test_register_requires_login(client):
    response = client.get("/individuals/", follow_redirects=False)
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_viewer_can_search_but_not_mutate(app, client):
    make_user(app, username="vic", role="viewer")
    login(client, "vic", "Passw0rd1")

    assert client.get("/individuals/").status_code == 200

    # Creating and other mutations are analyst-only.
    assert client.get("/individuals/new", follow_redirects=False).status_code == 302
    assert register(client).headers["Location"].endswith("/dashboard")


# --- Insert ---------------------------------------------------------------

def test_analyst_can_register_an_individual(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")

    response = register(client)
    assert response.status_code == 302
    assert "/individuals/" in response.headers["Location"]

    with app.app_context():
        person = IndividualRepository.get_by_national_id("12345678")
        assert person["full_name"] == "John Kamau"
        assert person["height_cm"] == 180
        assert person["reference"] == f"IM-{person['id']:05d}"
        assert person["created_by"] == "ana"
        assert AuditRepository.for_entity("individuals", person["id"])[0]["action"] == "INSERT"


def test_create_requires_a_name(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    response = register(client, full_name="")
    assert response.status_code == 400


def test_invalid_height_is_rejected(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    response = register(client, height_cm="400")
    assert response.status_code == 400


def test_duplicate_national_id_is_rejected(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    response = register(client, full_name="Someone Else")
    assert response.status_code == 400


# --- Search / read --------------------------------------------------------

def test_search_matches_name_and_filters(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    register(client, full_name="Mary Wanjiku", national_id="999", build="slim",
             email="mary@example.com", status="archived")

    # Flush the queued "Registered …" flashes so they don't appear in results.
    client.get("/individuals/")

    listing = client.get("/individuals/?q=Kamau")
    assert b"John Kamau" in listing.data
    assert b"Mary Wanjiku" not in listing.data

    # Reference search.
    with app.app_context():
        ref = IndividualRepository.get_by_national_id("12345678")["reference"]
    assert ref.encode() in client.get(f"/individuals/?q={ref}").data

    # Status filter.
    archived = client.get("/individuals/?status=archived")
    assert b"Mary Wanjiku" in archived.data
    assert b"John Kamau" not in archived.data


# --- Update ---------------------------------------------------------------

def test_analyst_can_update_and_it_is_audited(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)

    with app.app_context():
        person = IndividualRepository.get_by_national_id("12345678")
        person_id = person["id"]

    response = client.post(
        f"/individuals/{person_id}/edit",
        data=dict(SAMPLE, full_name="John K. Kamau", build="heavy"),
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        person = IndividualRepository.get(person_id)
        assert person["full_name"] == "John K. Kamau"
        assert person["build"] == "heavy"
        assert person["updated_by"] == "ana"
        actions = [row["action"] for row in
                   AuditRepository.for_entity("individuals", person_id)]
        assert "UPDATE" in actions


# --- Delete ---------------------------------------------------------------

def test_analyst_can_delete(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)

    with app.app_context():
        person_id = IndividualRepository.get_by_national_id("12345678")["id"]

    response = client.post(f"/individuals/{person_id}/delete", follow_redirects=False)
    assert response.status_code == 302

    with app.app_context():
        assert IndividualRepository.get(person_id) is None
        assert AuditRepository.for_entity("individuals", person_id)[0]["action"] == "DELETE"


# --- Detail ---------------------------------------------------------------

def test_detail_404_for_missing_record(client):
    login(client, "admin", "Imani@2026")
    assert client.get("/individuals/9999").status_code == 404


def test_viewer_can_view_a_record(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    with app.app_context():
        person_id = IndividualRepository.get_by_national_id("12345678")["id"]
    client.post("/auth/logout")

    make_user(app, username="vic", role="viewer")
    login(client, "vic", "Passw0rd1")
    detail = client.get(f"/individuals/{person_id}")
    assert detail.status_code == 200
    assert b"John Kamau" in detail.data
