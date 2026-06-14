"""Tests for the use_mutation tool — Task 11 (AWN Plan 2 production caller).

WRITE tool. The narrator calls use_mutation(actor, mutation_id, target) to
resolve an AWN mutation use mechanically. This is the thin tool wrapper that
resolves the actor's CreatureCore and mutation state from the snapshot and
delegates to sidequest.mutation.use_ops.use_mutation — all rules live there.

Harness mirrored from tests/agents/tools/test_adjust_system_strain_tool.py:
same fixtures (pg_store_with, pg_empty_store), same ToolContext construction,
same registry-bypass invocation pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import (
    use_mutation as _use_mutation_module,  # noqa: F401
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import AwnConfig
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.state import CharacterMutationState, MutationState

# ---------------------------------------------------------------------------
# Attribute map required by AwnConfig
# ---------------------------------------------------------------------------

_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}


# ---------------------------------------------------------------------------
# Minimal duck-typed packs
# ---------------------------------------------------------------------------


@dataclass
class _FakeAwnRules:
    """Minimal rules object for an AWN pack."""

    ruleset: str = "awn"
    _awn_cfg: Any = None

    def __post_init__(self) -> None:
        if self._awn_cfg is None:
            self._awn_cfg = AwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> AwnConfig:
        return self._awn_cfg


@dataclass
class _FakeAwnPack:
    rules: _FakeAwnRules = None  # type: ignore[assignment]
    mutations: MutationCatalog | None = None

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _FakeAwnRules()


@dataclass
class _NonAwnRules:
    ruleset: str = "native"

    def ruleset_config(self) -> None:
        return None


@dataclass
class _NonAwnPack:
    rules: _NonAwnRules = None  # type: ignore[assignment]
    mutations: MutationCatalog | None = None

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _NonAwnRules()


# ---------------------------------------------------------------------------
# Catalog + state builders
# ---------------------------------------------------------------------------


def _catalog() -> MutationCatalog:
    """Minimal valid catalog: one strain-costed per_scene positive, one at_will."""
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(
            body_part=["a"] * 6,
            nature=["b"] * 6,
            flavor=["c"] * 12,
        ),
        negatives=[
            NegativeMutationDef(
                id="negative/frail",
                name="Frail",
                roll_range=(1, 100),
                effect="frail",
            )
        ],
        positives=[
            PositiveMutationDef(
                id="structure/crushing_jaws",
                name="Crushing Jaws",
                category="structure",
                effect="bite",
                strain_cost=2,
                usage="per_scene",
                uses_per_period=1,
            ),
            PositiveMutationDef(
                id="sense/dark_sight",
                name="Dark Sight",
                category="sense",
                effect="see in dark",
                strain_cost=0,
                usage="at_will",
            ),
        ],
    )


def _mutation_state(actor: str, *positive_ids: str) -> MutationState:
    return MutationState(
        characters={
            actor: CharacterMutationState(
                mp_remaining=0,
                positive_ids=list(positive_ids),
            ),
        }
    )


# ---------------------------------------------------------------------------
# Snapshot / character builders
# ---------------------------------------------------------------------------


def _awn_character(
    name: str,
    *,
    strain_current: int = 0,
    strain_max: int = 12,
) -> Character:
    core = CreatureCore(
        name=name,
        description="A mutant survivor.",
        personality="tenacious",
        inventory=Inventory(),
        hp=HpPool(current=8, max=8, base_max=8),
        system_strain=SystemStrainPool(
            current=strain_current,
            max=strain_max,
            permanent=0,
        ),
    )
    return Character(
        core=core,
        backstory="Born changed.",
        char_class="Mutant",
        race="Mutant",
    )


def _build_snapshot(
    characters: list[Character] | None = None,
    *,
    mutation_state: MutationState | None = None,
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="dead_lands",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=[],
    )
    snap.mutation_state = mutation_state
    return snap


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(
    store,
    *,
    session_id: str = "s",
    genre_pack: Any | None = None,
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Rux",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["use_mutation"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_use_mutation_is_registered() -> None:
    assert "use_mutation" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Test 1: non-AWN pack raises ValueError
# ---------------------------------------------------------------------------


async def test_refuses_non_awn_pack() -> None:
    char = _awn_character("Rux")
    snap = _build_snapshot(characters=[char])
    pack = _NonAwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="AWN"):
        await _call(
            {"actor": "Rux", "mutation_id": "structure/crushing_jaws"},
            ctx,
        )


# ---------------------------------------------------------------------------
# Test 2: unknown actor returns NOT_FOUND
# ---------------------------------------------------------------------------


async def test_not_found_for_unknown_actor() -> None:
    snap = _build_snapshot(characters=[])
    pack = _FakeAwnPack(mutations=_catalog())
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Ghost", "mutation_id": "structure/crushing_jaws"},
        ctx,
    )
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Ghost" in r.message


# ---------------------------------------------------------------------------
# Test 3: happy path — strain cost applied, save called
# ---------------------------------------------------------------------------


async def test_happy_path_applies_strain_and_returns_result() -> None:
    char = _awn_character("Rux", strain_current=0, strain_max=12)
    state = _mutation_state("Rux", "structure/crushing_jaws")
    snap = _build_snapshot(characters=[char], mutation_state=state)
    pack = _FakeAwnPack(mutations=_catalog())
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Rux", "mutation_id": "structure/crushing_jaws"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["applied"] is True

    # Strain cost of 2 was paid
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Rux")
    assert core is not None
    assert core.system_strain is not None
    assert core.system_strain.current == 2

    # repository.save was called (proven by the reload above returning the mutated state)


# ---------------------------------------------------------------------------
# Test 4: exhausted per-scene usage → applied=False, reason contains "limit_exhausted"
# ---------------------------------------------------------------------------


async def test_refusal_payload_round_trips() -> None:
    """Exhaust the per-scene use; second call returns applied=False with limit_exhausted."""
    char = _awn_character("Rux", strain_current=0, strain_max=12)
    state = _mutation_state("Rux", "structure/crushing_jaws")
    snap = _build_snapshot(characters=[char], mutation_state=state)
    pack = _FakeAwnPack(mutations=_catalog())
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="limit-test", genre_pack=pack)

    # First use — should succeed
    r1 = await _call(
        {"actor": "Rux", "mutation_id": "structure/crushing_jaws"},
        ctx,
    )
    assert r1.status is ToolResultStatus.OK
    assert _payload(r1)["applied"] is True

    # Second use — should be refused (per_scene limit = 1)
    r2 = await _call(
        {"actor": "Rux", "mutation_id": "structure/crushing_jaws"},
        ctx,
    )
    assert r2.status is ToolResultStatus.OK, "refusal is DATA for the narrator, not an error"
    p2 = _payload(r2)
    assert p2["applied"] is False
    assert "limit_exhausted" in p2["reason"]
