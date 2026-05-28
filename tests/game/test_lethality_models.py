from __future__ import annotations

from sidequest.game.lethality import DownedResult, LethalityResult


def test_lethality_result_passthrough_shape():
    r = LethalityResult(
        base_total=7, final_total=7, traumatic=False, trauma_roll=0, trauma_target=6
    )
    assert r.base_total == 7
    assert r.final_total == 7
    assert r.traumatic is False


def test_lethality_result_traumatic_multiplies():
    r = LethalityResult(
        base_total=7, final_total=21, traumatic=True, trauma_roll=6, trauma_target=6
    )
    assert r.final_total == 21
    assert r.traumatic is True


def test_downed_result_mortal_only():
    r = DownedResult(mortal=True, major=False, major_roll=0, major_text="", save_made=True)
    assert r.mortal is True
    assert r.major is False


def test_downed_result_major_injury():
    r = DownedResult(
        mortal=True, major=True, major_roll=12, major_text="Instant death.", save_made=False
    )
    assert r.major is True
    assert r.major_roll == 12
    assert "death" in r.major_text.lower()
