"""Pre-narrator unregistered-subsystem gate — drops dispatches whose
``subsystem`` names no registered handler, before the dispatch bank AND before
the 59-3 lie-detector watcher reads ``turn_context.dispatch_package``
(Story 71-27, ADR-113).

Playtest coyote_star MP (2026-05-27) surfaced the router emitting a ``combat``
dispatch with no registered handler. ``combat`` is a confrontation *type*
(``params["type"]``) routed through the ``confrontation`` subsystem — it is NOT
a subsystem key. The dispatch bank already drops such an unknown subsystem with
a warning + ``error="unknown_subsystem"`` attribute, but only AFTER it has
already polluted the package the narrator-redaction path and the post-turn
watcher read. The fix is the "stop emitting" half of the story title: gate the
unhandlable dispatch out of the package in the pre-narrator pass and emit a loud
``intent_router.dispatch.unregistered`` span per drop (registering a ``combat``
handler would be a stub for a non-subsystem — CLAUDE.md "No Stubbing").

The span is DISTINCT from ``intent_router.dispatch.gated`` (Story 59-8): a gated
dispatch is a valid subsystem merely inert on this snapshot (a world-shape
skip); an unregistered dispatch is a ROUTER DEFECT — a name that has no handler
at all. Keeping them separate lets the GM-panel lie-detector tell the two apart.

Module under test:
- ``sidequest/agents/dispatch_precondition_gate.py``

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" —
  ``test_router_pass_gates_unregistered_combat_dispatch`` drives the real
  ``execute_intent_router_pre_narrator_pass`` and asserts the unhandlable
  dispatch never reaches the returned package (the object the watcher reads).
- "No Source-Text Wiring Tests" — wiring proven by behavior (the returned
  package contents + the registry), never by grepping source.
- "OTEL Observability Principle" — every gate decision emits a span; a clean
  turn emits zero.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.subsystems import get_registered
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# OTEL plumbing — isolated tracer/exporter per test
# ---------------------------------------------------------------------------


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _dispatch(
    *,
    subsystem: str,
    params: dict[str, Any],
    idempotency_key: str,
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params,
        idempotency_key=idempotency_key,
        confidence=1.0,
        visibility=_open_viz(),
    )


def _combat_dispatch(*, key: str = "k-combat-1") -> SubsystemDispatch:
    """The Playtest bug shape: the router names ``combat`` (a confrontation
    *type*) as if it were a subsystem key. No handler is registered for it."""
    return _dispatch(
        subsystem="combat",
        params={"type": "combat"},
        idempotency_key=key,
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="I draw my blade and lunge at the guard.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        scenario_state=None,
        player_seats={"player:Alice": "Alice"},
    )


def _all_dispatch_subsystems(package: DispatchPackage) -> list[str]:
    out: list[str] = []
    for pd in package.per_player:
        out.extend(d.subsystem for d in pd.dispatch)
    for ca in package.cross_player:
        out.extend(d.subsystem for d in ca.dispatch)
    return out


# A registry vocabulary that matches production at import time. The gate is
# injected with this set by the caller; using the live registry keeps the test
# honest about what "registered" means.
_REGISTERED = set(get_registered())


# ===========================================================================
# Pure function — gate_unregistered_subsystems(package, registered)
# ===========================================================================


def test_combat_dropped_because_no_registered_handler() -> None:
    """``combat`` is not a registered subsystem (it is a confrontation type) →
    structurally unhandlable → dropped, and an UnregisteredDispatch records it
    for the span layer."""
    from sidequest.agents.dispatch_precondition_gate import gate_unregistered_subsystems

    package = _package_with(_combat_dispatch())

    filtered, dropped = gate_unregistered_subsystems(package=package, registered=_REGISTERED)

    assert _all_dispatch_subsystems(filtered) == [], (
        "the unregistered 'combat' dispatch must be removed from the package; "
        f"got {_all_dispatch_subsystems(filtered)}"
    )
    assert len(dropped) == 1
    assert dropped[0].subsystem == "combat"
    assert dropped[0].idempotency_key == "k-combat-1"


def test_confrontation_kept_because_registered() -> None:
    """The real subsystem key ``confrontation`` (which serves combat via
    ``params["type"]``) passes through untouched — proving we drop the bogus
    key, not legitimate combat routing."""
    from sidequest.agents.dispatch_precondition_gate import gate_unregistered_subsystems

    package = _package_with(
        _dispatch(
            subsystem="confrontation",
            params={"type": "combat"},
            idempotency_key="k-conf",
        )
    )

    filtered, dropped = gate_unregistered_subsystems(package=package, registered=_REGISTERED)

    assert _all_dispatch_subsystems(filtered) == ["confrontation"]
    assert dropped == []


def test_unregistered_dropped_but_sibling_registered_dispatch_preserved() -> None:
    """Selective filtering: a turn that dispatches the bogus ``combat`` AND a
    real ``confrontation`` drops only the unhandlable one; the registered
    dispatch survives the package rebuild."""
    from sidequest.agents.dispatch_precondition_gate import gate_unregistered_subsystems

    package = _package_with(
        _combat_dispatch(key="k-combat-1"),
        _dispatch(
            subsystem="confrontation",
            params={"type": "ship_combat"},
            idempotency_key="k-conf",
        ),
    )

    filtered, dropped = gate_unregistered_subsystems(package=package, registered=_REGISTERED)

    assert _all_dispatch_subsystems(filtered) == ["confrontation"]
    assert [d.subsystem for d in dropped] == ["combat"]


def test_nothing_dropped_returns_package_unchanged() -> None:
    """Quiet turn: an all-registered package is returned as the SAME object
    (no needless copy) with an empty drop list."""
    from sidequest.agents.dispatch_precondition_gate import gate_unregistered_subsystems

    package = _package_with(
        _dispatch(subsystem="movement", params={"direction": "deeper"}, idempotency_key="k-mv")
    )

    filtered, dropped = gate_unregistered_subsystems(package=package, registered=_REGISTERED)

    assert filtered is package
    assert dropped == []


# ===========================================================================
# OTEL wrapper — run_unregistered_subsystem_gate emits one span per drop
# ===========================================================================


def test_gate_emits_one_unregistered_span_per_drop() -> None:
    """Each dropped dispatch emits a loud intent_router.dispatch.unregistered
    span so the GM panel sees the router defect — not a silent fallback."""
    from sidequest.agents.dispatch_precondition_gate import run_unregistered_subsystem_gate

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_combat_dispatch())

    filtered = run_unregistered_subsystem_gate(
        package=package, registered=_REGISTERED, tracer=tracer
    )

    assert _all_dispatch_subsystems(filtered) == []
    spans = [
        s for s in exporter.get_finished_spans() if s.name == "intent_router.dispatch.unregistered"
    ]
    assert len(spans) == 1, (
        f"expected exactly 1 unregistered span, got "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "combat"
    assert attrs.get("idempotency_key") == "k-combat-1"


def test_gate_emits_no_span_when_nothing_dropped() -> None:
    """All-registered turn → zero unregistered spans (no false defect signal)."""
    from sidequest.agents.dispatch_precondition_gate import run_unregistered_subsystem_gate

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _dispatch(
            subsystem="confrontation",
            params={"type": "combat"},
            idempotency_key="k-conf",
        )
    )

    run_unregistered_subsystem_gate(package=package, registered=_REGISTERED, tracer=tracer)

    spans = [
        s for s in exporter.get_finished_spans() if s.name == "intent_router.dispatch.unregistered"
    ]
    assert spans == []


# ===========================================================================
# Wiring / end-to-end — the gate is live in the pre-narrator router pass and
# keeps the unhandlable dispatch out of the package the watcher/redaction read.
# ===========================================================================


def _synthetic_pack() -> Any:
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()
    return pack


@pytest.mark.asyncio
async def test_router_pass_gates_unregistered_combat_dispatch() -> None:
    """Wiring: drive the real pre-narrator router pass with a router that
    dispatches the bogus ``combat`` subsystem. The returned package — the same
    object assigned to ``turn_context.dispatch_package`` and read by the 59-3
    watcher and narrator redaction — must carry NO ``combat`` dispatch.

    This proves the gate is wired with the LIVE registry (the pass injects
    ``set(get_registered())``), not just unit-tested in isolation: ``combat`` is
    unregistered in production, so the real pass drops it.
    """
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _snapshot()
    pack = _synthetic_pack()
    package = _package_with(_combat_dispatch())

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I draw my blade and lunge at the guard.",
        player_name="Alice",
    )

    assert _all_dispatch_subsystems(returned) == [], (
        "the pre-narrator pass must gate the unregistered 'combat' dispatch out "
        "of the package it returns (the same turn_context.dispatch_package the "
        f"watcher reads); got {_all_dispatch_subsystems(returned)}"
    )


@pytest.mark.asyncio
async def test_router_pass_preserves_registered_confrontation_dispatch() -> None:
    """Regression guard: a real ``confrontation`` dispatch (the correct way to
    route combat) MUST survive the pass untouched — the gate drops bogus keys,
    never legitimate combat routing."""
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _snapshot()
    pack = _synthetic_pack()
    package = _package_with(
        _dispatch(
            subsystem="confrontation",
            params={"type": "combat"},
            idempotency_key="k-conf",
        )
    )

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I draw my blade and lunge at the guard.",
        player_name="Alice",
    )

    assert "confrontation" in _all_dispatch_subsystems(returned), (
        "a registered confrontation dispatch must pass through the pass "
        f"untouched; got {_all_dispatch_subsystems(returned)}"
    )
