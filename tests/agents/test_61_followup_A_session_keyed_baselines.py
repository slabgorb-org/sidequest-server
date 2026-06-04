"""Story 61-followup-A — Session-id-keyed baselines on AnthropicSdkClient.

The 61-4 fingerprint detector's rolling baselines (`_cost_baseline`,
`_input_tokens_baseline`) live on the AnthropicSdkClient INSTANCE today,
not on a session key. Multiplayer session URLs are deterministic
(memory: project_session_id_dropin — /play/{date}-{world}-mp rejoins
the existing session) and the same client instance can fire calls for
multiple logical sessions, so the rolling window for one logical
session pollutes (or starves) the next.

This story makes the contract baseline-per-session: a session_id owns
its own K=10 deque. The pattern mirrors 61-followup-D's
`_session_cumulative_cost_usd: dict[str, float]` (anthropic_sdk_client.py:226).

Behavioral contract additions (per context-story-61-followup-A.md):

- `_cost_baseline` and `_input_tokens_baseline` are `dict[str, deque]`
  keyed on session_id, lazily initialized on first append.
- `_maybe_emit_cost_runaway` takes `session_id: str | None` and reads
  the per-session deque (or warmup floors if no entry exists).
- When `session_id is None` (non-narrator codepaths like the dungeon
  materializer one-shot curate), the detector is a no-op: neither
  reads nor appends to any baseline. Same semantics as the existing
  session-cumulative tracker's None bypass (see
  ``AnthropicSdkClient.complete_with_tools``, where ``session_id is
  not None`` gates both ``_check_cost_ceiling`` and
  ``_update_session_cumulative``).
- `reset_baselines()` becomes `reset_baselines(session_id: str)` and
  clears only the target session's deques. Calling it on a never-seen
  session_id is a no-op (no KeyError). The dormant-infrastructure
  docstring is updated to reflect the new per-session shape;
  SessionRoom.close_store() wiring is deferred to 61-followup-C.

These tests are the RED gate. See
`sprint/context/context-story-61-followup-A.md` for the full design.

Out of scope for A — do NOT let these tests grow to assert:
- SessionRoom.close_store() wiring of reset_baselines(session_id) —
  that's 61-followup-C.
- Promotion of narrator.sdk.usage log to a watcher event — that's
  61-followup-B.
- TTL / LRU eviction of the per-session dict — 61-followup-C.
- Adding session_id to the cost_runaway_suspected event payload —
  the context permits it as a smallest-possible addition if needed,
  but it's not an AC. Don't make tests require it.
"""

from __future__ import annotations

import asyncio
import inspect
import typing
from collections.abc import Mapping

import pytest

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub

# Reuse the SDK fake shape from 61-4. The handoff debt note flags this
# import as test-test coupling that should be consolidated into
# tests/agents/fakes/sdk_shape.py BEFORE a sixth sibling lands; A is the
# fifth and right-sizes per memory feedback_plan_ceremony (2pt story,
# don't expand scope). See Delivery Findings in the session file.
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
    """Bind watcher hub to the test loop + drop subscribers. Same shape
    as 61-4 and the four D test files."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _build_client(sdk: _Sdk) -> AnthropicSdkClient:
    """Construct a client wired to the fake SDK; cache_ttl=1h matches
    production default (ADR-101 + 60-4)."""
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


# ---------------------------------------------------------------------------
# 1. Structural drift detectors — type shape of the per-session deques
# ---------------------------------------------------------------------------


def test_cost_baseline_is_dict_keyed_on_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract: `_cost_baseline` is `dict[str, deque[float]]`.
    A fresh client starts with an empty dict.

    Without this shape, the per-session semantics required by ACs 1, 3,
    and 4 are physically impossible — the comparator has nowhere to
    store a separate window for each session_id.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])
    client = _build_client(sdk)

    assert isinstance(client._cost_baseline, Mapping), (
        "Story 61-followup-A: _cost_baseline must be a Mapping "
        "(dict[str, deque[float]]) so per-session keys can coexist. "
        f"Got type {type(client._cost_baseline).__name__!r}."
    )
    assert len(client._cost_baseline) == 0, (
        "A fresh client must start with no session entries — sessions "
        "are added lazily on first observation (mirrors the existing "
        "_session_cumulative_cost_usd dict pattern). "
        f"Got {len(client._cost_baseline)} entries: {list(client._cost_baseline)!r}."
    )


def test_input_tokens_baseline_is_dict_keyed_on_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract as _cost_baseline — the input-tokens window MUST
    be session-keyed too. The pair must be symmetric or the comparator
    can fire one trigger from session A's cost data and another from
    session B's input data."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])
    client = _build_client(sdk)

    assert isinstance(client._input_tokens_baseline, Mapping), (
        "Story 61-followup-A: _input_tokens_baseline must be a Mapping "
        "(dict[str, deque[int]]). "
        f"Got type {type(client._input_tokens_baseline).__name__!r}."
    )
    assert len(client._input_tokens_baseline) == 0, (
        "Fresh client must start with no session entries. "
        f"Got {len(client._input_tokens_baseline)} entries: "
        f"{list(client._input_tokens_baseline)!r}."
    )


# ---------------------------------------------------------------------------
# 2. Signature drift detectors — _maybe_emit_cost_runaway gets session_id;
#    reset_baselines gets a typed session_id parameter (lang-review rule #3)
# ---------------------------------------------------------------------------


def test_maybe_emit_cost_runaway_accepts_session_id_kwarg() -> None:
    """The internal hook must accept session_id so the call site
    inside ``complete_with_tools`` can plumb the value through.
    AC 1, 3, 5 all require session-aware reads.
    """
    sig = inspect.signature(AnthropicSdkClient._maybe_emit_cost_runaway)
    assert "session_id" in sig.parameters, (
        "Story 61-followup-A: _maybe_emit_cost_runaway MUST accept a "
        "session_id parameter (keyword-only) so the per-session deque "
        "can be looked up. "
        f"Got parameters: {list(sig.parameters)!r}."
    )
    # Keyword-only is the existing convention for this method's other
    # parameters (input_tokens, output_tokens, cost_usd, model are all
    # keyword-only via *,). The new parameter MUST follow suit.
    param = sig.parameters["session_id"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
        "session_id MUST be keyword-only to match the existing call-site "
        "style at orchestrator.py:2661 (session_id=current_session_id). "
        f"Got kind={param.kind!r}."
    )


def test_reset_baselines_requires_typed_session_id_param() -> None:
    """AC 4 + lang-review rule #3 (type annotations on public surface).

    reset_baselines() becomes reset_baselines(session_id: str). It is the
    seam 61-followup-C wires from SessionRoom.close_store(), so the
    parameter being typed-and-required (not Optional, not default-None)
    is load-bearing: an accidental reset_baselines() with no argument
    would silently clear nothing rather than crashing, and C's call site
    would compile without effect.
    """
    sig = inspect.signature(AnthropicSdkClient.reset_baselines)
    params = sig.parameters
    assert "session_id" in params, (
        "Story 61-followup-A AC 4: reset_baselines MUST require a "
        "session_id parameter so resets are scoped to one session, not "
        "instance-wide. "
        f"Got parameters: {list(params)!r}."
    )
    param = params["session_id"]
    # `from __future__ import annotations` (PEP 563) stringifies all
    # annotations in anthropic_sdk_client.py, so `param.annotation` is
    # the string `'str'`, not the type `str`. Use get_type_hints to
    # resolve the forward reference against the module's globals; that
    # gives us back the actual type for the identity check.
    hints = typing.get_type_hints(AnthropicSdkClient.reset_baselines)
    assert hints.get("session_id") is str, (
        "Lang-review rule #3 (type annotations at public surface) + "
        "context AC 4: session_id MUST be annotated as `str`. "
        f"Got resolved hint: {hints.get('session_id')!r} "
        f"(raw annotation: {param.annotation!r})."
    )
    assert param.default is inspect.Parameter.empty, (
        "session_id MUST be required (no default). A default-None "
        "signature lets callers accidentally reset nothing. "
        f"Got default={param.default!r}."
    )


# ---------------------------------------------------------------------------
# 3. AC 1 — Per-session baseline independence (direct call)
# ---------------------------------------------------------------------------


def test_direct_emit_records_per_session_baseline_independence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: accessing baseline for session A, then session B, MUST
    return independent deques.

    Direct test on `_maybe_emit_cost_runaway` (no SDK call required) —
    drives the simplest path. The wiring through complete_with_tools is
    covered separately by `test_complete_with_tools_routes_session_id_*`.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])
    client = _build_client(sdk)

    # Two pairs of distinct values so we can prove the deque contents are
    # not just non-empty but actually carry per-session data.
    client._maybe_emit_cost_runaway(
        input_tokens=15_000,
        output_tokens=500,
        cost_usd=0.05,
        model="claude-sonnet-4-6",
        session_id="session-A",
    )
    client._maybe_emit_cost_runaway(
        input_tokens=30_000,
        output_tokens=500,
        cost_usd=0.10,
        model="claude-sonnet-4-6",
        session_id="session-B",
    )

    assert "session-A" in client._cost_baseline, (
        "AC 1: session-A's first observation MUST create an entry in "
        f"_cost_baseline. Got keys: {list(client._cost_baseline)!r}."
    )
    assert "session-B" in client._cost_baseline, (
        "AC 1: session-B's first observation MUST create an entry in "
        f"_cost_baseline. Got keys: {list(client._cost_baseline)!r}."
    )
    a_costs = list(client._cost_baseline["session-A"])
    b_costs = list(client._cost_baseline["session-B"])
    assert a_costs == [pytest.approx(0.05)], (
        f"session-A's deque must hold exactly its own observation. Got {a_costs!r}."
    )
    assert b_costs == [pytest.approx(0.10)], (
        f"session-B's deque must hold exactly its own observation, "
        f"NOT session-A's. Got {b_costs!r}."
    )
    a_inputs = list(client._input_tokens_baseline["session-A"])
    b_inputs = list(client._input_tokens_baseline["session-B"])
    assert a_inputs == [15_000], (
        f"session-A's input deque must hold exactly its own observation. Got {a_inputs!r}."
    )
    assert b_inputs == [30_000], (
        f"session-B's input deque must hold exactly its own observation, "
        f"NOT session-A's. Got {b_inputs!r}."
    )


# ---------------------------------------------------------------------------
# 4. AC 2 — Fresh rejoin starts with clean baseline (warmup floor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_session_uses_warmup_floor_not_event(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC 2: a never-seen session_id starts in warmup mode (uses
    warmup floors, not zero) and a healthy call MUST NOT fire any
    cost_runaway_suspected event.

    A regression where a fresh session sat at baseline=0 would trip
    every cost trigger on call #1 (cost > 5 × 0 = 0 always). A
    regression where a fresh session inherited some OTHER session's
    baseline would defeat the entire story.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 12K-in / 500-out is the explicit healthy reference shape (matches
    # the _healthy() fixture in test_61_4). Cost is ~$0.0435 (12_000 *
    # $3/M + 500 * $15/M) — above warmup cost floor $0.03 but well
    # below 5x warmup floor = $0.15, so cost_multiple cannot fire.
    sdk = _Sdk(responses=[_resp(input_tokens=12_000, output_tokens=500)])
    client = _build_client(sdk)

    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="brand-new-session",
    )
    await asyncio.sleep(0.05)

    runaway_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(runaway_events) == 0, (
        "AC 2: a fresh session's first healthy call MUST NOT fire "
        "cost_runaway_suspected. A nonzero count here means either "
        "(a) the fresh-session baseline initialized to 0 instead of "
        "warmup floors, or (b) the fresh session inherited another "
        f"session's drifted baseline. Got events: {runaway_events!r}."
    )
    # And the session must now have its own deque populated.
    assert "brand-new-session" in client._cost_baseline, (
        "After the call lands, the fresh session must have its own "
        f"deque entry. Got keys: {list(client._cost_baseline)!r}."
    )


# ---------------------------------------------------------------------------
# 5. AC 3 + AC 5 — THE GAUNTLET: session A trips on cost_multiple even
#    after session B floods the (formerly shared) window. This is the
#    test that exposes today's instance-wide pollution bug.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_a_trips_cost_multiple_after_session_b_floods(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC 3 + AC 5 (regression / cross-session independence under load).

    The setup deliberately picks values that distinguish the
    per-session and instance-wide behaviors at the TRIGGER level, not
    just at the deque-contents level:

    - Session A: 10 healthy calls at input=11_000 / output=500
      → cost per call ≈ $0.0405 (11K*$3/M + 500*$15/M).
      Per-session A mean ≈ $0.0405 → clamped baseline = $0.0405
      (below the _BASELINE_COST_CEILING of $0.09).
    - Session B: 10 heavier calls at input=30_000 / output=500
      → cost per call ≈ $0.0975. These calls happen STRICTLY AFTER
      session A so that in the buggy instance-wide world they fill
      the K=10 deque and evict session A's observations entirely.
    - Trip call on session A: input=35_000 / output=8_000
      → cost ≈ $0.225 (35K*$3/M + 8K*$15/M).

    Trigger analysis on the TRIP call:

    * io_fingerprint requires output < 50; output is 8000 → silent.
    * input_absolute requires input > 40_000; input is 35_000 → silent.
    * cost_absolute requires cost > $0.30; cost is $0.225 → silent.
    * cost_multiple requires cost > 5 × baseline.

    Per-session correct (post-fix):
      Session A's clamped baseline = $0.0405. 5x = $0.2025.
      Trip cost $0.225 > $0.2025 → cost_multiple FIRES.

    Instance-wide today (RED, contaminated):
      The instance-wide deque holds the most recent 10 calls = all 10
      session-B calls (session A's 10 were evicted). Mean = $0.0975,
      clamped at min($0.0975, $0.09) = $0.09. 5x = $0.45.
      Trip cost $0.225 < $0.45 → cost_multiple is SILENT.
      No other trigger fires either (see analysis above) → 0 events.

    So this test fails today with `len(events) == 0` and passes after
    Dev's per-session refactor with `len(events) == 1, trigger=cost_multiple`.

    The shape was chosen to be the cleanest single-test discriminator:
    only the cost_multiple trigger depends on the baseline, and the
    baseline is the entire surface 61-followup-A modifies.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    a_baseline = _resp(input_tokens=11_000, output_tokens=500)
    b_flood = _resp(input_tokens=30_000, output_tokens=500)
    a_trip = _resp(input_tokens=35_000, output_tokens=8_000)
    sdk = _Sdk(responses=[a_baseline] * 10 + [b_flood] * 10 + [a_trip])
    client = _build_client(sdk)

    # Warm A.
    for _ in range(10):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="session-A",
        )
    # Flood from B (in the buggy world, B's calls evict A's from the
    # shared instance-wide deque).
    for _ in range(10):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="session-B",
        )
    # Clear out events from the warmup / flood phases — those can
    # legitimately include cost_runaway alarms during early warmup if
    # the cost_absolute trigger ever fires, and we only care about the
    # trip call's outcome.
    sock.events.clear()

    # Trip on A. In the per-session world A's baseline is still its
    # own $0.0405; in the instance-wide world it's polluted to ~$0.0975.
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="session-A",
    )
    await asyncio.sleep(0.05)

    runaway_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(runaway_events) == 1, (
        "AC 3 / AC 5: after session B floods, session A's trip call "
        "(cost ~$0.225) MUST still be compared against session A's own "
        "rolling baseline (mean ~$0.0405). 5×$0.0405 = $0.2025; the "
        "trip call exceeds that and MUST fire cost_runaway_suspected. "
        "Got 0 events → session A's baseline was contaminated by "
        "session B (the instance-wide pollution bug 61-followup-A fixes). "
        f"All events: {[(e.get('event_type'), e.get('fields', {}).get('trigger')) for e in sock.events]}."
    )
    fields = runaway_events[0].get("fields", {})
    assert fields.get("trigger") == "cost_multiple", (
        "AC 3: the trip MUST fire via cost_multiple (the baseline-relative "
        "trigger), not via cost_absolute (the safety-net floor). If trigger "
        "is cost_absolute, the per-session baseline was wrong but the "
        "absolute floor saved us — the per-session semantics aren't "
        f"actually working. Got trigger={fields.get('trigger')!r}, fields={fields!r}."
    )
    # baseline_cost_usd in the event MUST reflect session A's mean
    # (~$0.0405), NOT session B's mean (~$0.0975) and NOT the polluted
    # cross-session mean (~$0.069).
    baseline = fields.get("baseline_cost_usd", 0.0)
    assert 0.035 <= baseline <= 0.050, (
        "The event's baseline_cost_usd MUST report session A's own mean "
        "(~$0.0405). A value near $0.0975 means session B's deque was "
        "used; a value near $0.069 means a polluted mean. "
        f"Got baseline_cost_usd={baseline!r}."
    )


# ---------------------------------------------------------------------------
# 6. session_id=None bypass — direct call and wiring case
# ---------------------------------------------------------------------------


def test_session_id_none_direct_call_skips_baseline_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context behavioral addition: `session_id=None` is a complete
    no-op for the detector — no read, no append. This mirrors the
    existing None-bypass in the session-cumulative tracker (the
    ``if session_id is not None`` gate at the top of
    ``complete_with_tools``).

    Without this, non-narrator codepaths that pass session_id=None
    (dungeon materializer one-shot curate, future ad-hoc one-shots)
    would either crash on a None dict key or pollute a magic
    "<no-session>" bucket — both worse than no-op.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])
    client = _build_client(sdk)

    client._maybe_emit_cost_runaway(
        input_tokens=50_000,
        output_tokens=500,
        cost_usd=0.20,
        model="claude-sonnet-4-6",
        session_id=None,
    )

    assert len(client._cost_baseline) == 0, (
        "session_id=None MUST be a no-op for the baseline dict — no "
        "append, no magic-key bucket. "
        f"Got {len(client._cost_baseline)} entries: "
        f"{list(client._cost_baseline)!r}."
    )
    assert len(client._input_tokens_baseline) == 0, (
        "session_id=None MUST be a no-op for the input baseline dict. "
        f"Got {len(client._input_tokens_baseline)} entries: "
        f"{list(client._input_tokens_baseline)!r}."
    )


@pytest.mark.asyncio
async def test_complete_with_tools_with_none_session_id_does_not_populate_baseline(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Wiring case: a complete_with_tools call without a session_id
    (the non-narrator path — dungeon materializer) MUST NOT populate
    any baseline entry.

    Today's instance-wide deque would happily append that call,
    polluting the narrator's baseline. Story 61-followup-A's None bypass
    closes that leak.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_resp(input_tokens=12_000, output_tokens=500)])
    client = _build_client(sdk)

    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        # Explicitly omit session_id — equivalent to the default None.
    )
    await asyncio.sleep(0.05)

    assert len(client._cost_baseline) == 0, (
        "complete_with_tools without session_id MUST NOT populate the "
        "baseline dict (non-narrator paths don't have a session "
        "identity). "
        f"Got entries: {list(client._cost_baseline)!r}."
    )
    runaway_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(runaway_events) == 0, (
        "The session_id=None call is a healthy 12K-in/500-out call — "
        "it MUST NOT fire cost_runaway_suspected regardless of bypass "
        "shape. "
        f"Got events: {runaway_events!r}."
    )


# ---------------------------------------------------------------------------
# 7. AC 6 — Wiring: complete_with_tools routes session_id into the
#    per-session baseline dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_with_tools_routes_session_id_to_baseline_dict(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC 6 (integration / wiring): complete_with_tools must plumb its
    session_id parameter all the way down to the per-session deque
    append inside ``_maybe_emit_cost_runaway``.

    Server CLAUDE.md "Every Test Suite Needs a Wiring Test": the unit
    test at `test_direct_emit_records_per_session_baseline_independence`
    proves the comparator works in isolation. This test proves
    complete_with_tools is actually plumbing session_id through. Without
    it, a future refactor that drops the keyword argument from the
    internal call would silently regress to instance-wide pollution.

    Three calls: X, Y, X. Expected dict shape after:
      _cost_baseline = {"X": deque([cost_x1, cost_x2]),
                        "Y": deque([cost_y1])}
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    healthy = _resp(input_tokens=12_000, output_tokens=500)
    sdk = _Sdk(responses=[healthy, healthy, healthy])
    client = _build_client(sdk)

    for sid in ("session-X", "session-Y", "session-X"):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id=sid,
        )
    await asyncio.sleep(0.05)

    assert set(client._cost_baseline.keys()) == {"session-X", "session-Y"}, (
        "AC 6: exactly two session entries must exist after X/Y/X. "
        f"Got keys: {set(client._cost_baseline.keys())!r}."
    )
    x_costs = list(client._cost_baseline["session-X"])
    y_costs = list(client._cost_baseline["session-Y"])
    assert len(x_costs) == 2, (
        "session-X received TWO complete_with_tools calls; its deque "
        f"MUST hold two observations. Got {len(x_costs)}: {x_costs!r}."
    )
    assert len(y_costs) == 1, (
        "session-Y received ONE complete_with_tools call; its deque "
        f"MUST hold one observation. Got {len(y_costs)}: {y_costs!r}."
    )
    # Mirror assertion on the input-tokens dict so both deques stay
    # symmetric under the wiring.
    assert set(client._input_tokens_baseline.keys()) == {"session-X", "session-Y"}, (
        "AC 6: input-tokens baseline dict MUST mirror cost baseline. "
        f"Got keys: {set(client._input_tokens_baseline.keys())!r}."
    )
    assert len(list(client._input_tokens_baseline["session-X"])) == 2
    assert len(list(client._input_tokens_baseline["session-Y"])) == 1


# ---------------------------------------------------------------------------
# 8. AC 4 — reset_baselines(session_id) is per-session and noop-safe
# ---------------------------------------------------------------------------


def test_reset_baselines_clears_only_target_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: reset_baselines("A") MUST clear only session-A's deques.
    Session B's deques MUST be untouched.

    61-followup-C will eventually wire SessionRoom.close_store() to
    call reset_baselines(session_id). If the reset were instance-wide
    (today's signature), C's call site would clobber every concurrent
    session's baseline every time one session ended — a worse bug than
    the one A is trying to fix.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])
    client = _build_client(sdk)

    # Seed both sessions.
    for cost, sid in ((0.04, "session-A"), (0.04, "session-A"), (0.10, "session-B")):
        client._maybe_emit_cost_runaway(
            input_tokens=12_000,
            output_tokens=500,
            cost_usd=cost,
            model="claude-sonnet-4-6",
            session_id=sid,
        )
    assert "session-A" in client._cost_baseline
    assert "session-B" in client._cost_baseline

    client.reset_baselines("session-A")

    # Session A: gone from both dicts (the clean shape — context AC 4)
    # OR present-but-empty deque. Either is acceptable as a "clear"
    # implementation; we assert the OBSERVABLE behavior: no remembered
    # observations.
    a_costs = list(client._cost_baseline.get("session-A", ()))
    a_inputs = list(client._input_tokens_baseline.get("session-A", ()))
    assert a_costs == [], (
        "AC 4: reset_baselines('session-A') MUST clear session A's cost "
        f"deque. Got remaining observations: {a_costs!r}."
    )
    assert a_inputs == [], (
        "AC 4: reset_baselines('session-A') MUST clear session A's input "
        f"deque. Got remaining observations: {a_inputs!r}."
    )

    # Session B: completely untouched (this is the cross-session
    # protection that makes the C wiring safe).
    b_costs = list(client._cost_baseline["session-B"])
    b_inputs = list(client._input_tokens_baseline["session-B"])
    assert b_costs == [pytest.approx(0.10)], (
        "AC 4: resetting session A MUST NOT touch session B. "
        f"Expected B costs == [0.10]; got {b_costs!r}."
    )
    assert b_inputs == [12_000], (
        "AC 4: resetting session A MUST NOT touch session B's input "
        f"deque. Expected [12_000]; got {b_inputs!r}."
    )


def test_reset_baselines_noop_on_unknown_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4 edge case: resetting a session that was never observed
    MUST NOT raise (no KeyError).

    This matters for 61-followup-C's wiring: if a session ends before
    making its first SDK call (e.g., immediate disconnect), close_store
    will still fire reset_baselines on it. A KeyError there would crash
    the teardown path and leak the session.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])
    client = _build_client(sdk)

    # Should not raise. Asserting absence of exception is the assertion.
    client.reset_baselines("never-observed-session-id")

    # Dicts stay clean — no magic-key bucket created by the reset.
    assert "never-observed-session-id" not in client._cost_baseline, (
        "Resetting an unknown session MUST NOT create an entry as a "
        "side effect. "
        f"Got keys: {list(client._cost_baseline)!r}."
    )
    assert "never-observed-session-id" not in client._input_tokens_baseline, (
        "Resetting an unknown session MUST NOT create an input-baseline "
        f"entry. Got keys: {list(client._input_tokens_baseline)!r}."
    )
