"""Regression (re-authored 2026-06-17, Fate Contest binding): tea_and_murder
``social_duel`` is now a **Fate Contest** (``resolution_mode: contest``, ADR-144)
— not the ``opposed_check`` dial duel this file originally pinned. The Fate Core
binding owns the Duel of Wits: opposed 4dF, first to N victories.

What survives the conversion (and what this file still pins): the lifecycle
seating contract. ``_requires_opponent(cdef)`` now seats a ``contest``-mode (as
well as an ``opposed_check``) confrontation's location-fallback Other as
``opponent`` (and fails loud if none is available — a contest rolls BOTH sides
each exchange, so the Other is mandatory). This test pins the seating + the
``encounter.contest`` stamp, plus the no-Other fail-loud (ADR-116).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.encounter import (
    ContestState,
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    instantiate_encounter_from_trigger,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

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
            description="The laird of Glenross.",
            personality="Proud, sharp-tongued.",
            level=1,
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        last_seen_location=location,
        last_seen_turn=1,
    )


def test_social_duel_seats_other_as_opponent_via_location_fallback():
    """The router dispatches social_duel with npcs_present=[]; the Other is
    sourced from the location roster and MUST be seated side='opponent' AND
    ``encounter.contest`` must be stamped (proof the Fate Contest path was taken
    — a contest rolls BOTH sides each exchange, so the Other is opponent-side)."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.character_locations["Inspector Pryce"] = "Castle Ross — The Great Hall"
    snap.npcs.append(_make_npc("Sir Iain Ross", "Castle Ross — The Great Hall"))

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="social_duel",
        player_name="Inspector Pryce",
        npcs_present=[],
        genre_slug="tea_and_murder",
    )
    assert enc is not None
    sides = {a.name: a.side for a in enc.actors}
    assert sides.get("Inspector Pryce") == "player"
    assert sides.get("Sir Iain Ross") == "opponent", (
        f"contest Other must be seated opponent-side; got {sides}"
    )
    assert isinstance(enc.contest, ContestState)
    assert enc.contest is not None, "the contest path must stamp encounter.contest"


def _social_duel_encounter() -> StructuredEncounter:
    """A live Duel of Wits: Pryce (player) vs Sir Iain (opponent), 0/5 dials.

    Both carry per_actor_state stats so the opposed-check modifier resolves
    cleanly (Pryce a competent sleuth, Sir Iain the sharper wit)."""
    return StructuredEncounter(
        encounter_type="social_duel",
        win_condition="dial_threshold",
        # Threshold 7 + opponent stats at the ADR-093 ceiling (10 → +0) mirror
        # the calibrated cdef.
        player_metric=EncounterMetric(name="barbs_landed", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="barbs_landed", current=0, starting=0, threshold=7),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name="Inspector Pryce",
                role="participant",
                side="player",
                per_actor_state={"stats": {"Cunning": 12, "Nerve": 12, "Humour": 12}},
            ),
            EncounterActor(
                name="Sir Iain Ross",
                role="participant",
                side="opponent",
                per_actor_state={"stats": {"Cunning": 10, "Nerve": 10, "Humour": 10}},
            ),
        ],
    )


def test_social_duel_opponent_dial_advances_on_dice(monkeypatch):
    """End-to-end: the real social_duel cdef routes through the opposed_check
    branch and the OPPONENT's dial advances from HIS OWN roll — no narrator-fiat
    tool, no frozen 0/5. This is the mechanical inverse of the playtest bug.

    Player rolls low (5 + Cunning 12 mod +1 = 6 vs riposte DC 16 → Fail → no
    player advance). Sir Iain rolls high (18 + Cunning 14 mod +2 = 20 vs DC 16
    → Success → strike grants own=base=3). Opponent dial: 0 → 3.
    """
    from sidequest.server import narration_apply as _na

    monkeypatch.setattr(_na, "_roll_d20_server_side", lambda: 18)

    pack = _pack()
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.encounter = _social_duel_encounter()

    # Narrator emits ONLY the opponent's beat (the SOUL gate drops PC-side
    # selections; the player's beat rides the pending DICE_THROW stash).
    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(actor="Sir Iain Ross", beat_id="riposte", outcome=RollOutcome.Success),
        ],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=pack,
        opposed_player_d20=5,
        opposed_player_beat_id="riposte",
        opposed_player_actor="Inspector Pryce",
        from_explicit_action=True,
        room=room_for(snap),
    )

    assert snap.encounter.player_metric.current == 0, "player rolled Fail — no advance"
    assert snap.encounter.opponent_metric.current == 3, (
        "Sir Iain's riposte (strike base 3) landed on his own roll — his dial must "
        f"advance to 3, not freeze at 0; got {snap.encounter.opponent_metric.current}"
    )


def test_social_duel_with_no_other_fails_loud():
    """A Fate Contest with nobody on the other side cannot resolve — instantiating
    one with no Other (empty npcs_present, empty location roster) must fail loud
    rather than seat a one-sided duel (No Silent Fallbacks; ``_requires_opponent``
    now covers the ``contest`` path — a contest rolls BOTH sides each exchange)."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.character_locations["Inspector Pryce"] = "The Glen Road — Afternoon"
    # No NPCs anywhere → location fallback returns empty.
    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=_pack(),
            encounter_type="social_duel",
            player_name="Inspector Pryce",
            npcs_present=[],
            genre_slug="tea_and_murder",
        )
