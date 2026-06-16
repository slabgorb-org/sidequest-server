"""Turn-pipeline wiring for the per-session TensionTracker (Story 81-2, ADR-024).

RED premise (audit 2026-06-03): ``TensionTracker`` is constructed only in
tests. ``_SessionData`` carries no tracker, ``_execute_narration_turn`` never
drives one, and the ``tension:round_observed`` watcher event (emitted from
``TensionTracker.observe``) never fires in production. So the GM panel sees no
tension signal and ADR-024's dual-track pacing model is dead at runtime — the
narrator improvises pacing with zero mechanical backing, exactly what the OTEL
lie-detector principle exists to catch.

These tests pin the *producer* half of ADR-024 (81-3 is the consumer):

- AC1 — a per-session ``TensionTracker`` on ``_SessionData``, created once per
  session and reused across turns.
- AC2 — the production turn path feeds the tracker so it moves; an event-less
  turn does not crash and leaves the tracks in a valid bounded state.
- AC3 — the tension watcher event is emitted during a real turn.
- AC4 — a behavioral wiring test that fails on current ``develop`` (tracker
  never instantiated) and passes after the fix.

**Wiring-test discipline.** Per the server's "No Source-Text Wiring Tests"
rule (CLAUDE.md), the wiring proof is *behavioral* — drive the real
``_execute_narration_turn`` and observe the watcher event + accumulated
state on the per-session tracker — never a grep of handler source. The OTEL
watcher event is captured by monkeypatching ``watcher_hub.publish_event`` at
its source module (the tracker imports it lazily inside ``observe()``),
matching ``tests/game/test_tension_tracker_otel_wiring.py``.

**Seam contract these tests assume (see TEA Delivery Finding).** The tracker
is driven *once per turn* from a seam reachable inside
``_execute_narration_turn`` — i.e. in the handler, classifying the resolved
turn, where a quiet turn is a Boring observation. This is forced by two facts:
(1) ``narration_apply`` runs *inside* ``orchestrator.run_narration_turn``,
which turn-path tests mock, so a per-turn signal driven there would be
untestable without a live LLM and would only fire on combat turns; (2) ADR-024
wants a pacing signal that updates *every* turn, not only combat turns. Only
``observe()`` emits the watcher event and only ``update_stakes()`` moves the
stakes track — ``record_event()`` alone moves neither, so the AC3 assertion
intentionally forces reuse of ``observe()``'s existing emission rather than a
parallel telemetry path (epic-81 guardrail).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.tension_tracker import TensionTracker
from tests.server.conftest import (
    _build_turn_context_for_test,
    _make_minimal_narration_turn_result,
)


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Intercept ``watcher_hub.publish_event`` and collect every emitted event.

    ``TensionTracker.observe`` imports ``publish_event`` lazily from
    ``sidequest.telemetry.watcher_hub``, so patch the source attribute (not a
    re-exported alias) to catch the lazy resolution at call time. Mirrors the
    capture fixture in ``tests/game/test_tension_tracker_otel_wiring.py``.
    """
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.telemetry import watcher_hub as hub_mod

    monkeypatch.setattr(hub_mod, "publish_event", _capture)
    yield captured


def _tension_events(captured: list[dict]) -> list[dict]:
    """Filter captured events down to ``tension:round_observed`` emissions."""
    return [
        e
        for e in captured
        if e["component"] == "tension"
        and e["event_type"] == "state_transition"
        and e["fields"].get("op") == "round_observed"
    ]


# ---------------------------------------------------------------------------
# AC1 — per-session tracker exists (structural tripwire)
# ---------------------------------------------------------------------------


def test_session_data_has_per_session_tension_tracker(session_fixture) -> None:
    """``_SessionData`` carries a ``tension_tracker`` field that is a real,
    default-constructed ``TensionTracker`` per session — starting at zero.

    Reflection-based dataclass interrogation (the sanctioned "tripwire"
    pattern — runtime type check, not a source grep). Fails on current
    ``develop`` because the field does not exist.
    """
    sd, _handler = session_fixture

    field_names = {f.name for f in dataclasses.fields(sd)}
    assert "tension_tracker" in field_names, (
        "_SessionData must carry a per-session `tension_tracker` field "
        "(ADR-024 / story 81-2) — mirror the `entity_store` default_factory precedent"
    )

    tracker = sd.tension_tracker
    assert isinstance(tracker, TensionTracker), (
        f"tension_tracker must default-construct a TensionTracker, got {type(tracker)!r}"
    )
    # A freshly-seated session starts with no accumulated tension.
    assert tracker.drama_weight() == 0.0
    assert tracker.boring_streak() == 0


# ---------------------------------------------------------------------------
# AC2 + AC3 + AC4 — the production turn path drives the tracker and emits OTEL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_drives_session_tracker_and_emits_tension_watcher_event(
    session_fixture, captured_watcher_events: list[dict]
) -> None:
    """Driving one real ``_execute_narration_turn`` must feed the *session*
    tracker and emit the tension watcher event.

    This is the canonical behavioral wiring test (AC4): it fails on current
    ``develop`` (no per-session tracker, no driving, no event) and passes once
    the handler drives ``sd.tension_tracker`` once per turn via ``observe()``.

    Asserting against ``sd.tension_tracker`` (not a throwaway local) proves the
    event came from the per-session tracker that actually accumulates — a Dev
    who observed a local tracker but never stored it on ``sd`` would emit the
    event yet leave ``sd.tension_tracker`` at zero, failing the accumulation
    assertion.
    """
    sd, handler = session_fixture
    handler._validator = None
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You catch your breath. The corridor stays quiet.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I catch my breath.", turn_context)

    # AC3: the tension watcher event fired from the real turn path.
    events = _tension_events(captured_watcher_events)
    assert len(events) >= 1, (
        "the production turn path must emit a tension:round_observed watcher "
        "event (reuse TensionTracker.observe's emission — no parallel telemetry); "
        f"captured components: {[e['component'] for e in captured_watcher_events]}"
    )
    fields = events[0]["fields"]
    assert fields["field"] == "tension"
    assert 0.0 <= fields["drama_weight"] <= 1.0

    # AC2: the *session* tracker actually moved off its initial zero state —
    # a quiet turn is a Boring observation (gambler's ramp).
    assert sd.tension_tracker.boring_streak() >= 1, (
        "a quiet turn must feed the per-session tracker (Boring observation)"
    )
    assert 0.0 <= sd.tension_tracker.drama_weight() <= 1.0


@pytest.mark.asyncio
async def test_tracker_is_per_session_and_accumulates_across_turns(
    session_fixture, captured_watcher_events: list[dict]
) -> None:
    """The tracker is created once per session and accumulates across turns —
    state from turn 1 is visible in turn 2 (AC1), with one observation per turn
    (AC2 cadence).
    """
    sd, handler = session_fixture
    handler._validator = None
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_make_minimal_narration_turn_result("Nothing stirs.")
    )

    tracker_before = sd.tension_tracker

    # A fresh TurnContext per turn — production builds one per turn (its
    # PhaseTimings finalizes at turn end), so reusing one across turns is
    # unrealistic and trips "PhaseTimings already finalized".
    await handler._execute_narration_turn(sd, "I wait.", _build_turn_context_for_test(sd))
    streak_after_turn_1 = sd.tension_tracker.boring_streak()

    await handler._execute_narration_turn(sd, "I keep waiting.", _build_turn_context_for_test(sd))
    streak_after_turn_2 = sd.tension_tracker.boring_streak()

    # Same instance across turns — created once per session, not per turn.
    assert sd.tension_tracker is tracker_before, (
        "the TensionTracker must persist across turns (one per session), "
        "not be re-created each turn"
    )
    # State from turn 1 is visible in turn 2 — the boring streak accumulates.
    assert streak_after_turn_1 >= 1
    assert streak_after_turn_2 > streak_after_turn_1, (
        f"tracker must accumulate across turns: streak went "
        f"{streak_after_turn_1} -> {streak_after_turn_2}"
    )

    # Per-turn cadence: exactly one tension observation per turn, no batching
    # and no double-driving.
    assert len(_tension_events(captured_watcher_events)) == 2


@pytest.mark.asyncio
async def test_event_less_turn_does_not_crash_and_leaves_valid_state(
    session_fixture,
) -> None:
    """An event-less turn (no encounter, no combat — the fixture default) must
    not raise and must leave every tension axis in the valid ``[0.0, 1.0]``
    range (AC2 edge case).
    """
    sd, handler = session_fixture
    handler._validator = None
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_make_minimal_narration_turn_result("All is still.")
    )

    turn_context = _build_turn_context_for_test(sd)
    # Must not raise.
    result = await handler._execute_narration_turn(sd, "I listen.", turn_context)
    assert result is not None

    tracker = sd.tension_tracker
    assert 0.0 <= tracker.action_tension() <= 1.0
    assert 0.0 <= tracker.stakes_tension() <= 1.0
    assert 0.0 <= tracker.drama_weight() <= 1.0
