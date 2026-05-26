"""Tests for the apply_status tool — Phase C Task 4.

WRITE tool. Plan called for a `duration_rounds` arg, but the engine's
real status model (sidequest/game/status.py, ADR-078) tracks recovery
via a severity *tier* — Scratch / Wound / Scar / Boon — not a round
counter. This adapter takes ``severity`` instead and constructs a real
``Status`` row.
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
from sidequest.agents.tools import apply_status as _apply_status_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.status import StatusSeverity
from sidequest.game.turn import TurnManager


def _character(name: str) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )


def _npc(name: str) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=8, max=8, base_max=8),
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


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(store: SqliteStore, *, session_id: str = "s", turn: int = 7) -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=turn,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["apply_status"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def test_apply_status_is_registered() -> None:
    assert "apply_status" in default_registry.list_names()


async def test_status_appended_and_persisted() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, turn=7)

    r = await _call({"target": "Alice", "text": "prone"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["target"] == "Alice"
    assert p["text"] == "prone"
    assert p["severity"] == "Scratch"  # default
    assert p["active_statuses"] == ["prone"]

    # Persisted across round-trip.
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Alice")
    assert core is not None
    assert len(core.statuses) == 1
    persisted = core.statuses[0]
    assert persisted.text == "prone"
    assert persisted.severity is StatusSeverity.Scratch
    assert persisted.absorbed_shifts == 0
    assert persisted.created_turn == 7
    assert persisted.created_in_encounter is None


async def test_default_severity_is_scratch() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "text": "dazed"}, ctx)
    assert r.status is ToolResultStatus.OK
    assert _payload(r)["severity"] == "Scratch"

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Alice")
    assert core is not None
    assert core.statuses[0].severity is StatusSeverity.Scratch


async def test_wound_severity_flows_through() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "target": "Alice",
            "text": "broken rib",
            "severity": "Wound",
            "source": "goblin spear",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["severity"] == "Wound"

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Alice")
    assert core is not None
    assert core.statuses[0].severity is StatusSeverity.Wound
    assert core.statuses[0].text == "broken rib"


async def test_boon_severity_flows_through_to_npc() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Mira")],
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"target": "Mira", "text": "inspired", "severity": "Boon"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Mira")
    assert core is not None
    assert core.statuses[0].severity is StatusSeverity.Boon


async def test_unknown_target_returns_not_found() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Nobody", "text": "prone"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Nobody" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save: load() returns None.
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "text": "prone"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_empty_text_rejected_by_args_model() -> None:
    """``min_length=1`` on the args model surfaces as a validation error
    through the registry dispatch, not the handler."""
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="apply_status",
            arguments={"target": "Alice", "text": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_invalid_severity_rejected_by_args_model() -> None:
    """Literal enforcement: an unknown severity string fails validation."""
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-sev",
            name="apply_status",
            arguments={"target": "Alice", "text": "prone", "severity": "Fatal"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_multiple_statuses_accumulate() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    await _call({"target": "Alice", "text": "prone"}, ctx)
    r = await _call({"target": "Alice", "text": "dazed", "severity": "Wound"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["active_statuses"] == ["prone", "dazed"]

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Alice")
    assert core is not None
    assert [s.text for s in core.statuses] == ["prone", "dazed"]
    assert [s.severity for s in core.statuses] == [
        StatusSeverity.Scratch,
        StatusSeverity.Wound,
    ]


async def test_otel_span_carries_status_attrs(otel_capture) -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="apply_status",
            arguments={
                "target": "Alice",
                "text": "charmed",
                "severity": "Wound",
                "source": "succubus glance",
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["text"] == "charmed"

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_status"]
    assert write_spans, f"no tool.write.apply_status span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "apply_status"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.status.target") == "Alice"
    assert attrs.get("tool.status.text") == "charmed"
    assert attrs.get("tool.status.severity") == "Wound"
    assert attrs.get("tool.status.source") == "succubus glance"


async def test_parallel_apply_status_runs_sequentially() -> None:
    """Concurrent dispatches for the same session share a WRITE lock.
    Both statuses must land cleanly (no torn read-modify-write)."""
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="s1",
                name="apply_status",
                arguments={"target": "Alice", "text": "prone"},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="s2",
                name="apply_status",
                arguments={"target": "Alice", "text": "dazed", "severity": "Wound"},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Alice")
    assert core is not None
    assert len(core.statuses) == 2
    assert {s.text for s in core.statuses} == {"prone", "dazed"}
