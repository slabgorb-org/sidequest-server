"""Tests for the list_npcs_in_scene tool — Phase C Task 8.

READ tool. v1 scene resolution:
* ``scene_id`` arg matches ``Npc.location`` OR ``Npc.current_room``.
* When ``scene_id is None``, derive from the perspective PC's
  ``current_room``; if the PC has no room (or no perspective is set),
  return all NPCs.

No line-of-sight engine yet — no perception rule is registered. The
filter happens at the handler level by scene-id matching, which is
already perspective-respecting once a PC's room is known.
"""

from __future__ import annotations

import json
from typing import Any, cast

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import list_npcs_in_scene as _list_npcs_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Disposition
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _npc(
    name: str,
    *,
    location: str | None = None,
    current_room: str | None = None,
) -> Npc:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=4, max=4, base_max=4),
    )
    return Npc(
        core=core,
        disposition=Disposition(0),
        location=location,
        current_room=current_room,
    )


def _character(name: str, *, current_room: str | None = None) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
        pronouns="they/them",
        stats={"str": 12, "dex": 14, "wis": 10},
        is_friendly=True,
        current_room=current_room,
    )


def _build_snapshot(
    *,
    npcs: list[Npc] | None = None,
    characters: list[Character] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        npcs=npcs or [],
        characters=characters or [],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(
    store,
    *,
    perspective_pc: str | None = "Alice",
    session_id: str = "s",
    turn: int = 1,
) -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc=perspective_pc,
        turn_number=turn,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["list_npcs_in_scene"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_list_npcs_in_scene_is_registered() -> None:
    assert "list_npcs_in_scene" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_empty_npcs_returns_empty_list() -> None:
    snap = _build_snapshot(npcs=[])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] is None
    assert p["npcs"] == []


async def test_explicit_scene_id_matches_current_room_and_location() -> None:
    """scene_id="tavern" filters NPCs whose location OR current_room equals it."""
    npcs = [
        _npc("Innkeeper", location="tavern"),
        _npc("Bard", current_room="tavern"),
        _npc("WoodlandStranger", location="forest"),
        _npc("ShipCaptain", current_room="bridge"),
    ]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"scene_id": "tavern"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] == "tavern"
    names = sorted(n["name"] for n in p["npcs"])
    assert names == ["Bard", "Innkeeper"]


async def test_scene_id_none_falls_back_to_perspective_pc_current_room() -> None:
    """scene_id=None + PC with current_room='bridge' → filter by 'bridge'."""
    npcs = [
        _npc("ShipCaptain", current_room="bridge"),
        _npc("Engineer", current_room="engine_room"),
        _npc("Cook", location="bridge"),  # location matches too
    ]
    alice = _character("Alice", current_room="bridge")
    snap = _build_snapshot(npcs=npcs, characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] == "bridge"
    names = sorted(n["name"] for n in p["npcs"])
    assert names == ["Cook", "ShipCaptain"]


async def test_scene_id_none_no_perspective_returns_all_npcs() -> None:
    """scene_id=None + no perspective_pc → return everyone."""
    npcs = [
        _npc("A", location="x"),
        _npc("B", current_room="y"),
        _npc("C"),
    ]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] is None
    names = sorted(n["name"] for n in p["npcs"])
    assert names == ["A", "B", "C"]


async def test_scene_id_none_perspective_set_but_no_room_returns_all() -> None:
    """PC exists but has no current_room → fall back to all NPCs."""
    npcs = [_npc("A", location="x"), _npc("B", current_room="y")]
    alice = _character("Alice", current_room=None)
    snap = _build_snapshot(npcs=npcs, characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] is None
    names = sorted(n["name"] for n in p["npcs"])
    assert names == ["A", "B"]


async def test_scene_id_none_perspective_pc_not_in_characters_returns_all() -> None:
    """Perspective set but PC name absent from snapshot → fall back to all."""
    npcs = [_npc("A", location="x")]
    snap = _build_snapshot(npcs=npcs, characters=[])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Ghost")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] is None
    names = sorted(n["name"] for n in p["npcs"])
    assert names == ["A"]


async def test_payload_shape_ids_only() -> None:
    """Payload exposes only npc_id + name — narrator must call query_npc for details."""
    npc = _npc("Solo", location="x")
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"scene_id": "x"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] == "x"
    assert len(p["npcs"]) == 1
    entry = p["npcs"][0]
    assert set(entry.keys()) == {"npc_id", "name"}
    assert entry["npc_id"] == "Solo"
    assert entry["name"] == "Solo"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store()
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# Dispatch — verify no perception rule alters payload
# ---------------------------------------------------------------------------


async def test_dispatch_no_rule_registered_payload_unchanged() -> None:
    """Through the registry: no rule for this tool → payload pass-through."""
    npcs = [_npc("A", location="x"), _npc("B", location="x")]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="list_npcs_in_scene",
            arguments={"scene_id": "x"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["scene_id"] == "x"
    names = sorted(n["name"] for n in payload["npcs"])
    assert names == ["A", "B"]


# ---------------------------------------------------------------------------
# OTEL
# ---------------------------------------------------------------------------


async def test_otel_span_records_count(otel_capture) -> None:
    npcs = [
        _npc("A", location="tavern"),
        _npc("B", current_room="tavern"),
        _npc("C", location="forest"),
    ]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="list_npcs_in_scene",
            arguments={"scene_id": "tavern"},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.list_npcs_in_scene"]
    assert read_spans, f"no tool.read.list_npcs_in_scene span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "list_npcs_in_scene"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.npcs.count") == 2


async def test_otel_span_count_zero_when_nothing_matches(otel_capture) -> None:
    npcs = [_npc("A", location="forest")]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-zero",
            name="list_npcs_in_scene",
            arguments={"scene_id": "tavern"},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.list_npcs_in_scene"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.npcs.count") == 0
