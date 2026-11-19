"""Image features and prompt matching (Bit 13)."""
from __future__ import annotations

from io import BytesIO

from PIL import Image

from imani import features


def jpeg(colour, size=(80, 60)):
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


RED = jpeg((210, 20, 20))
BLUE = jpeg((20, 20, 210))
WHITE = jpeg((245, 245, 245))


def test_extract_features_reads_colours():
    record = features.extract_features(RED)
    assert record is not None
    assert record["colours"]["red"] > 0.9
    assert record["colours"]["blue"] < 0.1
    assert record["dominant"][0]["ratio"] > 0.9
    assert len(record["hist"]) == 108


def test_description_match_scores_colour_presence():
    record = features.extract_features(RED)
    high, high_detail = features.description_match("man in a red jacket", record)
    low, low_detail = features.description_match("man in a blue jacket", record)
    assert high > 70
    assert low == 0.0
    assert high_detail["colour_presence"] > 0.9
    assert low_detail["colour_presence"] == 0.0


def test_description_match_uses_synonyms_and_keywords():
    record = features.extract_features(RED)
    score, detail = features.description_match(
        "person wearing a maroon coat", record, "maroon coat",
    )
    assert detail["colours"] == ["red"]
    assert score > 40


def test_description_without_colour_falls_back_to_keywords():
    record = features.extract_features(RED)
    matched, _ = features.description_match("gate entrance", record, "Main gate entrance")
    missing, _ = features.description_match("car park", record, "Main gate entrance")
    assert matched > 0
    assert missing == 0.0


def test_image_similarity_is_deterministic():
    red = features.extract_features(RED)
    blue = features.extract_features(BLUE)
    same, _ = features.image_similarity(red, red)
    different, _ = features.image_similarity(red, blue)
    assert same == 100.0
    assert different == 0.0


def test_extract_features_rejects_garbage():
    assert features.extract_features(b"not an image") is None


def test_traits_in_parses_physical_features():
    parsed = features.traits_in(
        "tall athletic man, about 182 cm, limping with a stooped posture"
    )
    assert parsed["height_cm"] == 182.0
    assert parsed["build"] == "athletic"
    assert parsed["gait"] == "limp"
    assert parsed["posture"] == "stooped"


def test_traits_in_uses_height_hints_without_overriding_build():
    parsed = features.traits_in("short slim person")
    assert parsed["height_cm"] == 160.0
    assert parsed["build"] == "slim"
    average = features.traits_in("person of average height")
    assert average["height_cm"] == 170.0
    assert "build" not in average


def test_keyword_score_normalises_trait_synonyms():
    assert features.keyword_score("limping man", "gait: limp") == 1.0
    assert features.keyword_score("navy jacket", "blue jacket") == 1.0
