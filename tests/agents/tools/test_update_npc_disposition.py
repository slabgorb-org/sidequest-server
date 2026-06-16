"""Tests for the update_npc_disposition tool — Phase C Task 9.

WRITE tool. ``Npc.disposition`` is a single global ``Disposition`` (int
in [-100, 100]) — there is no multi-axis or per-PC disposition store in
v1. The plan's ``axis`` and ``perspective_pc`` args are accepted
forward-compat for ADR-020 (multi-axis disposition / per-PC observed
views); v1 records them in OTEL and ignores them mechanically.

Delta is coerced to int — ``Disposition`` is integer-valued. Clamp at
``[-100, 100]`` is provided by the ``Disposition`` ctor.
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
from sidequest.agents.tools import (
    update_npc_disposition as _update_npc_disposition_module,  # noqa: F401
)
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Disposition
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _npc(name: str, *, disposition: int = 0) -> Npc:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=4, max=4, base_max=4),
    )
    return Npc(core=core, disposition=Disposition(disposition))


def _build_snapshot(*, npcs: list[Npc] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        npcs=npcs or [],
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(store: SqliteStore, *, session_id: str = "s", turn: int = 3) -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=turn,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["update_npc_disposition"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def test_update_npc_disposition_is_registered() -> None:
    assert "update_npc_disposition" in default_registry.list_names()


async def test_positive_delta_increases_and_persists() -> None:
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=0)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": 15}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["npc_id"] == "Bart"
    assert p["delta"] == 15
    assert p["value_before"] == 0
    assert p["value_after"] == 15
    assert p["attitude_before"] == "neutral"
    assert p["attitude_after"] == "friendly"

    reloaded = store.load()
    assert reloaded is not None
    bart = next(n for n in reloaded.snapshot.npcs if n.core.name == "Bart")
    assert bart.disposition.value == 15


async def test_negative_delta_decreases_and_persists() -> None:
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=20)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": -15, "reason": "insulted"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == 20
    assert p["value_after"] == 5
    assert p["attitude_before"] == "friendly"
    assert p["attitude_after"] == "neutral"

    reloaded = store.load()
    assert reloaded is not None
    bart = next(n for n in reloaded.snapshot.npcs if n.core.name == "Bart")
    assert bart.disposition.value == 5


async def test_clamp_at_plus_100() -> None:
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=90)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": 999}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == 90
    assert p["value_after"] == 100
    assert p["attitude_after"] == "friendly"


async def test_clamp_at_minus_100() -> None:
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=-90)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": -999}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == -90
    assert p["value_after"] == -100
    assert p["attitude_after"] == "hostile"


async def test_attitude_band_flip_neutral_to_friendly() -> None:
    """Start at -5 (NEUTRAL), +50 → +45 (FRIENDLY)."""
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=-5)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": 50}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == -5
    assert p["value_after"] == 45
    assert p["attitude_before"] == "neutral"
    assert p["attitude_after"] == "friendly"


async def test_attitude_band_flip_friendly_to_hostile() -> None:
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=15)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": -50}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["attitude_before"] == "friendly"
    assert p["attitude_after"] == "hostile"


async def test_unknown_npc_returns_not_found() -> None:
    snap = _build_snapshot(npcs=[_npc("Bart")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Ghost", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Ghost" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save — load() returns None.
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_empty_npc_id_rejected_by_args_model() -> None:
    """``min_length=1`` surfaces as a validation error through registry dispatch."""
    snap = _build_snapshot(npcs=[_npc("Bart")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="update_npc_disposition",
            arguments={"npc_id": "", "delta": 1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_axis_accepted_forward_compat_mutates_global() -> None:
    """``axis="trust"`` accepted and recorded in OTEL but mutates global value."""
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=0)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"npc_id": "Bart", "delta": 12, "axis": "trust"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["axis"] == "trust"
    # v1: global value moves regardless of axis label.
    assert p["value_after"] == 12

    reloaded = store.load()
    assert reloaded is not None
    bart = next(n for n in reloaded.snapshot.npcs if n.core.name == "Bart")
    assert bart.disposition.value == 12


async def test_perspective_pc_accepted_forward_compat_mutates_global() -> None:
    """``perspective_pc="Alex"`` accepted; v1 still mutates the global view."""
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"npc_id": "Bart", "delta": -3, "perspective_pc": "Alex"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_after"] == 7

    reloaded = store.load()
    assert reloaded is not None
    bart = next(n for n in reloaded.snapshot.npcs if n.core.name == "Bart")
    # Global value is what changed; no per-PC store in v1.
    assert bart.disposition.value == 7


async def test_otel_span_carries_disposition_attrs(otel_capture) -> None:
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=5)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="update_npc_disposition",
            arguments={
                "npc_id": "Bart",
                "delta": 7,
                "axis": "trust",
                "perspective_pc": "Alex",
                "reason": "shared a meal",
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["value_after"] == 12

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.update_npc_disposition"]
    assert write_spans, f"no tool.write.update_npc_disposition span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "update_npc_disposition"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.disposition.npc_id") == "Bart"
    assert attrs.get("tool.disposition.axis") == "trust"
    assert attrs.get("tool.disposition.delta") == 7.0
    assert attrs.get("tool.disposition.perspective_pc") == "Alex"


async def test_otel_perspective_pc_empty_string_when_none(otel_capture) -> None:
    """OTEL doesn't accept None — handler writes empty string for unset perspective."""
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=0)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-none",
            name="update_npc_disposition",
            arguments={"npc_id": "Bart", "delta": 3},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.update_npc_disposition"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.disposition.perspective_pc") == ""
    # axis defaults to "general"
    assert attrs.get("tool.disposition.axis") == "general"


async def test_parallel_update_runs_sequentially() -> None:
    """Concurrent dispatches for the same session share a WRITE lock.
    Both deltas must land cleanly (no torn read-modify-write)."""
    snap = _build_snapshot(npcs=[_npc("Bart", disposition=0)])
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="r1",
                name="update_npc_disposition",
                arguments={"npc_id": "Bart", "delta": 4},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="r2",
                name="update_npc_disposition",
                arguments={"npc_id": "Bart", "delta": -7},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    bart = next(n for n in reloaded.snapshot.npcs if n.core.name == "Bart")
    # 0 + 4 - 7 = -3 if serialized; either order yields -3.
    assert bart.disposition.value == -3
