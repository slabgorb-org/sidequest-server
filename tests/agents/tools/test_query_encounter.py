"""Tests for the query_encounter tool — Phase C Task 18.

READ tool. Surfaces the active :class:`StructuredEncounter`:
encounter shell, both metric dials, and the combatant roster with
perception-coarsened edge data.

Perception (handler-side, *not* a registered rule)
--------------------------------------------------
Opponents surface as ``hp_band`` (ADR-078 severity band) only — never
raw HP, even from the perspective PC's narrator. Players and neutrals
surface raw ``hp_current``/``hp_max`` so the narrator can pace
party-side decisions decisively.

Edge band boundaries reuse Task 6's
:func:`sidequest.agents.narrator_perception_filter._hp_band` so the
unwounded/wounded/bloodied/staggering/down thresholds stay
single-sourced.

OTEL sentinel convention (matches query_scene_state):

* ``tool.encounter.id`` — empty string when no encounter.
* ``tool.encounter.beat`` — ``-1`` when no encounter.
* ``tool.encounter.combatant_count`` — ``0`` when no encounter.
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
from sidequest.agents.tools import query_encounter as _query_encounter_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _character(name: str, *, hp_current: int = 10, hp_max: int = 10) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
    )
    return Character(
        core=core,
        backstory="bs",
        char_class="Delver",
        race="Human",
        pronouns="they/them",
        stats={"str": 12, "dex": 14, "wis": 10},
        is_friendly=True,
    )


def _npc(name: str, *, hp_current: int = 10, hp_max: int = 10) -> Npc:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
    )
    return Npc(core=core)


def _encounter(
    *,
    beat: int = 0,
    actors: list[EncounterActor] | None = None,
    phase: EncounterPhase | None = None,
    outcome: str | None = None,
    resolved: bool = False,
    player_metric: EncounterMetric | None = None,
    opponent_metric: EncounterMetric | None = None,
    encounter_type: str = "brawl",
) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=player_metric or EncounterMetric(name="momentum", current=2, threshold=10),
        opponent_metric=opponent_metric or EncounterMetric(name="menace", current=1, threshold=10),
        beat=beat,
        structured_phase=phase,
        outcome=outcome,
        resolved=resolved,
        actors=actors or [],
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    npcs: list[Npc] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=npcs or [],
        encounter=encounter,
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
    registered = default_registry._tools["query_encounter"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_encounter_is_registered() -> None:
    assert "query_encounter" in default_registry.list_names()


# ---------------------------------------------------------------------------
# No-encounter short-circuit
# ---------------------------------------------------------------------------


async def test_no_encounter_returns_inactive() -> None:
    """``snapshot.encounter is None`` → minimal ``encounter_active=False`` payload."""
    snapshot = _build_snapshot(characters=[_character("Alice")], encounter=None)
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p == {"encounter_active": False}


async def test_no_encounter_sets_sentinel_otel_attrs() -> None:
    snapshot = _build_snapshot(characters=[_character("Alice")], encounter=None)
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    span = cast(MagicMock, ctx.otel_span)
    # MagicMock records every call; map (attr_name → value).
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.encounter.id"] == ""
    assert recorded["tool.encounter.beat"] == -1
    assert recorded["tool.encounter.combatant_count"] == 0


# ---------------------------------------------------------------------------
# Encounter shell — metrics, beat, phase, outcome
# ---------------------------------------------------------------------------


async def test_encounter_shell_round_trips() -> None:
    encounter = _encounter(
        beat=4,
        phase=EncounterPhase.Escalation,
        outcome=None,
        resolved=False,
        player_metric=EncounterMetric(name="resolve", current=3, threshold=10),
        opponent_metric=EncounterMetric(name="fear", current=2, threshold=8),
        encounter_type="confrontation",
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    r = await _call({}, ctx)
    p = _payload(r)
    assert p["encounter_active"] is True
    assert p["encounter_type"] == "confrontation"
    assert p["beat"] == 4
    assert p["structured_phase"] == EncounterPhase.Escalation.value
    assert p["outcome"] is None
    assert p["resolved"] is False
    assert p["player_metric"] == {
        "name": "resolve",
        "current": 3,
        "threshold": 10,
    }
    assert p["opponent_metric"] == {
        "name": "fear",
        "current": 2,
        "threshold": 8,
    }
    assert p["actors"] == []


async def test_structured_phase_none_serializes_as_none() -> None:
    encounter = _encounter(phase=None)
    snapshot = _build_snapshot(characters=[_character("Alice")], encounter=encounter)
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    assert p["structured_phase"] is None


async def test_resolved_outcome_surface() -> None:
    encounter = _encounter(resolved=True, outcome="player_victory")
    snapshot = _build_snapshot(characters=[_character("Alice")], encounter=encounter)
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    assert p["resolved"] is True
    assert p["outcome"] == "player_victory"


# ---------------------------------------------------------------------------
# Actor roster — perception coarsening for foes, raw for allies
# ---------------------------------------------------------------------------


async def test_player_actors_get_raw_edge() -> None:
    """``side="player"`` → ``hp_current`` / ``hp_max`` (no band)."""
    encounter = _encounter(
        actors=[
            EncounterActor(name="Alice", role="hero", side="player"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice", hp_current=7, hp_max=10)],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    assert p["actors"] == [
        {
            "name": "Alice",
            "role": "hero",
            "side": "player",
            "withdrawn": False,
            "hp_current": 7,
            "hp_max": 10,
        }
    ]


async def test_opponent_actor_gets_band_only_not_raw_edge() -> None:
    """``side="opponent"`` → ``hp_band`` only; raw current/max never appear."""
    encounter = _encounter(
        actors=[
            EncounterActor(name="Goblin", role="boss", side="opponent"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", hp_current=5, hp_max=10)],  # fraction=0.5 → bloodied
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    [entry] = p["actors"]
    assert entry["name"] == "Goblin"
    assert entry["side"] == "opponent"
    assert entry["hp_band"] == "bloodied"
    assert "hp_current" not in entry
    assert "hp_max" not in entry


async def test_neutral_actor_gets_raw_edge() -> None:
    """``side="neutral"`` → same raw surface as players."""
    encounter = _encounter(
        actors=[
            EncounterActor(name="Bystander", role="witness", side="neutral"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Bystander", hp_current=4, hp_max=10)],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    [entry] = p["actors"]
    assert entry["side"] == "neutral"
    assert entry["hp_current"] == 4
    assert entry["hp_max"] == 10
    assert "hp_band" not in entry


async def test_opponent_with_no_matching_creature_gets_unknown_band() -> None:
    """Roster entry without a matching ``CreatureCore`` → ``hp_band="unknown"``."""
    encounter = _encounter(
        actors=[
            EncounterActor(name="Phantom", role="boss", side="opponent"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[],  # no Phantom — only the actor stub exists
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    [entry] = p["actors"]
    assert entry["hp_band"] == "unknown"


async def test_opponent_with_zero_max_edge_gets_unknown_band() -> None:
    """``edge.max=0`` would zero-div the fraction → guarded to ``unknown``."""
    encounter = _encounter(
        actors=[
            EncounterActor(name="Wisp", role="minion", side="opponent"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Wisp", hp_current=0, hp_max=0)],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    [entry] = p["actors"]
    assert entry["hp_band"] == "unknown"


async def test_withdrawn_flag_surfaces() -> None:
    encounter = _encounter(
        actors=[
            EncounterActor(name="Alice", role="hero", side="player", withdrawn=True),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    assert p["actors"][0]["withdrawn"] is True


async def test_hp_band_boundaries_match_task_6() -> None:
    """Band boundaries reuse Task 6's ``_hp_band`` helper unchanged."""
    # fractions: 1.0 unwounded; 0.6 wounded; 0.5 wounded (=0.5 is NOT >0.5,
    # so falls through to bloodied per the strict-greater rule);
    # 0.25 staggering (=0.25 is NOT >0.25); 0.0 down.
    cases = [
        ("Pristine", 10, 10, "unwounded"),
        ("Bleeding", 6, 10, "wounded"),
        ("Halved", 5, 10, "bloodied"),  # 0.5 is not >0.5 → bloodied
        ("Bloody", 3, 10, "bloodied"),
        ("Edge", 1, 10, "staggering"),
        ("Down", 0, 10, "down"),
    ]
    actors = [
        EncounterActor(name=name, role="minion", side="opponent") for name, _c, _m, _b in cases
    ]
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc(name, hp_current=c, hp_max=m) for name, c, m, _b in cases],
        encounter=_encounter(actors=actors),
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    seen = {entry["name"]: entry["hp_band"] for entry in p["actors"]}
    expected = {name: band for name, _c, _m, band in cases}
    assert seen == expected


# ---------------------------------------------------------------------------
# Mixed roster smoke test
# ---------------------------------------------------------------------------


async def test_mixed_roster_each_side_surfaced_correctly() -> None:
    """Players + opponents + neutrals coexist; each gets the right shape."""
    encounter = _encounter(
        beat=2,
        actors=[
            EncounterActor(name="Alice", role="hero", side="player"),
            EncounterActor(name="Bob", role="hero", side="player"),
            EncounterActor(name="Goblin", role="boss", side="opponent"),
            EncounterActor(name="Bystander", role="witness", side="neutral"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[
            _character("Alice", hp_current=9, hp_max=10),
            _character("Bob", hp_current=4, hp_max=10),
        ],
        npcs=[
            _npc("Goblin", hp_current=3, hp_max=10),  # bloodied
            _npc("Bystander", hp_current=10, hp_max=10),
        ],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    p = _payload(await _call({}, ctx))
    by_name = {entry["name"]: entry for entry in p["actors"]}
    assert by_name["Alice"]["hp_current"] == 9
    assert by_name["Bob"]["hp_current"] == 4
    assert by_name["Goblin"]["hp_band"] == "bloodied"
    assert "hp_current" not in by_name["Goblin"]
    assert by_name["Bystander"]["hp_current"] == 10


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
    encounter = _encounter(
        beat=5,
        actors=[
            EncounterActor(name="Alice", role="hero", side="player"),
            EncounterActor(name="Goblin", role="boss", side="opponent"),
        ],
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice", hp_current=8, hp_max=10)],
        npcs=[_npc("Goblin", hp_current=2, hp_max=10)],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="query_encounter",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["beat"] == 5
    by_name = {a["name"]: a for a in payload["actors"]}
    assert by_name["Alice"]["hp_current"] == 8
    # 2/10 = 0.2 → staggering
    assert by_name["Goblin"]["hp_band"] == "staggering"
    assert "hp_current" not in by_name["Goblin"]


async def test_otel_attrs_on_active_encounter(otel_capture) -> None:
    encounter = _encounter(
        beat=6,
        actors=[
            EncounterActor(name="Alice", role="hero", side="player"),
            EncounterActor(name="Goblin", role="boss", side="opponent"),
            EncounterActor(name="Bystander", role="witness", side="neutral"),
        ],
        encounter_type="brawl",
    )
    snapshot = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin"), _npc("Bystander")],
        encounter=encounter,
    )
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="query_encounter",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_encounter"]
    assert read_spans, f"no dispatch span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_encounter"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.encounter.id") == "brawl"
    assert attrs.get("tool.encounter.beat") == 6
    assert attrs.get("tool.encounter.combatant_count") == 3


async def test_otel_attrs_use_sentinels_when_no_encounter(otel_capture) -> None:
    snapshot = _build_snapshot(characters=[_character("Alice")], encounter=None)
    ctx = _make_ctx(_store_with(snapshot), perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-sentinel",
            name="query_encounter",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_encounter"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.encounter.id") == ""
    assert attrs.get("tool.encounter.beat") == -1
    assert attrs.get("tool.encounter.combatant_count") == 0
