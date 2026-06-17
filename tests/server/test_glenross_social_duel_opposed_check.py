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


def test_social_duel_contest_does_not_advance_the_dial(monkeypatch):
    """Westley M1 (ADR-144 REPLACE) — the inverse of the original opposed_check test:
    social_duel is now a Fate Contest, so a stray beat_selection through the real
    narration-apply path MUST NOT run the dial apply_beat engine. The dial metrics
    stay frozen (the 4dF Contest engine, reached via FATE_ACTION, owns resolution).
    A dial tripwire proves apply_beat is never reached."""
    import sidequest.game.beat_kinds as beat_kinds_pkg

    def _dial_tripwire(*_a, **_kw):
        raise AssertionError("apply_beat (dial) reached on a Fate Contest — ADR-144 REPLACE")

    monkeypatch.setattr(beat_kinds_pkg, "apply_beat", _dial_tripwire)

    pack = _pack()
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    enc = _social_duel_encounter()
    enc.contest = ContestState(target=3)  # real social_duel is contest mode
    snap.encounter = enc

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(actor="Sir Iain Ross", beat_id="riposte", outcome=RollOutcome.Success),
        ],
    )

    # No exception => the dial tripwire was never tripped.
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=pack,
        from_explicit_action=False,
        room=room_for(snap),
    )

    assert snap.encounter is not None and snap.encounter.resolved is False
    assert snap.encounter.player_metric.current == 0
    assert snap.encounter.opponent_metric.current == 0, (
        "the dial must NOT advance on a contest — the stray beat is dropped and the "
        "4dF FATE_ACTION exchange resolves the duel (ADR-144 REPLACE)"
    )
    assert snap.encounter.contest is not None
    assert snap.encounter.contest.player_victories == 0
    assert snap.encounter.contest.opponent_victories == 0


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
