"""Story 73-1 (re-authored 2026-06-17, Fate Contest binding): ``negotiation``
(Polite Negotiation) and ``scandal`` (Scandal Eruption) in the ``tea_and_murder``
pack are now **Fate Contests** (``resolution_mode: contest``), not the
``opposed_check`` dial contests this file originally pinned. ADR-144's Fate Core
binding owns the drawing-room confrontations: opposed 4dF, first to N victories,
no homebrew dial to calibrate. The opposed-check d20 mechanics (and the
``opponent_default_stats`` ≤10 ceiling, the 7/7 dial calibration) are gone —
those ceiling/threshold/opposed-mode content tests were deleted with the
conversion (ADR-093 ceiling coverage survives via
``tests/genre/test_confrontation_calibration.py``).

What this file still pins is the lifecycle seating contract that survives the
conversion (mirroring ``tests/server/test_glenross_social_duel_opposed_check.py``):

* the terminal always-resolves ``push`` flag (``scandal.weather_it`` /
  ``negotiation.walk_away``);
* the Other is seated ``opponent``-side AND ``encounter.contest`` is stamped (the
  Fate Contest path was taken), and a no-Other instantiation fails loud (ADR-116 —
  a contest with nobody on the other side cannot resolve).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.encounter import ContestState
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    instantiate_encounter_from_trigger,
)

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


def _make_npc(name: str, location: str):
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.session import Npc

    return Npc(
        core=CreatureCore(
            name=name,
            description="A sharp-tongued member of the village.",
            personality="Proud, watchful.",
            level=1,
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        last_seen_location=location,
        last_seen_turn=1,
    )


# ── AC-2: start-line geometry survives the Fate Contest conversion ──────────


def test_scandal_exposure_keeps_head_start():
    """AC-2: the gossip's head start survives the 7/7 recalibration by moving
    into the *starting* values — exposure (opponent) starts ahead of containment
    (player). The asymmetry shifts from unequal finish lines to unequal start
    lines, keeping the gothic fiction intact while honoring the symmetric-7
    calibration the opposed_check resolver and guardrail require."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "scandal")
    assert cdef.opponent_metric.starting > cdef.player_metric.starting, (
        "exposure must start ahead of containment so the scandal's head start "
        "is preserved as a start-line asymmetry after the 7/7 recalibration"
    )


# ── AC-4: terminal push always resolves (no frozen-dial soft-lock) ──────────


def test_scandal_weather_it_always_resolves():
    """AC-4: scandal's terminal ``weather_it`` push must carry
    ``resolution: true`` so the voluntary exit ends the confrontation on ANY
    outcome tier (closes the 59-8 soft-lock class). The resolution *behavior* is
    asserted end-to-end in the integration file; this pins the authored flag."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "scandal")
    weather_it = next((b for b in cdef.beats if b.id == "weather_it"), None)
    assert weather_it is not None, "scandal must keep its weather_it push beat"
    assert weather_it.resolution is True, (
        "weather_it must declare resolution: true so the storm always resolves the scandal"
    )


def test_negotiation_walk_away_always_resolves():
    """AC-4 (regression guard): negotiation's ``walk_away`` push already carries
    ``resolution: true``; the conversion must not drop it."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "negotiation")
    walk_away = next((b for b in cdef.beats if b.id == "walk_away"), None)
    assert walk_away is not None, "negotiation must keep its walk_away push beat"
    assert walk_away.resolution is True, "walk_away must stay resolution: true"


# ── AC-6: opponent seating + no-Other fail-loud (ADR-116) ───────────────────


def test_negotiation_seats_other_as_opponent_via_location_fallback():
    """AC-6: a Fate Contest negotiation dispatched with npcs_present=[] seats the
    location-roster Other as ``side='opponent'`` AND stamps ``encounter.contest``
    (proof the contest path was taken — a contest also requires an opponent-side
    Other so the 4dF can roll both sides)."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.character_locations["Inspector Pryce"] = "Castle Ross — The Drawing Room"
    snap.npcs.append(_make_npc("Mrs. Galbraith", "Castle Ross — The Drawing Room"))

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="negotiation",
        player_name="Inspector Pryce",
        npcs_present=[],
        genre_slug="tea_and_murder",
    )
    assert enc is not None
    sides = {a.name: a.side for a in enc.actors}
    assert sides.get("Inspector Pryce") == "player"
    assert sides.get("Mrs. Galbraith") == "opponent", (
        f"contest Other must be seated opponent-side; got {sides}"
    )
    assert isinstance(enc.contest, ContestState)
    assert enc.contest is not None, "the contest path must stamp encounter.contest"


def test_scandal_seats_other_as_opponent_via_location_fallback():
    """AC-6: same opponent-seating + contest-stamp contract for scandal."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.character_locations["Inspector Pryce"] = "The Parish Tea Room"
    snap.npcs.append(_make_npc("Miss Crane", "The Parish Tea Room"))

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="scandal",
        player_name="Inspector Pryce",
        npcs_present=[],
        genre_slug="tea_and_murder",
    )
    assert enc is not None
    sides = {a.name: a.side for a in enc.actors}
    assert sides.get("Inspector Pryce") == "player"
    assert sides.get("Miss Crane") == "opponent", (
        f"contest Other must be seated opponent-side; got {sides}"
    )
    assert isinstance(enc.contest, ContestState)
    assert enc.contest is not None, "the contest path must stamp encounter.contest"


def test_negotiation_with_no_other_fails_loud():
    """AC-6 (contest path): a Fate Contest negotiation with nobody on the other
    side cannot resolve — instantiating with no Other (empty npcs_present, empty
    location roster) must fail loud rather than seat a one-sided negotiation. A
    contest rolls BOTH sides each exchange, so the Other is mandatory (No Silent
    Fallbacks / ADR-116; ``_requires_opponent`` now covers ``contest`` mode)."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.character_locations["Inspector Pryce"] = "The Glen Road — Afternoon"
    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=_pack(),
            encounter_type="negotiation",
            player_name="Inspector Pryce",
            npcs_present=[],
            genre_slug="tea_and_murder",
        )


def test_scandal_with_no_other_fails_loud():
    """AC-6 (contest path): same no-Other fail-loud contract for scandal."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.character_locations["Inspector Pryce"] = "The Glen Road — Afternoon"
    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=_pack(),
            encounter_type="scandal",
            player_name="Inspector Pryce",
            npcs_present=[],
            genre_slug="tea_and_murder",
        )
