"""165-4 REWORK RED — the dice-result range echo must reach the emitted DiceResultPayload.

Reviewer HIGH (rejected 2026-07-09): `DiceResultPayload.range_band`/`distance_cells`
were added to the protocol and rendered by the UI (`InlineDiceTray` `dice-result-range`),
but NO server code ever populates them. 165-3's reach gate computes exactly this data
(the `RangeAdjudication` verdict's `distance_cells` + the resolved weapon `range_band`)
and then DISCARDS it — `_enforce_tactical_reach(...)` is called for its spans only and
its return value is dropped at `sidequest/server/dispatch/dice.py:859-867`. Plan Task 9
scoped this population into the dice dispatch. Keith ruled (2026-07-09) to wire it for
real via the LIVE dispatch path.

This drives the REAL `dispatch_dice_throw` with an ARMED wwn attacker (so the strike
completes and broadcasts, unlike the weaponless observability harness) seated a known
Chebyshev distance from the opponent on a real mask + real dungeon store, and asserts
the broadcast CHECK `DiceResultPayload` carries the reach verdict's `distance_cells`
and resolved `range_band`. This is the production-path wiring test the reviewer required
— it fails on develop today because the verdict is discarded.

Reuses the armed heavy_metal harness (`_make_attacker`) + the observability seating
pattern. Content-gated.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR
from tests.integration.test_wwn_heavy_metal_combat import _load_heavy_metal, _make_attacker
from tests.server.tactical_emit_fixtures import _FakeDungeonStore, _runtime_mask_dict

# 7×5 interior; floor is x in 1..5, y in 1..3 (matches the 165-3 observability room).
_ROOM_ID = "combat_room_165_4"
_MASK_ROWS = ["#######", "#.....#", "#.....#", "#.....#", "#######"]


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _fire_and_collect_check_results(monkeypatch, *, attacker_cell, target_cell):
    """Seat an ARMED wwn combat, fire an ``attack`` through the REAL dispatch with a
    real dungeon store (so the reach gate engages), and return the CHECK-role
    DiceResultPayloads that were broadcast to the room."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.messages import DiceResultMessage
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import DiceDispatchError, dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    pack = _load_heavy_metal()
    assert pack.rules.ruleset == "wwn", "heavy_metal must be bound ruleset: wwn"

    attacker = "Sael"
    opponent = "The Collector's Blade"
    atk = _make_attacker(attacker)

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(atk)
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

    # Deterministic cells: attacker vs opponent so the reach gate measures a known
    # Chebyshev distance regardless of the room's authored anchors.
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

    broadcasts: list[object] = []
    try:
        dispatch_dice_throw(
            payload=DiceThrowPayload(
                request_id="req-165-4-range-echo",
                throw_params=ThrowParams(
                    velocity=(0.0, 5.0, -2.0),
                    angular=(1.0, 1.0, 1.0),
                    position=(0.5, 0.5),
                ),
                face=[20],  # a d20 attack roll that clears the strike DC
                beat_id="attack",
            ),
            rolling_player_id="player-sael",
            character_name=attacker,
            character_stats=dict(atk.stats),
            encounter=enc,
            pack=pack,
            genre_slug="heavy_metal",
            session_id="hm-165-4-range-echo",
            round_number=1,
            room_broadcast=broadcasts.append,
            snapshot=snap,
            dungeon_store=store,
        )
    except DiceDispatchError:
        # A raise after the check result is composed still leaves it in broadcasts;
        # a raise before it means the assertion below fails loudly (no check result).
        pass

    return [
        m.payload
        for m in broadcasts
        if isinstance(m, DiceResultMessage) and m.payload.roll_role == "check"
    ]


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_range_adjudicated_strike_echoes_distance_and_range_band_on_dice_result(
    monkeypatch,
):
    """RED-driver: attacker (1,1) vs opponent (3,1) — 2 cells apart. The reach gate
    measures the distance + resolves the weapon band at dispatch; the emitted CHECK
    DiceResultPayload MUST carry both onto the wire for the resolution-card readout.

    Fails on develop: the verdict is discarded (dispatch/dice.py:859-867), so both
    fields are None on the broadcast result."""
    results = _fire_and_collect_check_results(
        monkeypatch, attacker_cell=(1, 1), target_cell=(3, 1)
    )
    assert results, (
        "dispatch must broadcast a check DiceResult for a completed strike — none "
        "seen (the strike may have aborted before compose; check the harness)"
    )
    payload = results[0]

    assert payload.distance_cells == 2, (
        "the emitted dice-result must echo the Chebyshev distance the reach gate "
        f"measured — attacker (1,1) → target (3,1) = 2 cells; got "
        f"{payload.distance_cells!r} (verdict discarded → field never set)"
    )
    assert payload.range_band is not None, (
        "the emitted dice-result must echo the resolved weapon range band from the "
        "reach verdict — the resolution-card readout (InlineDiceTray dice-result-range) "
        "renders this but no server code sets it today (165-3's verdict is discarded)"
    )
