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
    with pytest.raises(ValueError, match=r"unknown authored key 'X'"):
        OceanProfile.from_authored({"X": 0.5})


def test_from_authored_out_of_range_high_raises():
    # authored 0..1 contract — 1.5 must not be silently clamped to 10.0
    with pytest.raises(ValueError, match=r"key 'O'.*expected 0\.\.1"):
        OceanProfile.from_authored({"O": 1.5})


def test_from_authored_out_of_range_low_raises():
    # authored 0..1 contract — -0.2 must not be silently clamped to 0.0
    with pytest.raises(ValueError, match=r"key 'O'.*expected 0\.\.1"):
        OceanProfile.from_authored({"O": -0.2})


def test_from_authored_empty_dict_all_center():
    p = OceanProfile.from_authored({})
    assert p.openness == 5.0
    assert p.conscientiousness == 5.0
    assert p.extraversion == 5.0
    assert p.agreeableness == 5.0
    assert p.neuroticism == 5.0


def test_from_authored_full_dict_round_trips():
    p = OceanProfile.from_authored({"O": 0.0, "C": 0.25, "E": 0.5, "A": 0.75, "N": 1.0})
    assert p.openness == 0.0
    assert p.conscientiousness == 2.5
    assert p.extraversion == 5.0
    assert p.agreeableness == 7.5
    assert p.neuroticism == 10.0
