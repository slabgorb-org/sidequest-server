"""Shared pytest fixtures for sidequest-server game-layer tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome

# ---------------------------------------------------------------------------
# Minimal beat defs for use in unit tests
# ---------------------------------------------------------------------------

_TAUNT_BEAT = BeatDef.model_validate(
    {
        "id": "taunt",
        "label": "Taunt",
        "kind": "strike",
        "base": 2,
        "stat_check": "CHA",
        "class_filter": ["Fighter"],
        "effect": "Pull the next blow onto yourself",
    }
)

# Enemy strike beat — used by targeting tests (test_taunt_targeting.py).
# target_edge_delta=3 so apply_beat routes a concrete debit through
# _opposite_side_first_actor and into CreatureCore.apply_hp_delta.
_ENEMY_STRIKE_BEAT = BeatDef.model_validate(
    {
        "id": "enemy_strike",
        "label": "Enemy Strike",
        "kind": "strike",
        "base": 1,
        "stat_check": "STR",
        "target_edge_delta": 3,
        "target_select": "focus",
        "effect": "A basic enemy attack",
    }
)

# Enemy spread beat — used by taunt redirect tests (test_taunt_targeting.py).
# target_edge_delta=6 across 2 player-side targets = 3 per target, which is
# non-zero after integer division so apply_beat actually fires edge debits.
# Spec: 2026-05-10 class-mechanical-surface §8 — taunt damage redirect (cap 1/round).
_ENEMY_SPREAD_BEAT = BeatDef.model_validate(
    {
        "id": "enemy_spread",
        "label": "Enemy Spread Attack",
        "kind": "strike",
        "base": 1,
        "stat_check": "STR",
        "target_edge_delta": 6,
        "target_select": "spread",
        "effect": "A sweeping attack that hits all enemies",
    }
)

_BEAT_REGISTRY: dict[str, BeatDef] = {
    "taunt": _TAUNT_BEAT,
    "enemy_strike": _ENEMY_STRIKE_BEAT,
    "enemy_spread": _ENEMY_SPREAD_BEAT,
}

_OUTCOME_MAP: dict[str, RollOutcome] = {
    "crit_success": RollOutcome.CritSuccess,
    "success": RollOutcome.Success,
    "tie": RollOutcome.Tie,
    "fail": RollOutcome.Fail,
    "crit_fail": RollOutcome.CritFail,
}


def _make_creature_core(name: str, edge_max: int = 10) -> CreatureCore:
    """Build a minimal CreatureCore for use in HP mocks."""
    return CreatureCore(
        name=name,
        description=f"{name} (test creature)",
        personality="neutral",
        hp=HpPool(current=edge_max, max=edge_max, base_max=edge_max),
    )


@dataclass
class TauntTestEncounter:
    """Thin wrapper exposing a convenience ``resolve_beat`` for test callsites.

    Exposes:
    - ``enc``              — the ``StructuredEncounter`` (actor state + taunt state)
    - ``fighter_id``       — name of the Fighter PC actor
    - ``cleric_id``        — name of the Cleric PC actor
    - ``_cores``           — dict mapping actor name → CreatureCore (mutable edge pools)
    - ``enemy_strike_beat``— the _ENEMY_STRIKE_BEAT def for targeting tests
    - ``enemy_spread_beat``— the _ENEMY_SPREAD_BEAT def for spread-redirect tests
    """

    enc: StructuredEncounter
    fighter_id: str
    cleric_id: str
    _cores: dict[str, CreatureCore] = field(default_factory=dict)
    enemy_strike_beat: BeatDef = field(default_factory=lambda: _ENEMY_STRIKE_BEAT)
    enemy_spread_beat: BeatDef = field(default_factory=lambda: _ENEMY_SPREAD_BEAT)

    def edge_resolver(self, name: str) -> CreatureCore | None:
        """Return the CreatureCore for *name*, or None if unknown."""
        return self._cores.get(name)

    def resolve_beat(self, *, actor_id: str, beat_id: str, outcome: str) -> None:
        """Convenience wrapper: find the actor by name, look up the beat def,
        map the outcome string to ``RollOutcome``, then call ``apply_beat``."""
        actor = self.enc.find_actor(actor_id)
        if actor is None:
            raise ValueError(f"actor {actor_id!r} not found in encounter")

        beat_def = _BEAT_REGISTRY.get(beat_id)
        if beat_def is None:
            raise ValueError(
                f"beat {beat_id!r} not in test registry; add it to conftest._BEAT_REGISTRY"
            )

        roll_outcome = _OUTCOME_MAP.get(outcome)
        if roll_outcome is None:
            raise ValueError(f"outcome {outcome!r} not in map; use one of {list(_OUTCOME_MAP)}")

        apply_beat(self.enc, actor, beat_def, roll_outcome)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def taunt_test_encounter() -> TauntTestEncounter:
    """Build a minimal encounter: Fighter PC + Cleric PC + 2 opponents.

    Encounter starts unresolved with both dials at 0/10.  Actor names
    double as IDs — the encounter engine uses ``actor.name`` as the
    lookup key throughout.

    ``_cores`` is populated so that tests can pass ``helper.edge_resolver``
    directly to ``apply_beat`` and inspect Edge mutations after resolution.
    Fighter is listed first in the player side so it is the default
    first-actor target for enemy strikes when taunt is inactive.
    """
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="fighter-1", role="Fighter", side="player"),
            EncounterActor(name="cleric-1", role="Cleric", side="player"),
            EncounterActor(name="enemy-1", role="grunt", side="opponent"),
            EncounterActor(name="enemy-2", role="grunt", side="opponent"),
        ],
    )
    cores = {
        "fighter-1": _make_creature_core("fighter-1"),
        "cleric-1": _make_creature_core("cleric-1"),
        "enemy-1": _make_creature_core("enemy-1"),
        "enemy-2": _make_creature_core("enemy-2"),
    }
    return TauntTestEncounter(
        enc=enc,
        fighter_id="fighter-1",
        cleric_id="cleric-1",
        _cores=cores,
    )


@pytest.fixture
def otel_capture() -> Iterator:
    """In-memory OTEL span exporter for span-assertion tests.

    Mirrors the pattern in ``tests/server/conftest.py`` (Story 45-36 fix
    included: clear accumulated processors before adding the test one so
    spans from a prior test don't bleed through).

    Yields the ``InMemorySpanExporter``; call ``.get_finished_spans()``
    to inspect emitted spans after the code under test runs.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)

    # Drop accumulated processors from prior test invocations.
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
