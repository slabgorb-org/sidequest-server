"""165-3 REWORK (ADR-096 v2, Track C2) — the reach gate is OBSERVABILITY-ONLY in v1.

Reviewer Critical #2 (scope settled in ``sprint/epic-165.yaml`` review_findings:
"make gate observability-only"): once the store resolves and the gate wakes up,
aborting an out-of-reach strike SOFTLOCKS combat. ``seat_actor_cells`` is the ONLY
writer of ``per_actor_state['cell']`` — there is no move message in v1 (plan §4:
"no new inbound messages"), ``adjudicate_tactical_move`` has zero prod callers, and
narration can't budge a token. Seating puts the melee PC at the entrance anchor and
the monster at a non-adjacent creature anchor, so the FIRST swing is out of reach
with no way to close — an unwinnable turn-one softlock (and a Zork-Problem
violation: narrated "I charge in and swing" can't update the cell either).

So v1 does NOT raise on a denied verdict. It emits the spans (the GM panel sees
"out of reach") and lets the strike proceed. Enforcement-abort ships WITH movement,
as a follow-up.

The two assertions are a matched pair and both are load-bearing:
  * the strike must NOT abort on reach  → the softlock fix
  * the denied span must STILL fire     → observability preserved; the gate is
                                          made silent-passing, NOT deleted
Dropping the second would let a Dev "fix" the softlock by disabling the gate — the
exact Illusionism OTEL exists to catch.

Reuses the proven heavy_metal (wwn) dispatch harness; content-gated.
"""

from __future__ import annotations

import pytest

from tests.integration.test_wwn_heavy_metal_dispatch import (
    _build_caster,
    _discover_caster_and_damage_spell,
    _has_real_content,
    _load_heavy_metal,
)
from tests.server.tactical_emit_fixtures import _FakeDungeonStore, _runtime_mask_dict

# 7×5 interior; floor is x in 1..5, y in 1..3.
_ROOM_ID = "combat_room_165_3"
_MASK_ROWS = ["#######", "#.....#", "#.....#", "#.....#", "#######"]


def _seat_and_fire(otel_capture, monkeypatch, *, attacker_cell, target_cell):
    """Seat a wwn combat with the attacker at ``attacker_cell`` and the opponent at
    ``target_cell`` on a real mask, then fire an ``attack`` strike through the REAL
    ``dispatch_dice_throw`` with a real dungeon store. Returns
    ``(reach_abort_reason_or_None, finished_span_names)``."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import DiceDispatchError, dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

    pack = _load_heavy_metal()
    assert pack.rules.ruleset == "wwn", "heavy_metal must be bound ruleset: wwn"
    class_display, _spell = _discover_caster_and_damage_spell(pack)

    attacker = "Sael"
    opponent = "The Collector's Blade"
    caster = _build_caster(pack, attacker, class_display=class_display)

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(caster)
    # _resolve_room_mask reads the roller's location to find the room's mask.
    snap.character_locations[attacker] = _ROOM_ID

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=attacker,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
        allow_synthetic_opponent=True,
    )
    assert enc is not None, "seating combat must produce an encounter"
    snap.encounter = enc

    # Stamp controlled cells: the reach gate needs BOTH actors seated. (We seat by
    # hand rather than via anchors so the distance is deterministic regardless of
    # the room's authored anchors.)
    for actor in enc.actors:
        actor.per_actor_state["cell"] = (
            list(attacker_cell) if actor.side == "player" else list(target_cell)
        )

    enc.initiative = [
        InitiativeEntry(token_id=attacker, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]
    monkeypatch.setattr("random.randint", lambda a, b: a)

    store = _FakeDungeonStore({_ROOM_ID: _runtime_mask_dict(_MASK_ROWS)})

    reach_abort: str | None = None
    try:
        dispatch_dice_throw(
            payload=DiceThrowPayload(
                request_id="req-165-3-observability",
                throw_params=ThrowParams(
                    velocity=(0.0, 5.0, -2.0),
                    angular=(1.0, 1.0, 1.0),
                    position=(0.5, 0.5),
                ),
                face=[15],  # a d20 attack roll
                beat_id="attack",
            ),
            rolling_player_id="player-sael",
            character_name=attacker,
            character_stats=dict(caster.stats),
            encounter=enc,
            pack=pack,
            genre_slug="heavy_metal",
            session_id="hm-165-3-observability",
            round_number=1,
            room_broadcast=[].append,
            snapshot=snap,
            dungeon_store=store,
        )
    except DiceDispatchError as exc:
        # A weaponless caster's strike may raise LATER (damage resolution) — that is
        # not the reach gate and is tolerated. Only a reach/range abort is the
        # softlock this test forbids.
        m = str(exc).lower()
        if "reach" in m or "range" in m or "cell" in m:
            reach_abort = str(exc)

    names = [s.name for s in otel_capture.get_finished_spans()]
    return reach_abort, names


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_out_of_reach_strike_does_not_abort_and_still_emits_denied_span(
    otel_capture, monkeypatch
):
    """RED-driver: attacker (1,1) vs opponent (5,1) — 4 cells, out of melee reach.
    On develop today the gate raises ``DiceDispatchError`` (softlock). v1 must emit
    ``tactical.move.denied`` (GM panel sees it) but let the strike proceed."""
    reach_abort, span_names = _seat_and_fire(
        otel_capture, monkeypatch, attacker_cell=(1, 1), target_cell=(5, 1)
    )

    assert "tactical.move.denied" in span_names, (
        "the reach gate must STILL fire its denied span on an out-of-reach strike — "
        "observability-only means silent-passing, NOT disabling the gate. Spans "
        f"seen: {span_names}"
    )
    assert reach_abort is None, (
        "the reach gate aborted an out-of-reach strike "
        f"({reach_abort!r}) — v1 is observability-only and must NOT raise: with no "
        "move mechanic to close distance, a raise softlocks combat on turn one "
        "(seating places the melee PC at the entrance and the monster across the "
        "cavern). Emit the span, do not abort. See sprint/epic-165.yaml."
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_in_reach_strike_emits_validated_span_and_does_not_abort(otel_capture, monkeypatch):
    """Coverage companion (skip-vs-fire symmetry): attacker (1,1) vs opponent (2,1) —
    adjacent, in melee reach. The gate validates (emits ``tactical.move.validated``)
    and never aborts. Pins the in-range half so a future 'abort on any verdict'
    regression is caught."""
    reach_abort, span_names = _seat_and_fire(
        otel_capture, monkeypatch, attacker_cell=(1, 1), target_cell=(2, 1)
    )

    assert "tactical.move.validated" in span_names, (
        f"an in-reach strike must emit tactical.move.validated; spans: {span_names}"
    )
    assert reach_abort is None, (
        f"an in-reach strike must never be reach-aborted; got {reach_abort!r}"
    )
