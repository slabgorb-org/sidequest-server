"""Regression — repurposed for Westley major M1 (ADR-144 REPLACE).

History: this file pinned the tea_and_murder ``social_duel`` ``concede`` push beat
resolving via the dial ``apply_beat`` engine on any outcome tier (the 59-8 Glenross
soft-lock fix — a failed concession used to leave the player locked in a Duel of
Wits the fiction had closed).

Westley M1 found that a contest still advertising dial beats (incl. a
``resolution: true`` push) is the bleed: the narrator can select one and run the
dial engine IN PARALLEL to the 4dF Contest engine, the layering ADR-144 forbids.
``social_duel`` is now a Fate Contest, so ``concede`` survives only as a
display-only stub id (for the world-class Abilities tab) with no dial-resolution
field. The dial no longer resolves it.

NB — the voluntary-exit / soft-lock concern this file originally guarded is REAL
and now belongs to the Contest engine (a withdraw/concede path in
``fate_contest``), not a dial push beat. That the converted contests have no
explicit player-driven concede is flagged as a Delivery Finding for the reviewer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResolutionMode
from sidequest.server.dispatch.confrontation import find_confrontation_def

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)


def _social_duel_cdef():
    pack = load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")
    cdef = find_confrontation_def(pack.rules.confrontations, "social_duel")
    assert cdef is not None, "tea_and_murder must define a social_duel confrontation"
    return cdef


def test_social_duel_is_a_fate_contest():
    """social_duel binds the Fate Contest engine, not the dial (ADR-144)."""
    assert _social_duel_cdef().resolution_mode == ResolutionMode.contest


def test_concede_survives_as_a_display_only_stub_not_a_dial_resolver():
    """Westley M1: ``concede`` keeps its id (the world-class Abilities tab references
    it) but is now a display-only stub — NO ``resolution: true`` (which would resolve
    the contest via the dial), no ``kind``, no ``stat_check``. The dial must never
    resolve a Fate Contest (ADR-144 REPLACE)."""
    cdef = _social_duel_cdef()
    concede = next((b for b in cdef.beats if b.id == "concede"), None)
    assert concede is not None, "social_duel keeps concede as a display-only stub id"
    assert concede.resolution is None, (
        "a contest beat must NOT carry resolution: true — that resolves the contest "
        "via the dial apply_beat engine, the layering ADR-144 forbids"
    )
    assert concede.kind is None and concede.stat_check is None
