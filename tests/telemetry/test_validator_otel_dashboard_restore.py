"""Regression tests for playtest 2026-04-30 — OTEL dashboard restoration.

Bugs #7 (Timeline single-span) and #8 (Patches/Delta empty mismatch).
The validator's ``turn_complete`` event used to carry ``phase_durations_ms``
as a flat dict but the dashboard's Timeline panel only knew how to render
a top-level ``spans`` array — so it drew a single agent_llm bar even
though the server tracked seven phases. And ``delta_empty`` ignored
narrator footnotes (the "Knowledge Gained" UI chip) — a turn that
introduced 3 new knowledge entries but no location/quest/lore patches
read as ``Patches: none, Delta empty: true``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sidequest.telemetry.turn_record import TurnRecord
from sidequest.telemetry.validator import Validator


def _make_record(**overrides: Any) -> TurnRecord:
    base: dict[str, Any] = dict(
        turn_id=3,
        timestamp=datetime.now(UTC),
        player_id="p1",
        player_input="The breach groans.",
        classified_intent="speak",
        agent_name="narrator",
        narration="The breach groans wider.",
        patches_applied=[],
        snapshot_before_hash="x",
        snapshot_after=object(),
        delta=None,
        beats_fired=[],
        extraction_tier=1,
        token_count_in=120,
        token_count_out=80,
        agent_duration_ms=8000,
        is_degraded=False,
        phase_durations_ms={
            "prompt_build": 800,
            "narrator_subprocess": 8000,
            "narrator_extraction": 200,
            "state_apply": 50,
            "persistence": 25,
            "broadcast": 10,
            "dispatch_post": 5,
        },
        phase_call_counts={
            k: 1
            for k in (
                "prompt_build",
                "narrator_subprocess",
                "narrator_extraction",
                "state_apply",
                "persistence",
                "broadcast",
                "dispatch_post",
            )
        },
        total_duration_ms=9090,
    )
    base.update(overrides)
    return TurnRecord(**base)


@pytest.mark.asyncio
async def test_turn_complete_carries_spans_array_for_timeline_chart(monkeypatch) -> None:
    """Bug #7: dashboard Timeline must receive a ``spans`` array — one
    bar per pipeline phase, in observed order — so the GM panel sees
    where the turn time actually went rather than a single agent_llm
    bar filling the row.
    """
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

    monkeypatch.setattr(
        "sidequest.telemetry.validator.publish_event",
        fake_publish,
    )

    v = Validator()
    v._checks = []  # noqa: SLF001
    await v._validate(_make_record())  # noqa: SLF001

    payload = captured[0]
    assert "spans" in payload, "turn_complete must include `spans` array"
    spans = payload["spans"]
    # One span per phase, dict iteration order preserved (Python 3.7+).
    assert [s["name"] for s in spans] == [
        "prompt_build",
        "narrator_subprocess",
        "narrator_extraction",
        "state_apply",
        "persistence",
        "broadcast",
        "dispatch_post",
    ]
    # Each span has a numeric start_ms / duration_ms, monotonically
    # advancing — the dashboard renders bars left-to-right based on
    # these.
    running = 0
    for span in spans:
        assert span["start_ms"] == running
        assert span["duration_ms"] >= 1
        running += span["duration_ms"]
    # Every span carries a `component` (the dashboard's flame label).
    assert all("component" in s for s in spans)


@pytest.mark.asyncio
async def test_turn_complete_falls_back_to_agent_llm_when_no_phases(monkeypatch) -> None:
    """A degraded turn missing per-phase data must still produce at
    least one bar so the Timeline never reads an empty spans array.
    """
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

    monkeypatch.setattr(
        "sidequest.telemetry.validator.publish_event",
        fake_publish,
    )

    v = Validator()
    v._checks = []  # noqa: SLF001
    record = _make_record(
        phase_durations_ms={},
        phase_call_counts={},
        agent_duration_ms=4321,
        is_degraded=True,
    )
    await v._validate(record)  # noqa: SLF001

    payload = captured[0]
    spans = payload["spans"]
    assert len(spans) == 1
    assert spans[0]["name"] == "agent_llm"
    assert spans[0]["duration_ms"] == 4321


@pytest.mark.asyncio
async def test_turn_complete_carries_footnotes_count(monkeypatch) -> None:
    """Bug #8: a turn that adds knowledge footnotes but no patches
    must surface footnotes_count > 0 and `delta_empty=False`.
    """
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

    monkeypatch.setattr(
        "sidequest.telemetry.validator.publish_event",
        fake_publish,
    )

    v = Validator()
    v._checks = []  # noqa: SLF001
    record = _make_record(
        patches_applied=[],
        beats_fired=[],
        footnotes_count=3,
    )
    await v._validate(record)  # noqa: SLF001

    payload = captured[0]
    assert payload["footnotes_count"] == 3
    assert payload["delta_empty"] is False, (
        "delta_empty must reflect knowledge entries — three new footnotes is not an empty turn"
    )


@pytest.mark.asyncio
async def test_turn_complete_delta_empty_when_truly_empty(monkeypatch) -> None:
    """No patches, no beats, no footnotes → delta_empty is True. Pin
    the boundary so a future regression doesn't silently flip it.
    """
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

    monkeypatch.setattr(
        "sidequest.telemetry.validator.publish_event",
        fake_publish,
    )

    v = Validator()
    v._checks = []  # noqa: SLF001
    record = _make_record(
        patches_applied=[],
        beats_fired=[],
        footnotes_count=0,
    )
    await v._validate(record)  # noqa: SLF001

    payload = captured[0]
    assert payload["footnotes_count"] == 0
    assert payload["delta_empty"] is True
