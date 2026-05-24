"""Tests for the magic_working subsystem dispatch handler (Story 59-5).

The magic_working handler is the second engine on the ADR-113 Intent Router
spine (following the confrontation cutover in 59-4). Story 59-5 introduces
``sidequest/agents/subsystems/magic_working.py`` with
``run_magic_working_dispatch`` — a SubsystemDispatch handler that calls
``apply_magic_working`` BEFORE the narrator runs, so the narrator sees
already-applied magical state instead of self-reporting engagement via
the (now-retired) ``result.magic_working`` sidecar field in
``narration_apply.py:1684-1709``.

These tests pin the handler's contract:

  AC1 (unit slice): given a SubsystemDispatch(subsystem="magic_working",
    params={<valid MagicWorking dict>}), the handler calls
    apply_magic_working and records a WorkingRecord in
    snapshot.magic_state.working_log. Returns SubsystemOutput.

  AC2 (retirement guard): the narration_apply pipeline no longer calls
    apply_magic_working from result.magic_working — setting that field
    has no effect on the snapshot's magic_state.

  AC3 (lie-detector coverage): the dispatch engagement watcher emits
    dispatch_engagement.magic_working.mismatch when the router dispatches
    magic_working but no working_log entry matches. (Already shipped in
    59-3; these tests VERIFY existing coverage, not new implementation.)

  AC4 (wiring): the magic_working handler is registered in the dispatch
    bank under "magic_working" and bank invocation routes through.

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" — registration test asserts the
  handler is reachable from the dispatch bank, not just importable.
- "No Source-Text Wiring Tests" — registration test uses
  get_registered() reflection, not file grepping.
- "No Silent Fallbacks" — handler raises (does NOT swallow) on missing
  actor or invalid magic state.
- "OTEL Observability Principle" — handler emits magic_working OTEL span
  so the GM panel can confirm engagement happened pre-narrator.

AC1/AC2/AC4 tests FAIL TODAY by design — handler does not exist yet.
AC3 tests PASS TODAY — watcher coverage already shipped in 59-3.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot
from sidequest.magic.models import LedgerBarSpec, WorldKnowledge, WorldMagicConfig
from sidequest.magic.state import MagicState
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
# Synthetic fixtures — minimal magic config + snapshot for a simple
# working application. Mirrors the shape used in tests/server/dispatch/.
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


_MINIMAL_BAR = LedgerBarSpec(
    id="vitality",
    scope="character",
    direction="down",
    range=(0.0, 100.0),
    threshold_low=10.0,
    consequence_on_low_cross="exhaustion",
    starts_at_chargen=50.0,
)


def _minimal_magic_config() -> WorldMagicConfig:
    return WorldMagicConfig(
        world_slug="test_world",
        genre_slug="test_magic_pack",
        allowed_sources=["innate"],
        active_plugins=["innate"],
        intensity=0.5,
        world_knowledge=WorldKnowledge(primary="acknowledged"),
        visibility={"primary": "acknowledged"},
        hard_limits=[],
        cost_types=["vitality"],
        ledger_bars=[_MINIMAL_BAR],
        narrator_register="neutral",
    )


def _magic_working_params(*, actor: str = "Alice") -> dict[str, Any]:
    """Build a valid MagicWorking dict suitable for dispatch.params."""
    return {
        "plugin": "innate",
        "mechanism": "native",
        "actor": actor,
        "costs": {"vitality": 5.0},
        "domain": "elemental",
        "narrator_basis": "Cast a ward of protection against the flames",
    }


def _magic_working_dispatch(
    *,
    actor: str = "Alice",
    idempotency_key: str = "k-magic-1",
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="magic_working",
        params=_magic_working_params(actor=actor),
        idempotency_key=idempotency_key,
        visibility=_open_viz(),
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="I cast a ward of protection against the flames.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _snapshot_with_magic(*, player_name: str = "Alice") -> GameSnapshot:
    """Snapshot with one player and a configured magic_state.

    The actor "Alice" has character-scope bars instantiated so
    apply_magic_working can debit costs.
    """
    config = _minimal_magic_config()
    magic = MagicState.from_config(config)
    magic.add_character("Alice")
    return GameSnapshot(
        genre_slug="test_magic_pack",
        world_slug="test_world",
        magic_state=magic,
        player_seats={"player:Alice": player_name},
    )


def _snapshot_without_magic(*, player_name: str = "Alice") -> GameSnapshot:
    """Snapshot with no magic_state — used to test fail-loud behavior."""
    return GameSnapshot(
        genre_slug="test_magic_pack",
        world_slug="test_world",
        magic_state=None,
        player_seats={"player:Alice": player_name},
    )


# ---------------------------------------------------------------------------
# AC1: handler applies magic working on snapshot — pre-narrator engagement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_working_handler_applies_working_on_snapshot() -> None:
    """AC1 (unit slice): the handler reads dispatch.params, constructs
    a valid MagicWorking, and calls apply_magic_working against the
    snapshot's magic_state.

    After the handler returns, snapshot.magic_state.working_log must
    contain a record matching the dispatch actor — proving the working
    was applied pre-narrator.

    FAILS TODAY: module sidequest/agents/subsystems/magic_working.py
    does not exist.
    """
    from sidequest.agents.subsystems.magic_working import (
        run_magic_working_dispatch,
    )

    snap = _snapshot_with_magic()
    dispatch = _magic_working_dispatch(actor="Alice")

    out = await run_magic_working_dispatch(
        dispatch,
        snapshot=snap,
        pack=MagicMock(),
        player_name="Alice",
    )

    assert snap.magic_state is not None
    assert len(snap.magic_state.working_log) == 1, (
        "handler must apply the working — narrator sees already-applied "
        "magical state when it runs after dispatch bank"
    )
    record = snap.magic_state.working_log[0]
    assert record.actor == "Alice", (
        f"working record actor mismatch; got {record.actor!r}"
    )
    assert record.plugin == "innate"
    assert record.mechanism == "native"
    assert record.domain == "elemental"

    from sidequest.agents.subsystems import SubsystemOutput

    assert isinstance(out, SubsystemOutput)


@pytest.mark.asyncio
async def test_magic_working_handler_debits_costs() -> None:
    """AC1 (cost leg): the handler's apply_magic_working call debits
    the requested costs from the actor's ledger bars.

    Confirms the handler is calling the real apply path, not a stub
    that only appends to working_log.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.magic_working import (
        run_magic_working_dispatch,
    )

    snap = _snapshot_with_magic()
    bar_key = "character|Alice|vitality"
    initial_value = snap.magic_state.ledger[bar_key].value  # type: ignore[union-attr]

    dispatch = _magic_working_dispatch(actor="Alice")
    await run_magic_working_dispatch(
        dispatch,
        snapshot=snap,
        pack=MagicMock(),
        player_name="Alice",
    )

    final_value = snap.magic_state.ledger[bar_key].value  # type: ignore[union-attr]
    assert final_value < initial_value, (
        f"handler must debit costs; vitality was {initial_value}, "
        f"now {final_value} (expected decrease of 5.0)"
    )
    assert final_value == initial_value - 5.0


@pytest.mark.asyncio
async def test_magic_working_handler_emits_otel_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (OTEL leg): the handler must emit a magic-related OTEL span
    so the GM panel can confirm magic engagement happened pre-narrator.

    Span capture pattern mirrors test_confrontation_dispatch.py:
    monkeypatch spans_module.tracer to return a local TracerProvider's
    tracer.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.magic_working import (
        run_magic_working_dispatch,
    )
    from sidequest.telemetry import spans as spans_module

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer("test-59-5-handler")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    snap = _snapshot_with_magic()
    dispatch = _magic_working_dispatch()

    await run_magic_working_dispatch(
        dispatch,
        snapshot=snap,
        pack=MagicMock(),
        player_name="Alice",
    )

    span_names = [s.name for s in exporter.get_finished_spans()]
    assert any("magic" in n and "working" in n for n in span_names), (
        "expected a magic_working span (the legacy "
        "magic.working span relocated into the handler). "
        f"Got spans: {span_names}"
    )


# ---------------------------------------------------------------------------
# Fail-loud behavior — no silent fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_working_handler_raises_when_no_magic_state() -> None:
    """No-silent-fallback (memory feedback_no_fallbacks_hard): dispatching
    magic_working against a snapshot with no magic_state must raise, not
    silently no-op. The bank records the error span and the watcher
    catches the gap.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.magic_working import (
        run_magic_working_dispatch,
    )

    snap = _snapshot_without_magic()
    dispatch = _magic_working_dispatch()

    with pytest.raises(  # noqa: B017
        Exception,
    ):
        await run_magic_working_dispatch(
            dispatch,
            snapshot=snap,
            pack=MagicMock(),
            player_name="Alice",
        )


@pytest.mark.asyncio
async def test_magic_working_handler_raises_on_unknown_actor() -> None:
    """No-silent-fallback: a dispatch with an actor who has no
    instantiated character bars must raise, not silently succeed.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.magic_working import (
        run_magic_working_dispatch,
    )

    snap = _snapshot_with_magic()
    dispatch = _magic_working_dispatch(actor="NonExistentCharacter")

    with pytest.raises(  # noqa: B017
        Exception,
    ):
        await run_magic_working_dispatch(
            dispatch,
            snapshot=snap,
            pack=MagicMock(),
            player_name="NonExistentCharacter",
        )


# ---------------------------------------------------------------------------
# AC2: Retirement guard — result.magic_working sidecar path removed
# ---------------------------------------------------------------------------


def test_narration_apply_ignores_result_magic_working_sidecar() -> None:
    """AC2 (behavioral retirement guard): setting result.magic_working
    on a turn result must NOT cause apply_magic_working to fire via the
    narration_apply pipeline.

    After 59-5 retires the sidecar consumer at narration_apply.py:1684-1709,
    the only path to magic engagement is the dispatch handler. This test
    verifies the retirement by driving the real apply pipeline with a
    result carrying a valid magic_working field and asserting no
    working_log entry is created.
    """
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    snap = _snapshot_with_magic()
    assert len(snap.magic_state.working_log) == 0  # type: ignore[union-attr]

    result = MagicMock()
    result.magic_working = _magic_working_params(actor="Alice")
    result.location = None
    result.scene_mood = None
    result.confrontation = None
    result.npcs_present = None
    result.items = None
    result.items_gained = None
    result.items_lost = None
    result.status_changes = None
    result.narration = "A magical ward shimmers into existence."
    result.action_rewrite = None
    result.quest_updates = None
    result.lore_established = None
    result.beat_selections = None
    result.npc_pool = None
    result.plotted_course = None
    result.companion_changes = None
    result.morale = None

    room = room_for(snap)
    with contextlib.suppress(Exception):
        _apply_narration_result_to_snapshot(
            snap,
            result,
            "Alice",
            room=room,
        )

    assert len(snap.magic_state.working_log) == 0, (  # type: ignore[union-attr]
        "After 59-5 retirement, result.magic_working on a narration result "
        "must NOT trigger apply_magic_working. The sidecar consumer at "
        "narration_apply.py:1684-1709 should be removed."
    )


# ---------------------------------------------------------------------------
# AC3: Lie-detector watcher covers magic_working dispatch mismatches
# (Verifies existing 59-3 coverage — these tests should PASS TODAY)
# ---------------------------------------------------------------------------


def test_lie_detector_emits_mismatch_when_magic_dispatched_not_engaged() -> None:
    """AC3 (verification): the dispatch engagement watcher shipped in
    59-3 AC4 must emit dispatch_engagement.magic_working.mismatch when
    the router dispatches magic_working but no working_log entry matches.

    This test SHOULD PASS TODAY — it verifies existing watcher coverage,
    not new implementation.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    snap = _snapshot_with_magic()
    dispatch = _magic_working_dispatch(actor="Alice")
    package = _package_with(dispatch)

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert len(mismatches) == 1, (
        "watcher must detect magic_working dispatch with no matching "
        f"working_log entry; got {len(mismatches)} mismatches"
    )
    assert mismatches[0].subsystem == "magic_working"


def test_lie_detector_no_false_positive_when_magic_engaged() -> None:
    """AC3 (no false positive): when the dispatch handler has applied a
    matching working_log entry for the actor, the watcher must emit
    NO mismatch span.

    This test SHOULD PASS TODAY.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )
    from sidequest.magic.models import MagicWorking

    snap = _snapshot_with_magic()
    working = MagicWorking.model_validate(_magic_working_params(actor="Alice"))
    snap.magic_state.apply_working(working)  # type: ignore[union-attr]

    dispatch = _magic_working_dispatch(actor="Alice")
    package = _package_with(dispatch)

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)
    assert len(mismatches) == 0, (
        "watcher must NOT emit a mismatch when the working_log has a "
        f"matching entry; got {len(mismatches)} false positives"
    )


# ---------------------------------------------------------------------------
# AC4: Handler registration — wiring test (CLAUDE.md "Every Test Suite
# Needs a Wiring Test"). Reflection-based, not source-grep.
# ---------------------------------------------------------------------------


def test_magic_working_handler_registered_with_dispatch_bank() -> None:
    """Wiring guarantee: importing sidequest.agents.subsystems runs
    _register_defaults() which must include the magic_working handler
    under the key "magic_working". Without this, the bank's
    _REGISTRY.get("magic_working") returns None and dispatches log as
    unknown_subsystem — silently dropping engagement.

    FAILS TODAY: _register_defaults() does not include magic_working.
    """
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "magic_working" in registry, (
        f"magic_working handler not registered; bank has {sorted(registry)}. "
        "Story 59-5 must add the registration in "
        "sidequest/agents/subsystems/__init__.py:_register_defaults()."
    )
    fn = registry["magic_working"]
    assert callable(fn) and getattr(fn, "__name__", "") == "run_magic_working_dispatch", (
        f"registered magic_working handler should be run_magic_working_dispatch; "
        f"got {fn!r}"
    )


@pytest.mark.asyncio
async def test_run_dispatch_bank_invokes_magic_working_handler() -> None:
    """End-to-end through the real dispatch bank: a package with one
    magic_working dispatch produces a snapshot whose magic_state has a
    new working_log entry.

    Also pins the kwargs filtering: the bank passes only the kwargs the
    handler signature declares (per _filter_context_for_callable).

    FAILS TODAY: handler does not exist; bank logs unknown_subsystem.
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    snap = _snapshot_with_magic()
    package = _package_with(_magic_working_dispatch())

    await run_dispatch_bank(
        package,
        context={
            "snapshot": snap,
            "pack": MagicMock(),
            "player_name": "Alice",
        },
    )

    assert snap.magic_state is not None
    assert len(snap.magic_state.working_log) == 1, (
        "dispatch bank did not engage the magic engine — handler either "
        "unregistered, signature mismatch, or no-op'd silently"
    )
    assert snap.magic_state.working_log[0].actor == "Alice"
