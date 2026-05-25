"""Tests for the query_scene_state tool — Phase C Task 15.

READ tool. Surfaces the current scene's *room* (perspective-PC current_room,
with fallback chain), *beat* (StructuredEncounter.beat), *tension*
(ScenarioState.tension), and an optional minimal *scenario* section
(resolved, discovered_clue_count, guilty_npc).

v1 "hide nothing" — no perception rule registered; raw payload always.

Plan deviations exercised here:

* The plan listed three sections (room/beat/tension). We accept a fourth
  ``"scenario"`` section because ``ScenarioState`` carries useful narrator
  surface (``resolved``, ``len(discovered_clues)``, ``guilty_npc``) that
  doesn't belong on the flat tension scalar. The three plan sections are
  still the default ``include``.

* OTEL attribute values cannot be ``None``. We use sentinels:

    * ``tool.scene.room_id`` — empty string when no room resolves.
    * ``tool.scene.beat`` — ``-1`` when no StructuredEncounter.
    * ``tool.scene.tension`` — ``-1.0`` when no ScenarioState.

* v1 "current room" is resolved from per-actor ``current_room`` (no
  ``GameSnapshot.scene_id`` exists). Priority:
    1. ``ctx.perspective_pc``'s ``Character.current_room`` (when set).
    2. Otherwise, first PC with a non-None ``current_room``.
    3. Otherwise, ``None``.
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
from sidequest.agents.tools import query_scene_state as _query_scene_state_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.persistence import SqliteStore
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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
        backstory="bs",
        char_class="Delver",
        race="Human",
        pronouns="they/them",
        stats={"str": 12, "dex": 14, "wis": 10},
        is_friendly=True,
        current_room=current_room,
    )


def _encounter(beat: int = 0) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="confrontation",
        player_metric=EncounterMetric(name="resolve", threshold=10),
        opponent_metric=EncounterMetric(name="menace", threshold=10),
        beat=beat,
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    encounter: StructuredEncounter | None = None,
    scenario_state: ScenarioState | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        encounter=encounter,
        scenario_state=scenario_state,
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(
    store: SqliteStore | MagicMock,
    *,
    perspective_pc: str | None = None,
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["query_scene_state"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_scene_state_is_registered() -> None:
    assert "query_scene_state" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Default include (room+beat+tension) happy path
# ---------------------------------------------------------------------------


async def test_default_include_returns_room_beat_tension() -> None:
    """No `include` arg → default to room+beat+tension."""
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=3),
        scenario_state=ScenarioState(tension=0.4),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["room_id"] == "bridge"
    assert p["beat"] == 3
    assert p["encounter_active"] is True
    assert p["tension"] == 0.4
    assert p["include"] == ["room", "beat", "tension"]
    # scenario section was not requested
    assert "scenario" not in p


# ---------------------------------------------------------------------------
# Section selection
# ---------------------------------------------------------------------------


async def test_include_room_only() -> None:
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=5),
        scenario_state=ScenarioState(tension=0.9),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": ["room"]}, ctx)
    p = _payload(r)
    assert p["room_id"] == "bridge"
    assert "beat" not in p
    assert "tension" not in p
    assert "scenario" not in p
    assert p["include"] == ["room"]


async def test_include_scenario_returns_scenario_section() -> None:
    state = ScenarioState(
        tension=0.7,
        resolved=True,
        guilty_npc="butler-001",
        discovered_clues={"c1", "c2", "c3"},
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="parlor")],
        scenario_state=state,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": ["scenario"]}, ctx)
    p = _payload(r)
    assert p["scenario"] == {
        "resolved": True,
        "discovered_clue_count": 3,
        "guilty_npc": "butler-001",
    }
    assert "room_id" not in p
    assert "beat" not in p
    assert "tension" not in p


async def test_include_all_four_sections() -> None:
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=2),
        scenario_state=ScenarioState(tension=0.5, resolved=False, guilty_npc=""),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call(
        {"include": ["room", "beat", "tension", "scenario"]},
        ctx,
    )
    p = _payload(r)
    assert p["room_id"] == "bridge"
    assert p["beat"] == 2
    assert p["tension"] == 0.5
    # guilty_npc="" coerces to None in payload (empty-string is the
    # ScenarioState pre-pick sentinel — not a real id).
    assert p["scenario"] == {
        "resolved": False,
        "discovered_clue_count": 0,
        "guilty_npc": None,
    }


async def test_empty_include_returns_minimal_payload() -> None:
    """include=[] → only the 'include' echo field; no sections present."""
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=1),
        scenario_state=ScenarioState(tension=0.2),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": []}, ctx)
    p = _payload(r)
    assert p == {"include": []}


# ---------------------------------------------------------------------------
# Room resolution heuristic
# ---------------------------------------------------------------------------


async def test_room_resolution_prefers_perspective_pc() -> None:
    """When perspective_pc is set and has a room, that wins over other PCs."""
    snapshot = _build_snapshot(
        characters=[
            _character("Alice", current_room="bridge"),
            _character("Bob", current_room="galley"),
        ],
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Bob")

    r = await _call({"include": ["room"]}, ctx)
    assert _payload(r)["room_id"] == "galley"


async def test_room_resolution_falls_back_to_first_pc_when_no_perspective() -> None:
    """perspective_pc is None → first PC with a current_room."""
    snapshot = _build_snapshot(
        characters=[
            _character("Alice", current_room=None),
            _character("Bob", current_room="galley"),
        ],
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc=None)

    r = await _call({"include": ["room"]}, ctx)
    assert _payload(r)["room_id"] == "galley"


async def test_room_resolution_falls_back_when_perspective_pc_has_no_room() -> None:
    """perspective_pc is set but their current_room is None → fallback chain."""
    snapshot = _build_snapshot(
        characters=[
            _character("Alice", current_room=None),
            _character("Bob", current_room="galley"),
        ],
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": ["room"]}, ctx)
    assert _payload(r)["room_id"] == "galley"


async def test_room_resolution_none_when_no_pc_has_a_room() -> None:
    snapshot = _build_snapshot(
        characters=[
            _character("Alice", current_room=None),
            _character("Bob", current_room=None),
        ],
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": ["room"]}, ctx)
    assert _payload(r)["room_id"] is None


# ---------------------------------------------------------------------------
# Optional snapshot fields → None
# ---------------------------------------------------------------------------


async def test_no_encounter_returns_beat_none_and_inactive_flag() -> None:
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=None,
        scenario_state=ScenarioState(tension=0.3),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": ["beat"]}, ctx)
    p = _payload(r)
    assert p["beat"] is None
    assert p["encounter_active"] is False


async def test_no_scenario_state_returns_tension_none() -> None:
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=1),
        scenario_state=None,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({"include": ["tension", "scenario"]}, ctx)
    p = _payload(r)
    assert p["tension"] is None
    assert p["scenario"] is None


# ---------------------------------------------------------------------------
# Session-level failure
# ---------------------------------------------------------------------------


async def test_no_session_returns_fatal_error() -> None:
    store = MagicMock()
    store.load.return_value = None
    ctx = _make_ctx(store)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# Dispatch + OTEL
# ---------------------------------------------------------------------------


async def test_dispatch_payload_round_trip() -> None:
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=4),
        scenario_state=ScenarioState(tension=0.6),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="query_scene_state",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["room_id"] == "bridge"
    assert payload["beat"] == 4
    assert payload["tension"] == 0.6


async def test_otel_attrs_on_hit(otel_capture) -> None:
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room="bridge")],
        encounter=_encounter(beat=7),
        scenario_state=ScenarioState(tension=0.8),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="query_scene_state",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_scene_state"]
    assert read_spans, f"no dispatch span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_scene_state"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.scene.room_id") == "bridge"
    assert attrs.get("tool.scene.beat") == 7
    # OTEL float values come back as float; allow tolerance.
    assert abs(cast(float, attrs.get("tool.scene.tension")) - 0.8) < 1e-9


async def test_otel_attrs_use_sentinels_when_fields_missing(otel_capture) -> None:
    """No room / no encounter / no scenario_state → sentinel attribute values."""
    snapshot = _build_snapshot(
        characters=[_character("Alice", current_room=None)],
        encounter=None,
        scenario_state=None,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-sentinel",
            name="query_scene_state",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_scene_state"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    # Sentinels: "" / -1 / -1.0
    assert attrs.get("tool.scene.room_id") == ""
    assert attrs.get("tool.scene.beat") == -1
    assert abs(cast(float, attrs.get("tool.scene.tension")) - (-1.0)) < 1e-9
