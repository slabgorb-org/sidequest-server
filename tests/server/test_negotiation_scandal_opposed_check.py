"""Story 73-1: generalize the 59-8 social_duel opposed_check fix to its two
drawing-room siblings — ``negotiation`` (Polite Negotiation) and ``scandal``
(Scandal Eruption) in the ``tea_and_murder`` pack.

Both ran the default ``beat_selection`` resolution mode: only the PLAYER rolled
a d20, and the opponent's dial moved solely via the narrator's
``advance_confrontation`` write tool. That is the same structural unfairness 59-8
found in ``social_duel`` — a Conflict (two-sided contest) resolved by a Challenge
resolver (one side rolls) — with the same two consequences: the opponent dial can
freeze, and the GM panel sees "Claude said delta=N" instead of an honest opposed
roll.

This file pins the CONTENT shape and the lifecycle seating contract (mirroring
``tests/server/test_glenross_social_duel_opposed_check.py``):

* ``resolution_mode: opposed_check`` + ``opponent_default_stats`` (≤10) covering
  every stat the beats roll;
* the ADR-093 calibration (both thresholds == 7), including ``scandal``'s
  deliberate asymmetric-dial recalibration (head start moves from the *finish
  line* to the *start line*);
* the terminal always-resolves ``push`` flag (``scandal.weather_it``);
* the Other is seated ``opponent``-side, and a no-Other instantiation fails loud
  (ADR-116).

The opposed dice mechanics + the ``encounter.opposed_roll_resolved`` OTEL span
are exercised in ``tests/integration/test_negotiation_scandal_resolve.py`` (the
``otel_capture`` fixture lives under ``tests/integration``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResolutionMode
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


# ── AC-1 / AC-2: content shape — opposed_check + opponent_default_stats ──────


def test_negotiation_is_opposed_check_with_opponent_stats():
    """AC-1: negotiation declares ``opposed_check`` and carries
    ``opponent_default_stats`` covering every stat its beats roll
    (Cunning/Nerve). Without those stats ``resolve_opponent_modifier`` fails
    loud on the opponent's roll (No Silent Fallbacks)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "negotiation")
    assert cdef is not None
    assert cdef.resolution_mode == ResolutionMode.opposed_check, (
        "negotiation must convert to opposed_check (was beat_selection — only "
        "the player rolled, the opponent dial froze)"
    )
    stats = cdef.opponent_default_stats or {}
    beat_stats = {b.stat_check for b in cdef.beats}
    missing = beat_stats - set(stats)
    assert not missing, (
        f"opponent_default_stats missing {missing} — the opponent rolls these "
        f"and resolve_opponent_modifier fails loud without them"
    )


def test_scandal_is_opposed_check_with_opponent_stats():
    """AC-2: scandal declares ``opposed_check`` and carries
    ``opponent_default_stats`` covering every stat its beats roll
    (Cunning/Pride/Nerve)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "scandal")
    assert cdef is not None
    assert cdef.resolution_mode == ResolutionMode.opposed_check, (
        "scandal must convert to opposed_check"
    )
    stats = cdef.opponent_default_stats or {}
    beat_stats = {b.stat_check for b in cdef.beats}
    missing = beat_stats - set(stats)
    assert not missing, (
        f"opponent_default_stats missing {missing} — resolve_opponent_modifier "
        f"fails loud without them"
    )


def test_negotiation_opponent_default_stats_within_adr093_ceiling():
    """AC-3: every opponent_default_stats value is <= 10 (ADR-093 parity
    ceiling — challenge comes from dial/DC geometry, not stat inflation)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "negotiation")
    stats = cdef.opponent_default_stats or {}
    assert stats, "negotiation opposed_check needs opponent_default_stats"
    offending = {k: v for k, v in stats.items() if isinstance(v, int) and v > 10}
    assert not offending, f"opponent_default_stats above the ADR-093 ceiling: {offending}"


def test_scandal_opponent_default_stats_within_adr093_ceiling():
    """AC-3: scandal opponent_default_stats values are all <= 10."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "scandal")
    stats = cdef.opponent_default_stats or {}
    assert stats, "scandal opposed_check needs opponent_default_stats"
    offending = {k: v for k, v in stats.items() if isinstance(v, int) and v > 10}
    assert not offending, f"opponent_default_stats above the ADR-093 ceiling: {offending}"


# ── AC-1 / AC-3: thresholds calibrated to 7/7 ───────────────────────────────


def test_negotiation_thresholds_calibrated_to_7():
    """AC-1/AC-3: negotiation keeps both metric thresholds at 7 (ADR-093
    opposed_check calibration)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "negotiation")
    assert cdef.player_metric.threshold == 7
    assert cdef.opponent_metric.threshold == 7


def test_scandal_thresholds_recalibrated_to_7_7():
    """AC-2 (the central design decision): scandal's dials were asymmetric —
    player ``containment`` threshold 5, opponent ``exposure`` threshold 8 (a
    deliberate fiction: the gossip has a head start). But ADR-093 v1 owns the
    opposed_check threshold: both must be 7 (the calibrated tie band assumes
    symmetric finish lines, enforced by ``test_confrontation_calibration.py``).
    Asymmetric thresholds are explicit ADR-093 v2 territory. So both move to 7
    and the head start is preserved by the start-line geometry instead — see
    ``test_scandal_exposure_keeps_head_start``."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "scandal")
    assert cdef.player_metric.threshold == 7, "containment threshold must recalibrate 5 → 7"
    assert cdef.opponent_metric.threshold == 7, "exposure threshold must recalibrate 8 → 7"


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
    """AC-6: an ``opposed_check`` negotiation dispatched with npcs_present=[]
    seats the location-roster Other as ``side='opponent'`` (pre-conversion it
    was a beat_selection social, so the Other was seated ``neutral`` and its
    dial could never advance)."""
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
        f"opposed_check Other must be seated opponent-side; got {sides}"
    )


def test_scandal_seats_other_as_opponent_via_location_fallback():
    """AC-6: same opponent-seating contract for scandal."""
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
        f"opposed_check Other must be seated opponent-side; got {sides}"
    )


def test_negotiation_with_no_other_fails_loud():
    """AC-6: an opposed negotiation with nobody on the other side cannot resolve
    — instantiating with no Other (empty npcs_present, empty location roster)
    must fail loud rather than seat a one-sided negotiation (No Silent
    Fallbacks / ADR-116)."""
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
    """AC-6: same no-Other fail-loud contract for scandal."""
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
