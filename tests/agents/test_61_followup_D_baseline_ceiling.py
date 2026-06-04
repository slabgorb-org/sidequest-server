"""Story 61-followup-D — Baseline ceiling (mitigation A).

The trained-into-silence hole in 61-4: the rolling K=10 baseline is an
unbounded mean of observed calls. A sustained N-x-over-target regime
trains the baseline upward into the new amplitude, and the
``5 × baseline`` cost-multiple rule (and the ``2 × baseline`` I/O
fingerprint rule) slides up with it — silent on the regime, only loud
on a spike.

Mitigation: clamp the **baseline used in comparison** at 3× the warmup
floors:

- ``_BASELINE_COST_CEILING = 0.09``   (3× ``_WARMUP_COST_USD_FLOOR``)
- ``_BASELINE_INPUT_CEILING = 36_000`` (3× ``_WARMUP_INPUT_TOKENS_FLOOR``)

After the clamp, the comparator's effective trip envelope is:

- Cost trigger: never higher than 5 × $0.09 = $0.45/call.
- I/O trigger: never higher than 2 × 36_000 = 72_000 input tokens
  (with the existing ``output_tokens < 50`` half preserved).

These tests are the RED gate. See
``sprint/context/context-story-61-followup-D.md`` §A for the locked
decisions.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from sidequest.agents.anthropic_sdk_client import (
    _BASELINE_COST_CEILING,
    _BASELINE_INPUT_CEILING,
    _WARMUP_COST_USD_FLOOR,
    _WARMUP_INPUT_TOKENS_FLOOR,
    AnthropicSdkClient,
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub

# Reuse the SDK fake shape that 61-4's tests established. Importing from
# the 61-4 module would create a test-test coupling; the shape is small
# enough to redefine here.
from tests._helpers.doubles import FakeSocket
from tests.agents.test_61_4_cost_runaway_alarm import (  # type: ignore[attr-defined]
    _resp,
    _Sdk,
    _system_blocks,
    _tools_empty,
    _user_msg,
)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind watcher hub to the test loop + drop subscribers. Same as 61-4."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _build_client(sdk: _Sdk) -> AnthropicSdkClient:
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


# ---------------------------------------------------------------------------
# 1. Constants are 3× the warmup floors (drift detector)
# ---------------------------------------------------------------------------


def test_baseline_ceilings_are_3x_warmup_floors() -> None:
    """Sanity / drift detector. Per story §A, the ceilings are derived
    from the steady-state target ($0.03/turn cost, 12_000-token input).
    If someone changes a warmup floor in 61-4 without updating the
    61-followup-D ceiling, this test catches the asymmetry before the
    next reviewer pass does.

    Two-step assertion (reviewer 2026-05-23 test-analyzer): primary
    literal value check (catches "someone changes the warmup floor
    and forgot to update the ceiling derivation"); secondary 3×
    relationship check (catches "someone changes the multiplier
    without updating the literal").
    """
    # Primary — the locked literal values from story §A.
    assert pytest.approx(0.09) == _BASELINE_COST_CEILING, (
        f"_BASELINE_COST_CEILING must equal $0.09 (story §A locked "
        f"value). Got {_BASELINE_COST_CEILING!r}."
    )
    assert _BASELINE_INPUT_CEILING == 36_000, (
        f"_BASELINE_INPUT_CEILING must equal 36_000 (story §A locked "
        f"value). Got {_BASELINE_INPUT_CEILING!r}."
    )

    # Secondary — the 3× derivation. If a future change to the warmup
    # floor breaks the 3× relationship, this catches the asymmetry.
    assert pytest.approx(3.0 * _WARMUP_COST_USD_FLOOR) == _BASELINE_COST_CEILING, (
        f"_BASELINE_COST_CEILING must equal 3× _WARMUP_COST_USD_FLOOR. "
        f"Got {_BASELINE_COST_CEILING!r} vs "
        f"3×{_WARMUP_COST_USD_FLOOR!r}={3.0 * _WARMUP_COST_USD_FLOOR!r}."
    )
    assert _BASELINE_INPUT_CEILING == 3 * _WARMUP_INPUT_TOKENS_FLOOR, (
        f"_BASELINE_INPUT_CEILING must equal 3× _WARMUP_INPUT_TOKENS_FLOOR. "
        f"Got {_BASELINE_INPUT_CEILING!r} vs "
        f"3×{_WARMUP_INPUT_TOKENS_FLOOR!r}={3 * _WARMUP_INPUT_TOKENS_FLOOR!r}."
    )


# ---------------------------------------------------------------------------
# 2. Behavioral catch — input clamp catches a post-ramp call that
#    would have slipped past the unclamped comparator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_clamp_catches_post_ramp_call_unclamped_would_miss(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """The behavioral teeth of mitigation A — the input axis is where the
    clamp's marginal protection is most visible.

    Sequence:
    - 10 sustained 60K-in / 500-out calls (output ≥ 50 keeps io_fingerprint
      silent; cost ~$0.1875 per call is above the $0.15 warmup floor so
      cost_multiple WILL fire during warmup — those events are not what
      this test asserts on).
    - After K=10, observed ``input_tokens_baseline`` = 60_000.
    - Probe call: 80_000-in / 12-out.

    Without the clamp:
      Trip threshold for io_fingerprint = 2 × 60_000 = 120_000. The
      80_000 probe is sub-2x the *observed* baseline → io_fingerprint
      is silent. Operator misses the canary.

    With the clamp at _BASELINE_INPUT_CEILING (36_000):
      Effective trip threshold = 2 × 36_000 = 72_000. The 80_000 probe
      is above 72_000 AND output_tokens=12 < 50 → io_fingerprint fires.

    The probe call's emitted event MUST carry ``trigger="io_fingerprint"``
    AND ``warmup=False`` (post-warmup) AND ``baseline_input_tokens ≤
    _BASELINE_INPUT_CEILING`` (proves the reported baseline reflects the
    clamp, not the drifted mean).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sustained = _resp(input_tokens=60_000, output_tokens=500)
    probe = _resp(input_tokens=80_000, output_tokens=12)
    sdk = _Sdk(responses=[sustained for _ in range(10)] + [probe])
    client = _build_client(sdk)

    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="61-baseline-test",
        )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    post_warmup_io = [
        e
        for e in events
        if e["fields"]["warmup"] is False and e["fields"]["trigger"] == "io_fingerprint"
    ]
    assert len(post_warmup_io) == 1, (
        "The 80K-in/12-out probe AFTER a 60K-in baseline must fire "
        "io_fingerprint via the CLAMPED comparator (72K threshold) — "
        "unclamped this call would pass silently (120K threshold). Got "
        f"{len(post_warmup_io)} post-warmup io_fingerprint events. "
        f"All events: {[(e['fields']['trigger'], e['fields']['warmup']) for e in events]}"
    )
    fields = post_warmup_io[0]["fields"]
    assert fields["baseline_input_tokens"] <= _BASELINE_INPUT_CEILING, (
        "Post-warmup probe MUST report a baseline_input_tokens at or "
        f"below the clamp ceiling ({_BASELINE_INPUT_CEILING}); a higher "
        "value means the clamp didn't engage. Got "
        f"baseline_input_tokens={fields['baseline_input_tokens']!r}"
    )


# ---------------------------------------------------------------------------
# 3. Reported field fidelity — baseline_cost_usd reflects the clamp,
#    not the drifted mean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_cost_usd_field_is_clamped_post_warmup(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """The event's ``baseline_cost_usd`` field is the GM panel's signal
    for "what threshold did the alarm compare against?" If the clamp
    engages, that field MUST reflect the clamp value, not the raw mean.

    Without this, operator-facing math breaks: an event showing
    ``baseline_cost_usd=0.165, cost_usd=0.50`` implies trip threshold
    $0.825 and "this barely tripped" — when in reality the clamped
    threshold is $0.45 and the call is ~1.1× threshold.

    Sequence: train cost baseline at $0.1875/call (60K-in/500-out costs
    ~$0.1875) for K=10. Probe at the same shape — the probe's emitted
    event is post-warmup (warmup=False). Assert its
    ``baseline_cost_usd`` field is ``≤ _BASELINE_COST_CEILING`` ($0.09),
    NOT the unclamped mean (~$0.1875).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sustained = _resp(input_tokens=60_000, output_tokens=500)
    # Probe must trip SOMETHING so we get an event to inspect. cost ≈
    # $0.1875 > $0.15 warmup floor (but warmup is exhausted) AND > 5 ×
    # $0.09 clamped baseline = $0.45? No, 0.1875 < 0.45. So we need a
    # different probe that trips post-warmup. Use the io_fingerprint via
    # output<50 with input above the clamped threshold.
    probe = _resp(input_tokens=80_000, output_tokens=12)
    sdk = _Sdk(responses=[sustained for _ in range(10)] + [probe])
    client = _build_client(sdk)

    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="61-baseline-test",
        )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    post_warmup = [e for e in events if e["fields"]["warmup"] is False]
    assert len(post_warmup) == 1, (
        "Expected exactly one post-warmup event from the 80K probe; got "
        f"{len(post_warmup)} post-warmup events."
    )
    fields = post_warmup[0]["fields"]
    # The clamp must engage: observed mean of 10× $0.1875 ≈ $0.1875 is
    # well above $0.09 ceiling, so reported baseline must equal the
    # ceiling, not the mean.
    assert fields["baseline_cost_usd"] <= _BASELINE_COST_CEILING + 1e-9, (
        f"baseline_cost_usd MUST reflect the clamp value "
        f"(≤ {_BASELINE_COST_CEILING}) when the drifted mean exceeds it. "
        f"Got baseline_cost_usd={fields['baseline_cost_usd']!r} "
        f"(unclamped this would be ~$0.1875)."
    )


# ---------------------------------------------------------------------------
# 4. Healthy steady-state — clamp is a no-op when baseline is at target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clamp_is_noop_for_healthy_steady_state_baseline(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """When the rolling baseline is at or below the ceiling (healthy
    steady state, ~$0.03/turn ~12K-in), the clamp MUST NOT lower the
    reported baseline below the observed mean — otherwise the operator
    sees a misleadingly low threshold and the comparator becomes
    over-sensitive (false-positive-prone).

    The clamp is a CEILING (``min(mean, ceiling)``), not a fixed value.

    Sequence: 10 healthy 12K-in / 500-out calls (cost ~$0.045/turn,
    above $0.03 floor but well below $0.09 ceiling). Probe at the same
    shape doesn't trip — and that's the assertion: NO event fires on
    the steady-state probe, and the deque holds 10 observations
    averaging to ~$0.045 / 12_000 tokens.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 12K-in / 500-out costs $3/MTok × 12K + $15/MTok × 500 ≈ $0.0435.
    # That's > the $0.03 warmup floor → cost_multiple WOULD trip during
    # warmup at $0.0435 > 5 × $0.03 = $0.15? No, 0.0435 < 0.15, silent.
    # output=500 keeps io_fingerprint silent. So warmup is fully clean.
    healthy = _resp(input_tokens=12_000, output_tokens=500)
    sdk = _Sdk(responses=[healthy for _ in range(11)])
    client = _build_client(sdk)

    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="61-baseline-test",
        )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert events == [], (
        "Healthy steady-state (12K-in / 500-out × 11 calls) MUST NOT "
        "fire any cost_runaway_suspected event — the clamp is a CEILING, "
        "not a fixed value, so it MUST be a no-op when the observed "
        "baseline is below the ceiling. Got "
        f"{len(events)} events: {[e['fields']['trigger'] for e in events]}"
    )


# ---------------------------------------------------------------------------
# 5. Negative — cost-multiple does not fire on a post-warmup call below
#    the absolute floor when the clamp is in play (precision check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_multiple_silent_for_low_cost_post_warmup_when_clamped(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Precision check: the clamp lowers the cost_multiple trip threshold
    from ~5 × $0.1875 = $0.9375 (unclamped, post-ramp) to 5 × $0.09 =
    $0.45 (clamped). A probe BELOW the clamp's trip threshold (e.g.,
    $0.2 — also below the $0.30 absolute floor) MUST stay silent on
    cost_multiple.

    Without this assertion, a buggy implementation that interpreted the
    clamp as "always trip on >$0.09" would pass — that would convert
    every healthy call into an alarm. The clamp must preserve the 5×
    multiplier.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # Train cost baseline at $0.1875 (output 500 keeps io_fingerprint
    # silent throughout). Warmup: $0.1875 > $0.15 floor → cost_multiple
    # WILL fire during warmup; that's expected and we filter for
    # post-warmup events below.
    sustained = _resp(input_tokens=60_000, output_tokens=500)
    # Probe at ~$0.066 — well above $0.045 (1.5×) but well below $0.30
    # (absolute floor) AND below 5 × $0.09 = $0.45 (clamped multiple).
    # io_fingerprint silent (input 20K < 2 × 36K clamp; output 500 ≥ 50).
    # No trigger should fire — assert silence.
    probe = _resp(input_tokens=20_000, output_tokens=500)
    sdk = _Sdk(responses=[sustained for _ in range(10)] + [probe])
    client = _build_client(sdk)

    with caplog.at_level(logging.ERROR, logger="sidequest.agents.anthropic_sdk_client"):
        for _ in range(11):
            await client.complete_with_tools(
                system_blocks=_system_blocks(),
                messages=_user_msg(),
                tools=_tools_empty(),
                model="claude-sonnet-4-6",
                session_id="61-baseline-test",
            )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    post_warmup = [e for e in events if e["fields"]["warmup"] is False]
    assert post_warmup == [], (
        "Post-warmup 20K-in / 500-out probe (cost ~$0.066) is below the "
        "$0.30 absolute floor AND below 5 × $0.09 clamped baseline "
        "($0.45) AND below 2 × 36K clamped input threshold (72K) AND "
        "output ≥ 50. NO post-warmup event should fire. Got "
        f"{len(post_warmup)} post-warmup events: "
        f"{[(e['fields']['trigger'], e['fields']['cost_usd']) for e in post_warmup]}"
    )
