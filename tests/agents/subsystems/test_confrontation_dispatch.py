"""Tests for the new confrontation subsystem dispatch handler (Story 59-4).

The confrontation handler is the engine-side of the ADR-113 Intent Router
spine. Story 59-4 introduces ``sidequest/agents/subsystems/confrontation.py``
with ``run_confrontation_dispatch`` — a SubsystemDispatch handler that calls
``instantiate_encounter_from_trigger`` BEFORE the narrator runs, so the
narrator narrates already-real state instead of self-reporting engagement
via the (now-retired) ``begin_confrontation`` sidecar lift.

These tests pin the handler's contract:

  AC1 (unit slice): given a SubsystemDispatch(subsystem="confrontation",
    params={"type": "negotiation"}), the handler mutates
    snapshot.encounter (encounter_type matches) and emits the
    ``encounter_confrontation_initiated_span`` OTEL span — the same span
    the legacy ``narration_apply.py:2528`` consumer site emits today,
    relocated into the handler so the new live path's OTEL coverage
    matches the old path.

  Handler registration: after import-time ``_register_defaults()``, the
    handler is reachable via the subsystems registry under the key
    ``"confrontation"`` and ``run_dispatch_bank`` invokes it.

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" — registration test asserts the
  handler is reachable from the dispatch bank, not just importable.
- "No Source-Text Wiring Tests" — registration test uses
  ``get_registered()`` reflection, not file grepping.
- "No Silent Fallbacks" — handler raises (does NOT swallow) on unknown
  encounter_type per ``instantiate_encounter_from_trigger`` contract;
  test pins that.
- "OTEL Observability Principle" — handler emits the encounter-init span
  so the GM panel can confirm engagement happened pre-narrator.

These tests FAIL TODAY by design — module does not exist yet.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# OTEL plumbing — isolated tracer/exporter per test (matches the watcher
# tests' convention so spans don't leak between cases).
# ---------------------------------------------------------------------------


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# Synthetic fixtures — minimal genre pack + snapshot for the negotiation
# confrontation case. Mirrors the shape used in tests/server/dispatch/.
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _confrontation_dispatch(
    *,
    enc_type: str = "negotiation",
    idempotency_key: str = "k-conf-1",
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="confrontation",
        params={"type": enc_type},
        idempotency_key=idempotency_key,
        confidence=1.0,
        visibility=_open_viz(),
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="I block his way and call the bluff.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _synthetic_pack_with_negotiation() -> Any:
    """Build a minimal GenrePack with one ConfrontationDef ('negotiation').

    Mirrors the canonical synthetic-pack helper at
    ``tests/server/test_59_1_confrontation_engagement.py`` (the 59-1
    fixture shape — kept verbatim so the GREEN-phase implementation
    cannot drift from the live ConfrontationDef contract).
    """
    from unittest.mock import MagicMock as _MagicMock

    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

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
    pack = _MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


def _snapshot_no_encounter(*, player_name: str = "Alice") -> GameSnapshot:
    """Snapshot with one player and no active encounter — the precondition
    the negotiation dispatch must satisfy to actually instantiate."""
    return GameSnapshot(
        genre_slug="test_negotiation_pack",
        world_slug="test_world",
        encounter=None,
        player_seats={"player:Alice": player_name},
    )


# ---------------------------------------------------------------------------
# AC1: handler creates encounter on snapshot — pre-narrator engagement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confrontation_handler_creates_encounter_on_snapshot() -> None:
    """AC1 (unit slice): the new handler reads ``dispatch.params["type"]``,
    calls ``instantiate_encounter_from_trigger`` against the supplied
    snapshot+pack, and mutates ``snapshot.encounter`` in place.

    This is the heart of the cutover. The narrator no longer creates the
    encounter from ``result.confrontation``; the dispatch handler does it
    pre-narrator so the narrator's snapshot view is already-real state.

    FAILS TODAY: module ``sidequest/agents/subsystems/confrontation.py``
    does not exist.
    """
    from sidequest.agents.subsystems.confrontation import (  # type: ignore[import-not-found]
        run_confrontation_dispatch,
    )

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()
    dispatch = _confrontation_dispatch(enc_type="negotiation")

    out = await run_confrontation_dispatch(
        dispatch,
        snapshot=snap,
        pack=pack,
        player_name="Alice",
        npcs_present=[],
    )

    assert snap.encounter is not None, (
        "handler must mutate snapshot.encounter in place — narrator sees "
        "already-real engagement state when it runs after dispatch bank"
    )
    assert snap.encounter.encounter_type == "negotiation", (
        f"handler must honor dispatch.params['type']; got "
        f"encounter_type={snap.encounter.encounter_type!r}"
    )
    # SubsystemOutput contract — the handler returns directives + data.
    # The handler need not produce narrator directives (the encounter is
    # the directive), but the return MUST be a SubsystemOutput so the
    # bank doesn't crash on a None return.
    from sidequest.agents.subsystems import SubsystemOutput

    assert isinstance(out, SubsystemOutput)


@pytest.mark.asyncio
async def test_confrontation_handler_emits_encounter_initiated_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (OTEL leg): the handler must emit
    ``encounter_confrontation_initiated_span`` per the legacy
    ``narration_apply.py:2528`` consumer site. The span moves with the
    creation logic; relocating without re-emitting leaves the GM panel
    blind on the new path.

    The exact span name is asserted because the GM panel queries by name —
    a rename would silently break the dashboard.

    Span capture pattern mirrors ``tests/integration/test_npc_wiring.py``:
    monkeypatch ``spans_module.tracer`` to return a local TracerProvider's
    tracer so the span emitter (which calls ``tracer()``) writes into
    our InMemorySpanExporter without conflicting with the global tracer
    provider OTEL refuses to replace.
    """
    from sidequest.agents.subsystems.confrontation import (
        run_confrontation_dispatch,
    )
    from sidequest.telemetry import spans as spans_module

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer("test-59-4-handler")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()
    dispatch = _confrontation_dispatch()

    await run_confrontation_dispatch(
        dispatch,
        snapshot=snap,
        pack=pack,
        player_name="Alice",
        npcs_present=[],
    )

    span_names = [s.name for s in exporter.get_finished_spans()]
    assert any("encounter" in n and "initiated" in n for n in span_names), (
        "expected an encounter-initiated span (the legacy "
        "encounter_confrontation_initiated_span relocated into the "
        f"handler). Got spans: {span_names}"
    )


@pytest.mark.asyncio
async def test_confrontation_handler_no_op_when_encounter_already_active() -> None:
    """Re-entry guard: if ``snapshot.encounter`` is already active and
    unresolved, ``instantiate_encounter_from_trigger`` returns None
    (existing contract at ``encounter_lifecycle.py:268-270``). The handler
    must propagate this: do not crash, do not replace the active
    encounter, return a SubsystemOutput with no directives.

    Pins fail-loud non-violation — silence here is correct because the
    underlying function silently returns None, not because the handler
    swallowed an error.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.confrontation import (  # type: ignore[import-not-found]
        run_confrontation_dispatch,
    )
    from sidequest.game.encounter import EncounterMetric, StructuredEncounter

    active = StructuredEncounter(
        encounter_type="duel",
        player_metric=EncounterMetric(name="player", threshold=10),
        opponent_metric=EncounterMetric(name="opponent", threshold=10),
    )
    snap = _snapshot_no_encounter()
    snap.encounter = active
    pack = _synthetic_pack_with_negotiation()
    dispatch = _confrontation_dispatch(enc_type="negotiation")

    await run_confrontation_dispatch(
        dispatch,
        snapshot=snap,
        pack=pack,
        player_name="Alice",
        npcs_present=[],
    )

    # Existing duel was NOT clobbered.
    assert snap.encounter is active, (
        "handler must not replace an active unresolved encounter — "
        "existing engagement is authoritative"
    )
    assert snap.encounter.encounter_type == "duel"


@pytest.mark.asyncio
async def test_confrontation_handler_raises_on_unknown_encounter_type() -> None:
    """No-silent-fallback (memory ``feedback_no_fallbacks_hard``,
    CLAUDE.md "No Silent Fallbacks"): an unknown encounter_type must
    propagate as ValueError so the bank records it and the watcher
    catches the gap. Swallowing here would let the router dispatch
    something the engine can't engage, and the GM panel would see prose
    with no mechanical backing — the exact failure mode the spine exists
    to prevent.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.confrontation import (  # type: ignore[import-not-found]
        run_confrontation_dispatch,
    )

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()  # only "negotiation" is known
    dispatch = _confrontation_dispatch(enc_type="not_a_real_type")

    with pytest.raises(ValueError, match="unknown encounter_type"):
        await run_confrontation_dispatch(
            dispatch,
            snapshot=snap,
            pack=pack,
            player_name="Alice",
            npcs_present=[],
        )


# ---------------------------------------------------------------------------
# Handler registration — wiring test (CLAUDE.md "Every Test Suite Needs
# a Wiring Test"). Reflection-based, not source-grep.
# ---------------------------------------------------------------------------


def test_confrontation_handler_registered_with_dispatch_bank() -> None:
    """Wiring guarantee: importing ``sidequest.agents.subsystems`` runs
    ``_register_defaults()`` which must include the new confrontation
    handler under the key ``"confrontation"``. Without this, the bank's
    ``_REGISTRY.get("confrontation")`` returns None and dispatches log
    as ``unknown_subsystem`` — silently dropping engagement.

    FAILS TODAY: ``_register_defaults()`` only registers
    {reflect_absence, distinctive_detail_hint, npc_agency}.
    """
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "confrontation" in registry, (
        f"confrontation handler not registered; bank has {sorted(registry)}. "
        "Story 59-4 must add the registration in "
        "sidequest/agents/subsystems/__init__.py:_register_defaults()."
    )
    fn = registry["confrontation"]
    assert callable(fn) and getattr(fn, "__name__", "") == "run_confrontation_dispatch", (
        f"registered confrontation handler should be run_confrontation_dispatch; "
        f"got {fn!r}"
    )


@pytest.mark.asyncio
async def test_run_dispatch_bank_invokes_confrontation_handler() -> None:
    """End-to-end through the real dispatch bank: a package with one
    confrontation dispatch produces a snapshot whose encounter is set.
    This is the AC1 OTEL-order test's prerequisite — if the bank doesn't
    route the dispatch to the new handler, no encounter is created.

    Also pins the kwargs filtering: the bank passes only the kwargs the
    handler signature declares (per ``_filter_context_for_callable``).
    The handler must declare ``snapshot``, ``pack``, ``player_name``,
    ``npcs_present`` as kw-only or positional-or-keyword.

    FAILS TODAY: handler does not exist; bank logs unknown_subsystem.
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()
    package = _package_with(_confrontation_dispatch())

    await run_dispatch_bank(
        package,
        context={
            "snapshot": snap,
            "pack": pack,
            "player_name": "Alice",
            "npcs_present": [],
        },
    )

    assert snap.encounter is not None and snap.encounter.encounter_type == "negotiation", (
        "dispatch bank did not engage the confrontation engine — handler "
        "either unregistered, signature mismatch, or no-op'd silently"
    )
