"""Regression: tea_and_murder ``social_duel`` resolves as an opposed check, and
its Other is seated as a metric-bearing ``opponent`` actor.

Playtest 59-8 (Glenross): the Duel of Wits ran in the default ``beat_selection``
mode (only the PLAYER rolled); the Other (Sir Iain) was seated ``neutral`` by the
location fallback (``social`` is not in ``_ADVERSARIAL_CATEGORIES``), so the
opponent dial had no dice-driven advance path and froze at 0 — the duel could
never resolve on his side. Keith's call: make it dice-driven (``opposed_check``),
which requires the Other to be ``opponent``-side so ``_resolve_opposed_check_branch``
can roll it.

The engine fix is ``_requires_opponent(cdef)``: an ``opposed_check`` confrontation
of ANY category seats its location-fallback Other as ``opponent`` (and fails loud
if none is available). This test pins both the content shape and the seating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResolutionMode
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    instantiate_encounter_from_trigger,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"

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


def test_social_duel_is_opposed_check_with_opponent_stats():
    """Content invariant: opposed_check + opponent_default_stats covering every
    stat the beats roll (Cunning/Nerve/Humour). Without the stats,
    resolve_opponent_modifier fails loud on the opponent's roll."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "social_duel")
    assert cdef is not None
    assert cdef.resolution_mode == ResolutionMode.opposed_check
    stats = cdef.opponent_default_stats or {}
    beat_stats = {b.stat_check for b in cdef.beats}
    missing = beat_stats - set(stats)
    assert not missing, (
        f"opponent_default_stats missing {missing} — the opponent rolls these "
        f"and resolve_opponent_modifier fails loud without them"
    )


def test_social_duel_seats_other_as_opponent_via_location_fallback():
    """The router dispatches social_duel with npcs_present=[]; the Other is
    sourced from the location roster and MUST be seated side='opponent'
    (pre-fix it was 'neutral' and its dial could never advance)."""
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
        f"opposed_check Other must be seated opponent-side; got {sides}"
    )


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
    """An opposed check with nobody on the other side cannot resolve — instantiating
    one with no Other (empty npcs_present, empty location roster) must fail loud
    rather than seat a one-sided duel (No Silent Fallbacks)."""
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
