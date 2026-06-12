"""Story 91-5 — Dark-spend detector: ledger instrumented-total API (RED).

The SessionCostLedger (cost_safety.py) tracks per-session cumulative spend
since server start. Story 91-5 needs to compare this "instrumented total"
against the Admin API billed figure. The ledger must expose
``instrumented_total_usd()`` — the cross-session sum — as the Layer 1
figure for the reconciliation.

These tests pin the ``SessionCostLedger.instrumented_total_usd()`` contract:

**AC-T1 — Empty ledger returns 0.0.**
A fresh server (no sessions yet) must return 0.0, not raise.

**AC-T2 — Single-session total is that session's cumulative.**
One session with recorded spend must return that exact amount.

**AC-T3 — Multi-session total is the cross-session sum.**
The ledger sums ALL sessions — this is the "instrumented" figure that
represents all server-side Anthropic spend since process start.

**AC-T4 — record_call updates instrumented_total_usd.**
``record_call`` is the production write path (used by aside/router adapters,
91-4). After a record_call, the total must include that call's cost.

**AC-T5 — reset_for_tests clears the total.**
The autouse fixture (tests/conftest.py) calls reset_for_tests(); after that
call instrumented_total_usd() must be 0.0 (test isolation contract).

**AC-T6 — instrumented_total_usd sums across narrator + adapter call sites.**
The narrator's cumulative (keyed directly by session_id) and adapter
cumulatives (also keyed by session_id via update_cumulative) must BOTH
be counted. There must not be a double-count for one session.

Note: tests use unique session IDs ("91-5-…") to stay independent under
xdist. The autouse conftest fixture resets state between tests.
"""

from __future__ import annotations

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.cost_safety import ledger

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"

# Ceiling high enough to never trigger in these tests
_NO_CEILING = 1_000.0


# ===========================================================================
# AC-T1 — Empty ledger returns 0.0
# ===========================================================================


def test_instrumented_total_empty_ledger() -> None:
    """Fresh ledger (reset by autouse fixture) must return 0.0 without
    raising. Division-by-zero and KeyError guards are both required."""
    total = ledger().instrumented_total_usd()
    assert total == 0.0, (
        f"empty ledger.instrumented_total_usd() must be 0.0, got {total!r} — "
        "the ledger may not have this method yet (RED state expected)"
    )


# ===========================================================================
# AC-T2 — Single-session total
# ===========================================================================


def test_instrumented_total_single_session() -> None:
    """One session with update_cumulative must report that session's cost."""
    cost_ledger = ledger()
    cost = compute_cost_usd(
        input_tokens=5_000,
        output_tokens=200,
        cached_input_read_tokens=0,
        model=_HAIKU,
    )
    cost_ledger.update_cumulative(
        session_id="91-5-single", cost_usd=cost, model=_HAIKU, ceiling_usd=_NO_CEILING
    )
    total = cost_ledger.instrumented_total_usd()
    assert total == pytest.approx(cost, abs=1e-9), (
        f"single-session total must equal the recorded cost {cost:.6f}, got {total}"
    )


# ===========================================================================
# AC-T3 — Multi-session total is the cross-session sum
# ===========================================================================


def test_instrumented_total_multiple_sessions_sums_all() -> None:
    """Two independent sessions' costs must be summed — the reconciliation
    needs the process-wide total, not one session's spend."""
    cost_ledger = ledger()
    cost_a = compute_cost_usd(
        input_tokens=3_000, output_tokens=100, cached_input_read_tokens=0, model=_HAIKU
    )
    cost_b = compute_cost_usd(
        input_tokens=11_000, output_tokens=400, cached_input_read_tokens=0, model=_SONNET
    )
    cost_ledger.update_cumulative(
        session_id="91-5-multi-a", cost_usd=cost_a, model=_HAIKU, ceiling_usd=_NO_CEILING
    )
    cost_ledger.update_cumulative(
        session_id="91-5-multi-b", cost_usd=cost_b, model=_SONNET, ceiling_usd=_NO_CEILING
    )

    total = cost_ledger.instrumented_total_usd()
    assert total == pytest.approx(cost_a + cost_b, abs=1e-9), (
        f"multi-session total must be sum of all sessions: "
        f"a={cost_a:.6f} + b={cost_b:.6f} = {cost_a + cost_b:.6f}, got {total}"
    )


# ===========================================================================
# AC-T4 — record_call feeds instrumented_total_usd
# ===========================================================================


def test_record_call_feeds_instrumented_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """record_call() is the production write path for adapters (aside/router
    from 91-4). A call through record_call must show up in instrumented_total.
    Mocking watcher_hub so the detector events don't pollute test infra."""
    from unittest.mock import patch


    cost_ledger = ledger()
    cost = compute_cost_usd(
        input_tokens=2_000, output_tokens=80, cached_input_read_tokens=0, model=_HAIKU
    )
    # Suppress runaway events — not the test subject here
    with patch("sidequest.agents.cost_safety._watcher_publish_event"):
        cost_ledger.record_call(
            session_id="91-5-record",
            caller="aside",
            model=_HAIKU,
            input_tokens=2_000,
            output_tokens=80,
            cost_usd=cost,
            ceiling_usd=_NO_CEILING,
        )

    total = cost_ledger.instrumented_total_usd()
    assert total == pytest.approx(cost, abs=1e-9), (
        f"record_call must feed into instrumented_total_usd; got {total}, expected {cost:.6f}"
    )


# ===========================================================================
# AC-T5 — reset_for_tests clears the total
# ===========================================================================


def test_reset_for_tests_clears_instrumented_total() -> None:
    """The test-isolation hook must zero out the instrumented total so
    xdist tests that share a session ID don't accumulate cross-test spend."""
    cost_ledger = ledger()
    cost = compute_cost_usd(
        input_tokens=1_000, output_tokens=50, cached_input_read_tokens=0, model=_HAIKU
    )
    cost_ledger.update_cumulative(
        session_id="91-5-reset", cost_usd=cost, model=_HAIKU, ceiling_usd=_NO_CEILING
    )
    assert cost_ledger.instrumented_total_usd() > 0.0, "pre-condition: total is non-zero before reset"

    cost_ledger.reset_for_tests()
    total_after = cost_ledger.instrumented_total_usd()
    assert total_after == 0.0, (
        f"reset_for_tests() must zero instrumented_total_usd; got {total_after!r} after reset"
    )


# ===========================================================================
# AC-T6 — No double-count across multiple calls to the same session
# ===========================================================================


def test_instrumented_total_accumulates_within_session() -> None:
    """Multiple calls to the same session must accumulate, not overwrite —
    and must not double-count any single call."""
    cost_ledger = ledger()
    cost1 = compute_cost_usd(
        input_tokens=1_000, output_tokens=50, cached_input_read_tokens=0, model=_HAIKU
    )
    cost2 = compute_cost_usd(
        input_tokens=1_500, output_tokens=60, cached_input_read_tokens=0, model=_HAIKU
    )
    from unittest.mock import patch

    with patch("sidequest.agents.cost_safety._watcher_publish_event"):
        cost_ledger.update_cumulative(
            session_id="91-5-acc", cost_usd=cost1, model=_HAIKU, ceiling_usd=_NO_CEILING
        )
        cost_ledger.update_cumulative(
            session_id="91-5-acc", cost_usd=cost2, model=_HAIKU, ceiling_usd=_NO_CEILING
        )

    total = cost_ledger.instrumented_total_usd()
    expected = cost1 + cost2
    assert total == pytest.approx(expected, abs=1e-9), (
        f"two calls to the same session must accumulate; expected {expected:.6f}, got {total}"
    )


def test_instrumented_total_does_not_double_count_narrator_and_adapter() -> None:
    """The narrator's spend and the adapter's spend for the same session
    must NOT be double-counted — they both write to the same
    cumulative_cost_usd[session_id] key via update_cumulative, which
    is additive, not max-of-two."""
    from unittest.mock import patch

    cost_ledger = ledger()
    narrator_cost = compute_cost_usd(
        input_tokens=11_000, output_tokens=400, cached_input_read_tokens=0, model=_SONNET
    )
    aside_cost = compute_cost_usd(
        input_tokens=2_000, output_tokens=80, cached_input_read_tokens=0, model=_HAIKU
    )
    # Both writes go to the same session bucket
    with patch("sidequest.agents.cost_safety._watcher_publish_event"):
        cost_ledger.update_cumulative(
            session_id="91-5-dedup", cost_usd=narrator_cost, model=_SONNET, ceiling_usd=_NO_CEILING
        )
        cost_ledger.update_cumulative(
            session_id="91-5-dedup", cost_usd=aside_cost, model=_HAIKU, ceiling_usd=_NO_CEILING
        )

    total = cost_ledger.instrumented_total_usd()
    expected = narrator_cost + aside_cost
    assert total == pytest.approx(expected, abs=1e-9), (
        f"narrator + aside for same session must sum (no double-count); "
        f"expected {expected:.6f}, got {total}"
    )
