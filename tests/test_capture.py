"""CCTV capture ingestion & re-identification tests (Bit 8)."""
from __future__ import annotations

from imani import capture
from imani.models import (
    IndividualRepository,
    ProfileRepository,
    SightingRepository,
)

from .conftest import login
from .test_individuals import make_user, register


CAPTURE = {
    "camera": "gate-3",
    "height_cm": 180,
    "build": "athletic",
    "posture": "upright",
    "gait": "brisk",
}


def post_capture(client, key="secret-key", **overrides):
    payload = dict(CAPTURE, **overrides)
    return client.post("/ingest/capture", json=payload,
                       headers={"X-API-Key": key})


def ingest(app, **overrides):
    payload = dict(CAPTURE, **overrides)
    with app.app_context():
        return capture.ingest(payload)


def active(app):
    with app.app_context():
        return [dict(row) for row in IndividualRepository.all(status="active")]


# --- Normalisation --------------------------------------------------------

def test_height_out_of_range_is_rejected(app):
    with app.app_context():
        _, problems = capture.normalise_traits({"height_cm": 400})
    assert problems


def test_unknown_enum_is_rejected(app):
    with app.app_context():
        _, problems = capture.normalise_traits({"build": "banana"})
    assert problems


def test_face_template_list_is_accepted(app):
    with app.app_context():
        traits, problems = capture.normalise_traits(
            {"face_template": [0.1, 0.2, 0.3]}
        )
    assert problems == []
    assert traits["face_template"] == "0.1, 0.2, 0.3"
    assert traits["face"] == [0.1, 0.2, 0.3]


# --- Identity resolution --------------------------------------------------

def test_unknown_capture_is_new(app):
    with app.app_context():
        best, ranked, status = capture.identify(
            capture.probe_from(capture.normalise_traits(CAPTURE)[0])
        )
    assert best is None
    assert status == capture.NEW


def test_confident_capture_is_identified(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)  # height 180, athletic / upright / brisk

    with app.app_context():
        traits, _ = capture.normalise_traits(CAPTURE)
        best, _, status = capture.identify(capture.probe_from(traits))
    assert status == capture.IDENTIFIED
    assert best["score"] >= 85


def test_borderline_capture_needs_review(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)  # height 180

    with app.app_context():
        # A probe two centimetres off scores 75 — inside the review band.
        traits, _ = capture.normalise_traits({"height_cm": 182})
        best, _, status = capture.identify(capture.probe_from(traits))
    assert status == capture.REVIEW
    assert 70 <= best["score"] < 85


# --- Profile learning -----------------------------------------------------

def test_profile_learns_height_mean(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)

    first = ingest(app, height_cm=180)
    person_id = first["individual"]["id"]
    ingest(app, height_cm=184)

    with app.app_context():
        profile = ProfileRepository.get(person_id)
    assert profile["sample_count"] == 2
    assert 180 <= profile["height_mean"] <= 184


def test_profile_face_gallery_is_capped(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)

    first = ingest(app, height_cm=180)
    person_id = first["individual"]["id"]

    with app.app_context():
        for index in range(10):
            vector = [0.0] * index + [1.0] + [0.0] * (10 - index)
            ProfileRepository.upsert(
                person_id, {"face_template": ", ".join(map(str, vector))},
                max_faces=3,
            )
        profile = ProfileRepository.get(person_id)
    import json
    assert len(json.loads(profile["face_templates"])) <= 3


# --- Ingestion API --------------------------------------------------------

def test_ingest_api_disabled_without_key(app, client):
    app.config["INGEST_API_KEY"] = ""
    assert post_capture(client).status_code == 403


def test_ingest_api_rejects_wrong_key(app, client):
    app.config["INGEST_API_KEY"] = "secret-key"
    assert post_capture(client, key="nope").status_code == 403


def test_ingest_api_enrols_a_new_person(app, client):
    app.config["INGEST_API_KEY"] = "secret-key"
    response = post_capture(client)
    assert response.status_code == 201

    body = response.get_json()
    assert body["status"] == "new"
    assert body["created_person"] is True
    assert body["individual"]["name"].startswith("Unknown")

    with app.app_context():
        person = IndividualRepository.get(body["individual"]["id"])
    assert person["source"] == "cctv"
    assert person["height_cm"] == 180


def test_returning_person_is_attached_not_duplicated(app, client):
    app.config["INGEST_API_KEY"] = "secret-key"

    first = post_capture(client).get_json()
    second = post_capture(client).get_json()

    assert second["status"] == "identified"
    assert second["created_person"] is False
    assert second["individual"]["id"] == first["individual"]["id"]

    # Only one person was ever created.
    assert len(active(app)) == 1

    with app.app_context():
        rows, total = SightingRepository.search()
    assert total == 2
    assert all(row["individual_id"] == first["individual"]["id"] for row in rows)


def test_ingest_api_rejects_invalid_payload(app, client):
    app.config["INGEST_API_KEY"] = "secret-key"
    response = post_capture(client, height_cm=999)
    assert response.status_code == 400
    assert response.get_json()["problems"]


# --- Sightings console ----------------------------------------------------

def test_sightings_requires_login(client):
    response = client.get("/sightings/", follow_redirects=False)
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_analyst_can_browse_sightings(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    result = ingest(app)

    listing = client.get("/sightings/")
    assert listing.status_code == 200
    assert result["reference"] in listing.get_data(as_text=True)

    detail = client.get(f"/sightings/{result['sighting_id']}")
    assert detail.status_code == 200
    assert "Captured traits" in detail.get_data(as_text=True)


def test_viewer_cannot_open_review_queue(app, client):
    make_user(app, username="vic", role="viewer")
    login(client, "vic", "Passw0rd1")
    response = client.get("/sightings/review", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_review_queue_lists_tentative_matches(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)  # height 180
    result = ingest(app, height_cm=182, build=None, posture=None, gait=None)
    assert result["status"] == "review"

    response = client.get("/sightings/review")
    assert response.status_code == 200
    assert result["reference"] in response.get_data(as_text=True)


def test_confirm_resolves_a_capture(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    result = ingest(app, height_cm=182, build=None, posture=None, gait=None)

    client.post(f"/sightings/{result['sighting_id']}/confirm")
    with app.app_context():
        assert SightingRepository.get(result["sighting_id"])["resolved"] == 1


def test_reassign_moves_a_capture(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)  # John Kamau
    result = ingest(app)

    # A second, unrelated person to move the capture onto.
    register(client, full_name="Mary Wanjiku", national_id="999")
    target = [p for p in active(app) if p["full_name"] == "Mary Wanjiku"][0]

    client.post(f"/sightings/{result['sighting_id']}/reassign",
                data={"individual_id": target["id"]})
    with app.app_context():
        assert SightingRepository.get(result["sighting_id"])["individual_id"] == target["id"]


def test_enrol_splits_a_capture_into_a_new_person(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    result = ingest(app)  # attached to John Kamau
    before = len(active(app))

    response = client.post(f"/sightings/{result['sighting_id']}/enroll",
                           follow_redirects=False)
    assert response.status_code == 302
    assert len(active(app)) == before + 1


def test_delete_removes_a_capture(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    register(client)
    result = ingest(app)

    client.post(f"/sightings/{result['sighting_id']}/delete")
    with app.app_context():
        assert SightingRepository.get(result["sighting_id"]) is None


def test_simulate_form_posts_a_capture(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")

    response = client.post("/sightings/simulate", data=dict(CAPTURE),
                           follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        assert SightingRepository.count() == 1
