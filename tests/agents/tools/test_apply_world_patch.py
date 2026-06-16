"""Tests for the apply_world_patch tool — Phase C Task 27.

WRITE tool. ADR-011 escape hatch. v1 supports five top-level string
fields of ``WorldStatePatch``. Heavy OTEL on every invocation
(deprecation tracking — zero-spans criterion per ADR-011).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import apply_world_patch as _apply_world_patch_module  # noqa: F401
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager


def _build_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="testworld",
        turn_manager=TurnManager(interaction=1),
        characters=[],
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(store: SqliteStore, *, session_id: str = "s") -> ToolContext:
    return ToolContext(
        world_id="testworld",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=3,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["apply_world_patch"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_apply_world_patch_is_registered() -> None:
    assert "apply_world_patch" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy paths — each supported field
# ---------------------------------------------------------------------------


async def test_location_path_applies_to_snapshot() -> None:
    snap = _build_snapshot()
    # Seat Alice so the WorldStatePatch.location path has a seat to write
    # into; otherwise the implementation's "no seated PCs" fallback kicks
    # in and we still want to assert observable state changed.
    snap.player_seats = {"p1": "Alice"}
    snap.character_locations["Alice"] = "old place"
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"path": "/location", "value": "the crystal cavern", "reason": "scene change"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["path"] == "/location"
    assert p["value"] == "the crystal cavern"
    assert p["reason"] == "scene change"
    assert p["applied_field"] == "location"

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.character_locations["Alice"] == "the crystal cavern"


async def test_time_of_day_path_applies() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"path": "/time_of_day", "value": "midnight", "reason": "time skip"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.time_of_day == "midnight"


async def test_atmosphere_path_applies() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"path": "/atmosphere", "value": "tense and smoky", "reason": "mood shift"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.atmosphere == "tense and smoky"


async def test_current_region_path_applies() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "path": "/current_region",
            "value": "Tin Quarter",
            "reason": "party crossed the bridge",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.current_region == "Tin Quarter"


async def test_active_stakes_path_applies() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "path": "/active_stakes",
            "value": "rescue the merchant before dawn",
            "reason": "new ticking clock",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.active_stakes == "rescue the merchant before dawn"


# ---------------------------------------------------------------------------
# Rejection paths — recoverable errors
# ---------------------------------------------------------------------------


async def test_unsupported_path_returns_recoverable_error_with_supported_list() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "path": "/quest_log/main",
            "value": "find the amulet",
            "reason": "narrator wanted to set quest text",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message is not None
    assert "/quest_log/main" in r.message
    # Supported list surfaced so the narrator can pivot.
    assert "/location" in r.message
    assert "/time_of_day" in r.message


async def test_non_string_value_for_string_path_recoverable_error() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"path": "/location", "value": 42, "reason": "wrong type"},
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message is not None
    assert "/location" in r.message
    assert "string" in r.message


# ---------------------------------------------------------------------------
# Validator errors — empty path / empty reason
# ---------------------------------------------------------------------------


async def test_empty_path_rejected_by_args_model() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-path",
            name="apply_world_patch",
            arguments={"path": "", "value": "x", "reason": "x"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_empty_reason_rejected_by_args_model() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-reason",
            name="apply_world_patch",
            arguments={"path": "/location", "value": "anywhere", "reason": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# Fatal errors
# ---------------------------------------------------------------------------


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save → load() returns None.
    ctx = _make_ctx(store)

    r = await _call(
        {"path": "/location", "value": "anywhere", "reason": "no session"},
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# OTEL — heavy attrs on success AND on rejection
# ---------------------------------------------------------------------------


async def test_otel_span_carries_world_patch_attrs_on_success(otel_capture) -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-ok",
            name="apply_world_patch",
            arguments={
                "path": "/atmosphere",
                "value": "thick fog",
                "reason": "weather shift",
            },
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_world_patch"]
    assert write_spans, f"no tool.write.apply_world_patch span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "apply_world_patch"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.world_patch.path") == "/atmosphere"
    assert attrs.get("tool.world_patch.reason") == "weather shift"
    assert attrs.get("tool.world_patch.path_kind") == "atmosphere"
    assert attrs.get("tool.world_patch.supported") is True


async def test_otel_span_carries_attrs_on_unsupported_path_rejection(otel_capture) -> None:
    """Heavy OTEL on rejection — deprecation tracker counts these too."""
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-reject",
            name="apply_world_patch",
            arguments={
                "path": "/hp_changes/Alice",
                "value": -3,
                "reason": "tried to use escape hatch for damage",
            },
        ),
        ctx,
    )
    assert out.is_error is True

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_world_patch"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    # All four world-patch attrs set even though the call was rejected.
    assert attrs.get("tool.world_patch.path") == "/hp_changes/Alice"
    assert attrs.get("tool.world_patch.reason") == "tried to use escape hatch for damage"
    assert attrs.get("tool.world_patch.path_kind") == "hp_changes"
    assert attrs.get("tool.world_patch.supported") is False
    assert attrs.get("tool.result_status") == "error_recoverable"


# ---------------------------------------------------------------------------
# Sequential WRITE-lock — concurrent dispatches don't tear state
# ---------------------------------------------------------------------------


async def test_parallel_apply_world_patch_runs_sequentially() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="s1",
                name="apply_world_patch",
                arguments={
                    "path": "/atmosphere",
                    "value": "smoky",
                    "reason": "scene one",
                },
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="s2",
                name="apply_world_patch",
                arguments={
                    "path": "/time_of_day",
                    "value": "dusk",
                    "reason": "scene two",
                },
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    # Both writes landed — neither got clobbered by the other.
    assert reloaded.snapshot.atmosphere == "smoky"
    assert reloaded.snapshot.time_of_day == "dusk"


# ---------------------------------------------------------------------------
# Dispatch path returns serialized payload
# ---------------------------------------------------------------------------


async def test_dispatch_path_returns_json_payload() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-dispatch",
            name="apply_world_patch",
            arguments={
                "path": "/current_region",
                "value": "Iron Ward",
                "reason": "crossed the gate",
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["path"] == "/current_region"
    assert payload["value"] == "Iron Ward"
    assert payload["applied_field"] == "current_region"
