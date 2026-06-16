"""Resolved-encounter husk reap (phantom-wound CRITICAL, sq-playtest 2026-06-14).

A confrontation that resolved on a prior turn lingered on ``snapshot.encounter``
as a zeroed husk; the narrator then layered a fresh sword fight on the corpse
with no live encounter, no dice, no HP delta. ``reap_resolved_encounter_husk``
clears the husk at turn start — but only on a genuine new player turn, never on
the dice-replay re-entry (which narrates a just-resolved encounter and must keep
it), and never on a live (unresolved) encounter.
"""

from __future__ import annotations

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch.encounter_lifecycle import reap_resolved_encounter_husk


def _snapshot(encounter: StructuredEncounter | None) -> GameSnapshot:
    return GameSnapshot(genre_slug="heavy_metal", world_slug="barsoom", encounter=encounter)


def _encounter(*, resolved: bool) -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="arena_bout",
        win_condition="hp_depletion",
        player_metric={"name": "hp", "current": 0, "starting": 0, "threshold": 1},
        opponent_metric={"name": "hp", "current": 0, "starting": 0, "threshold": 1},
    )
    enc.resolved = resolved
    if resolved:
        enc.outcome = "player_victory"
    return enc


def test_reaps_resolved_husk_on_genuine_player_turn() -> None:
    snap = _snapshot(_encounter(resolved=True))
    reaped = reap_resolved_encounter_husk(snap, is_dice_replay=False, turn=16)
    assert reaped is True
    assert snap.encounter is None


def test_does_not_reap_live_encounter() -> None:
    """A fight in progress (resolved=False) must never be cleared."""
    live = _encounter(resolved=False)
    snap = _snapshot(live)
    reaped = reap_resolved_encounter_husk(snap, is_dice_replay=False, turn=5)
    assert reaped is False
    assert snap.encounter is live


def test_does_not_reap_on_dice_replay_reentry() -> None:
    """The dice-resolution replay narrates a JUST-resolved encounter in the same
    logical turn — reaping there would strip the encounter the replay describes."""
    resolved = _encounter(resolved=True)
    snap = _snapshot(resolved)
    reaped = reap_resolved_encounter_husk(snap, is_dice_replay=True, turn=16)
    assert reaped is False
    assert snap.encounter is resolved


def test_no_encounter_is_a_noop() -> None:
    snap = _snapshot(None)
    reaped = reap_resolved_encounter_husk(snap, is_dice_replay=False, turn=1)
    assert reaped is False
    assert snap.encounter is None
