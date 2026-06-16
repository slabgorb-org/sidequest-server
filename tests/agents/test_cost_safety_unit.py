"""Transport-free unit coverage for the cost-safety detector + ledger.

Story 119-3: the narrator/Haiku transport ported from the raw ``anthropic``
Messages SDK onto ``claude-agent-sdk``. The cost-runaway fingerprint detector
and the per-session cumulative ceiling (the structural backstop the project did
not have when $313 burned in 48h, 2026-05-23) live in
:mod:`sidequest.agents.cost_safety` and are **transport-independent** — only the
usage figures feeding them changed source (``response.usage`` → the agent SDK's
``ResultMessage.usage`` dict). This suite pins that detector/ledger logic
directly, where it lives, so the load-bearing protection retains coverage after
the per-iteration integration tests (which exercised the deleted raw-SDK loop)
were removed. The integration *wiring* — that ``complete_with_tools`` and the
Haiku sites still feed this logic — is covered by ``test_119_3_*`` and
``test_119_3_narrator_behavior_on_agent_sdk``.

Triggers (ADR-134 / story 61-4 / 61-followup-D), priority
``io_fingerprint > input_absolute > cost_multiple > cost_absolute``:

* **io_fingerprint** — ``input > 2× baseline AND output < 50`` (the $313 shape).
* **input_absolute** — ``input > 40_000`` always (the high-output sibling).
* **cost_multiple** — ``cost > 5× baseline``.
* **cost_absolute** — ``cost > $0.30`` always.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from sidequest.agents import cost_safety
from sidequest.agents.anthropic_sdk_client import AnthropicSdkCostCeilingExceeded


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def _record(event_type: str, fields: dict[str, Any], **_kw: Any) -> None:
        events.append((event_type, fields))

    monkeypatch.setattr(cost_safety, "_watcher_publish_event", _record)
    return events


def _runaway_triggers(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [f["trigger"] for name, f in events if name == "cost_runaway_suspected"]


# ===========================================================================
# Runaway fingerprint detector
# ===========================================================================


def test_io_fingerprint_fires_in_warmup(captured_events: list[Any]) -> None:
    """The 2026-05-23 $313 shape: huge input, near-zero output, no baseline yet.
    Warmup floor 2×12_000=24_000; 60_000 in / 12 out trips io_fingerprint."""
    cost_safety.check_and_emit_runaway(
        cost_window=None,
        input_window=None,
        input_tokens=60_000,
        output_tokens=12,
        cost_usd=0.18,
        model="claude-sonnet-4-6",
        session_id="s1",
        caller="narrator",
    )
    assert _runaway_triggers(captured_events) == ["io_fingerprint"]


def test_input_absolute_fires_for_high_output_sibling(captured_events: list[Any]) -> None:
    """input > 40_000 always fires regardless of output shape — the high-output
    sibling the io_fingerprint (output<50) would miss."""
    cost_safety.check_and_emit_runaway(
        cost_window=None,
        input_window=None,
        input_tokens=45_000,
        output_tokens=4_000,  # high output → not io_fingerprint
        cost_usd=0.20,
        model="claude-sonnet-4-6",
        session_id="s1",
        caller="narrator",
    )
    assert _runaway_triggers(captured_events) == ["input_absolute"]


def test_cost_multiple_fires_in_warmup(captured_events: list[Any]) -> None:
    """cost > 5× the $0.03 warmup floor ($0.15) with an otherwise normal shape
    trips cost_multiple (not io / input_absolute)."""
    cost_safety.check_and_emit_runaway(
        cost_window=None,
        input_window=None,
        input_tokens=2_000,
        output_tokens=800,
        cost_usd=0.20,
        model="claude-sonnet-4-6",
        session_id="s1",
        caller="narrator",
    )
    assert _runaway_triggers(captured_events) == ["cost_multiple"]


def test_no_trigger_for_a_healthy_call(captured_events: list[Any]) -> None:
    cost_safety.check_and_emit_runaway(
        cost_window=None,
        input_window=None,
        input_tokens=3_000,
        output_tokens=600,
        cost_usd=0.02,
        model="claude-sonnet-4-6",
        session_id="s1",
        caller="narrator",
    )
    assert _runaway_triggers(captured_events) == []


def test_post_warmup_baseline_is_clamped_against_trained_silence(
    captured_events: list[Any],
) -> None:
    """61-followup-D §A: a sustained ramp must not train the comparator into
    silence. A full K=10 window of large inputs still trips because the baseline
    is clamped at 3× the warmup floor (36_000), so 2× → 72_000."""
    big_input_window: deque[int] = deque([200_000] * 10, maxlen=10)
    big_cost_window: deque[float] = deque([5.0] * 10, maxlen=10)
    cost_safety.check_and_emit_runaway(
        cost_window=big_cost_window,
        input_window=big_input_window,
        input_tokens=80_000,  # > 2× clamped 36_000 baseline, and > 40_000
        output_tokens=10,
        cost_usd=0.50,
        model="claude-sonnet-4-6",
        session_id="s1",
        caller="narrator",
    )
    # io_fingerprint wins on priority (output<50, input>72_000).
    assert _runaway_triggers(captured_events) == ["io_fingerprint"]


def test_runaway_event_carries_caller_attribution(captured_events: list[Any]) -> None:
    """[COST-1] cross-model attribution: the caller tag splits a Haiku alarm
    from a narrator alarm."""
    cost_safety.check_and_emit_runaway(
        cost_window=None,
        input_window=None,
        input_tokens=60_000,
        output_tokens=12,
        cost_usd=0.18,
        model="claude-haiku-4-5",
        session_id="s1",
        caller="intent_router",
    )
    runaway = [f for name, f in captured_events if name == "cost_runaway_suspected"]
    assert runaway and runaway[0]["caller"] == "intent_router"


# ===========================================================================
# Per-session cumulative ceiling
# ===========================================================================


def test_ceiling_crossing_raises_and_emits_once(captured_events: list[Any]) -> None:
    ledger = cost_safety.SessionCostLedger()
    # First call stays under the $1 ceiling — no raise, no event.
    ledger.update_cumulative(session_id="s1", cost_usd=0.40, model="m", ceiling_usd=1.0)
    assert not [n for n, _ in captured_events if n == "session.cost_ceiling_exceeded"]

    # The call that crosses raises and emits the typed event exactly once.
    with pytest.raises(AnthropicSdkCostCeilingExceeded) as exc:
        ledger.update_cumulative(session_id="s1", cost_usd=0.80, model="m", ceiling_usd=1.0)
    assert exc.value.session_id == "s1"
    assert exc.value.cumulative_cost_usd == pytest.approx(1.20)
    emits = [n for n, _ in captured_events if n == "session.cost_ceiling_exceeded"]
    assert emits == ["session.cost_ceiling_exceeded"], "ceiling event must fire exactly once"


def test_check_ceiling_terminally_refuses_after_crossing() -> None:
    """Pre-flight refusal on ANY call site after a crossing — the 'no further
    billing' half of the terminal-refusal contract."""
    ledger = cost_safety.SessionCostLedger()
    ledger.cumulative_cost_usd["s1"] = 1.50
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        ledger.check_ceiling("s1", ceiling_usd=1.0)


def test_check_ceiling_passes_below_ceiling() -> None:
    ledger = cost_safety.SessionCostLedger()
    ledger.cumulative_cost_usd["s1"] = 0.50
    ledger.check_ceiling("s1", ceiling_usd=1.0)  # no raise
