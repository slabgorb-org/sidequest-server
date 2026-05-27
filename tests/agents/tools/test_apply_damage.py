"""Tests for the apply_damage tool — Phase C Task 3.

WRITE tool. The narrator says "apply HP damage" — the engine model is
ADR-078 edge/composure. This tool translates: damage amount is subtracted
from the target's ``CreatureCore.hp.current`` via ``apply_hp_delta``.
"""

from __future__ import annotations

import asyncio
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
from sidequest.agents.tools import apply_damage as _apply_damage_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _character(name: str, *, edge_current: int = 10, edge_max: int = 10) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=edge_current, max=edge_max, base_max=edge_max),
    )
    return Character(
        core=core,
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )


def _npc(name: str, *, edge_current: int = 8, edge_max: int = 8) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=edge_current, max=edge_max, base_max=edge_max),
        ),
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    npcs: list[Npc] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=npcs or [],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, session_id: str = "s") -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["apply_damage"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def test_apply_damage_is_registered() -> None:
    assert "apply_damage" in default_registry.list_names()


async def test_damage_reduces_target_edge() -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 3, "damage_type": "slashing"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["target"] == "Alice"
    assert p["amount"] == 3
    assert p["damage_type"] == "slashing"
    assert p["target_hp_after"] == 7

    # Persisted: reloading should reflect the mutation.
    reloaded = store.load()
    assert reloaded is not None
    found = reloaded.snapshot.find_creature_core("Alice")
    assert found is not None
    assert found.hp.current == 7


async def test_damage_zero_is_noop_but_returns_ok() -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 0}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["amount"] == 0
    assert p["target_hp_after"] == 10
    assert p["damage_type"] == "untyped"  # default
    assert p["source"] == ""  # default


async def test_damage_targets_npc() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", edge_current=8)],
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Goblin", "amount": 5, "source": "Alice's swing"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["target"] == "Goblin"
    assert p["target_hp_after"] == 3
    assert p["source"] == "Alice's swing"


async def test_damage_clamps_at_zero() -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=2)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 99}, ctx)
    assert r.status is ToolResultStatus.OK
    assert _payload(r)["target_hp_after"] == 0


async def test_unknown_target_returns_not_found() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Nobody", "amount": 3}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Nobody" in r.message


async def test_no_active_session_returns_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store()
    # Note: no init_session / save — load() returns None.
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 3}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_negative_amount_rejected_by_args_model() -> None:
    """ge=0 constraint on the Pydantic args model surfaces as a validation error
    through the registry dispatch, not the handler."""
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-neg",
            name="apply_damage",
            arguments={"target": "Alice", "amount": -3},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_otel_span_carries_damage_attrs(otel_capture) -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="apply_damage",
            arguments={
                "target": "Alice",
                "amount": 4,
                "damage_type": "fire",
                "source": "lava splash",
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_damage"]
    assert write_spans, f"no tool.write.apply_damage span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "apply_damage"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.damage.target") == "Alice"
    assert attrs.get("tool.damage.amount") == 4
    assert attrs.get("tool.damage.damage_type") == "fire"
    assert attrs.get("tool.damage.source") == "lava splash"
    assert attrs.get("tool.damage.target_hp_after") == 6
    assert payload["target_hp_after"] == 6


async def test_otel_span_emitted_for_zero_amount(otel_capture) -> None:
    """amount=0 still emits the span (with target_hp_after unchanged)."""
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-zero",
            name="apply_damage",
            arguments={"target": "Alice", "amount": 0},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_damage"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.damage.amount") == 0
    assert attrs.get("tool.damage.target_hp_after") == 10


async def test_parallel_damage_against_same_session_runs_sequentially() -> None:
    """Two concurrent apply_damage dispatches share the per-session WRITE lock
    (Phase B Registry), so they cannot overlap. Verified via persisted state:
    both reductions land cleanly (no torn read-modify-write)."""
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="d1",
                name="apply_damage",
                arguments={"target": "Alice", "amount": 3},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="d2",
                name="apply_damage",
                arguments={"target": "Alice", "amount": 4},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    # Sequential ordering means the second invocation sees the first's write.
    # 10 - 3 - 4 = 3 — *not* 10 - 3 = 7 (which would happen if reads raced).
    reloaded = store.load()
    assert reloaded is not None
    found = reloaded.snapshot.find_creature_core("Alice")
    assert found is not None
    assert found.hp.current == 3

    # And the payloads' target_hp_after values are a serial sequence:
    payloads = sorted([json.loads(r.content)["target_hp_after"] for r in results], reverse=True)
    assert payloads == [7, 3]
