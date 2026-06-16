"""Tests for the update_resource_pool tool — Phase C Task 5.

WRITE tool. Plan called for a ``target: str`` arg, but per ADR-033 the
real resource model is session-scoped (``GameSnapshot.resources:
dict[str, ResourcePool]``), not per-actor. Per-actor edge already has
its own tool (``apply_damage``). This adapter takes ``pool: str`` to
name a session-scoped pool and forwards a signed delta through
``GameSnapshot.apply_resource_patch`` with ``ResourcePatchOp.Add`` —
"Add" handles negatives correctly because the underlying primitive
treats the value as a signed delta.
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
    update_resource_pool as _update_resource_pool_module,  # noqa: F401
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.resource_pool import ResourcePool, ResourceThreshold
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager


def _pool(
    name: str,
    *,
    current: float = 10.0,
    min: float = 0.0,
    max: float = 100.0,
    thresholds: list[ResourceThreshold] | None = None,
) -> ResourcePool:
    return ResourcePool(
        name=name,
        label="",
        current=current,
        min=min,
        max=max,
        voluntary=True,
        decay_per_turn=0.0,
        thresholds=thresholds or [],
    )


def _build_snapshot(
    *,
    resources: dict[str, ResourcePool] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        resources=resources or {},
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
    registered = default_registry._tools["update_resource_pool"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def test_update_resource_pool_is_registered() -> None:
    assert "update_resource_pool" in default_registry.list_names()


async def test_positive_delta_adds_and_persists() -> None:
    snap = _build_snapshot(resources={"mana": _pool("mana", current=10.0)})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"pool": "mana", "delta": 5}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["pool"] == "mana"
    assert p["delta"] == 5
    assert p["old_value"] == 10.0
    assert p["new_value"] == 15.0
    assert p["crossed_thresholds"] == []

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.resources["mana"].current == 15.0


async def test_negative_delta_subtracts_and_persists() -> None:
    snap = _build_snapshot(resources={"mana": _pool("mana", current=10.0)})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"pool": "mana", "delta": -4, "source": "fireball cast"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["old_value"] == 10.0
    assert p["new_value"] == 6.0

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.resources["mana"].current == 6.0


async def test_engine_clamps_at_max() -> None:
    snap = _build_snapshot(resources={"mana": _pool("mana", current=10.0, max=100.0)})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"pool": "mana", "delta": 999}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["old_value"] == 10.0
    assert p["new_value"] == 100.0


async def test_engine_clamps_at_min() -> None:
    snap = _build_snapshot(resources={"mana": _pool("mana", current=5.0, min=0.0)})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"pool": "mana", "delta": -50}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["old_value"] == 5.0
    assert p["new_value"] == 0.0


async def test_unknown_pool_returns_not_found() -> None:
    snap = _build_snapshot(resources={"mana": _pool("mana")})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"pool": "ghost", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "ghost" in r.message


async def test_empty_pool_name_rejected_by_args_model() -> None:
    """``min_length=1`` on the args model surfaces as a validation error
    through the registry dispatch, not the handler."""
    snap = _build_snapshot(resources={"mana": _pool("mana")})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="update_resource_pool",
            arguments={"pool": "", "delta": 1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save — load() returns None.
    ctx = _make_ctx(store)

    r = await _call({"pool": "mana", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_threshold_crossing_surfaced_in_payload() -> None:
    """Driving downward across a 5.0 threshold must surface the crossing."""
    threshold = ResourceThreshold(
        at=5.0,
        event_id="sanity_break",
        narrator_hint="hint",
        direction="down",
    )
    snap = _build_snapshot(
        resources={
            "sanity": _pool(
                "sanity",
                current=10.0,
                min=0.0,
                max=10.0,
                thresholds=[threshold],
            )
        }
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"pool": "sanity", "delta": -8}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["new_value"] == 2.0
    assert len(p["crossed_thresholds"]) == 1
    crossed = p["crossed_thresholds"][0]
    assert crossed["at"] == 5.0
    assert crossed["event_id"] == "sanity_break"
    assert crossed["direction"] == "down"


async def test_otel_span_carries_resource_attrs(otel_capture) -> None:
    snap = _build_snapshot(resources={"mana": _pool("mana", current=10.0)})
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="update_resource_pool",
            arguments={"pool": "mana", "delta": -3, "source": "minor cantrip"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["new_value"] == 7.0

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.update_resource_pool"]
    assert write_spans, f"no tool.write.update_resource_pool span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "update_resource_pool"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.resource.pool") == "mana"
    assert attrs.get("tool.resource.delta") == -3
    assert attrs.get("tool.resource.source") == "minor cantrip"
    assert attrs.get("tool.resource.value_after") == 7.0


async def test_parallel_update_runs_sequentially() -> None:
    """Concurrent dispatches for the same session share a WRITE lock.
    Both deltas must land cleanly (no torn read-modify-write)."""
    snap = _build_snapshot(resources={"mana": _pool("mana", current=10.0)})
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="r1",
                name="update_resource_pool",
                arguments={"pool": "mana", "delta": 3},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="r2",
                name="update_resource_pool",
                arguments={"pool": "mana", "delta": -5},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    # 10 + 3 - 5 = 8 if serialized; either order yields 8.
    assert reloaded.snapshot.resources["mana"].current == 8.0
