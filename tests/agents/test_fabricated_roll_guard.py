"""Unit tests for the fabricated-roll detector (sq-playtest 2026-06-13)."""

from __future__ import annotations

import pytest

from sidequest.agents.fabricated_roll_guard import detect_fabricated_roll


def test_detects_the_reported_reprisal_leak() -> None:
    # The exact beneath_sunden leak: a missed opponent reprisal narrated as a
    # mechanical summary with an invented die (engine rolled 4, prose said 3).
    narration = "The roll of 3 misses. No damage to you.\n\n---\n\nThe axe swings wide."
    assert detect_fabricated_roll(narration, roll_tool_fired=False) == "roll of 3"


@pytest.mark.parametrize(
    "narration",
    [
        "You rolled a 14 and the blade bites home.",
        "Its attack comes in at vs AC and glances off.",
        "The blow lands — AC 10 was not enough to stop it.",
        "A 6 to-hit, and the spear skitters past.",
        "The d20 tumbles: a hit.",
        "A natural 20 — the strike is perfect.",
        "He rolled 17 to hit and the mace connects.",
    ],
)
def test_detects_mechanical_citation_variants(narration: str) -> None:
    assert detect_fabricated_roll(narration, roll_tool_fired=False) is not None


@pytest.mark.parametrize(
    "narration",
    [
        # Clean miss prose — the desired output for the reprisal case.
        "The thing isn't there — the iron point scrapes stone. It is still up, still close.",
        # Ordinary fiction that merely contains the word "roll".
        "He rolled to the side as the boulder crashed down.",
        "She rolled her eyes and turned away.",
        "Thunder rolled across the broken sky.",
        "",
    ],
)
def test_clean_prose_does_not_trip_the_guard(narration: str) -> None:
    assert detect_fabricated_roll(narration, roll_tool_fired=False) is None


def test_roll_tool_fired_suppresses_detection() -> None:
    # When a real die was rolled this turn, a number may be legitimate — stand
    # down rather than false-positive on an honest dice turn.
    narration = "The roll of 3 misses. No damage to you."
    assert detect_fabricated_roll(narration, roll_tool_fired=True) is None
