"""Story 61-8 §D3 + §D4 — projection edge cases the 61-7 review fan-out
flagged but the round-2 review-fix scoped out.

§D3 — empty ``encounter.actors`` list. An unresolved encounter with no
actors must not anchor anyone as in-scene; the encounter-branch must
fall through to location resolution cleanly. Regression guard against an
over-eager branch that returns True for any unresolved encounter.

§D4 — PC has no ``current_room`` (degraded location). The projection's
``current_room_id`` resolution can return None when the acting PC hasn't
been placed in a room yet. The 61-2 doctrine is to SKIP the room_states
and npcs projections in that case (gaslighting-doctrine §
``project_narrator_gaslighting_doctrine.md``) and let the original
payload ride through. This is regression coverage — the existing
projection tests cover the happy path; this file guards the degraded
path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import sidequest.agents.tools  # noqa: F401 — populate default_registry
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot, Npc, RoomState
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_61_7 shapes so verdicts converge)
# ---------------------------------------------------------------------------


def _npc(
    name: str,
    *,
    current_room: str | None = None,
    location: str | None = None,
    last_seen_location: str | None = None,
) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=4, max=4, base_max=4),
        ),
        current_room=current_room,
        location=location,
        last_seen_location=last_seen_location,
    )


def _character(name: str, *, current_room: str | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory="hero",
        char_class="Delver",
        race="Human",
        current_room=current_room,
    )


def _make_snapshot(
    *,
    acting_pc: str = "Alice",
    pc_room: str | None = "main_hall",
    npcs: list[Npc] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    pc = _character(acting_pc, current_room=pc_room)
    room_states: dict[str, RoomState] = {pc_room: RoomState(room_id=pc_room)} if pc_room else {}
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=10),
        characters=[pc],
        npcs=list(npcs or []),
        room_states=room_states,
        atmosphere="dim",
        current_region="upper_caverns",
        encounter=encounter,
    )
    if pc_room is not None:
        snap.character_locations[acting_pc] = pc_room
    snap.player_seats[f"player:{acting_pc.lower()}"] = acting_pc
    return snap


def _empty_actors_encounter() -> StructuredEncounter:
    """An unresolved encounter with zero actors — the §D3 fixture."""
    return StructuredEncounter(
        encounter_type="social",
        player_metric=EncounterMetric(name="poise", threshold=10),
        opponent_metric=EncounterMetric(name="suspicion", threshold=10),
        actors=[],
        resolved=False,
    )


def _projection_payload(snap: GameSnapshot, *, player_name: str = "Alice") -> dict[str, Any]:
    """Drive the production projection and return the full state_summary
    payload (not just npc names) so degraded-path tests can assert on
    room_states pass-through too."""
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snap.genre_slug)
    sd = _SessionData(
        genre_slug=snap.genre_slug,
        world_slug=snap.world_slug,
        player_name=player_name,
        player_id=f"player:{player_name.lower()}",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd._room = room_for(snap, slug=snap.world_slug)
    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.state_summary is not None
    return json.loads(ctx.state_summary)


def _projection_npc_names(snap: GameSnapshot, *, player_name: str = "Alice") -> set[str]:
    payload = _projection_payload(snap, player_name=player_name)
    names: set[str] = set()
    for entry in payload.get("npcs") or []:
        core = entry.get("core")
        name = core.get("name", "") if isinstance(core, dict) else entry.get("name", "")
        if name:
            names.add(name)
    return names


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _tool_ctx(store: SqliteStore, *, perspective_pc: str = "Alice") -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _tool_npc_names(snap: GameSnapshot, *, perspective_pc: str = "Alice") -> set[str]:
    store = _store_with(snap)
    ctx = _tool_ctx(store, perspective_pc=perspective_pc)
    registered = default_registry._tools["list_npcs_in_scene"]
    args = registered.args_model.model_validate({})
    result = cast(ToolResult, await registered.handler(args, ctx))
    assert result.status is ToolResultStatus.OK, (
        f"tool call failed: status={result.status} message={result.message!r}"
    )
    payload = cast(dict[str, Any], result.payload)
    return {entry["name"] for entry in payload.get("npcs", [])}


# ---------------------------------------------------------------------------
# §D3 — empty encounter.actors list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_encounter_with_empty_actors_does_not_anchor_anyone() -> None:
    """An unresolved encounter with ``actors=[]`` must not anchor any
    NPC as in-scene via the encounter branch — standard location
    resolution applies. Regression guard against a future change that
    short-circuits ``is_npc_anchored_by_encounter`` to ``True`` for any
    unresolved encounter.

    Fixture: NPC at ``distant_chamber`` (all three fields), acting PC at
    ``main_hall``, empty-actors encounter active. The NPC must be
    DROPPED by both call sites — location mismatch wins.
    """
    npc = _npc(
        "OffStageActor",
        current_room="distant_chamber",
        location="distant_chamber",
        last_seen_location="distant_chamber",
    )
    snap = _make_snapshot(npcs=[npc], encounter=_empty_actors_encounter())

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Empty-actors encounter convergence: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)} — both call sites must reach the same verdict."
    )
    assert "OffStageActor" not in proj, (
        "OffStageActor kept despite being off-stage and the encounter "
        "having no actors. An empty actors list must not function as a "
        "blanket in-scene grant; the encounter branch must require "
        "explicit actor membership."
    )


@pytest.mark.asyncio
async def test_unresolved_encounter_with_empty_actors_preserves_location_in_scene_npc() -> None:
    """Half-two of the §D3 contract: an NPC who IS at the PC's room
    must still be kept when the encounter is unresolved-but-empty. The
    encounter's emptiness is not an override either way — location
    resolution proceeds normally.
    """
    npc = _npc("OnSceneNpc", current_room="main_hall")
    snap = _make_snapshot(npcs=[npc], encounter=_empty_actors_encounter())

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool
    assert "OnSceneNpc" in proj, (
        "Location-resolved NPC dropped from in-scene roster despite "
        "matching the PC's room. An empty-actors encounter must not "
        "suppress legitimate location-based scene membership."
    )


# ---------------------------------------------------------------------------
# §D4 — PC has no current_room (degraded actor location)
# ---------------------------------------------------------------------------


def test_pc_with_no_current_room_skips_room_states_projection() -> None:
    """Story 61-8 §D4 — degraded actor location path.

    When the acting PC's ``current_room`` is unresolvable, the projection
    must SKIP the ``room_states`` and ``npcs`` projections (gaslighting-
    doctrine: degraded location ≠ no rooms / no NPCs). The original
    payload's ``room_states`` rides through unmodified.

    Fixture: PC with ``current_room=None``, NPC at any location, NO
    encounter. The room_states projection must NOT have been applied
    (the original payload's room_states is preserved); the npcs payload
    is unchanged from the snapshot's serialized ``npcs`` list.
    """
    # Build a snapshot where the PC has no current_room, but a few
    # room_states exist. With current_room_id None, the projection
    # should pass them through.
    pc_name = "Alice"
    snap = _make_snapshot(
        acting_pc=pc_name,
        pc_room=None,  # NO current_room on the PC
        npcs=[_npc("DistantNpc", current_room="distant_chamber")],
    )
    # Inject a room_states entry so we can assert pass-through.
    snap.room_states["distant_chamber"] = RoomState(room_id="distant_chamber")
    # No character_locations[pc] set — party_location returns the
    # degraded result the projection respects.

    payload = _projection_payload(snap, player_name=pc_name)
    # Degraded-path contract: room_states is NOT scoped to the PC's
    # room (because there isn't one). The original snapshot's
    # room_states dict survives.
    assert "distant_chamber" in (payload.get("room_states") or {}), (
        "Degraded-actor-location path lost the original room_states "
        "entries — the projection must NOT prune room_states when the "
        "actor location is unresolvable (gaslighting-doctrine §"
        "project_narrator_gaslighting_doctrine.md)."
    )


def test_pc_with_no_current_room_skips_npcs_projection() -> None:
    """§D4 sibling: the npcs projection must also be skipped when the
    actor location is unresolvable. An off-stage-looking NPC survives
    in the prompt because the projection cannot make an informed
    in-scene determination.
    """
    pc_name = "Alice"
    snap = _make_snapshot(
        acting_pc=pc_name,
        pc_room=None,
        npcs=[
            _npc("FarAwayNpc", current_room="distant_chamber"),
            _npc("OtherFarNpc", location="distant_chamber"),
        ],
    )
    payload = _projection_payload(snap, player_name=pc_name)
    npc_names = {
        (
            entry.get("core", {}).get("name")
            if isinstance(entry.get("core"), dict)
            else entry.get("name")
        )
        for entry in (payload.get("npcs") or [])
    }
    assert "FarAwayNpc" in npc_names, (
        "FarAwayNpc dropped under degraded actor location. The npcs "
        "projection must skip (let the original payload through) when "
        "current_room_id is unresolvable — silently dropping NPCs is "
        "the gaslighting failure mode this §D4 path exists to prevent."
    )
    assert "OtherFarNpc" in npc_names


# ---------------------------------------------------------------------------
# §D1 — unresolvable-name drop counter (split from npcs_dropped)
# ---------------------------------------------------------------------------


def test_unresolvable_name_drops_counted_separately_from_off_scene_drops() -> None:
    """Story 61-8 §D1 (review-fix round 2) — the new
    ``npcs_unresolvable_name_dropped`` counter must increment on entries
    whose ``core.name`` / top-level ``name`` extraction yields a falsy
    value, and those entries must NOT inflate ``npcs_dropped`` (which is
    reserved for legitimately off-scene NPCs).

    Drives ``_apply_phase_c_projections`` directly with a hand-built
    payload that contains a malformed entry — the production
    ``CreatureCore.name_non_blank`` validator blocks empty names at
    pydantic construction, so this branch is only reachable when the
    serialization itself drifts. The test simulates that drift by
    injecting the malformed payload directly.
    """
    from sidequest.server.session_helpers import _apply_phase_c_projections

    # In-scene NPC (valid), off-scene NPC (valid name, wrong room),
    # malformed payload entry (None name) — three entries; one kept,
    # one off-scene, one unresolvable-name.
    npc_in_scene = _npc("InScene", current_room="main_hall")
    npc_off_stage = _npc("OffStage", current_room="distant_chamber")
    snap = _make_snapshot(npcs=[npc_in_scene, npc_off_stage])

    # Hand-build the payload as if from snapshot.model_dump(), then
    # inject a malformed entry to exercise the §D1 branch.
    payload: dict[str, Any] = {
        "npcs": [
            {"core": {"name": "InScene"}},
            {"core": {"name": "OffStage"}},
            # Malformed: core present but name=None — the §D1 branch
            # must catch this and route to npcs_unresolvable_name_dropped.
            {"core": {"name": None}, "disposition": 0},
        ],
        "room_states": {"main_hall": {"room_id": "main_hall"}},
        "characters": [],
    }

    counts = _apply_phase_c_projections(snap, payload, current_room_id="main_hall")

    assert counts["npcs_unresolvable_name_dropped"] == 1, (
        f"Expected exactly 1 unresolvable-name drop; got "
        f"{counts['npcs_unresolvable_name_dropped']}. The §D1 branch "
        "at session_helpers.py:_apply_phase_c_projections must catch "
        "the malformed entry (core.name=None) and increment the "
        "dedicated counter, not roll it into npcs_dropped."
    )
    # The off-stage NPC IS a legitimate drop; assert npcs_dropped
    # carries that count and NOTHING ELSE — i.e. the malformed entry
    # is not double-counted into npcs_dropped.
    assert counts["npcs_dropped"] == 1, (
        f"npcs_dropped expected 1 (just OffStage); got {counts['npcs_dropped']}. "
        "The unresolvable-name drop must be excluded from npcs_dropped "
        "to keep the GM-panel signals distinguishable."
    )
    # And the in-scene NPC survives.
    assert any((e.get("core") or {}).get("name") == "InScene" for e in payload["npcs"])


# ---------------------------------------------------------------------------
# §D3 — direct unit test for is_npc_anchored_by_encounter(empty_actors)
# ---------------------------------------------------------------------------


def test_is_npc_anchored_by_encounter_returns_false_for_empty_actors() -> None:
    """Story 61-8 §D3 (review-fix round 2) — direct predicate unit
    test. The convergence integration tests above verify both call
    sites give the same answer, but a future change that desyncs the
    predicates while both happening to return False would still
    converge. This unit test isolates the predicate itself: an
    unresolved encounter with ``actors=[]`` must return False for
    every NPC regardless of name. Guards the §D3 contract at the
    predicate boundary, independent of call-site wiring.
    """
    from sidequest.game.npc_scene import is_npc_anchored_by_encounter

    encounter = _empty_actors_encounter()
    npc = _npc("Anyone")

    assert is_npc_anchored_by_encounter(npc, encounter) is False, (
        "is_npc_anchored_by_encounter returned True for an NPC against "
        "an empty-actors encounter. An empty actors list must NEVER "
        "function as a blanket in-scene grant; the predicate must "
        "require explicit actor membership."
    )
