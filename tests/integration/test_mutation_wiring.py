"""End-to-end AWN mutation wiring test (AWN Plan 2, Task 12).

Mandatory integration proof per CLAUDE.md:
  "Every Test Suite Needs a Wiring Test"
  "assert spans, not source text"

Chain exercised:
  synthetic awn GenrePack with mutations catalog
  → init_mutation_state_for_session (Task 10)
  → use_mutation resolved from default_registry by name (barrel-import wiring)
  → CwnRulesetModule.apply_system_strain pays System Strain on real core
  → awn.mutation.used span fires
  → cwn.system_strain.delta span fires

The test is self-contained: no real genre-pack content required.
PG isolation follows the same pattern as tests/agents/tools/conftest.py
_pg_isolation but is expressed inline here (integration dir has no autouse
_pg_isolation, so it is managed explicitly).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    default_registry,
)
from sidequest.agents.tools import (  # noqa: F401 — side-effect: wires tools onto default_registry
    use_mutation as _use_mutation_module,
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
from sidequest.mutation.state import CharacterMutationState
from sidequest.server.mutation_init import init_mutation_state_for_session
from sidequest.telemetry import spans as spans_module

# ---------------------------------------------------------------------------
# Attribute map (canonical CWN/AWN full-word keys, standard-six values)
# ---------------------------------------------------------------------------

_AMAP = {
    "STRENGTH": "STR",
    "DEXTERITY": "DEX",
    "CONSTITUTION": "CON",
    "INTELLIGENCE": "INT",
    "WISDOM": "WIS",
    "CHARISMA": "CHA",
}

# ---------------------------------------------------------------------------
# Synthetic AWN pack with a mutations catalog
# (copied + adapted from tests/agents/test_use_mutation_tool.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAwnRules:
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


def _catalog() -> MutationCatalog:
    """Minimal valid catalog: one strain-costed per_scene positive + one at_will.

    Copied from tests/mutation/test_use_ops.py _catalog() builder, with
    exotic/acid_spit renamed to match the strain-costed per_scene shape.
    """
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
                id="exotic/acid_spit",
                name="Acid Spit",
                category="exotic",
                effect="spit acid",
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


# ---------------------------------------------------------------------------
# Character / snapshot builders
# ---------------------------------------------------------------------------


def _rux(*, strain_current: int = 0, strain_max: int = 10) -> Character:
    core = CreatureCore(
        name="Rux",
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


def _build_snapshot(char: Character) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="dead_lands",
        turn_manager=TurnManager(interaction=1),
        characters=[char],
        npcs=[],
    )
    return snap


# ---------------------------------------------------------------------------
# PG store helpers (inline — integration dir has no autouse _pg_isolation)
# ---------------------------------------------------------------------------


def _pg_store_with(snapshot: GameSnapshot, migrated_db: str) -> Any:
    """Build a PgSaveRepository isolated to a unique slug and seed it."""
    import uuid

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    # Truncate tables to ensure a clean starting state for this test.
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")

    import os

    os.environ["SIDEQUEST_DATABASE_URL"] = plain
    db_pool.close_pool()

    slug = f"wiring-{uuid.uuid4().hex[:8]}"
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode="solo",
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
    )
    repo.init_session()
    repo.save(snapshot)
    return repo


def _make_ctx(store: Any, pack: Any) -> ToolContext:
    return ToolContext(
        world_id="dead_lands",
        session_id="wiring-test",
        perspective_pc="Rux",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=pack,
    )


# ---------------------------------------------------------------------------
# OTEL tracer setup using InMemorySpanExporter
# (same pattern as tests/game/ruleset/test_cwn_system_strain.py)
# ---------------------------------------------------------------------------


def _make_exporter() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test.mutation_wiring")


# ---------------------------------------------------------------------------
# The wiring test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_wiring_end_to_end(
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full chain: synthetic AWN pack + mutations → chargen init → registry
    tool resolution → strain paid → both awn.mutation.used AND
    cwn.system_strain.delta spans captured.

    This is the lie detector: if any link is missing (barrel import, registry
    lookup, init importability, strain routing), the test fails loudly.
    """
    # Step 1 — Install the in-memory span exporter so production emit sites
    #           resolve to it via spans_module.tracer (same seam as the
    #           *_otel_wiring integration tests).
    exporter, local_tracer = _make_exporter()
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    # Step 2 — Build the synthetic AWN pack with a mutations catalog.
    catalog = _catalog()
    pack = _FakeAwnPack(mutations=catalog)

    # Step 3 — Build a snapshot with Rux (Mutant, SystemStrainPool max=10).
    char = _rux(strain_current=0, strain_max=10)
    snap = _build_snapshot(char)

    # Step 4 — Call init_mutation_state_for_session (Task 10 importability +
    #           functionality proof). Rux is class Mutant which IS in
    #           mp_economy.mutant_classes — seeds should populate.
    init_mutation_state_for_session(
        snap,
        catalog=catalog,
        character_name="Rux",
        character_class="Mutant",
        session_id="wiring-test",
    )
    assert snap.mutation_state is not None, (
        "init_mutation_state_for_session must populate mutation_state on the snapshot"
    )
    assert "Rux" in snap.mutation_state.characters, (
        "Rux (class=Mutant) must appear in mutation_state.characters after init"
    )

    # Give Rux ownership of the strain-costed mutation so use_mutation can fire.
    snap.mutation_state.characters["Rux"] = CharacterMutationState(
        mp_remaining=0,
        positive_ids=["exotic/acid_spit"],
    )

    # Step 5 — Persist the snapshot into Postgres (required by the tool's
    #           repository.load() / repository.save() contract).
    store = _pg_store_with(snap, migrated_db)

    # Step 6 — Resolve the tool from default_registry BY NAME.
    #           This is the barrel-import wiring check: if use_mutation.py is
    #           not imported in the tools __init__.py barrel, the key won't exist.
    assert "use_mutation" in default_registry._tools, (
        "'use_mutation' must be registered in default_registry — "
        "check that sidequest/agents/tools/__init__.py imports use_mutation"
    )
    registered = default_registry._tools["use_mutation"]

    # Step 7 — Invoke the tool handler (async).
    ctx = _make_ctx(store, pack)
    args = registered.args_model.model_validate(
        {"actor": "Rux", "mutation_id": "exotic/acid_spit", "target": ""}
    )
    result = await registered.handler(args, ctx)

    # Step 8 — Assert the tool payload: applied=True.
    assert result.payload is not None, "use_mutation must return a payload"
    payload = result.payload
    assert payload["applied"] is True, (
        f"use_mutation should apply: applied={payload['applied']!r}, "
        f"reason={payload.get('reason')!r}"
    )

    # Step 9 — Verify strain was paid on the RELOADED state (proves save() ran).
    reloaded = store.load()
    assert reloaded is not None, "store.load() must return state after tool save()"
    core = reloaded.snapshot.find_creature_core("Rux")
    assert core is not None, "Rux must survive round-trip through PG"
    assert core.system_strain is not None, "SystemStrainPool must survive round-trip"
    strain_cost = 2  # exotic/acid_spit has strain_cost=2
    assert core.system_strain.current == strain_cost, (
        f"strain current should be {strain_cost} after acid_spit; got {core.system_strain.current}"
    )

    # Step 10 — Assert BOTH required spans fired.
    span_names = [s.name for s in exporter.get_finished_spans()]
    assert "awn.mutation.used" in span_names, (
        f"Expected 'awn.mutation.used' span; captured spans: {span_names}"
    )
    assert "cwn.system_strain.delta" in span_names, (
        f"Expected 'cwn.system_strain.delta' span; captured spans: {span_names}"
    )
