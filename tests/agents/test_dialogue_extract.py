"""Unit tests for extract_spoken_lines (MP dialogue visibility fix).

Playtest 2026-05-17 (Keith + Sebby): when a player types quoted dialogue
to an NPC in a multiplayer session, peers never see the spoken words —
only the narrator's reply. The SOUL.md Agency rule (describe the world,
not the player) correctly stops the narrator from echoing the line, and
NarrationPayload has no field for it, so the player's speech is lost on
barrier-fire (ACTION_REVEAL CLEARED wipes the wait-phase reveal).

``extract_spoken_lines`` pulls the verbatim quoted spans out of a
player's submitted action so they can be surfaced, attributed, into the
shared transcript. Pure string transform — reuses the same dialogue
regex pov_swap already uses (single-sourced via ``_split_by_dialogue``).

These tests RED until ``sidequest.agents.pov_swap.extract_spoken_lines``
exists.
"""

from __future__ import annotations

from sidequest.agents.pov_swap import extract_spoken_lines


def test_extracts_single_quoted_line_dropping_stage_direction() -> None:
    action = 'I walk up to the guard and say "Hello, traveler. What news from the north?"'
    assert extract_spoken_lines(action) == ["Hello, traveler. What news from the north?"]


def test_extracts_multiple_quoted_lines_in_order() -> None:
    action = '"Stand down," I tell him, then turn to Rux: "We move at dawn."'
    assert extract_spoken_lines(action) == ["Stand down,", "We move at dawn."]


def test_no_quotes_returns_empty() -> None:
    assert extract_spoken_lines("I search the room for a hidden lever.") == []


def test_strips_inner_whitespace_and_drops_empty_quotes() -> None:
    action = 'he gestures "   " then declares "  Onward!  "'
    assert extract_spoken_lines(action) == ["Onward!"]


def test_empty_input_returns_empty() -> None:
    assert extract_spoken_lines("") == []
