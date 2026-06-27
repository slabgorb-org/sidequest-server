"""ADR-153 §2 firewall — wiring proof: a seated dogfight resolves via SWN
``hp_depletion``, and the ``encounter.resolved`` OTEL span fires with
``source="hp_depletion"`` — never a native dial sweep.

CLAUDE.md mandates a wiring test that drives the production resolution path and
asserts on OTEL, not source text (the GM panel is the lie detector). This is
the firewall proof for the whole story: when an NPC gun solution ablates the PC
frame HP to 0, the duel resolves off HP, and the span source is ``hp_depletion``
— proving the native energy dial (deleted from the def by Task 1) is genuinely
out of the resolution path.

This test PASSES against the current engine (``resolve_dogfight_shots`` already
routes through ``check_hp_depletion``); it locks the firewall against
regression — if a future change reintroduces a dial sweep on the dogfight path,
the ``dial_threshold_sweep`` guard below fails.

Run serially (``-n0``) to avoid the known OTEL span-count xdist deadlock
(``project_server_test_otel_deadlock``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.dogfight_shot import GunSolution, resolve_dogfight_shots
from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.genre.models.inventory import DamageSpec
from tests.fixtures.dogfight_playtest_encounter import make_seated_dogfight


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """In-memory OTEL exporter for span assertions (matches the pattern in
    tests/agents/conftest.py + test_innate_v1_cast_resolution.py)."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_dogfight_resolves_via_hp_depletion(otel_capture: InMemorySpanExporter) -> None:
    # PC frame at 1 HP; NPC has a guaranteed-hit, armor-piercing gun solution ->
    # hull to <=0 -> hp_depletion resolves the duel (NOT a dial or resolution beat).
    enc, edge_resolver = make_seated_dogfight(pc_hp=1, npc_hp=8)
    opponent = next(a for a in enc.actors if a.side == "opponent")
    pc = next(a for a in enc.actors if a.side == "player")

    gun_solution = GunSolution(
        shooter_role=opponent.role,
        shooter_name=opponent.name,
        target_role=pc.role,
        target_name=pc.name,
        attack=AttackRollParams(modifier=10, target_number=0),  # always hits
        weapon=DamageSpec(dice="1d4", armor_piercing=20),
        weapon_name="multifocal laser",
        target_armor=0,
        geometry_modifier=0,
    )

    res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=[gun_solution],
        d20_by_shooter={opponent.role: 20},
        edge_resolver=edge_resolver,
    )

    assert res.depletion is not None, "the PC frame hit 0 HP — depletion must resolve"
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory", (
        f"player frame down + opponent standing → opponent_victory, got {enc.outcome!r}"
    )

    spans = otel_capture.get_finished_spans()
    resolved = [s for s in spans if s.name == "encounter.resolved"]
    assert resolved, "encounter.resolved span must fire (GM-panel lie detector)"
    assert any(s.attributes.get("source") == "hp_depletion" for s in resolved), (
        "the resolution span must record source=hp_depletion, proving the SWN HP "
        "path (not a native dial) resolved the duel"
    )
    # Firewall: never resolved by a native dial sweep (deleted from the def by Task 1).
    assert all(s.attributes.get("source") != "dial_threshold_sweep" for s in resolved), (
        "a dogfight must never resolve via dial_threshold_sweep (ADR-153 firewall)"
    )
