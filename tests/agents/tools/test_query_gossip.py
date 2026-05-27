"""Tests for the query_gossip tool — Phase C Task 12.

READ tool. ADR-053 gossip surfaces as ``Npc.belief_state.beliefs`` whose
``source`` is :class:`BeliefSourceToldBy`. v1 scene resolution mirrors
Task 8 (``list_npcs_in_scene``):

* explicit ``scene_id`` arg matches ``Npc.location`` OR ``Npc.current_room``.
* ``scene_id is None`` + ``perspective_pc`` → derive from the PC's
  ``current_room``; if absent, scan all NPCs.

``since_turn`` filters by ``belief.turn_learned`` (inclusive lower bound).
``limit`` caps the result list size.

No perception rule registered — scene-id matching is the v1 audibility
approximation. When LOS / audibility lands, add a rule here.
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
from sidequest.agents.tools import query_gossip as _query_gossip_module  # noqa: F401
from sidequest.game.belief_state import (
    BeliefFact,
    BeliefSourceInferred,
    BeliefSourceToldBy,
    BeliefSourceWitnessed,
    BeliefState,
    BeliefSuspicion,
)
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
    belief_state: BeliefState | None = None,
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
        belief_state=belief_state or BeliefState(),
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
    registered = default_registry._tools["query_gossip"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _told_by(
    subject: str,
    content: str,
    *,
    told_by: str,
    turn_learned: int = 1,
) -> BeliefFact:
    return BeliefFact(
        subject=subject,
        content=content,
        turn_learned=turn_learned,
        source=BeliefSourceToldBy(by=told_by),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_gossip_is_registered() -> None:
    assert "query_gossip" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_empty_npcs_returns_empty_items() -> None:
    snap = _build_snapshot(npcs=[])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scene_id"] is None
    assert p["items"] == []


async def test_only_told_by_beliefs_are_returned() -> None:
    """Witnessed / Inferred beliefs are NOT gossip — filter them out."""
    bs = BeliefState()
    bs.add_belief(_told_by("knight", "is a traitor", told_by="Innkeeper", turn_learned=2))
    bs.add_belief(
        BeliefFact(
            subject="knight",
            content="wore red armor",
            turn_learned=2,
            source=BeliefSourceWitnessed(),
        )
    )
    bs.add_belief(
        BeliefSuspicion.make(
            subject="knight",
            content="has a secret",
            turn_learned=2,
            source=BeliefSourceInferred(),
            confidence=0.7,
        )
    )
    snap = _build_snapshot(npcs=[_npc("Bard", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert len(p["items"]) == 1
    item = p["items"][0]
    assert item["subject"] == "knight"
    assert item["content"] == "is a traitor"
    assert item["told_by"] == "Innkeeper"
    assert item["npc"] == "Bard"
    assert item["variant"] == "fact"
    assert item["turn_learned"] == 2


async def test_explicit_scene_id_filters_by_npc_room_or_location() -> None:
    """scene_id="tavern" excludes NPCs in other locations."""
    bs_bard = BeliefState()
    bs_bard.add_belief(_told_by("king", "is sick", told_by="Servant"))
    bs_witch = BeliefState()
    bs_witch.add_belief(_told_by("king", "is dead", told_by="Crow"))

    npcs = [
        _npc("Bard", location="tavern", belief_state=bs_bard),
        _npc("Witch", current_room="forest_glade", belief_state=bs_witch),
    ]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"scene_id": "tavern"}, ctx)
    p = _payload(r)
    assert p["scene_id"] == "tavern"
    assert len(p["items"]) == 1
    assert p["items"][0]["npc"] == "Bard"


async def test_since_turn_filters_older_beliefs() -> None:
    bs = BeliefState()
    bs.add_belief(_told_by("a", "old gossip", told_by="X", turn_learned=3))
    bs.add_belief(_told_by("a", "fresh gossip", told_by="Y", turn_learned=7))
    snap = _build_snapshot(npcs=[_npc("Bard", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"since_turn": 5}, ctx)
    p = _payload(r)
    assert len(p["items"]) == 1
    assert p["items"][0]["content"] == "fresh gossip"


async def test_since_turn_inclusive_lower_bound() -> None:
    """since_turn=N includes beliefs where turn_learned == N."""
    bs = BeliefState()
    bs.add_belief(_told_by("a", "boundary", told_by="X", turn_learned=5))
    snap = _build_snapshot(npcs=[_npc("Bard", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"since_turn": 5}, ctx)
    p = _payload(r)
    assert len(p["items"]) == 1


async def test_scene_id_none_falls_back_to_perspective_pc_room() -> None:
    bs_bridge = BeliefState()
    bs_bridge.add_belief(_told_by("captain", "knows the route", told_by="Mate"))
    bs_engine = BeliefState()
    bs_engine.add_belief(_told_by("captain", "is lost", told_by="Stoker"))
    npcs = [
        _npc("ShipCaptain", current_room="bridge", belief_state=bs_bridge),
        _npc("Engineer", current_room="engine_room", belief_state=bs_engine),
    ]
    alice = _character("Alice", current_room="bridge")
    snap = _build_snapshot(npcs=npcs, characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    p = _payload(r)
    assert p["scene_id"] == "bridge"
    assert len(p["items"]) == 1
    assert p["items"][0]["npc"] == "ShipCaptain"


async def test_scene_id_none_no_perspective_returns_all_gossip() -> None:
    bs_a = BeliefState()
    bs_a.add_belief(_told_by("topic", "from A", told_by="X"))
    bs_b = BeliefState()
    bs_b.add_belief(_told_by("topic", "from B", told_by="Y"))
    npcs = [
        _npc("A", location="x", belief_state=bs_a),
        _npc("B", current_room="y", belief_state=bs_b),
    ]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    p = _payload(r)
    assert p["scene_id"] is None
    contents = sorted(i["content"] for i in p["items"])
    assert contents == ["from A", "from B"]


async def test_limit_caps_result_size() -> None:
    bs = BeliefState()
    for i in range(5):
        bs.add_belief(_told_by(f"s{i}", f"c{i}", told_by="X", turn_learned=1))
    snap = _build_snapshot(npcs=[_npc("Bard", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"limit": 2}, ctx)
    p = _payload(r)
    assert len(p["items"]) == 2


async def test_suspicion_with_told_by_source_is_included() -> None:
    """Gossip can be a Suspicion (not just a Fact) when the source is told_by."""
    bs = BeliefState()
    bs.add_belief(
        BeliefSuspicion.make(
            subject="merchant",
            content="might be a smuggler",
            turn_learned=4,
            source=BeliefSourceToldBy(by="Dockhand"),
            confidence=0.6,
        )
    )
    snap = _build_snapshot(npcs=[_npc("Bard", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    p = _payload(r)
    assert len(p["items"]) == 1
    assert p["items"][0]["variant"] == "suspicion"
    assert p["items"][0]["told_by"] == "Dockhand"


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
# Dispatch + OTEL
# ---------------------------------------------------------------------------


async def test_dispatch_payload_unchanged_no_perception_rule() -> None:
    bs = BeliefState()
    bs.add_belief(_told_by("topic", "the lord is in danger", told_by="Maid"))
    snap = _build_snapshot(npcs=[_npc("Bard", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="query_gossip",
            arguments={"scene_id": "tavern"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["scene_id"] == "tavern"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["content"] == "the lord is in danger"


async def test_otel_span_records_item_count(otel_capture) -> None:
    bs_a = BeliefState()
    bs_a.add_belief(_told_by("a", "x", told_by="P"))
    bs_b = BeliefState()
    bs_b.add_belief(_told_by("b", "y", told_by="Q"))
    npcs = [
        _npc("A", location="tavern", belief_state=bs_a),
        _npc("B", location="tavern", belief_state=bs_b),
    ]
    snap = _build_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="query_gossip",
            arguments={"scene_id": "tavern"},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_gossip"]
    assert read_spans, f"no tool.read.query_gossip span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_gossip"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.gossip.item_count") == 2
    assert attrs.get("tool.gossip.scene_id") == "tavern"


async def test_otel_item_count_zero_when_nothing_matches(otel_capture) -> None:
    bs = BeliefState()
    bs.add_belief(_told_by("a", "x", told_by="P", turn_learned=1))
    snap = _build_snapshot(npcs=[_npc("A", location="tavern", belief_state=bs)])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-zero",
            name="query_gossip",
            arguments={"since_turn": 999},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_gossip"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.gossip.item_count") == 0
