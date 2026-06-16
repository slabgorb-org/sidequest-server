"""Tests for the query_magic_state tool — Phase C Task 22.

READ tool over ``GameSnapshot.magic_state``. Perception is enforced
handler-side (not via the ``_RULES`` table) — self / no-perspective
returns the full ledger + spell lists; another PC's perspective gets
counts only with bar values hidden.

Coverage:
- ``magic_state=None`` → magic_state_present=False with OTEL flag.
- Self query returns bars + known/prepared/spent + control_tier.
- Other-PC query returns counts only (no bar values, no spell ids).
- ``perspective_pc=None`` is treated as self.
- Character with no entries returns empty collections + control_tier=0.
- All OTEL attributes are set on the dispatch span.
- Empty ``character_id`` is rejected by the args validator.
- No active session returns a fatal error.
- WorkingRecord with ``actor=character`` is counted in active_working_count.
"""

from __future__ import annotations

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
from sidequest.agents.tools import query_magic_state as _query_magic_state_module  # noqa: F401
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import MagicState, WorkingRecord


def _world_config() -> WorldMagicConfig:
    """Minimal config with two character bars (mana + sanity)."""
    return WorldMagicConfig(
        world_slug="testworld",
        genre_slug="caverns_and_claudes",
        allowed_sources=["innate"],
        active_plugins=["innate_v1"],
        intensity=0.5,
        world_knowledge=WorldKnowledge(primary="acknowledged", local_register="folkloric"),
        visibility={"primary": "acknowledged", "local_register": "folkloric"},
        hard_limits=[HardLimit(id="x", description="x")],
        cost_types=["mana", "sanity"],
        ledger_bars=[
            LedgerBarSpec(
                id="mana",
                scope="character",
                direction="down",
                range=(0.0, 10.0),
                threshold_low=2.0,
                consequence_on_low_cross="exhausted",
                starts_at_chargen=10.0,
            ),
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.25,
                consequence_on_low_cross="break",
                starts_at_chargen=1.0,
            ),
        ],
        narrator_register="plain",
    )


def _build_snapshot(*, magic_state: MagicState | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="testworld",
        turn_manager=TurnManager(interaction=1),
        characters=[],
        magic_state=magic_state,
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(
    store: SqliteStore,
    *,
    perspective_pc: str | None = "Alice",
) -> ToolContext:
    return ToolContext(
        world_id="testworld",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=3,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["query_magic_state"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_magic_state_is_registered() -> None:
    assert "query_magic_state" in default_registry.list_names()


# ---------------------------------------------------------------------------
# No magic-state world
# ---------------------------------------------------------------------------


async def test_no_magic_state_returns_present_false() -> None:
    """``snapshot.magic_state is None`` → magic_state_present=False payload."""
    snap = _build_snapshot(magic_state=None)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["character_id"] == "Alice"
    assert p["magic_state_present"] is False
    # No bar/spell fields should leak from this branch.
    assert "character_bars" not in p
    assert "known_spells" not in p


async def test_no_magic_state_records_otel_flag() -> None:
    snap = _build_snapshot(magic_state=None)
    store = _store_with(snap)
    span = MagicMock()
    ctx = ToolContext(
        world_id="testworld",
        session_id="s",
        perspective_pc="Alice",
        turn_number=1,
        store=store,
        otel_span=span,
        perception_filter=NarratorPerceptionFilter(),
    )

    await _call({"character_id": "Alice"}, ctx)
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["tool.magic.magic_state_present"] is False
    assert attrs["tool.magic.character_id"] == "Alice"
    assert attrs["tool.magic.active_spell_count"] == 0
    assert attrs["tool.magic.mana_remaining"] == -1.0


# ---------------------------------------------------------------------------
# Self query — full payload
# ---------------------------------------------------------------------------


async def test_self_query_returns_full_payload() -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    ms.known_spells["Alice"] = ["fireball", "magic_missile"]
    ms.prepared_spells["Alice"] = {1: ["magic_missile"], 3: ["fireball"]}
    ms.spent_spells["Alice"] = {1: ["light"]}
    ms.control_tier["Alice"] = 2

    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["magic_state_present"] is True
    assert p["is_self"] is True
    assert p["character_id"] == "Alice"
    # Character bars present with values.
    bar_ids = {b["bar_id"] for b in p["character_bars"]}
    assert bar_ids == {"mana", "sanity"}
    mana = next(b for b in p["character_bars"] if b["bar_id"] == "mana")
    assert mana["value"] == 10.0
    assert mana["max"] == 10.0
    # Spell lists exact.
    assert set(p["known_spells"]) == {"fireball", "magic_missile"}
    assert p["prepared_spells"] == {"1": ["magic_missile"], "3": ["fireball"]}
    assert p["spent_spells"] == {"1": ["light"]}
    assert p["control_tier"] == 2
    assert p["active_working_count"] == 0


async def test_self_query_perspective_none_treated_as_self() -> None:
    """``perspective_pc=None`` is the omniscient / pre-bind path."""
    ms = MagicState.from_config(_world_config())
    ms.add_character("Bob")
    ms.known_spells["Bob"] = ["bless"]

    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"character_id": "Bob"}, ctx)
    p = _payload(r)
    assert p["is_self"] is True
    assert p["known_spells"] == ["bless"]
    # Bar values must be visible on the omniscient path.
    assert p["character_bars"], "expected at least one character bar"


async def test_self_query_with_no_entries_returns_empty_collections() -> None:
    """No known/prepared/spent/control_tier entries → empties with control_tier=0."""
    ms = MagicState.from_config(_world_config())
    # No add_character call → no character bars either.
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Ghost")

    r = await _call({"character_id": "Ghost"}, ctx)
    p = _payload(r)
    assert p["magic_state_present"] is True
    assert p["is_self"] is True
    assert p["character_bars"] == []
    assert p["known_spells"] == []
    assert p["prepared_spells"] == {}
    assert p["spent_spells"] == {}
    assert p["control_tier"] == 0
    assert p["active_working_count"] == 0


# ---------------------------------------------------------------------------
# Other-PC query — coarsened
# ---------------------------------------------------------------------------


async def test_other_pc_query_returns_counts_only() -> None:
    """perspective != target → counts only, no bar values, no spell ids."""
    ms = MagicState.from_config(_world_config())
    ms.add_character("Bob")
    ms.known_spells["Bob"] = ["fireball", "magic_missile", "ward"]
    ms.prepared_spells["Bob"] = {1: ["magic_missile", "ward"], 3: ["fireball"]}
    ms.control_tier["Bob"] = 5

    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")  # not Bob

    r = await _call({"character_id": "Bob"}, ctx)
    p = _payload(r)
    assert p["magic_state_present"] is True
    assert p["is_self"] is False
    assert p["known_spell_count"] == 3
    assert p["prepared_spell_count"] == 3
    assert p["active_working_count"] == 0
    # No bar values or spell ids should leak.
    assert "character_bars" not in p
    assert "known_spells" not in p
    assert "prepared_spells" not in p
    assert "spent_spells" not in p
    assert "control_tier" not in p


async def test_other_pc_query_hides_mana_remaining_in_otel() -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Bob")

    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    span = MagicMock()
    ctx = ToolContext(
        world_id="testworld",
        session_id="s",
        perspective_pc="Alice",
        turn_number=1,
        store=store,
        otel_span=span,
        perception_filter=NarratorPerceptionFilter(),
    )

    await _call({"character_id": "Bob"}, ctx)
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["tool.magic.is_self"] is False
    assert attrs["tool.magic.mana_remaining"] == -1.0


# ---------------------------------------------------------------------------
# WorkingRecord filter
# ---------------------------------------------------------------------------


async def test_working_record_with_actor_counted() -> None:
    """WorkingRecord whose ``actor`` matches the queried character is counted."""
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    ms.working_log.append(
        WorkingRecord(
            plugin="innate_v1",
            mechanism="cast",
            actor="Alice",
            costs={"mana": 1.0},
            domain="evocation",
            narrator_basis="zap",
        )
    )
    ms.working_log.append(
        WorkingRecord(
            plugin="innate_v1",
            mechanism="cast",
            actor="Bob",
            costs={"mana": 1.0},
            domain="evocation",
            narrator_basis="other zap",
        )
    )

    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice"}, ctx)
    p = _payload(r)
    assert p["active_working_count"] == 1


# ---------------------------------------------------------------------------
# Validator / fatal errors
# ---------------------------------------------------------------------------


async def test_empty_character_id_rejected_by_args_model() -> None:
    snap = _build_snapshot(magic_state=None)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="query_magic_state",
            arguments={"character_id": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save → load() returns None.
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# OTEL — dispatch span carries the per-tool attrs
# ---------------------------------------------------------------------------


async def test_otel_span_self_query_attributes(otel_capture) -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-self",
            name="query_magic_state",
            arguments={"character_id": "Alice"},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_magic_state"]
    assert read_spans, f"no tool.read.query_magic_state span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_magic_state"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.magic.character_id") == "Alice"
    assert attrs.get("tool.magic.magic_state_present") is True
    assert attrs.get("tool.magic.is_self") is True
    assert attrs.get("tool.magic.active_spell_count") == 0
    # mana bar starts at 10.0 in the test config.
    assert attrs.get("tool.magic.mana_remaining") == 10.0


async def test_otel_span_other_pc_marks_not_self(otel_capture) -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Bob")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-other",
            name="query_magic_state",
            arguments={"character_id": "Bob"},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_magic_state"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.magic.character_id") == "Bob"
    assert attrs.get("tool.magic.is_self") is False
    assert attrs.get("tool.magic.mana_remaining") == -1.0


# ---------------------------------------------------------------------------
# Dispatch path returns serialized payload
# ---------------------------------------------------------------------------


async def test_dispatch_path_returns_json_payload() -> None:
    """End-to-end through default_registry.dispatch — payload must JSON-encode."""
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    ms.known_spells["Alice"] = ["shield"]
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-dispatch",
            name="query_magic_state",
            arguments={"character_id": "Alice"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["character_id"] == "Alice"
    assert payload["is_self"] is True
    assert payload["known_spells"] == ["shield"]
