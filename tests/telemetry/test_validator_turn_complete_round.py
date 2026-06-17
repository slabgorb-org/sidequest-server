"""Regression: turn_complete must carry its forensic ``round`` so the
saved-session (forensic) Timing tab can see it.

Bug (2026-06-17, dust_and_lead playtest): the validator emits turn_complete
OUT OF FRAME (``publish_event`` with ``tx=None`` — it runs on the validator's
own long-lived background task, so the turn's SaveTransaction is not in scope).
The out-of-frame persist path stamps the telemetry row's ``round`` column from
``fields.get("round")`` (watcher_hub ``_persist_turn_telemetry``) and leaves
``event_seq`` NULL. turn_complete carried no ``"round"`` key, so the row landed
with ``round=NULL, event_seq=NULL``.

The forensic round-bundle reader (``_telemetry_for_round``) selects a round's
rows with::

    (event_seq IS NOT NULL AND event_seq BETWEEN seq_start AND seq_end)
    OR round = round_number

so a NULL-round / NULL-seq turn_complete matches *no* round and never appears
in any saved-session bundle. The Timing tab reads only ``turn_complete`` events,
so every saved session showed "No data yet" for P50/P95/P99, agent duration,
the turn-duration scatter, token usage, and extraction tier.

The correct round value is ``record.turn_id``: ``narrative_log.round_number``
(which defines the forensic rounds) is written with
``snapshot.turn_manager.interaction`` (websocket_session_handler lines
1571/1582), and ``TurnRecord.turn_id`` IS that same
``snapshot.turn_manager.interaction`` (line 2643). So ``turn_id`` is exactly the
forensic round number — not a single-player coincidence.

This locks the EMISSION (the new behavior). The downstream persist
(``fields.get("round")``) and reader (``OR round = round_number``) clauses are
pre-existing and exercised by ``state_transition`` rows, which carry a round and
already surface correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sidequest.telemetry.turn_record import TurnRecord
from sidequest.telemetry.validator import Validator


def _make_record(**overrides: Any) -> TurnRecord:
    base: dict[str, Any] = dict(
        turn_id=1,
        timestamp=datetime.now(UTC),
        player_id="p1",
        player_input="hello",
        classified_intent="speak",
        agent_name="narrator",
        narration="The wind picks up.",
        patches_applied=[],
        snapshot_before_hash="x",
        snapshot_after=object(),
        delta=None,
        beats_fired=[],
        extraction_tier=1,
        token_count_in=10,
        token_count_out=20,
        agent_duration_ms=14336,
        is_degraded=False,
        phase_durations_ms={"preprocess_llm": 87000, "narrator_subprocess": 14336},
        phase_call_counts={"preprocess_llm": 1, "narrator_subprocess": 1},
        total_duration_ms=101000,
    )
    base.update(overrides)
    return TurnRecord(**base)


def _capture_turn_complete(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_publish(
        event_type: str,
        payload: dict[str, Any],
        *,
        component: str = "sidequest-server",  # noqa: ARG001
        severity: str = "info",  # noqa: ARG001
    ) -> None:
        if event_type == "turn_complete":
            captured.append(payload)

    monkeypatch.setattr("sidequest.telemetry.validator.publish_event", fake_publish)
    return captured


@pytest.mark.asyncio
async def test_turn_complete_carries_round_equal_to_turn_id(monkeypatch) -> None:
    """The emitted turn_complete payload must include ``round == turn_id`` so the
    out-of-frame persist stamps the forensic ``round`` column and the saved-session
    Timing tab can find it."""
    captured = _capture_turn_complete(monkeypatch)

    v = Validator()
    # Pin to the emission codepath only — real checks would re-publish noise.
    v._checks = []  # noqa: SLF001 — intentional test seam
    await v._validate(_make_record(turn_id=7))  # noqa: SLF001

    assert len(captured) == 1
    payload = captured[0]
    assert "round" in payload, (
        "turn_complete has no 'round' field -> persists with round=NULL -> "
        "invisible to every forensic round bundle (Timing tab 'No data yet')"
    )
    assert payload["round"] == 7, "round must equal turn_id (the forensic round_number)"
    # Must be an int: _persist_turn_telemetry drops a non-int round to NULL
    # (`if not isinstance(rnd, int): rnd = None`), which would re-orphan the row.
    assert isinstance(payload["round"], int)
    # turn_number is the legacy alias of the same interaction value.
    assert payload["round"] == payload["turn_number"]


@pytest.mark.asyncio
async def test_degraded_turn_complete_also_carries_round(monkeypatch) -> None:
    """The degraded codepath (no per-phase data, severity=warning) must also stamp
    the round — degraded turns are exactly the ones a GM most wants to inspect
    forensically."""
    captured = _capture_turn_complete(monkeypatch)

    v = Validator()
    v._checks = []  # noqa: SLF001
    await v._validate(  # noqa: SLF001
        _make_record(
            turn_id=9,
            is_degraded=True,
            phase_durations_ms={},
            phase_call_counts={},
        )
    )

    assert len(captured) == 1
    assert captured[0]["round"] == 9
    assert isinstance(captured[0]["round"], int)
