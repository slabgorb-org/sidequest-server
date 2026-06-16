"""Wire-first boundary tests for Story 45-11 — turn_manager.round invariant.

Felix's Playtest 3 save ended at ``turn_manager.round = 65`` while
``SELECT MAX(round_number) FROM narrative_log = 72`` — a 7-round gap.
Round-keyed gating reads ``turn_manager.round`` as authoritative, so when
that counter lags the durable narrative log every gated subsystem
operates on stale data (Sebastien's lie-detector goes dark).

These tests exercise the *production* narration write pipeline
(``_execute_narration_turn`` → ``record_interaction`` →
``append_narrative``) — a unit test on ``TurnManager.advance_round()`` does
NOT satisfy the wire-first gate because the bug is precisely that the
production write sequence never calls the unit-tested method
(`context-story-45-11.md` — "Outermost reachable seam").

What these tests prove:

1. (AC1) The ``turn_manager.round_invariant`` span fires on EVERY narration
   turn regardless of whether the invariant holds (Sebastien needs to see
   "round=72, max_round=72" to know the detector is engaged, not only on
   violation).
2. (AC2) After the production fix wires ``advance_round`` into the
   resolution pipeline, ``snapshot.turn_manager.round`` matches
   ``MAX(narrative_log.round_number)`` after every tick — ``gap == 0``,
   ``holds == True``.
3. (AC2 negative) When divergence is synthesized mid-test (manually
   rolling ``round`` backward), the next tick's span fires with
   ``holds == False`` and ``gap > 0`` so the violation reaches the GM
   panel rather than being silently corrected.

These tests are RED until Story 45-11's GREEN phase lands the span emit
and the ``record_interaction`` lockstep advance.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.setup import init_tracer
from sidequest.telemetry.watcher_hub import WatcherHub
from tests.server.conftest import _build_turn_context_for_test

SPAN_NAME = "turn_manager.round_invariant"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
    """Install an in-memory exporter on the live TracerProvider so the
    invariant span can be inspected after a turn runs.

    Mirrors the pattern used by tests/server/test_turn_span_wiring.py — the
    only OTEL fixture pattern this repo blesses for span-content assertions.
    """
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
        exporter.clear()


def _max_narrative_round_via_sql(sd) -> int:
    """Read ``MAX(round_number)`` directly out of the narrative_log table.

    This bypasses any helper the production code adds — the test's contract
    is that ``snapshot.turn_manager.round`` matches the SQL ground truth,
    not whatever the helper happens to return.
    """
    row = sd.store._conn.execute("SELECT MAX(round_number) FROM narrative_log").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _narration_result(text: str = "ok") -> NarrationTurnResult:
    return NarrationTurnResult(
        narration=text,
        is_degraded=False,
        agent_duration_ms=1,
    )


# ---------------------------------------------------------------------------
# AC1 — span fires on every turn (whether or not invariant holds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_invariant_span_fires_once_per_narration_turn(
    otel_capture,
    session_handler_factory,
) -> None:
    """Drive 5 narration turns; expect exactly 5 ``turn_manager.round_invariant``
    spans, each with the full attribute set the GM panel charts.

    Span MUST fire on every tick, not only on violation — a detector that
    only fires when broken is invisible until something else breaks
    (`context-story-45-11.md` §"OTEL spans").
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_narration_result(),
    )

    for _ in range(5):
        # Each narration turn needs a fresh TurnContext (production builds one
        # per message); reusing a context across turns finalizes PhaseTimings
        # after the first turn and crashes the second.
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I look around.", turn_context)

    spans = otel_capture.get_finished_spans()
    invariant = [s for s in spans if s.name == SPAN_NAME]
    assert len(invariant) == 5, (
        f"expected 5 {SPAN_NAME!r} spans (one per tick), got {len(invariant)}; "
        f"saw spans: {sorted({s.name for s in spans})}"
    )

    required_attrs = {"round", "interaction", "max_narrative_round", "gap", "holds"}
    for s in invariant:
        attrs = dict(s.attributes or {})
        missing = required_attrs - set(attrs.keys())
        assert not missing, (
            f"{SPAN_NAME} span missing attributes {sorted(missing)}; "
            f"present: {sorted(attrs.keys())}"
        )


# ---------------------------------------------------------------------------
# AC2 — invariant holds (gap=0, holds=True) on every tick after fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_invariant_gap_is_zero_across_10_turns(
    otel_capture,
    session_handler_factory,
) -> None:
    """Drive 10 narration turns; assert at every tick that the span carries
    ``gap == 0`` and ``holds == True`` AND that the underlying snapshot
    tracks the SQL ground truth.

    This is the production-fix proof: ``turn_manager.round`` no longer lags
    ``MAX(narrative_log.round_number)`` after the resolution pipeline
    advances the counter in lockstep with each interaction.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_narration_result(),
    )

    for _ in range(10):
        # Fresh TurnContext per turn (PhaseTimings finalizes after each call).
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I press onward.", turn_context)

        # Per-tick snapshot/SQL invariant — Felix's bug is that this drifts.
        sql_max = _max_narrative_round_via_sql(sd)
        assert sd.snapshot.turn_manager.round == sql_max, (
            f"turn_manager.round={sd.snapshot.turn_manager.round} lags "
            f"MAX(narrative_log.round_number)={sql_max} mid-run — "
            f"the resolution pipeline never wrote round back."
        )

    spans = otel_capture.get_finished_spans()
    invariant = [s for s in spans if s.name == SPAN_NAME]
    assert len(invariant) == 10

    for i, s in enumerate(invariant):
        attrs = dict(s.attributes or {})
        assert attrs.get("gap") == 0, (
            f"tick {i}: gap={attrs.get('gap')}, expected 0 — invariant violated. "
            f"round={attrs.get('round')}, max={attrs.get('max_narrative_round')}"
        )
        assert attrs.get("holds") is True, f"tick {i}: holds={attrs.get('holds')}, expected True"


# ---------------------------------------------------------------------------
# AC2 negative — divergence is captured, not silently corrected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_invariant_span_captures_synthetic_divergence(
    otel_capture,
    session_handler_factory,
) -> None:
    """Synthesize the Felix-style gap — manually roll ``turn_manager.round``
    backward — and assert the next tick's span captures it with
    ``holds=False`` and ``gap > 0``.

    Per AC2: "Sebastien needs to see the violation captured, not silently
    corrected." A detector that auto-heals divergence on read is worse than
    no detector — it makes the lie-detector tell its own lies.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_narration_result(),
    )

    # Run a clean turn so the narrative_log has at least one row.
    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I begin.", turn_context)

    # Synthesize Felix's 7-round gap — roll the display counter backward.
    sd.snapshot.turn_manager.round = max(sd.snapshot.turn_manager.round - 5, 0)

    # Clear the exporter so we observe ONLY the second tick's spans.
    otel_capture.clear()

    # Fresh context for the second turn — PhaseTimings finalizes per turn.
    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "And press on.", turn_context)

    spans = otel_capture.get_finished_spans()
    invariant = [s for s in spans if s.name == SPAN_NAME]
    assert len(invariant) == 1, (
        f"expected exactly 1 {SPAN_NAME} span on the divergence tick, got {len(invariant)}"
    )

    attrs = dict(invariant[0].attributes or {})
    gap = attrs.get("gap")
    holds = attrs.get("holds")
    assert isinstance(gap, int) and gap > 0, (
        f"divergence span must report gap>0, got gap={gap!r} "
        f"(round={attrs.get('round')}, "
        f"max_narrative_round={attrs.get('max_narrative_round')})"
    )
    assert holds is False, f"divergence span must report holds=False, got holds={holds!r}"


# ---------------------------------------------------------------------------
# AC1 — empty narrative log on the first tick reports max_narrative_round=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_invariant_span_handles_empty_narrative_log(
    otel_capture,
    session_handler_factory,
) -> None:
    """On a brand-new session the invariant span must still fire on the very
    first tick. ``max_narrative_round`` is read AFTER ``append_narrative``
    in the production sequence, so by the time the span emits the row that
    just landed should be reflected — but the helper must not crash on
    edge cases (empty log read paths exist on other code paths).
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_narration_result(),
    )

    # Pre-condition: narrative_log is empty.
    assert _max_narrative_round_via_sql(sd) == 0

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "Hello world.", turn_context)

    spans = otel_capture.get_finished_spans()
    invariant = [s for s in spans if s.name == SPAN_NAME]
    assert len(invariant) == 1
    attrs = dict(invariant[0].attributes or {})
    # After one turn, narrative_log has one row.
    assert attrs.get("max_narrative_round", 0) >= 1, (
        f"first-tick span should reflect the just-appended row, "
        f"got max_narrative_round={attrs.get('max_narrative_round')}"
    )


# ---------------------------------------------------------------------------
# AC3 — span routes to the GM panel watcher feed end-to-end
# ---------------------------------------------------------------------------


class _CapturingSubscriber:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.mark.asyncio
async def test_round_invariant_emits_typed_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
    session_handler_factory,
) -> None:
    """Drive a real narration turn with the ``WatcherSpanProcessor``
    installed; assert the typed ``state_transition`` event lands on a hub
    subscriber with ``component='turn_manager'`` and the documented
    payload shape (AC3).

    This is the integration check that proves the dashboard pipeline is
    end-to-end live: span emit → on_end → SPAN_ROUTES → typed event →
    subscriber. Mirrors tests/telemetry/test_dogfight_watcher_routing.py.
    """
    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())
    sub = _CapturingSubscriber()
    await hub.subscribe(sub)  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(hub))
    local_tracer = provider.get_tracer("test-round-invariant-watcher-routing")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The torch flickers.",
            is_degraded=False,
            agent_duration_ms=1,
        ),
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I look around.", turn_context)

    # Allow the cross-thread broadcast hop to flush.
    await asyncio.sleep(0.05)

    typed = [
        e
        for e in sub.events
        if e.get("event_type") == "state_transition" and e.get("component") == "turn_manager"
    ]
    assert typed, (
        "no state_transition event for component=turn_manager reached the "
        f"watcher hub. Events seen: "
        f"{[(e.get('event_type'), e.get('component')) for e in sub.events]}"
    )

    fields = typed[0]["fields"]
    assert fields.get("field") == "round_invariant", (
        f"fields.field={fields.get('field')!r}, expected 'round_invariant'"
    )
    # The dashboard charts gap; holds drives violation colouring. Both must
    # ride the typed event payload, not just the OTEL span.
    assert "gap" in fields, "typed event missing 'gap' — dashboard chart goes blank"
    assert "holds" in fields, "typed event missing 'holds' — colour-code logic breaks"
    assert "round" in fields
    assert "max_narrative_round" in fields

    # Augment-not-replace: the firehose ``agent_span_close`` sibling must
    # also exist (same invariant the dogfight/quest/npc tests check).
    flat_names = {
        e["fields"].get("name") for e in sub.events if e.get("event_type") == "agent_span_close"
    }
    assert SPAN_NAME in flat_names, (
        f"flat agent_span_close missing for {SPAN_NAME!r}; firehose got: "
        f"{sorted(n for n in flat_names if n)}"
    )


# ---------------------------------------------------------------------------
# Felix's exact shape — loaded save with pre-existing divergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loaded_save_with_preexisting_divergence_captures_violation(
    otel_capture,
    session_handler_factory,
) -> None:
    """Reproduce Felix's Playtest 3 shape directly: 72 narrative_log rows
    persisted, ``turn_manager.round`` frozen at 65, drive one narration
    turn, assert the invariant span captures the violation
    (holds=False, gap>0) — does NOT silently auto-correct.

    Per ``context-story-45-11.md`` §"Out of scope":

      "Backfilling turn_manager.round on existing saves. The invariant
       detector logs the gap; existing saves with round=65 / max=72
       load and play with the gap visible."

    This test is the durable proof of that scope decision: a freshly-loaded
    Felix-style save must surface its divergence to Sebastien's GM panel,
    not vanish it on read. Strategy A's lockstep-advance fix carries the
    gap forward but never erases it; the OTEL span is the lie-detector.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_narration_result(),
    )

    # Synthesize Felix's save: 72 narrative_log rows, turn_manager.round=65.
    # We seed rows directly via the existing append_narrative API rather
    # than a custom SQL INSERT so the schema, column order, and tag
    # serialization match production writes exactly. Insertion order is
    # monotonic round_number → SQL MAX = 72.
    from sidequest.game.session import NarrativeEntry

    for r in range(1, 73):
        sd.store.append_narrative(
            NarrativeEntry(
                timestamp=0,
                round=r,
                author="narrator",
                content=f"Felix's row {r}",
                tags=[],
            ),
        )
    assert _max_narrative_round_via_sql(sd) == 72

    # Frozen display counter — the original Felix bug.
    sd.snapshot.turn_manager.round = 65
    sd.snapshot.turn_manager.interaction = 72

    # Drive one narration turn. Strategy A advances both interaction and
    # round in lockstep, so post-tick: interaction=73, round=66, max=73.
    # The pre-existing 7-round gap is preserved (not backfilled).
    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "And so it goes.", turn_context)

    spans = otel_capture.get_finished_spans()
    invariant = [s for s in spans if s.name == SPAN_NAME]
    assert len(invariant) == 1, (
        f"expected exactly 1 {SPAN_NAME} span on the post-load tick, got {len(invariant)}"
    )

    attrs = dict(invariant[0].attributes or {})
    assert attrs.get("holds") is False, (
        f"loaded-save divergence must surface as holds=False; got "
        f"holds={attrs.get('holds')!r} (round={attrs.get('round')}, "
        f"max={attrs.get('max_narrative_round')}). The detector cannot "
        f"silently auto-correct — Sebastien's panel needs to see the lie."
    )
    gap = attrs.get("gap")
    assert isinstance(gap, int) and gap > 0, (
        f"loaded-save divergence must surface as gap>0; got gap={gap!r}"
    )
    # The exact gap depends on whether the GREEN fix advances both
    # counters in lockstep (gap stays at the original 7) or some other
    # shape; we don't pin the magnitude — only that the violation is
    # captured. Story scope explicitly forbids backfilling, so gap MUST
    # NOT be zero.
    assert gap >= 1
