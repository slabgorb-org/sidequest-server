"""Tests for the query_npc tool — Phase C Task 7.

READ tool. Second per-tool perception rule. The v1 rule strips the raw
``disposition_value`` from the payload when ``perspective_pc`` is set,
leaving only the qualitative ``attitude`` band visible to the narrator.
Per-PC dispositions are forward-looking; v1 has a single global
``Disposition`` per NPC (ADR-020 scaffolding).
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
from sidequest.agents.tools import query_npc as _query_npc_module  # noqa: F401
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Disposition
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _npc(
    name: str,
    *,
    description: str = "A quiet figure.",
    personality: str = "watchful",
    disposition: int = 0,
    location: str | None = "Tavern",
    last_seen_location: str | None = "Tavern",
    last_seen_turn: int = 2,
    pronouns: str | None = "they/them",
    appearance: str | None = "weathered cloak",
    age: str | None = "indeterminate",
    build: str | None = "lean",
    height: str | None = "average",
    distinguishing_features: list[str] | None = None,
    creature_id: str | None = None,
    threat_level: int | None = None,
    abilities: list[str] | None = None,
    morale: str | None = None,
) -> Npc:
    core = CreatureCore(
        name=name,
        description=description,
        personality=personality,
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=4, max=4, base_max=4),
    )
    return Npc(
        core=core,
        disposition=Disposition(disposition),
        location=location,
        last_seen_location=last_seen_location,
        last_seen_turn=last_seen_turn,
        pronouns=pronouns,
        appearance=appearance,
        age=age,
        build=build,
        height=height,
        distinguishing_features=distinguishing_features or [],
        creature_id=creature_id,
        threat_level=threat_level,
        abilities=abilities or [],
        morale=morale,
    )


def _build_snapshot(*, npcs: list[Npc] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=3),
        npcs=npcs or [],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(
    store,
    *,
    perspective_pc: str | None = "Alice",
    session_id: str = "s",
    turn: int = 3,
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
    registered = default_registry._tools["query_npc"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration / happy paths
# ---------------------------------------------------------------------------


def test_query_npc_is_registered() -> None:
    assert "query_npc" in default_registry.list_names()


async def test_happy_path_with_both_includes() -> None:
    """include_disposition=True + include_backstory=True → full payload."""
    npc = _npc(
        "Reverend Murchison",
        description="A pinched man with ink-stained cuffs.",
        personality="evasive",
        disposition=20,  # → friendly
        pronouns="he/him",
        appearance="ink-stained cuffs",
        distinguishing_features=["limp", "spectacles"],
    )
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call(
        {
            "npc_id": "Reverend Murchison",
            "include_disposition": True,
            "include_backstory": True,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    # Identity / always-present fields
    assert p["npc_id"] == "Reverend Murchison"
    assert p["name"] == "Reverend Murchison"
    assert p["description"] == "A pinched man with ink-stained cuffs."
    assert p["personality"] == "evasive"
    assert p["pronouns"] == "he/him"
    assert p["appearance"] == "ink-stained cuffs"
    assert p["distinguishing_features"] == ["limp", "spectacles"]
    assert p["location"] == "Tavern"
    assert p["last_seen_location"] == "Tavern"
    assert p["last_seen_turn"] == 2
    # Disposition section
    assert p["disposition_value"] == 20
    assert p["attitude"] == "friendly"
    # Backstory aliases description in v1
    assert p["backstory"] == "A pinched man with ink-stained cuffs."


async def test_include_disposition_false_drops_disposition_fields() -> None:
    npc = _npc("Murchison", disposition=-30)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call(
        {"npc_id": "Murchison", "include_disposition": False},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert "disposition_value" not in p
    assert "attitude" not in p
    # Identity still present
    assert p["name"] == "Murchison"


async def test_include_backstory_false_drops_backstory() -> None:
    """Default (include_backstory=False) → no backstory key."""
    npc = _npc("Murchison")
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"npc_id": "Murchison"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert "backstory" not in p
    # Default include_disposition=True → both disposition fields present
    assert "disposition_value" in p
    assert "attitude" in p


async def test_unknown_npc_returns_not_found() -> None:
    snap = _build_snapshot(npcs=[_npc("Murchison")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"npc_id": "Zorblax"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Zorblax" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store()
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"npc_id": "Murchison"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_empty_npc_id_rejected_by_args_model() -> None:
    snap = _build_snapshot(npcs=[_npc("Murchison")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="query_npc",
            arguments={"npc_id": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_creature_npc_surfaces_threat_and_abilities() -> None:
    npc = _npc(
        "Patient Butcher",
        creature_id="patient_butcher",
        threat_level=3,
        abilities=["meat hook drag", "render to fat"],
        morale="resolute",
    )
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"npc_id": "Patient Butcher"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["creature_id"] == "patient_butcher"
    assert p["threat_level"] == 3
    assert p["abilities"] == ["meat hook drag", "render to fat"]
    assert p["morale"] == "resolute"


# ---------------------------------------------------------------------------
# Perception coarsening — load-bearing v1 behavior
# ---------------------------------------------------------------------------


async def test_perception_with_perspective_strips_disposition_value() -> None:
    """Through dispatch: perspective is set → raw int dropped, attitude kept."""
    npc = _npc("Murchison", disposition=25)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-coarse",
            name="query_npc",
            arguments={"npc_id": "Murchison", "include_disposition": True},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert "disposition_value" not in payload
    assert payload["attitude"] == "friendly"
    # Identity kept
    assert payload["name"] == "Murchison"


async def test_perception_none_perspective_returns_exact_disposition() -> None:
    """perspective_pc=None (e.g. GM/debug context) → raw value preserved."""
    npc = _npc("Murchison", disposition=-25)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-none",
            name="query_npc",
            arguments={"npc_id": "Murchison", "include_disposition": True},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["disposition_value"] == -25
    assert payload["attitude"] == "hostile"


async def test_perception_with_include_disposition_false_no_field_to_coarsen() -> None:
    """include_disposition=False → no disposition fields, rule is a no-op."""
    npc = _npc("Murchison", disposition=20)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-no-disp",
            name="query_npc",
            arguments={"npc_id": "Murchison", "include_disposition": False},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert "disposition_value" not in payload
    assert "attitude" not in payload


# ---------------------------------------------------------------------------
# OTEL — dispatch span attribute coverage
# ---------------------------------------------------------------------------


async def test_otel_span_records_id_name_and_coarsened_flag(otel_capture) -> None:
    """perspective != None and include_disposition=True → coarsened=True."""
    npc = _npc("Murchison", disposition=10)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-1",
            name="query_npc",
            arguments={"npc_id": "Murchison", "include_disposition": True},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_npc"]
    assert read_spans, f"no tool.read.query_npc span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_npc"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.npc.id") == "Murchison"
    assert attrs.get("tool.npc.name") == "Murchison"
    assert attrs.get("tool.npc.perception_coarsened") is True


async def test_otel_span_none_perspective_marks_not_coarsened(otel_capture) -> None:
    npc = _npc("Murchison", disposition=10)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-2",
            name="query_npc",
            arguments={"npc_id": "Murchison"},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_npc"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.npc.perception_coarsened") is False


async def test_otel_span_include_disposition_false_marks_not_coarsened(
    otel_capture,
) -> None:
    """No disposition field in payload → nothing to coarsen → flag is False."""
    npc = _npc("Murchison", disposition=10)
    snap = _build_snapshot(npcs=[npc])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-3",
            name="query_npc",
            arguments={"npc_id": "Murchison", "include_disposition": False},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_npc"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.npc.perception_coarsened") is False
