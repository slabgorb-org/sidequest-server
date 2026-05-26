"""Tests for the query_character tool — Phase C Task 6.

READ tool. First per-tool perception rule — exercises the _RULES table
end-to-end. The perception layer coarsens HP and drops sensitive sections
when the target is another party member; self / no-perspective gets the
exact sheet.

Plan deviation: ``"resources"`` was dropped from the ``include`` Literal.
Resources are session-scoped pools (ADR-033, Task 5 / update_resource_pool),
not per-character — adding them here would propagate scope confusion.
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
from sidequest.agents.tools import query_character as _query_character_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.turn import TurnManager


def _character(
    name: str,
    *,
    hp_current: int = 10,
    hp_max: int = 10,
    is_friendly: bool = True,
    char_class: str = "Delver",
    race: str = "Human",
    pronouns: str = "they/them",
    backstory: str = "A test hero.",
    stats: dict[str, int] | None = None,
    statuses: list[Status] | None = None,
    items: list[dict] | None = None,
    gold: int = 0,
) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=items or [], gold=gold),
        statuses=statuses or [],
        hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
    )
    return Character(
        core=core,
        backstory=backstory,
        char_class=char_class,
        race=race,
        pronouns=pronouns,
        stats=stats or {"str": 12, "dex": 14, "wis": 10},
        is_friendly=is_friendly,
    )


def _build_snapshot(*, characters: list[Character] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
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
    registered = default_registry._tools["query_character"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration / happy paths
# ---------------------------------------------------------------------------


def test_query_character_is_registered() -> None:
    assert "query_character" in default_registry.list_names()


async def test_happy_path_returns_stats_and_status() -> None:
    """Default include=[stats, status] — handler returns identity + stats + status."""
    alice = _character(
        "Alice",
        stats={"str": 14, "dex": 12, "wis": 10},
        statuses=[
            Status(text="inspired", severity=StatusSeverity.Boon, created_turn=2),
        ],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    # Identity always present
    assert p["character_id"] == "Alice"
    assert p["name"] == "Alice"
    assert p["race"] == "Human"
    assert p["char_class"] == "Delver"
    assert p["pronouns"] == "they/them"
    assert p["is_friendly"] is True
    # Default include
    assert p["stats"] == {"str": 14, "dex": 12, "wis": 10}
    assert isinstance(p["status"], list)
    assert p["status"][0]["text"] == "inspired"
    assert p["status"][0]["severity"] == "Boon"
    # Edge always present on self (it drives perception coarsening for non-self)
    assert p["hp_current"] == 10
    assert p["hp_max"] == 10
    assert p["hp_fraction"] == 1.0
    # Non-requested sections absent
    assert "backstory" not in p
    assert "inventory" not in p


async def test_include_backstory_only() -> None:
    alice = _character("Alice", backstory="Lost their family to the deep.")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice", "include": ["backstory"]}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["backstory"] == "Lost their family to the deep."
    assert "stats" not in p
    assert "status" not in p
    assert "inventory" not in p


async def test_include_inventory_returns_items_and_gold() -> None:
    alice = _character(
        "Alice",
        items=[{"name": "torch", "qty": 3}],
        gold=42,
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice", "include": ["inventory"]}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["inventory"] == {"items": [{"name": "torch", "qty": 3}], "gold": 42}
    assert "stats" not in p


async def test_empty_include_returns_identity_only() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice", "include": []}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["character_id"] == "Alice"
    assert p["name"] == "Alice"
    assert "stats" not in p
    assert "status" not in p
    assert "backstory" not in p
    assert "inventory" not in p


async def test_unknown_character_returns_not_found() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Zorblax"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Zorblax" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save — load() returns None.
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"character_id": "Alice"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_empty_character_id_rejected_by_args_model() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="query_character",
            arguments={"character_id": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# Perception coarsening (the load-bearing v1 test)
# ---------------------------------------------------------------------------


async def test_perception_self_returns_exact_sheet() -> None:
    """Through dispatch: perspective==target → full sheet through perception filter."""
    alice = _character(
        "Alice",
        stats={"str": 14, "dex": 12, "wis": 10},
        hp_current=6,
        hp_max=10,
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-self",
            name="query_character",
            arguments={"character_id": "Alice", "include": ["stats", "status"]},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    # Exact: stats present with values
    assert payload["stats"] == {"str": 14, "dex": 12, "wis": 10}
    # Exact: edge numbers visible
    assert payload["hp_current"] == 6
    assert payload["hp_max"] == 10
    # No hp_band on self — that's only for coarsened other-PC views
    assert "hp_band" not in payload


async def test_perception_other_pc_coarsens_to_band() -> None:
    """Through dispatch: perspective != target → stats dropped, hp_band added."""
    alice = _character("Alice")
    bob = _character(
        "Bob",
        stats={"str": 16, "dex": 8, "wis": 18},
        hp_current=4,
        hp_max=10,
        statuses=[Status(text="bleeding", severity=StatusSeverity.Wound)],
        backstory="Bob's secret backstory.",
        items=[{"name": "letter", "qty": 1}],
    )
    snap = _build_snapshot(characters=[alice, bob])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-other",
            name="query_character",
            arguments={
                "character_id": "Bob",
                "include": ["stats", "status", "inventory", "backstory"],
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    # Identity kept
    assert payload["character_id"] == "Bob"
    assert payload["name"] == "Bob"
    assert payload["race"] == "Human"
    assert payload["char_class"] == "Delver"
    assert payload["is_friendly"] is True
    # Exact stats / numbers / sensitive sections DROPPED
    assert "stats" not in payload
    assert "inventory" not in payload
    assert "backstory" not in payload
    assert "hp_current" not in payload
    assert "hp_max" not in payload
    assert "hp_fraction" not in payload
    # Status kept (visible)
    assert payload["status"][0]["text"] == "bleeding"
    # Band derived from hp_fraction=0.4 → bloodied (>0.25)
    assert payload["hp_band"] == "bloodied"


async def test_perception_none_perspective_returns_exact() -> None:
    """perspective_pc=None (e.g. pre-chargen / GM context) → no coarsening."""
    bob = _character("Bob", stats={"str": 11})
    snap = _build_snapshot(characters=[bob])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-none",
            name="query_character",
            arguments={"character_id": "Bob"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["stats"] == {"str": 11}
    assert "hp_band" not in payload


async def test_hp_band_thresholds() -> None:
    """All five edge bands are reachable through the coarsening rule."""
    alice = _character("Alice")
    # Build PCs at each band boundary.
    # Band boundaries (rule): unwounded >0.75 · wounded >0.5 · bloodied
    # >0.25 · staggering >0 · down ==0.
    band_cases = [
        ("Unwounded", 10, 10, "unwounded"),  # 1.0
        ("Wounded", 7, 10, "wounded"),  # 0.7 (in (0.5, 0.75])
        ("Bloodied", 4, 10, "bloodied"),  # 0.4 (in (0.25, 0.5])
        ("Staggering", 2, 10, "staggering"),  # 0.2 (in (0, 0.25])
        ("Down", 0, 10, "down"),  # 0.0
    ]
    chars = [alice] + [
        _character(name, hp_current=cur, hp_max=mx) for name, cur, mx, _ in band_cases
    ]
    snap = _build_snapshot(characters=chars)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    for name, _cur, _mx, expected_band in band_cases:
        out = await default_registry.dispatch(
            ToolUseBlock(
                id=f"t-band-{name}",
                name="query_character",
                arguments={"character_id": name},
            ),
            ctx,
        )
        payload = json.loads(out.content)
        assert payload["hp_band"] == expected_band, (
            f"{name}: expected {expected_band}, got {payload.get('hp_band')!r}"
        )


# ---------------------------------------------------------------------------
# OTEL — the dispatch span must carry per-tool attrs
# ---------------------------------------------------------------------------


async def test_otel_span_self_query_no_coarsening(otel_capture) -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-self",
            name="query_character",
            arguments={"character_id": "Alice", "include": ["stats", "status"]},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_character"]
    assert read_spans, f"no tool.read.query_character span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_character"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.character.id") == "Alice"
    # List-of-string attribute
    assert tuple(attrs.get("tool.character.include") or ()) == ("stats", "status")
    assert attrs.get("tool.character.perception_coarsened") is False


async def test_otel_span_other_pc_marks_coarsened(otel_capture) -> None:
    alice = _character("Alice")
    bob = _character("Bob", hp_current=3, hp_max=10)
    snap = _build_snapshot(characters=[alice, bob])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-other",
            name="query_character",
            arguments={"character_id": "Bob", "include": ["stats"]},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_character"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.character.id") == "Bob"
    assert attrs.get("tool.character.perception_coarsened") is True
