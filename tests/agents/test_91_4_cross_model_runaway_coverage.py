"""Story 91-4 — Cross-model runaway coverage (RED).

Epic 91 ("Dark Spend") doctrine: NO UNINSTRUMENTED TOKEN — and no
*unguarded* token either. ADR-134's two billing-safety layers (the
multi-trigger cost-runaway detector and the per-session cumulative
hard-kill ceiling) today live exclusively inside
``AnthropicSdkClient.complete_with_tools`` — the narrator/dungeon-curate
path. The two Haiku adapters that story 91-1 brought into the telemetry
choke point (``_AsideLlm``, ``_IntentRouterLlm``) are still *outside*
both safety layers: a runaway Haiku ramp (the exact 8×/turn, $3.3-3.5/day
shape the [COST-1] forensics found) would bill without ever tripping an
alarm, and Haiku spend does not count against the $10/session
catastrophe line at all.

These tests pin the extension contract:

**AC-1 — Session identity is an explicit decision at every adapter
construction site.** ``build_aside_llm`` / ``build_intent_router_llm`` /
``build_intent_router_for_session`` take a REQUIRED keyword-only
``session_id: str | None``. Omitting it is a ``TypeError`` — a future
call site cannot silently construct an uncovered adapter (the dark-spend
pattern reborn). ``session_id=None`` is the explicit ADR-134 opt-out for
genuinely sessionless callers, never a default.

**AC-2 — The ADR-134 detector covers the Haiku call sites.** An aside or
intent-router call whose usage matches a runaway fingerprint fires the
same ``cost_runaway_suspected`` watcher event the narrator path fires,
carrying ``session_id``, ``model``, and a ``caller`` discriminator so
the GM panel can attribute cross-model alarms.

**AC-3 — Baseline integrity across models.** Cheap Haiku traffic in a
session must NOT train that session's narrator rolling baseline (a
shared per-session window would drag the mean to fractions of a cent and
false-fire ``cost_multiple`` on every healthy Sonnet turn). Conversely a
Haiku call is judged against a Haiku-appropriate rolling baseline, so a
cache-death cost spike on the router IS caught post-warmup.

**AC-4 — One per-session cumulative across all call sites and models.**
Aside + router + narrator spend accumulate into a single per-session
total; the narrator's ``session.cost_running_total`` pulse reflects the
combined figure (the GM-panel "X / $10" counter must not undercount by
the Haiku share).

**AC-5 — The hard-kill ceiling is cross-call-site terminal.** Whichever
call site crosses the ceiling raises ``AnthropicSdkCostCeilingExceeded``
and emits ``session.cost_ceiling_exceeded`` exactly once; every
subsequent call for that session — narrator, aside, OR router — refuses
pre-flight without touching the SDK. Other sessions are unaffected
(per-session keying, ADR-134/ADR-122).

**AC-6 — ``session_id=None`` stays a hard bypass** (ADR-134 invariant):
no detector, no cumulative, no ceiling — but the 91-1 usage telemetry
still fires (bypassing safety must never mean bypassing accounting).

**AC-7 — Env ceiling stays fail-loud on the adapter paths.** The
``SIDEQUEST_SESSION_COST_CEILING_USD`` No-Silent-Fallbacks validation
(NaN/inf/non-positive → ``AnthropicSdkConfigError``) guards the adapter
constructions too — a NaN ceiling silently disables the entire kill.

Production wiring (the handler passes the real room slug) is pinned in
``tests/handlers/test_91_4_aside_session_wiring.py`` against the real
``PlayerActionHandler`` harness.

All session ids here are unique per test ("91-4-…") — the cross-instance
sharing these tests force implies process-level state, and unique ids
keep tests order-independent under xdist without a reset API.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import (
    AnthropicSdkClient,
    AnthropicSdkConfigError,
    AnthropicSdkCostCeilingExceeded,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests._helpers.doubles import FakeSocket

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Shared fakes / helpers (mirrors test_91_1_sdk_choke_point_instrumentation)
# ---------------------------------------------------------------------------


def _fake_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        cache_creation=None,
    )


def _aside_resp(*, input_tokens: int, output_tokens: int) -> SimpleNamespace:
    block = SimpleNamespace(
        type="text",
        text='{"answer": "ok", "outcome": "answered", "grounded_on": ["inventory"]}',
    )
    return SimpleNamespace(
        content=[block],
        usage=_fake_usage(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="end_turn",
        model=_HAIKU,
    )


def _router_resp(*, input_tokens: int, output_tokens: int) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="emit_dispatch_package", input={"package": 1})
    return SimpleNamespace(
        content=[block],
        usage=_fake_usage(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="tool_use",
        model=_HAIKU,
    )


def _narrator_resp(*, input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="The door creaks open.")],
        usage=_fake_usage(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="end_turn",
        model=_SONNET,
    )


def _cost(resp: SimpleNamespace) -> float:
    """Expected cost through the REAL pricing function (no duplicated tables)."""
    u = resp.usage
    return compute_cost_usd(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cached_input_read_tokens=u.cache_read_input_tokens,
        cached_input_write_tokens=u.cache_creation_input_tokens,
        model=resp.model,
    )


class _RecordingMsgs:
    """``messages.create`` double that records every call and pops scripted
    responses — call count is the load-bearing assertion for "refused
    pre-flight without touching the SDK"."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("RecordingSdk: out of scripted responses")
        return self._responses.pop(0)


class _RecordingSdk:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _RecordingMsgs(responses)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test loop and clear subscribers
    (canonical pattern, tests/agents/test_61_4_cost_runaway_alarm.py)."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


async def _subscribe(bound_hub: WatcherHub) -> FakeSocket:
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    return sock


async def _drain() -> None:
    """Let the hub's scheduled publishes land on the loop."""
    await asyncio.sleep(0.05)


def _events(sock: FakeSocket, event_type: str) -> list[dict[str, Any]]:
    return [e for e in sock.events if e.get("event_type") == event_type]


def _build_aside(monkeypatch: pytest.MonkeyPatch, sdk: Any, *, session_id: str | None) -> Any:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: sdk)
    return llm_factory.build_aside_llm(session_id=session_id)


def _build_router(monkeypatch: pytest.MonkeyPatch, sdk: Any, *, session_id: str | None) -> Any:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: sdk)
    return llm_factory.build_intent_router_llm(session_id=session_id)


async def _drive_router(adapter: Any) -> dict[str, Any]:
    return await adapter.emit_tool(
        system="S",
        user="attack the goblin",
        tool_name="emit_dispatch_package",
        tool_description="d",
        tool_schema={"type": "object"},
    )


def _build_narrator(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> AnthropicSdkClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


async def _drive_narrator(client: AnthropicSdkClient, *, session_id: str | None) -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="go")],
        [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})],
        None,
        model=_SONNET,
        session_id=session_id,
        caller="narrator",
    )


# ===========================================================================
# AC-1 — session identity is an explicit decision at every construction site
# ===========================================================================


def test_build_aside_llm_requires_explicit_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``session_id`` must be a ``TypeError`` — a call site that
    forgets it gets an uncovered (detector-less, ceiling-less) adapter,
    which is exactly the silent-bypass failure mode this epic kills.
    Opting out is spelled ``session_id=None``, never implied."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: _RecordingSdk([]))
    with pytest.raises(TypeError):
        llm_factory.build_aside_llm()  # type: ignore[call-arg]


def test_build_intent_router_llm_requires_explicit_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: _RecordingSdk([]))
    with pytest.raises(TypeError):
        llm_factory.build_intent_router_llm()  # type: ignore[call-arg]


def test_build_intent_router_for_session_requires_explicit_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production router construction seam
    (``intent_router_pass.build_intent_router_for_session``) must demand the
    session id too — its sole production caller
    (``websocket_session_handler``) HAS the slug and must pass it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory
    from sidequest.server import intent_router_pass

    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: _RecordingSdk([]))
    with pytest.raises(TypeError):
        intent_router_pass.build_intent_router_for_session()  # type: ignore[call-arg]


def test_intent_router_for_session_binds_session_to_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session id handed to the production seam must reach the adapter
    (private-attr binding assert, same idiom as 91-1's ``adapter._sdk is
    sentinel``)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory
    from sidequest.server import intent_router_pass

    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: _RecordingSdk([]))
    router = intent_router_pass.build_intent_router_for_session(session_id="91-4-bind")
    assert getattr(router._llm, "_session_id", None) == "91-4-bind", (  # noqa: SLF001
        "build_intent_router_for_session(session_id=...) must bind the session "
        "id onto the adapter — otherwise the router's Haiku spend is invisible "
        "to the detector and the cumulative ceiling"
    )


# ===========================================================================
# AC-2 — the ADR-134 detector covers the Haiku call sites
# ===========================================================================


async def test_aside_io_fingerprint_fires_cost_runaway(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """A 30K-in/20-out aside (the 2026-05-23 fingerprint shape, Haiku
    edition — prompt bloat billing fat input for almost no output) must
    fire ``cost_runaway_suspected`` with trigger=io_fingerprint. Today the
    aside adapter runs NO detector at all."""
    monkeypatch.delenv("SIDEQUEST_SESSION_COST_CEILING_USD", raising=False)
    sock = await _subscribe(bound_hub)
    adapter = _build_aside(
        monkeypatch,
        _RecordingSdk([_aside_resp(input_tokens=30_000, output_tokens=20)]),
        session_id="91-4-aside-iofp",
    )

    await adapter.complete(system="S", user="U")
    await _drain()

    events = _events(sock, "cost_runaway_suspected")
    assert len(events) == 1, (
        "an aside call matching the io-fingerprint shape must fire exactly one "
        f"cost_runaway_suspected event; got {len(events)} "
        f"(all: {[e.get('event_type') for e in sock.events]})"
    )
    fields = events[0].get("fields", {})
    assert fields.get("trigger") == "io_fingerprint", fields
    assert fields.get("model") == _HAIKU, (
        "the event must carry the Haiku model id — cross-model attribution is "
        f"the point of this story; fields={fields}"
    )
    assert fields.get("session_id") == "91-4-aside-iofp", fields
    assert fields.get("caller") == "aside", (
        "cost_runaway_suspected must gain a 'caller' discriminator so the GM "
        f"panel can tell WHICH call site is running away; fields={fields}"
    )
    assert events[0].get("severity") == "warn", events[0]


async def test_router_input_absolute_fires_cost_runaway(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """A 45K-input router call (prompt-bloat canary above the 40K absolute
    floor, output too high for the io fingerprint) must fire with
    trigger=input_absolute and caller=intent_router."""
    monkeypatch.delenv("SIDEQUEST_SESSION_COST_CEILING_USD", raising=False)
    sock = await _subscribe(bound_hub)
    adapter = _build_router(
        monkeypatch,
        _RecordingSdk([_router_resp(input_tokens=45_000, output_tokens=120)]),
        session_id="91-4-router-absin",
    )

    out = await _drive_router(adapter)
    await _drain()

    assert out == {"package": 1}
    events = _events(sock, "cost_runaway_suspected")
    assert len(events) == 1, f"expected one event, got {len(events)}"
    fields = events[0].get("fields", {})
    assert fields.get("trigger") == "input_absolute", fields
    assert fields.get("caller") == "intent_router", fields
    assert fields.get("session_id") == "91-4-router-absin", fields
    assert fields.get("input_tokens") == 45_000, fields


async def test_router_cost_spike_post_warmup_fires_cost_multiple(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """The cache-death shape this epic exists for: K=10 healthy router calls
    (~4K in / 100 out) seed a Haiku-scale rolling baseline, then the cache
    dies and input 7.5×es. The spike must fire ``cost_multiple`` against
    the HAIKU baseline — judged against the narrator's warmup floors
    ($0.03 / 12K) the spike is invisible (cost ≈ $0.03 < $0.15 trip,
    input 30K stays under 2×12K with output ≥ 50)."""
    monkeypatch.delenv("SIDEQUEST_SESSION_COST_CEILING_USD", raising=False)
    sock = await _subscribe(bound_hub)
    session = "91-4-router-spike"
    seeds = [_router_resp(input_tokens=4_000, output_tokens=100) for _ in range(10)]
    spike = _router_resp(input_tokens=30_000, output_tokens=100)
    adapter = _build_router(monkeypatch, _RecordingSdk([*seeds, spike]), session_id=session)

    for _ in range(10):
        await _drive_router(adapter)
    await _drain()
    assert not _events(sock, "cost_runaway_suspected"), (
        "the healthy seed calls must not fire (cost ≈ $0.0045 and input 4K are "
        "far below every trigger)"
    )

    await _drive_router(adapter)
    await _drain()

    events = _events(sock, "cost_runaway_suspected")
    assert len(events) == 1, (
        "the post-warmup cost spike must fire exactly once against the Haiku "
        f"rolling baseline; got {len(events)}"
    )
    fields = events[0].get("fields", {})
    assert fields.get("trigger") == "cost_multiple", fields
    assert fields.get("warmup") is False, (
        "after K=10 observations the comparator must be the rolling mean, not "
        f"the warmup floor; fields={fields}"
    )
    assert fields.get("caller") == "intent_router", fields


# ===========================================================================
# AC-3 — baseline integrity across models
# ===========================================================================


async def test_cheap_haiku_traffic_does_not_pollute_narrator_baseline(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """12 cheap asides (~$0.00125 each) then one HEALTHY narrator turn
    (~$0.039, 11K in / 400 out) in the same session. If the Haiku calls
    trained the session's shared baseline window, the narrator turn would
    false-fire cost_multiple (trip ≈ 5 × $0.00125 = $0.006). No
    ``cost_runaway_suspected`` event may fire anywhere in this test —
    a detector that cries wolf on every healthy Sonnet turn gets muted
    and protects nothing."""
    monkeypatch.delenv("SIDEQUEST_SESSION_COST_CEILING_USD", raising=False)
    sock = await _subscribe(bound_hub)
    session = "91-4-no-pollute"
    aside_sdk = _RecordingSdk(
        [_aside_resp(input_tokens=1_000, output_tokens=50) for _ in range(12)]
    )
    adapter = _build_aside(monkeypatch, aside_sdk, session_id=session)
    for _ in range(12):
        await adapter.complete(system="S", user="U")

    narrator = _build_narrator(
        monkeypatch, _RecordingSdk([_narrator_resp(input_tokens=11_000, output_tokens=400)])
    )
    await _drive_narrator(narrator, session_id=session)
    await _drain()

    events = _events(sock, "cost_runaway_suspected")
    assert not events, (
        "a healthy narrator turn after cheap Haiku traffic must NOT trip the "
        "detector — Haiku calls must not train the narrator's rolling baseline "
        f"for the session; got {[e.get('fields') for e in events]}"
    )


# ===========================================================================
# AC-4 — one per-session cumulative across all call sites and models
# ===========================================================================


async def test_running_total_reflects_cross_site_spend(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """Aside + router + narrator spend in one session must surface as ONE
    cumulative: the narrator's ``session.cost_running_total`` pulse equals
    the sum of all three calls' costs (computed through the real pricing
    function). Today the pulse reads a narrator-instance-private dict and
    structurally undercounts by the entire Haiku share."""
    monkeypatch.delenv("SIDEQUEST_SESSION_COST_CEILING_USD", raising=False)
    sock = await _subscribe(bound_hub)
    session = "91-4-cross-site-sum"

    aside_r = _aside_resp(input_tokens=2_000, output_tokens=100)
    router_r = _router_resp(input_tokens=4_000, output_tokens=100)
    narrator_r = _narrator_resp(input_tokens=11_000, output_tokens=400)

    aside = _build_aside(monkeypatch, _RecordingSdk([aside_r]), session_id=session)
    await aside.complete(system="S", user="U")
    router = _build_router(monkeypatch, _RecordingSdk([router_r]), session_id=session)
    await _drive_router(router)
    narrator = _build_narrator(monkeypatch, _RecordingSdk([narrator_r]))
    await _drive_narrator(narrator, session_id=session)
    await _drain()

    pulses = [
        e
        for e in _events(sock, "session.cost_running_total")
        if e.get("fields", {}).get("session_id") == session
    ]
    assert pulses, "the narrator turn must emit the session.cost_running_total pulse"
    expected = _cost(aside_r) + _cost(router_r) + _cost(narrator_r)
    got = pulses[-1].get("fields", {}).get("cumulative_cost_usd")
    assert got == pytest.approx(expected, abs=1e-9), (
        f"the GM-panel running total must include the Haiku share: expected "
        f"aside({_cost(aside_r):.6f}) + router({_cost(router_r):.6f}) + "
        f"narrator({_cost(narrator_r):.6f}) = {expected:.6f}, got {got!r}"
    )


# ===========================================================================
# AC-5 — the hard-kill ceiling is cross-call-site terminal
# ===========================================================================


async def test_aside_crossing_ceiling_raises_and_announces_once(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """An aside call that pushes the session cumulative over the (env-
    lowered) ceiling must raise the typed exception and emit the
    ``session.cost_ceiling_exceeded`` event; the NEXT aside for the same
    session refuses pre-flight — zero SDK calls — and stays silent on the
    watcher (announced-once dedup, ADR-134)."""
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.02")
    sock = await _subscribe(bound_hub)
    session = "91-4-aside-kill"

    # 30K in / 500 out ≈ $0.0325 — over the $0.02 ceiling, but shaped to
    # trip no runaway trigger (output ≥ 50, input < 40K, cost < $0.15).
    crossing = _aside_resp(input_tokens=30_000, output_tokens=500)
    adapter = _build_aside(monkeypatch, _RecordingSdk([crossing]), session_id=session)
    with pytest.raises(AnthropicSdkCostCeilingExceeded) as exc_info:
        await adapter.complete(system="S", user="U")
    await _drain()

    assert exc_info.value.session_id == session
    assert exc_info.value.ceiling_usd == pytest.approx(0.02)
    assert exc_info.value.cumulative_cost_usd == pytest.approx(_cost(crossing), abs=1e-9)

    events = _events(sock, "session.cost_ceiling_exceeded")
    assert len(events) == 1, f"exactly one ceiling event on first cross; got {len(events)}"
    assert events[0].get("fields", {}).get("session_id") == session
    assert events[0].get("fields", {}).get("model") == _HAIKU, (
        "the kill event must name the model that crossed — Haiku spend killing "
        f"a session is exactly what 91-4 makes visible; fields={events[0].get('fields')}"
    )

    refused_sdk = _RecordingSdk([_aside_resp(input_tokens=1_000, output_tokens=50)])
    second = _build_aside(monkeypatch, refused_sdk, session_id=session)
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await second.complete(system="S", user="U")
    await _drain()

    assert refused_sdk.messages.calls == [], (
        "a killed session's aside must refuse PRE-FLIGHT — the SDK must never "
        "be touched (the 'no further billing' half of the contract)"
    )
    assert len(_events(sock, "session.cost_ceiling_exceeded")) == 1, (
        "announced-once dedup: the refusal re-raises but must not re-emit"
    )


async def test_ceiling_crossed_by_narrator_blocks_adapters(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """The cumulative is ONE pot per session: when the NARRATOR crosses the
    ceiling, subsequent aside AND router calls for that session must refuse
    pre-flight without touching the SDK. Today the adapters know nothing of
    the narrator's kill — a dead session keeps billing Haiku forever."""
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.02")
    sock = await _subscribe(bound_hub)
    session = "91-4-narrator-kill"

    narrator = _build_narrator(
        monkeypatch,
        _RecordingSdk([_narrator_resp(input_tokens=11_000, output_tokens=400)]),  # ≈ $0.039
    )
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await _drive_narrator(narrator, session_id=session)
    await _drain()

    aside_sdk = _RecordingSdk([_aside_resp(input_tokens=1_000, output_tokens=50)])
    aside = _build_aside(monkeypatch, aside_sdk, session_id=session)
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await aside.complete(system="S", user="U")
    assert aside_sdk.messages.calls == [], (
        "after the narrator kill, the aside adapter must refuse pre-flight — "
        "zero SDK calls for the killed session"
    )

    router_sdk = _RecordingSdk([_router_resp(input_tokens=4_000, output_tokens=100)])
    router = _build_router(monkeypatch, router_sdk, session_id=session)
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await _drive_router(router)
    assert router_sdk.messages.calls == [], (
        "after the narrator kill, the router adapter must refuse pre-flight — "
        "zero SDK calls for the killed session"
    )

    assert len(_events(sock, "session.cost_ceiling_exceeded")) == 1, (
        "one kill, one event — the adapter refusals must stay silent"
    )


async def test_ceiling_kill_is_per_session(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """Session A's kill must not refuse session B (per-session keying —
    one long-lived process backs many sessions, ADR-122)."""
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.02")
    await _subscribe(bound_hub)

    killer = _build_aside(
        monkeypatch,
        _RecordingSdk([_aside_resp(input_tokens=30_000, output_tokens=500)]),
        session_id="91-4-iso-a",
    )
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await killer.complete(system="S", user="U")

    bystander_sdk = _RecordingSdk([_aside_resp(input_tokens=1_000, output_tokens=50)])
    bystander = _build_aside(monkeypatch, bystander_sdk, session_id="91-4-iso-b")
    out = await bystander.complete(system="S", user="U")

    assert "answered" in out, "session B must proceed normally after session A's kill"
    assert len(bystander_sdk.messages.calls) == 1


# ===========================================================================
# AC-6 — session_id=None stays a hard bypass (ADR-134 invariant)
# ===========================================================================


async def test_session_none_bypasses_safety_but_keeps_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``session_id=None`` (the explicit sessionless opt-out) must bypass
    detector AND cumulative AND ceiling — two calls each costing well over
    a microscopic ceiling sail through, no events fire — but the 91-1
    usage accounting still emits (bypassing safety must never mean
    bypassing the books)."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.0001")
    sock = await _subscribe(bound_hub)

    sdk = _RecordingSdk(
        [
            _aside_resp(input_tokens=2_000, output_tokens=100),
            _aside_resp(input_tokens=2_000, output_tokens=100),
        ]
    )
    adapter = _build_aside(monkeypatch, sdk, session_id=None)

    await adapter.complete(system="S", user="U")
    out = await adapter.complete(system="S", user="U")
    await _drain()

    assert "answered" in out, "the second sessionless call must not be refused"
    assert len(sdk.messages.calls) == 2
    assert not _events(sock, "session.cost_ceiling_exceeded"), (
        "session_id=None must bypass the ceiling entirely (ADR-134: 'off for "
        "None', not a synthesized bucket)"
    )
    assert not _events(sock, "cost_runaway_suspected")
    usage_lines = [
        r.getMessage()
        for r in caplog.records
        if ".sdk.usage" in r.getMessage() and "caller=aside" in r.getMessage()
    ]
    assert len(usage_lines) == 2, (
        "the 91-1 usage telemetry must still fire for sessionless calls — "
        f"got {len(usage_lines)} usage lines"
    )


# ===========================================================================
# AC-7 — env ceiling stays fail-loud on the adapter paths
# ===========================================================================


@pytest.mark.parametrize("bad", ["nan", "inf", "-1", "bogus"])
async def test_adapter_ceiling_env_invalid_fails_loud(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A NaN ceiling makes every ``cumulative >= ceiling`` comparison False —
    silently disabling the kill. The existing AnthropicSdkConfigError
    validation must guard the adapter path too: building a session-bound
    adapter (or making its first call) under an invalid env must raise."""
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", bad)
    with pytest.raises(AnthropicSdkConfigError):
        adapter = _build_aside(
            monkeypatch,
            _RecordingSdk([_aside_resp(input_tokens=1_000, output_tokens=50)]),
            session_id="91-4-env-loud",
        )
        await adapter.complete(system="S", user="U")
