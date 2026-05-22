"""Tests for the begin_confrontation tool — Story 59-1.

WRITE tool. The SDK engagement writer the narrator backend was missing:
on the default ``anthropic_sdk`` path no tool could START a confrontation
(advance_confrontation only advances an active one; generate_encounter is a
fatal stub). begin_confrontation creates the StructuredEncounter during the
tool-dispatch loop, reusing ``instantiate_encounter_from_trigger`` — the same
helper the legacy narration_apply consumer calls.

Covers: registration, the AC2 engagement-field schema, the encounter-creation
happy path, the already-active guard, fail-loud on missing pack/PC, and the
mandatory registry-dispatch wiring test. Fixtures are synthetic — never a live
genre_packs/* pack (project rule).
"""

from __future__ import annotations

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
from sidequest.agents.tools import begin_confrontation as _begin_confrontation_module  # noqa: F401
from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef, RulesConfig

_ENGAGEMENT_FIELD_NAMES = ("confrontation", "confrontation_type")


def _negotiation_pack() -> GenrePack:
    """Synthetic social pack with a single ``negotiation`` ConfrontationDef.
    Mirrors tests/server/test_59_1_confrontation_engagement.py.
    """
    cdef = ConfrontationDef(
        type="negotiation",
        label="Negotiation",
        category="social",
        player_metric=MetricDef(name="leverage", starting=0, threshold=10),
        opponent_metric=MetricDef(name="leverage", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "press",
                    "label": "Press the Point",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "CHA",
                }
            )
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


def _snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=1),
    )
    snap.character_locations["Neil"] = "The Bridge"
    return snap


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(
    store: SqliteStore,
    *,
    pack: GenrePack | None,
    perspective_pc: str | None = "Neil",
) -> ToolContext:
    return ToolContext(
        world_id="test_world",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=pack,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["begin_confrontation"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _otel(ctx: ToolContext) -> dict[str, Any]:
    span = cast(MagicMock, ctx.otel_span)
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


# ---------------------------------------------------------------------------
# Registration + AC2 engagement-field schema
# ---------------------------------------------------------------------------


def test_begin_confrontation_is_registered() -> None:
    assert "begin_confrontation" in default_registry.list_names()


def test_begin_confrontation_exposes_engagement_field() -> None:
    """AC2: begin_confrontation's input schema lets the narrator set the
    confrontation engagement type — it is the writer AC2's detector finds."""
    d = next(d for d in default_registry.tool_definitions() if d.name == "begin_confrontation")
    props = d.input_schema.get("properties", {})
    assert any(field in props for field in _ENGAGEMENT_FIELD_NAMES), (
        f"begin_confrontation must expose an engagement field "
        f"{_ENGAGEMENT_FIELD_NAMES}; got props {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# Happy path — creates the StructuredEncounter during dispatch
# ---------------------------------------------------------------------------


async def test_begin_confrontation_creates_encounter() -> None:
    """The handler instantiates a StructuredEncounter of the requested social
    type on the loaded snapshot and persists it — the SDK engagement path."""
    store = _store_with(_snapshot())
    ctx = _make_ctx(store, pack=_negotiation_pack())

    r = await _call({"confrontation_type": "negotiation", "reason": "standoff"}, ctx)

    assert r.status is ToolResultStatus.OK
    saved = store.load()
    assert saved is not None
    assert saved.snapshot.encounter is not None
    assert saved.snapshot.encounter.encounter_type == "negotiation"
    assert saved.snapshot.encounter.resolved is False

    recorded = _otel(ctx)
    assert recorded["tool.begin_confrontation.type"] == "negotiation"
    assert recorded["tool.begin_confrontation.created"] is True


# ---------------------------------------------------------------------------
# Already-active guard — recoverable, advance_confrontation's job instead
# ---------------------------------------------------------------------------


async def test_begin_confrontation_refuses_when_encounter_active() -> None:
    snap = _snapshot()
    snap.encounter = StructuredEncounter(
        encounter_type="negotiation",
        player_metric=EncounterMetric(name="leverage", current=2, threshold=10),
        opponent_metric=EncounterMetric(name="leverage", current=0, threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, pack=_negotiation_pack())

    r = await _call({"confrontation_type": "negotiation"}, ctx)

    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert "already active" in (r.message or "")
    assert _otel(ctx)["tool.begin_confrontation.created"] is False


# ---------------------------------------------------------------------------
# Fail loud — no silent fallback when the wiring is incomplete
# ---------------------------------------------------------------------------


async def test_begin_confrontation_fails_loud_without_pack() -> None:
    store = _store_with(_snapshot())
    ctx = _make_ctx(store, pack=None)

    r = await _call({"confrontation_type": "negotiation"}, ctx)

    assert r.status is ToolResultStatus.ERROR_FATAL
    assert "no genre pack" in (r.message or "")


async def test_begin_confrontation_fails_loud_without_perspective_pc() -> None:
    store = _store_with(_snapshot())
    ctx = _make_ctx(store, pack=_negotiation_pack(), perspective_pc=None)

    r = await _call({"confrontation_type": "negotiation"}, ctx)

    assert r.status is ToolResultStatus.ERROR_FATAL
    assert "no perspective PC" in (r.message or "")


# ---------------------------------------------------------------------------
# Wiring test (CLAUDE.md mandate) — reachable through the real dispatch path
# ---------------------------------------------------------------------------


async def test_begin_confrontation_dispatched_through_registry_engages() -> None:
    """Drive the tool through ``default_registry.dispatch`` (the production
    SDK tool-dispatch entry) and assert it surfaces success AND mutated state —
    proving the engagement writer is wired, not just registered."""
    store = _store_with(_snapshot())
    ctx = _make_ctx(store, pack=_negotiation_pack())

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-begin",
            name="begin_confrontation",
            arguments={"confrontation_type": "negotiation"},
        ),
        ctx,
    )

    assert out.is_error is False
    saved = store.load()
    assert saved is not None and saved.snapshot.encounter is not None
    assert saved.snapshot.encounter.encounter_type == "negotiation"
