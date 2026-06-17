"""Story 73-1 (re-authored 2026-06-17; updated for Westley major M1, ADR-144
REPLACE): the ``negotiation`` and ``scandal`` confrontations (tea_and_murder) are
**Fate Contests** (``resolution_mode: contest``).

History: this file once pinned a terminal ``push`` beat (``walk_away`` /
``weather_it``) that ended the confrontation via the dial ``apply_beat`` engine on
any outcome tier (the 59-8 soft-lock fix). Westley M1 found that those dial beats
were the bleed: a contest that still advertises dial beats lets the narrator select
one and run the dial engine IN PARALLEL to the 4dF Contest engine — the layering
ADR-144 forbids. The content fix strips every dial beat from the contest defs, so a
Fate Contest now carries ZERO dial-resolution beats and resolves ONLY via the 4dF
exchange (FATE_ACTION). This file now pins that new invariant against the REAL pack.

The opposed-4dF Contest mechanics + ``fate.contest.*`` spans (and the contest
voluntary/withdraw behavior) live in ``tests/server/dispatch/test_fate_contest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResolutionMode
from sidequest.server.dispatch.confrontation import find_confrontation_def

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)

_CONTEST_CONFRONTATIONS = ["negotiation", "scandal"]

# The dial-resolution fields a beat must NOT carry on a contest def (these are the
# inputs the dial apply_beat engine reads; their presence is the bleed M1 closes).
# ``base`` defaults to 1 (always non-None) so it is checked separately, not here.
_DIAL_FIELDS = ("kind", "stat_check", "deltas", "target_tag", "resolution")


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


@pytest.mark.parametrize("ctype", _CONTEST_CONFRONTATIONS)
def test_contest_confrontation_is_contest_mode(ctype: str):
    """The negotiation/scandal defs bind the Fate Contest engine (ADR-144)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None, f"tea_and_murder must define a {ctype} confrontation"
    assert cdef.resolution_mode == ResolutionMode.contest, (
        f"{ctype} must be a Fate Contest (resolution_mode=contest); got "
        f"{cdef.resolution_mode}"
    )


@pytest.mark.parametrize("ctype", _CONTEST_CONFRONTATIONS)
def test_contest_def_carries_no_armed_dial_beats(ctype: str):
    """Westley M1 (ADR-144 REPLACE): a Fate Contest resolves via the 4dF exchange
    engine, NEVER the dial apply_beat path. Any beat it carries is a DISPLAY-ONLY
    stub for the world-tier class Abilities tab — it must NOT carry a dial-resolution
    field (kind/stat_check/deltas/...), or the narrator could select an armed dial
    beat and run the dial engine in parallel to the Contest (the bleed M1 closes).
    The ConfrontationDef loader validator enforces this; this test pins it on the
    REAL converted pack."""
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None
    for beat in cdef.beats:
        armed = [f for f in _DIAL_FIELDS if getattr(beat, f, None) is not None]
        # base defaults to 1 — only an explicit non-default magnitude is "armed".
        if beat.base != 1:
            armed.append("base")
        assert not armed, (
            f"{ctype} contest beat {beat.id!r} carries dial-resolution field(s) "
            f"{armed}; a contest beat must be a display-only stub (id + label + "
            "narrator_hint only) — the 4dF exchange resolves it, not the dial"
        )


@pytest.mark.parametrize("ctype", _CONTEST_CONFRONTATIONS)
def test_contest_def_authors_player_metric_for_the_victory_target(ctype: str):
    """The Contest victory target is seeded from player_metric.threshold
    (encounter_lifecycle); the def MUST author it (Westley minor F1, No Silent
    Fallbacks). The authored head-start (negotiation opponent starts at 1) lives on
    the metric's ``starting`` and is seeded into the tally (Westley M2)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None
    assert cdef.player_metric is not None, f"{ctype} contest must author player_metric"
    assert cdef.player_metric.threshold >= 1


def test_scandal_authored_opponent_head_start_survives():
    """Westley M2 regression: the gossip's authored head-start on the scandal
    contest moved from the dial finish-line to opponent_metric.starting — it must
    persist in content (seating seeds ContestState.opponent_victories from it)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "scandal")
    assert cdef is not None and cdef.opponent_metric is not None
    assert cdef.opponent_metric.starting == 2, (
        "the scandal contest's authored opponent head-start (starting=2) must be "
        f"preserved; got {cdef.opponent_metric.starting}"
    )
