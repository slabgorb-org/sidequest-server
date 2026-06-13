"""OTEL span tests for the environment_clock subsystem (Task 3.3).

The GM panel is the lie detector: the survival-clock burn must be provably
engaged vs. improvised. ``light.tick`` (INFO) fires once per environment_clock
tick on a resolved light pool — both the unlit burn path and the lit no-burn
path — carrying the burn's mechanical attributes (``light.current`` /
``light.max`` / ``region`` / ``lit`` / ``burned`` / ``crossed_threshold`` /
``penalty_applied``).

Spans are captured via an in-memory OTEL exporter monkeypatched onto
``spans.tracer`` (drive-and-assert, never a source-text grep), mirroring
``tests/agents/subsystems/test_equip_dispatch.py``.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems.environment_clock import run_environment_clock_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.resource_pool import ResourcePool, ResourceThreshold
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def _snap_with_light(current: float, character_name: str = "Delver") -> GameSnapshot:
    snap = GameSnapshot()
    snap.resources["light"] = ResourcePool(
        name="light",
        label="Light",
        current=current,
        min=0.0,
        max=6.0,
        voluntary=False,
        decay_per_turn=0.0,
        thresholds=[
            ResourceThreshold(at=1.0, event_id="guttering", narrator_hint="the torch is dying"),
            ResourceThreshold(at=0.0, event_id="dark", narrator_hint="the dark closes in"),
        ],
    )
    snap.characters.append(
        Character(
            core=CreatureCore(
                name=character_name,
                description="A torch-bearing delver.",
                personality="cautious",
                inventory=Inventory(),
            ),
            char_class="Fighter",
            race="Human",
            backstory="Descends into the dark.",
        )
    )
    return snap


def _dispatch(
    *, region: str = "entrance", lit: bool = False, character_name: str = "Delver"
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="environment_clock",
        params={"region": region, "lit": lit, "character_name": character_name},
        idempotency_key="environment_clock_1",
        confidence=1.0,
        visibility=_tag_all(),
    )


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-environment-clock")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _spans_named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


@pytest.mark.asyncio
async def test_burn_to_zero_emits_light_tick_with_penalty(capture_spans):
    """A burn to the pool floor emits one ``light.tick`` span carrying the
    burn's mechanical truth, including ``penalty_applied`` True."""
    snap = _snap_with_light(1.0)
    await run_environment_clock_dispatch(_dispatch(region="black_pit", lit=False), snapshot=snap)

    ticks = _spans_named(capture_spans, "light.tick")
    assert len(ticks) == 1
    attrs = ticks[0].attributes or {}
    assert attrs["light.current"] == 0.0
    assert attrs["light.max"] == 6.0
    assert attrs["region"] == "black_pit"
    assert attrs["lit"] is False
    assert attrs["burned"] is True
    assert attrs["penalty_applied"] is True


@pytest.mark.asyncio
async def test_unlit_burn_above_floor_emits_no_penalty(capture_spans):
    """A burn that stays above the floor emits ``light.tick`` with
    ``penalty_applied`` False (no darkness status minted)."""
    snap = _snap_with_light(6.0)
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)

    ticks = _spans_named(capture_spans, "light.tick")
    assert len(ticks) == 1
    attrs = ticks[0].attributes or {}
    assert attrs["light.current"] == 5.0
    assert attrs["burned"] is True
    assert attrs["penalty_applied"] is False


@pytest.mark.asyncio
async def test_lit_region_emits_light_tick_not_burned(capture_spans):
    """A lit region makes a real clock decision (no burn, clear penalty) and
    so emits ``light.tick`` with ``burned`` False — the GM panel sees the
    clock engaged-and-idle rather than dark."""
    snap = _snap_with_light(6.0)
    await run_environment_clock_dispatch(_dispatch(lit=True), snapshot=snap)

    ticks = _spans_named(capture_spans, "light.tick")
    assert len(ticks) == 1
    attrs = ticks[0].attributes or {}
    assert attrs["lit"] is True
    assert attrs["burned"] is False
    assert attrs["light.current"] == 6.0
    assert attrs["penalty_applied"] is False


@pytest.mark.asyncio
async def test_no_light_pool_emits_no_tick(capture_spans):
    """No ``light`` pool means the pack never declared the survival clock —
    a structured skip, not a tick. No ``light.tick`` span is emitted."""
    snap = GameSnapshot()
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    assert not _spans_named(capture_spans, "light.tick")
