"""Matching engine tests — scoring, bands and the /match flow (Bit 6)."""
from __future__ import annotations

from imani import matching
from imani.models import IndividualRepository, MatchRepository

from .conftest import login
from .test_individuals import SAMPLE, make_user


def person(**overrides):
    base = {
        "id": 1, "full_name": "Test Person", "reference": "IM-00001",
        "height_cm": None, "build": None, "posture": None, "gait": None,
        "face_template": None,
    }
    base.update(overrides)
    return base


def probe(**overrides):
    base = {"height_cm": None, "build": None, "posture": None, "gait": None,
            "face": None}
    base.update(overrides)
    return base


# --- Scoring --------------------------------------------------------------

def test_confidence_bands():
    assert matching.band_for(90) == "HIGH"
    assert matching.band_for(85) == "HIGH"
    assert matching.band_for(84.9) == "MODERATE"
    assert matching.band_for(70) == "MODERATE"
    assert matching.band_for(60) == "LOW"
    assert matching.band_for(54.9) == "VERY LOW"


def test_exact_height_scores_100():
    result = matching.score(probe(height_cm=180), person(height_cm=180))
    assert result["score"] == 100.0
    assert result["band"] == "HIGH"
    assert result["traits_used"] == ["height_cm"]


def test_height_uses_a_tolerance():
    # 4 cm off an 8 cm tolerance → 50%.
    result = matching.score(probe(height_cm=184), person(height_cm=180))
    assert result["score"] == 50.0
    # 8 cm off → 0%.
    assert matching.score(probe(height_cm=188), person(height_cm=180))["score"] == 0.0


def test_ordinal_and_grouped_traits():
    # Adjacent build categories get half credit.
    assert matching.ordinal_similarity("average", "athletic", matching.BUILD_SCALE) == 0.5
    # Same gait group gets half credit; different groups get none.
    assert matching.gait_similarity("normal", "brisk") == 0.5
    assert matching.gait_similarity("normal", "limp") == 0.0


def test_weights_are_renormalised_for_available_traits():
    # Height matches perfectly (weight .25), build is opposite (weight .15 → 0).
    result = matching.score(
        probe(height_cm=180, build="slim"),
        person(height_cm=180, build="heavy"),
    )
    # 0.25 / (0.25 + 0.15) = 0.625 → 62.5
    assert result["score"] == 62.5
    assert result["band"] == "LOW"


def test_face_templates_compare_by_cosine():
    result = matching.score(probe(face=[1.0, 0.0, 0.0]),
                            person(face_template="1, 0, 0"))
    assert result["score"] == 100.0
    assert "face" in result["breakdown"]

    # Orthogonal vectors → no facial similarity.
    orthogonal = matching.score(probe(face=[1.0, 0.0]), person(face_template="0,1"))
    assert orthogonal["score"] == 0.0


def test_parse_template_rejects_junk():
    assert matching.parse_template("") == (None, None)
    assert matching.parse_template("0.1, 0.2")[0] == [0.1, 0.2]
    assert matching.parse_template("not,numbers")[1] is not None
    assert matching.parse_template("1")[1] is not None       # needs 2+ numbers


def test_no_comparable_traits_scores_nothing():
    assert matching.score(probe(height_cm=180), person()) is None


# --- Access control -------------------------------------------------------

def test_match_requires_login(client):
    response = client.get("/match/", follow_redirects=False)
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_viewer_cannot_run_a_match(app, client):
    make_user(app, username="vic", role="viewer")
    login(client, "vic", "Passw0rd1")
    response = client.get("/match/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


# --- Running a match ------------------------------------------------------

def test_run_match_ranks_and_persists(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    client.post("/individuals/new", data=SAMPLE)

    response = client.post(
        "/match/",
        data={"title": "Checkpoint 04", "height_cm": "180", "build": "athletic",
              "posture": "upright", "gait": "brisk", "min_score": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/match/" in response.headers["Location"]

    page = client.get(response.headers["Location"])
    assert page.status_code == 200
    assert b"John Kamau" in page.data

    with app.app_context():
        run = MatchRepository.recent_runs(1)[0]
        assert run["reference"] == "MR-00001"
        assert run["result_count"] == 1
        results = MatchRepository.results_for(run["id"])
        assert results[0]["individual_name"] == "John Kamau"
        assert results[0]["score"] == 100.0
        assert results[0]["band"] == "HIGH"


def test_match_needs_at_least_one_trait(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    response = client.post("/match/", data={"title": "Empty"})
    assert response.status_code == 400


def test_invalid_face_template_is_rejected(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    response = client.post("/match/", data={"title": "Bad face",
                                            "face_template": "a,b,c"})
    assert response.status_code == 400


def test_face_template_match_through_the_ui(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    client.post("/individuals/new", data=dict(SAMPLE, face_template="1,0,0"))
    client.get("/individuals/")  # flush the "Registered …" flash

    response = client.post(
        "/match/",
        data={"title": "Face probe", "face_template": "1,0,0", "min_score": "0"},
        follow_redirects=False,
    )
    page = client.get(response.headers["Location"])
    assert page.status_code == 200
    assert b"Face" in page.data

    with app.app_context():
        run = MatchRepository.recent_runs(1)[0]
        results = MatchRepository.results_for(run["id"])
        assert results[0]["score"] == 100.0
        assert "face" in results[0]["breakdown"]


def test_archived_individuals_are_not_matched(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    client.post("/individuals/new", data=dict(SAMPLE, status="archived"))

    client.post("/match/", data={"title": "Only active", "height_cm": "180",
                                 "build": "athletic", "posture": "upright",
                                 "gait": "brisk"}, follow_redirects=False)
    with app.app_context():
        run = MatchRepository.recent_runs(1)[0]
        assert run["result_count"] == 0


# --- History --------------------------------------------------------------

def test_history_lists_runs(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    client.post("/individuals/new", data=SAMPLE)
    client.post("/match/", data={"title": "Case 12", "height_cm": "180",
                                 "build": "athletic", "posture": "upright",
                                 "gait": "brisk"})
    client.get("/match/history")

    page = client.get("/match/history")
    assert page.status_code == 200
    assert b"MR-00001" in page.data
    assert b"Case 12" in page.data


def test_delete_run(app, client):
    make_user(app)
    login(client, "ana", "Passw0rd1")
    client.post("/individuals/new", data=SAMPLE)
    client.post("/match/", data={"title": "Doomed", "height_cm": "180",
                                 "build": "athletic", "posture": "upright",
                                 "gait": "brisk"})
    with app.app_context():
        run_id = MatchRepository.recent_runs(1)[0]["id"]

    response = client.post(f"/match/{run_id}/delete", follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        assert MatchRepository.get_run(run_id) is None
