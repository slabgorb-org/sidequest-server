import pytest

from sidequest.genre.models.ocean import OceanProfile


def test_from_authored_short_keys_scaled():
    # authored 0..1 short keys → runtime 0..10 full keys
    p = OceanProfile.from_authored({"O": 0.5, "C": 0.7, "E": 0.4, "A": 0.5, "N": 0.4})
    assert p.openness == 5.0
    assert p.conscientiousness == 7.0
    assert p.extraversion == 4.0
    assert p.agreeableness == 5.0
    assert p.neuroticism == 4.0


def test_from_authored_missing_dimension_defaults_to_center():
    p = OceanProfile.from_authored({"O": 1.0})
    assert p.openness == 10.0
    assert p.conscientiousness == 5.0  # default center


def test_from_authored_unknown_key_raises():
    with pytest.raises(ValueError):
        OceanProfile.from_authored({"X": 0.5})
