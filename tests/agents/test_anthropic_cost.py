"""Tests for Anthropic cost math (per-call USD computation)."""

from __future__ import annotations

import pytest

from sidequest.agents.anthropic_cost import (
    COST_BAND_NEEDS_WORK_USD,
    COST_BAND_STOP_USD,
    UnknownModel,
    compute_cost_usd,
    cost_band,
    model_pricing,
)


def test_sonnet_4_6_pricing_constants() -> None:
    p = model_pricing("claude-sonnet-4-6")
    assert p.input_per_mtok_usd == 3.0
    assert p.output_per_mtok_usd == 15.0
    assert p.cached_input_read_per_mtok_usd == 0.3
    assert p.cached_input_write_per_mtok_usd == 3.75


def test_haiku_4_5_pricing_constants() -> None:
    p = model_pricing("claude-haiku-4-5-20251001")
    assert p.input_per_mtok_usd == 1.0
    assert p.output_per_mtok_usd == 5.0


def test_opus_4_7_pricing_constants() -> None:
    p = model_pricing("claude-opus-4-7")
    assert p.input_per_mtok_usd == 15.0
    assert p.output_per_mtok_usd == 75.0


def test_unknown_model_raises() -> None:
    with pytest.raises(UnknownModel):
        model_pricing("claude-banana-9")


def test_cost_is_sum_of_buckets() -> None:
    cost = compute_cost_usd(
        input_tokens=1000,
        output_tokens=500,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )
    # 1000 in @ $3/M + 500 out @ $15/M = 0.003 + 0.0075 = 0.0105
    assert cost == pytest.approx(0.0105, rel=1e-6)


def test_cached_read_is_90_percent_discount() -> None:
    cost = compute_cost_usd(
        input_tokens=200,
        output_tokens=0,
        cached_input_read_tokens=800,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )
    # 200 fresh in @ $3/M = 0.0006
    # 800 cached read @ $0.30/M = 0.00024
    # total 0.00084
    assert cost == pytest.approx(0.00084, rel=1e-6)


def test_cached_write_is_125_percent_of_input() -> None:
    cost = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_tokens=1000,
        model="claude-sonnet-4-6",
    )
    # 1000 cache write @ $3.75/M = 0.00375
    assert cost == pytest.approx(0.00375, rel=1e-6)


def test_input_tokens_does_not_double_count_cached() -> None:
    """API convention: `input_tokens` is the *uncached* fresh input only.

    Callers must pass the SDK's `usage.input_tokens` (excludes cached_*)
    directly — compute_cost_usd does not subtract.
    """
    cost = compute_cost_usd(
        input_tokens=100,
        output_tokens=0,
        cached_input_read_tokens=900,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )
    # Fresh: 100 @ $3/M = 0.0003
    # Cached read: 900 @ $0.30/M = 0.00027
    # Total: 0.00057 (not 0.0033, which would be 1100 @ $3/M)
    assert cost == pytest.approx(0.00057, rel=1e-6)


# --- Per-turn cost bands (thresholds set by Keith 2026-05-25) ---
# < $0.05 healthy · [$0.05, $0.12] watch · > $0.12 runaway.


def test_cost_band_threshold_constants() -> None:
    assert COST_BAND_NEEDS_WORK_USD == 0.05
    assert COST_BAND_STOP_USD == 0.12


def test_cost_band_all_systems_go_below_5_cents() -> None:
    assert cost_band(0.0) == "all_systems_go"
    assert cost_band(0.04999) == "all_systems_go"


def test_cost_band_needs_work_is_inclusive_at_lower_bound() -> None:
    # Exactly $0.05 is "needs work", not "all systems go".
    assert cost_band(0.05) == "needs_work"
    assert cost_band(0.10) == "needs_work"


def test_cost_band_needs_work_is_inclusive_at_upper_bound() -> None:
    # Exactly $0.12 is still "needs work"; only strictly above trips runaway.
    assert cost_band(0.12) == "needs_work"


def test_cost_band_stop_everything_above_12_cents() -> None:
    assert cost_band(0.1201) == "stop_everything"
    assert cost_band(0.17198) == "stop_everything"  # the 05-22 footgun, per-turn-equiv
