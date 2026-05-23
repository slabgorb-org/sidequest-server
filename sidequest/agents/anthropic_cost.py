"""Anthropic API pricing — pure functions.

Pricing snapshot from 2026-05-15. Update when Anthropic publishes a change;
unit tests pin the constants so a change is forced through review.
"""

from __future__ import annotations

from dataclasses import dataclass


class UnknownModel(ValueError):
    """compute_cost_usd was passed a model id not in the pricing table."""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Anthropic per-model pricing snapshot.

    Two ephemeral cache-write rates are tracked explicitly: ``ephemeral_5m``
    bills at 1.25x the base input rate and ``ephemeral_1h`` at 2x. Prior to
    Story 60-4 the 1h rate field did not exist; 1h writes were silently
    priced at the 5m rate, *understating* the real bill by ~60% on the 1h
    line. With 60-4's moving cache_control breakpoint, every continuation
    write lands in the 1h bucket, so accurate 1h pricing is now load-bearing
    for the GM-panel cost_usd display.
    """

    model: str
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    cached_input_read_per_mtok_usd: float
    cached_input_write_per_mtok_usd: float
    """5m ephemeral cache_write rate (1.25x base input)."""
    cached_input_write_1h_per_mtok_usd: float
    """1h ephemeral cache_write rate (2x base input). Story 60-4."""


_PRICING: dict[str, ModelPricing] = {
    "claude-sonnet-4-6": ModelPricing(
        model="claude-sonnet-4-6",
        input_per_mtok_usd=3.0,
        output_per_mtok_usd=15.0,
        cached_input_read_per_mtok_usd=0.30,
        cached_input_write_per_mtok_usd=3.75,
        cached_input_write_1h_per_mtok_usd=6.0,
    ),
    "claude-haiku-4-5-20251001": ModelPricing(
        model="claude-haiku-4-5-20251001",
        input_per_mtok_usd=1.0,
        output_per_mtok_usd=5.0,
        cached_input_read_per_mtok_usd=0.10,
        cached_input_write_per_mtok_usd=1.25,
        cached_input_write_1h_per_mtok_usd=2.0,
    ),
    "claude-opus-4-7": ModelPricing(
        model="claude-opus-4-7",
        input_per_mtok_usd=15.0,
        output_per_mtok_usd=75.0,
        cached_input_read_per_mtok_usd=1.50,
        cached_input_write_per_mtok_usd=18.75,
        cached_input_write_1h_per_mtok_usd=30.0,
    ),
}


def model_pricing(model: str) -> ModelPricing:
    try:
        return _PRICING[model]
    except KeyError as exc:
        raise UnknownModel(f"No pricing entry for model {model!r}") from exc


def compute_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_read_tokens: int,
    cached_input_write_tokens: int = 0,
    cached_input_write_5m_tokens: int = 0,
    cached_input_write_1h_tokens: int = 0,
    model: str,
) -> float:
    """Sum the per-bucket cost for one API call.

    ``input_tokens`` must be fresh (uncached) input only — matches the
    Anthropic SDK's ``usage.input_tokens`` semantics, which excludes cached
    buckets.

    Cache-write tokens come in three flavors, kept separate so 1h writes
    (Story 60-4) are billed at the real 2x base rate instead of the 5m
    rate:

    - ``cached_input_write_tokens`` — legacy aggregate. Priced at the 5m
      rate. Preserved for backward compatibility with callers that pre-date
      the per-TTL split; new code should pass the 5m/1h split directly.
    - ``cached_input_write_5m_tokens`` — explicit 5m write count (1.25x).
    - ``cached_input_write_1h_tokens`` — explicit 1h write count (2x).

    Callers may pass either the legacy aggregate OR the split, but not
    both for the same TTL bucket (no double-counting). Passing all three
    is meaningful when a turn carries a real 5m+1h mix.
    """
    p = model_pricing(model)
    return (
        input_tokens * p.input_per_mtok_usd
        + output_tokens * p.output_per_mtok_usd
        + cached_input_read_tokens * p.cached_input_read_per_mtok_usd
        + cached_input_write_tokens * p.cached_input_write_per_mtok_usd
        + cached_input_write_5m_tokens * p.cached_input_write_per_mtok_usd
        + cached_input_write_1h_tokens * p.cached_input_write_1h_per_mtok_usd
    ) / 1_000_000
