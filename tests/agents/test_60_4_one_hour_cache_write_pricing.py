"""Story 60-4 — 1h cache_write must be priced at the real 2x base rate.

Anthropic prices ephemeral_5m cache writes at 1.25x the base input rate and
ephemeral_1h cache writes at 2x the base input rate. ``anthropic_cost.py``
currently has a single ``cached_input_write_per_mtok_usd`` field that holds
the 5m rate. The result: when ``cumulative_cache_write_1h`` is non-zero (the
fix path 60-4 enables), the GM panel's ``cost_usd`` *understates* the real
bill by ~60% on the 1h-write line.

Surfaced in 60-3's evidence chain:
    > anthropic_cost.py prices 1h writes at the 5m rate ($3.75, not the real
    > 2x = $6/Mtok). The GM-panel cost_usd therefore *understates* real
    > 1h-write billing.

This test gate pins the corrected pricing. Dev is free to expose it via new
kwargs (``cached_input_write_5m_tokens`` + ``cached_input_write_1h_tokens``)
or via a new ``ModelPricing`` field — the contract these tests assert is the
observable cost, not the API shape.

Pricing (Anthropic public, 2026-05-15 snapshot):

| Model            | Input/M | 5m-Write/M (1.25x) | 1h-Write/M (2x) |
|------------------|---------|--------------------|-----------------|
| Sonnet 4.6       | $3.00   | $3.75              | $6.00           |
| Haiku 4.5        | $1.00   | $1.25              | $2.00           |
| Opus 4.7         | $15.00  | $18.75             | $30.00          |
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd, model_pricing

# --- AC-5: ModelPricing exposes the 1h-write rate -------------------------


def test_sonnet_4_6_one_hour_write_rate_is_2x_input() -> None:
    """Sonnet 1h-write must be $6.00/M (2x input). The dev can expose this
    via ``cached_input_write_1h_per_mtok_usd`` or another named attribute
    — this test discovers via attribute scan to keep API-shape flexible.

    What we forbid: a single ``cached_input_write_per_mtok_usd`` that
    silently prices 1h writes at the 5m rate.
    """
    p = model_pricing("claude-sonnet-4-6")

    # Find any attribute on ModelPricing whose name signals "1h write rate".
    one_hour_attr = _find_one_hour_write_rate(p)
    assert one_hour_attr is not None, (
        "ModelPricing must expose a 1h cache_write rate. Looked for an "
        "attribute matching /1h.*write|write.*1h/ on "
        f"{type(p).__name__} fields={[f for f in vars(p)]!r}. "
        "Without it, the SDK client cannot price 1h writes correctly and "
        "the GM-panel cost_usd understates the real bill."
    )

    value = getattr(p, one_hour_attr)
    assert value == pytest.approx(6.0, rel=1e-6), (
        f"Sonnet 4.6 1h-write rate must be $6.00/Mtok (2x the $3/M input "
        f"rate, per Anthropic's published pricing); got {value!r} on "
        f"attribute {one_hour_attr!r}"
    )


def test_haiku_4_5_one_hour_write_rate_is_2x_input() -> None:
    p = model_pricing("claude-haiku-4-5-20251001")
    one_hour_attr = _find_one_hour_write_rate(p)
    assert one_hour_attr is not None, "ModelPricing.<haiku 4.5> must expose a 1h cache_write rate"
    value = getattr(p, one_hour_attr)
    assert value == pytest.approx(2.0, rel=1e-6), (
        f"Haiku 4.5 1h-write rate must be $2.00/Mtok (2x the $1/M input "
        f"rate); got {value!r} on {one_hour_attr!r}"
    )


def test_opus_4_7_one_hour_write_rate_is_2x_input() -> None:
    p = model_pricing("claude-opus-4-7")
    one_hour_attr = _find_one_hour_write_rate(p)
    assert one_hour_attr is not None, "ModelPricing.<opus 4.7> must expose a 1h cache_write rate"
    value = getattr(p, one_hour_attr)
    assert value == pytest.approx(30.0, rel=1e-6), (
        f"Opus 4.7 1h-write rate must be $30.00/Mtok (2x the $15/M input "
        f"rate); got {value!r} on {one_hour_attr!r}"
    )


# --- AC-5: compute_cost_usd routes 1h writes through the 2x rate -----------


def test_compute_cost_usd_accepts_split_5m_and_1h_writes() -> None:
    """compute_cost_usd must accept the 5m vs 1h split so callers can
    attribute the actual TTL of each write. Without this, the SDK client
    can only pass the aggregate and 1h writes get priced as 5m.

    Signature-shape: discover via inspect.signature so dev can land either
    ``cached_input_write_5m_tokens`` / ``cached_input_write_1h_tokens`` or
    a single nested struct — the test requires SOME parameter that lets
    callers pass a 1h-only count.
    """
    sig = inspect.signature(compute_cost_usd)
    param_names = list(sig.parameters.keys())
    has_1h_param = any("1h" in name and "write" in name for name in param_names)
    has_5m_param = any("5m" in name and "write" in name for name in param_names)
    assert has_1h_param and has_5m_param, (
        "compute_cost_usd must accept a 5m/1h split of cache_write tokens — "
        f"got parameters={param_names!r}. The SDK client already tracks "
        "cumulative_cache_write_5m and cumulative_cache_write_1h "
        "(anthropic_sdk_client.py:130-131); the cost function must consume "
        "them so 1h writes are billed at 2x the base rate, not the 5m rate."
    )


def test_one_hour_write_cost_for_sonnet_is_2x_five_minute_write_cost() -> None:
    """The headline behavior — for the same token count, a 1h cache_write
    costs more than a 5m cache_write. Specifically (Sonnet, 1M tokens):
        5m write: 1.25M × $3 = $3.75
        1h write: 2.00M × $3 = $6.00
    Ratio 1.6x. The current aggregated-as-5m implementation gives ratio
    1.0x, which is wrong by 60% on the 1h line.

    Calls compute_cost_usd with kwargs that the dev MUST expose — see
    test_compute_cost_usd_accepts_split_5m_and_1h_writes above.
    """
    cost_1h = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_5m_tokens=0,
        cached_input_write_1h_tokens=1000,
        model="claude-sonnet-4-6",
    )
    cost_5m = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_5m_tokens=1000,
        cached_input_write_1h_tokens=0,
        model="claude-sonnet-4-6",
    )

    assert cost_1h == pytest.approx(0.006, rel=1e-6), (
        f"1000 tokens at Sonnet's 1h-write rate ($6/M) must cost $0.006; "
        f"got {cost_1h!r}. If this equals 0.00375 the 1h rate is still "
        f"falling back to the 5m rate — anthropic_cost.py wasn't updated."
    )
    assert cost_5m == pytest.approx(0.00375, rel=1e-6), (
        f"1000 tokens at Sonnet's 5m-write rate ($3.75/M) must still cost "
        f"$0.00375; got {cost_5m!r}. The 5m rate must stay unchanged — "
        f"only the 1h path is wrong today."
    )
    assert cost_1h / cost_5m == pytest.approx(1.6, rel=1e-6), (
        f"the 1h rate is 1.6x the 5m rate (1.25x→2x); got ratio {cost_1h / cost_5m!r}"
    )


def test_mixed_5m_and_1h_writes_sum_correctly() -> None:
    """A real continuation pair (per 60-3's measured iter-1 + iter-2 chain
    post-fix): the iter-1 cold write at 1h plus a hypothetical leftover
    5m write. Both must contribute at their own rate.
    """
    cost = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_5m_tokens=2000,  # leftover/legacy 5m write
        cached_input_write_1h_tokens=10000,  # 1h cold prefix write
        model="claude-sonnet-4-6",
    )
    # 2000 × $3.75/M = 0.0075; 10000 × $6/M = 0.06; sum = 0.0675
    assert cost == pytest.approx(0.0675, rel=1e-6), (
        f"mixed 5m + 1h writes must price at their respective rates and "
        f"sum to $0.0675; got {cost!r}"
    )


def test_legacy_aggregate_write_kwarg_still_works_if_preserved() -> None:
    """If dev preserves the legacy ``cached_input_write_tokens`` aggregate
    kwarg for backward compatibility, calling it without the per-TTL split
    must NOT silently produce 1h-priced cost from a 5m-priced bucket (or
    vice-versa). The aggregate MUST default to the 5m rate so legacy
    callers that never split keep their existing cost arithmetic.

    Skipped if the dev removed the aggregate kwarg entirely (clean signature
    redesign — also acceptable).
    """
    sig = inspect.signature(compute_cost_usd)
    if "cached_input_write_tokens" not in sig.parameters:
        pytest.skip(
            "dev removed cached_input_write_tokens — clean redesign; no "
            "backward-compat contract to verify"
        )

    cost = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_tokens=1000,
        model="claude-sonnet-4-6",
    )
    # Aggregate -> 5m rate (the historical and safer default). 1000 × $3.75/M.
    assert cost == pytest.approx(0.00375, rel=1e-6), (
        f"legacy aggregate kwarg must continue to price at the 5m rate; "
        f"got {cost!r}. Existing callers that pass the aggregate must not "
        f"silently shift to 1h pricing — that would over-charge them."
    )


# --- helpers ---------------------------------------------------------------


def _find_one_hour_write_rate(pricing_obj: object) -> str | None:
    """Return the name of the attribute that looks like '1h cache_write rate'.

    Allows the dev to land any of:
      - cached_input_write_1h_per_mtok_usd
      - one_hour_cache_write_per_mtok_usd
      - cache_write_1h_per_mtok_usd
    without locking the test to one specific naming.

    Enumerates via ``dataclasses.fields`` (handles ``slots=True`` dataclasses)
    with a ``dir`` fallback for non-dataclass objects.
    """
    if dataclasses.is_dataclass(pricing_obj):
        field_names: list[str] = [f.name for f in dataclasses.fields(pricing_obj)]
    else:
        field_names = [n for n in dir(pricing_obj) if not n.startswith("_")]
    candidates = [
        name for name in field_names if "1h" in name and ("write" in name or "creation" in name)
    ]
    if not candidates:
        return None
    # Prefer the most specific match if multiple landed.
    candidates.sort(key=len, reverse=True)
    return candidates[0]
