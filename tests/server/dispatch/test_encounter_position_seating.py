"""Task 8 (ADR-096 v2, Track C2) — seat actor cells at encounter instantiation.

When a confrontation is seated on a room that has a tactical grid, the actors
must receive durable ``per_actor_state['cell']`` positions drawn from the room's
``RegionTactical`` anchors (entrance → player, creature anchors → opponents) —
so the reach gate (Task 7) has cells to adjudicate. The single chokepoint is
``instantiate_encounter_from_trigger`` (stamped by ``created_turn``).

RED until that seating is wired in. Fixture-driven on
``build_sd_with_tactical_region`` (the persisted-mask + anchors shape); content-
gated (skips without the caverns pack).

PLUMBING NOTE (raised as a Delivery Finding for Dev/Architect): the chokepoint
takes ``snapshot, pack, ...`` — it has NO ``dungeon_store`` access today (the
store is reached via ``sd.dungeon_store`` in map_emit.py, and ``sd`` is not in
scope here). The plan says the seating reads ``dungeon_store.load_masks()`` INSIDE
this function, so the store must be plumbed in. This test pins the natural,
plan-aligned form — a ``dungeon_store`` kwarg — and asserts the OUTCOME (cells +
no-op). If Dev plumbs the store differently, adapt the CALL shape here; the
outcome assertions are the real AC.
"""

from __future__ import annotations

import pytest

from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

# WN initiative rolls 1d8+DEX at seating, so the seated player needs a stat block
# (the shared fixture builds Rux with empty stats). Seed one before instantiation
# so the seam completes and reaches the Task-8 seating step.
_WN_STATS = {"STR": 12, "DEX": 12, "CON": 12, "INT": 12, "WIS": 12, "CHA": 12}


def _seed_player_stats(snap) -> None:
    snap.characters[0].stats.update(_WN_STATS)


def test_instantiation_seats_actor_cells_from_region_anchors():
    """After seating a combat on a grid room, every seated actor carries a cell
    drawn from the room's anchors — entrance (1,1) and creature (3,1) in the
    fixture. Before Task 8, no cells are stamped (the adjudicators are inert)."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

    sd, snap, room_id = build_sd_with_tactical_region(creature_revealed=False)
    _seed_player_stats(snap)

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=sd.genre_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[NpcMention(name="rope-spider", side="opponent")],
        genre_slug="caverns_and_claudes",
        allow_synthetic_opponent=True,
        dungeon_store=sd.dungeon_store,  # Task 8: plumb the store so the seam can seat
    )
    assert enc is not None, "seating a combat must produce an encounter"

    seated = {a.name: a.per_actor_state.get("cell") for a in enc.actors}
    assert all(cell is not None for cell in seated.values()), (
        f"every actor must be seated on a grid cell at instantiation; got {seated}"
    )
    # Cells come from the room's anchors: entrance (1,1) + creature (3,1).
    anchor_cells = {(1, 1), (3, 1)}
    for name, cell in seated.items():
        assert tuple(cell) in anchor_cells, f"{name} seated off-anchor at {cell}"


def test_instantiation_without_dungeon_store_seats_no_cells():
    """No-op boundary (No Silent Fallbacks): a region-mode session with no
    dungeon_store seats an encounter but places no cells — combat runs grid-less,
    exactly as today. Stable across RED and GREEN (no dungeon_store passed)."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

    _sd, snap, _room_id = build_sd_with_tactical_region(creature_revealed=False)
    _seed_player_stats(snap)

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_sd.genre_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[NpcMention(name="rope-spider", side="opponent")],
        genre_slug="caverns_and_claudes",
        allow_synthetic_opponent=True,
    )
    assert enc is not None
    for actor in enc.actors:
        assert "cell" not in actor.per_actor_state, (
            f"{actor.name} was seated on a cell without a tactical grid — grid-less "
            "combat must place nothing"
        )


@pytest.mark.asyncio
async def test_live_confrontation_dispatch_forwards_dungeon_store_to_seating(monkeypatch):
    """WIRING (the real-caller half): the LIVE seating caller
    ``run_confrontation_dispatch`` must FORWARD its ``dungeon_store`` to the seating
    chokepoint — otherwise Task 8 seating never runs in production (the store IS
    already carried in the intent-router-pass dispatch-bank context; the gap was
    that the subsystem entrypoint dropped it). Spy on the chokepoint and assert the
    store arrives. This closes the loop the stub-spy dispatch wiring test cannot: it
    proves a production creation path, not a hand-passed kwarg."""
    import sidequest.agents.subsystems.confrontation as conf
    from sidequest.protocol.dispatch import SubsystemDispatch

    sd, snap, _room_id = build_sd_with_tactical_region(creature_revealed=False)
    _seed_player_stats(snap)

    received: dict = {}

    def _spy(**kwargs):
        received.update(kwargs)
        return None  # None == no encounter seated; the dispatch handles it gracefully

    monkeypatch.setattr(conf, "instantiate_encounter_from_trigger", _spy)

    await conf.run_confrontation_dispatch(
        SubsystemDispatch(
            subsystem="confrontation",
            params={"type": "combat"},
            idempotency_key="conf-165-3-seating-wire",
            confidence=1.0,
        ),
        snapshot=snap,
        pack=sd.genre_pack,
        player_name="Rux",
        npcs_present=[],
        dungeon_store=sd.dungeon_store,
    )
    assert received.get("dungeon_store") is sd.dungeon_store, (
        "run_confrontation_dispatch did not forward dungeon_store to "
        "instantiate_encounter_from_trigger — Task 8 seating would be dead in "
        f"production; received keys={sorted(received)}"
    )
