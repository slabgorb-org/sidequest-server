"""Story 119-4 RED — auth durability + cost/credit observability (the real subset).

119-4 was scoped for the 119-2 migration (a static ``ANTHROPIC_AUTH_TOKEN`` env
token that a long-running server would see expire). **119-3 shipped instead** —
the ``claude-agent-sdk`` transport, which resolves the host's auto-refreshing
OAuth login via the bundled CLI (there is no static env token in this path), and
the epic was reframed 2026-06-15 (the "Agent SDK credit" was CANCELLED — the
subscription is simply free). So the story title's "swap the static env token for
an auto-refreshing profile" and "200 monthly credit + 50.52 prepaid overflow
buffer / before it silently bills PAYG" reference a **void premise** and are
formally descoped (see the session-file Design Deviations). 119-3 already lands
the no-silent-PAYG-fallback invariant (``assert_subscription_auth`` +
``is_error`` → raise).

What is genuinely unbuilt and buildable post-119-3, and what these tests pin
(scope decision confirmed by the user 2026-06-16 — "build the real subset"):

* **AC2 — affirmative auth-path / billing-pool label.** Every inference over the
  subscription transport tags an ``auth_path == "subscription"`` on the
  ``llm.request`` span and the ``narrator.sdk.usage`` watcher event, so the
  GM/cost panel can VERIFY the narrator is drawing the free pool (the lie
  detector — CLAUDE.md OTEL principle, a Keith/dev surface, NOT player-facing).
* **AC1' — fail loud on expiry/absence, *legibly*.** 119-3 raises on the
  ``is_error`` ResultMessage path, but (a) a *raised* ``query()`` exception (how
  an absent/expired login surfaces, OQ-5) currently propagates RAW — not mapped
  to the typed ``AgentSdkAuthUnavailable`` — and (b) NEITHER auth-failure path
  emits a watcher event, so the GM panel cannot see auth failures. Both gaps are
  closed here (No Silent Fallbacks: loud AND legible).
* **AC4 — cost is NOTIONAL under the free transport.** ``compute_cost_usd`` still
  prices tokens at the PAYG snapshot, so every cost figure
  (``narrator.sdk.usage``, ``session.cost_running_total``,
  ``session.cost_ceiling_exceeded``) is a *notional* shape signal, not real
  billed dollars. The surfaces must label it ``cost_basis == "notional"`` so the
  panel never reads notional dollars as a real bill — and the $10 ceiling must
  still FIRE (not silently never-fire) as a notional-shape ceiling.
* **AC3' — honest usage monitor.** The ``anthropic-ratelimit-*`` headers the old
  raw-SDK span documented are UNREACHABLE from the agent-SDK subprocess (it
  returns a ``ResultMessage``, not HTTP response headers), so the original
  "monitor the credit/overflow from the ratelimit headers" is reframed: surface
  the SDK-reported ``total_cost_usd`` (which the ``ResultMessage`` *does* carry)
  as a distinct field so a real-spend PAYG leak would be visible next to the
  notional figure — and NEVER fabricate a measured subscription-budget number we
  cannot read (the OQ-6 cache-split precedent).

All tests drive the hermetic fake ``query`` seam (OQ-9) — no live subscription,
no network. Imports of new symbols happen inside each test so RED fails at the
assertion (or missing symbol), never at collection.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.tooling_protocol import CacheableBlock, Message, ToolDefinition
from tests.agents.fakes.fake_agent_sdk import (
    FakeAssistantMessage,
    FakeQuery,
    FakeResultMessage,
    FakeTextBlock,
    RaisingFakeQuery,
    converged_text_stream,
    error_result_stream,
    fake_usage,
)

_SONNET = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Contract constants — the exact field / event names Dev implements in GREEN.
# Centralized so the span attribute, the watcher field, and the assertion can
# never drift apart.
# ---------------------------------------------------------------------------

# AC2 — affirmative auth-path label.
_AUTH_PATH_FIELD = "auth_path"  # on the narrator.sdk.usage watcher event
_AUTH_PATH_SPAN_ATTR = "llm.auth_path"  # on the llm.request span
_AUTH_PATH_SUBSCRIPTION = "subscription"

# AC1' — the auth-failure watcher event fired before the loud raise.
_AUTH_FAILURE_EVENT = "narrator.auth_unavailable"

# AC4 — the notional cost-basis marker on every cost-bearing surface.
_COST_BASIS_FIELD = "cost_basis"
_COST_BASIS_NOTIONAL = "notional"

# AC3' — the SDK-reported (transport's own) spend, surfaced distinct from the
# notional figure so a non-zero value flags a PAYG leak.
_SDK_REPORTED_COST_FIELD = "sdk_reported_cost_usd"
# A measured subscription budget/credit is UNKNOWABLE from this transport —
# these keys must NEVER appear claiming a measured value (No Silent Fallbacks).
_FORBIDDEN_FABRICATED_BUDGET_KEYS = (
    "subscription_budget_remaining",
    "credit_remaining",
    "budget_remaining_usd",
    "overflow_buffer_usd",
)


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both PAYG creds unset → the SDK resolves the subscription login."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _new_client() -> Any:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    return AnthropicSdkClient()


async def _drive(client: Any, *, session_id: str | None = None) -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="look around")],
        [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})],
        None,
        model=_SONNET,
        session_id=session_id,
    )


def _record_into(events: list[tuple[str, dict[str, Any]]]):
    def _record(event_type: str, fields: dict[str, Any], **_kw: Any) -> None:
        events.append((event_type, dict(fields)))

    return _record


def _patch_all_watchers(
    monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, dict[str, Any]]]
) -> None:
    """Capture watcher events from BOTH emit sites.

    ``narrator.sdk.usage`` / ``session.cost_running_total`` fire from
    ``anthropic_sdk_client``; ``session.cost_ceiling_exceeded`` /
    ``cost_runaway_suspected`` fire from ``cost_safety`` (the client delegates
    cumulative/ceiling to the shared ledger). Patch both module-level refs.
    """
    from sidequest.agents import anthropic_sdk_client, cost_safety

    recorder = _record_into(events)
    monkeypatch.setattr(anthropic_sdk_client, "_watcher_publish_event", recorder)
    monkeypatch.setattr(cost_safety, "_watcher_publish_event", recorder)


def _fields_for(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    matches = [f for n, f in events if n == name]
    assert matches, f"expected a {name!r} watcher event; saw {[n for n, _ in events]!r}"
    return matches[-1]


# ===========================================================================
# AC2 — affirmative auth-path / billing-pool label (the lie detector)
# ===========================================================================


async def test_usage_event_tags_subscription_auth_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``narrator.sdk.usage`` event must tag ``auth_path='subscription'`` so
    the cost panel can affirmatively verify the narrator drew the free pool."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(converged_text_stream(text="The door creaks open."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client())

    usage = _fields_for(events, "narrator.sdk.usage")
    assert usage.get(_AUTH_PATH_FIELD) == _AUTH_PATH_SUBSCRIPTION, (
        "narrator.sdk.usage must tag auth_path='subscription' (AC2 lie detector); "
        f"got {usage.get(_AUTH_PATH_FIELD)!r}"
    )


async def test_llm_request_span_tags_subscription_auth_path(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """WIRING: drive the real production ``complete_with_tools`` and assert the
    ``llm.request`` span emitted on that path carries
    ``llm.auth_path='subscription'`` — proves the label is wired into the live
    inference path, not just a helper."""
    from sidequest.agents import anthropic_sdk_client

    fake = FakeQuery(converged_text_stream(text="A lantern sways."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client())

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert spans, "the llm.request span must fire on the ported inference path"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get(_AUTH_PATH_SPAN_ATTR) == _AUTH_PATH_SUBSCRIPTION, (
        "llm.request span must carry llm.auth_path='subscription'; "
        f"got {attrs.get(_AUTH_PATH_SPAN_ATTR)!r}"
    )


# ===========================================================================
# AC1' — fail loud on expiry / absence, legibly (typed error + OTEL event)
# ===========================================================================


async def test_raised_query_maps_to_typed_auth_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *raised* ``query()`` exception (how an absent/expired subscription login
    surfaces, OQ-5) must be mapped to the typed ``AgentSdkAuthUnavailable`` — not
    propagate as a raw transport exception a caller cannot classify as auth."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    fake = RaisingFakeQuery(RuntimeError("subscription OAuth login expired"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AgentSdkAuthUnavailable):
        await _drive(_new_client())


async def test_auth_failure_emits_watcher_event_before_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auth failure must emit the ``narrator.auth_unavailable`` watcher event
    BEFORE the loud raise — else the GM panel cannot see that the narrator fell
    over because the subscription login expired (today: raises silently, no
    event)."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(error_result_stream(subtype="error_auth"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AgentSdkAuthUnavailable):
        await _drive(_new_client())

    assert any(name == _AUTH_FAILURE_EVENT for name, _ in events), (
        f"an auth failure must emit the {_AUTH_FAILURE_EVENT!r} watcher event "
        f"before raising (GM-panel visibility); events seen: {[n for n, _ in events]!r}"
    )


async def test_is_error_still_raises_no_silent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEGATIVE GUARD (passes today, must keep passing): the ``is_error`` path
    raises, never returns a degraded-success ToolingResult. The AC1' event work
    must not regress the fail-loud invariant 119-3 already lands."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClientError

    fake = FakeQuery(error_result_stream(subtype="error_auth"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AnthropicSdkClientError):
        await _drive(_new_client())


# ===========================================================================
# AC4 — cost is NOTIONAL under the free transport (label it; ceiling still fires)
# ===========================================================================


async def test_usage_event_marks_cost_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``narrator.sdk.usage`` carries a notional cost figure (token×PAYG-rate),
    not real billed dollars — it must declare ``cost_basis='notional'`` so the
    panel never reads it as a real bill."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(converged_text_stream(text="Coins clink."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client())

    usage = _fields_for(events, "narrator.sdk.usage")
    assert usage.get(_COST_BASIS_FIELD) == _COST_BASIS_NOTIONAL, (
        "narrator.sdk.usage cost is notional under subscription auth — it must "
        f"carry cost_basis='notional'; got {usage.get(_COST_BASIS_FIELD)!r}"
    )


async def test_cost_running_total_marks_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-turn ``session.cost_running_total`` pulse — the panel's 'X / $10'
    denominator — must also declare ``cost_basis='notional'``; the $10 is a
    notional-shape ceiling now, not a real-dollar budget."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(converged_text_stream(text="The hall is quiet."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client(), session_id="sess-running-total")

    pulse = _fields_for(events, "session.cost_running_total")
    assert pulse.get(_COST_BASIS_FIELD) == _COST_BASIS_NOTIONAL, (
        "session.cost_running_total must carry cost_basis='notional'; "
        f"got {pulse.get(_COST_BASIS_FIELD)!r}"
    )


async def test_ceiling_still_fires_and_is_marked_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notional-shape ceiling must NEITHER silently never-fire NOR fire on
    real dollars: with a tiny ceiling, one turn's notional cost crosses it and
    raises ``AnthropicSdkCostCeilingExceeded`` (it still fires), and the
    ``session.cost_ceiling_exceeded`` event declares ``cost_basis='notional'``."""
    # Ceiling parsed at construction — set BEFORE building the client.
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.00001")

    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkCostCeilingExceeded

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(converged_text_stream(text="The vault door groans."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await _drive(_new_client(), session_id="sess-ceiling")

    ceiling_evt = _fields_for(events, "session.cost_ceiling_exceeded")
    assert ceiling_evt.get(_COST_BASIS_FIELD) == _COST_BASIS_NOTIONAL, (
        "session.cost_ceiling_exceeded is a notional-shape kill under "
        f"subscription auth — it must carry cost_basis='notional'; "
        f"got {ceiling_evt.get(_COST_BASIS_FIELD)!r}"
    )


async def test_ceiling_does_not_fire_spuriously_on_normal_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEGATIVE GUARD (passes today, must keep passing): a normal turn under the
    default $10 ceiling does NOT raise — the notional reframe must not make the
    ceiling fire spuriously on healthy traffic."""
    monkeypatch.delenv("SIDEQUEST_SESSION_COST_CEILING_USD", raising=False)

    from sidequest.agents import anthropic_sdk_client

    fake = FakeQuery(converged_text_stream(text="A small, ordinary turn."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    result = await _drive(_new_client(), session_id="sess-normal")
    assert result.text == "A small, ordinary turn.", (
        "a healthy turn must converge without tripping the notional ceiling"
    )


# ===========================================================================
# AC3' — honest usage monitor (SDK-reported spend; no fabricated budget)
# ===========================================================================


def _stream_with_sdk_cost(text: str, *, total_cost_usd: float) -> list[Any]:
    """A converged stream whose terminal ResultMessage reports a specific
    transport-side ``total_cost_usd`` (the agent SDK's own spend view)."""
    return [
        FakeAssistantMessage(content=[FakeTextBlock(text=text)]),
        FakeResultMessage(
            result=text,
            is_error=False,
            subtype="success",
            num_turns=2,
            usage=fake_usage(),
            total_cost_usd=total_cost_usd,
        ),
    ]


async def test_usage_event_surfaces_sdk_reported_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``ResultMessage.total_cost_usd`` the transport reports must be
    surfaced as ``sdk_reported_cost_usd`` on ``narrator.sdk.usage`` — distinct
    from our notional ``cost_usd`` — so a non-zero value (a real PAYG leak) is
    visible next to the notional figure. A sentinel non-zero value proves the
    field is actually read, not defaulted."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    sentinel = 0.4242
    fake = FakeQuery(_stream_with_sdk_cost("A torch hisses.", total_cost_usd=sentinel))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client())

    usage = _fields_for(events, "narrator.sdk.usage")
    assert usage.get(_SDK_REPORTED_COST_FIELD) == pytest.approx(sentinel), (
        "narrator.sdk.usage must surface the transport's reported total_cost_usd "
        f"as {_SDK_REPORTED_COST_FIELD!r}; got {usage.get(_SDK_REPORTED_COST_FIELD)!r}"
    )


async def test_usage_surfaces_never_fabricate_subscription_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEGATIVE GUARD (No Silent Fallbacks): the real subscription budget /
    rate-limit pool is NOT machine-readable from the agent-SDK subprocess. No
    cost surface may report a *measured* budget/credit-remaining number — the
    void-premise '200 credit / 50.52 overflow buffer' must never be resurrected
    as a fabricated value (OQ-6 cache-split precedent)."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(converged_text_stream(text="Quiet usage."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client(), session_id="sess-no-fabrication")

    for name, fields in events:
        for forbidden in _FORBIDDEN_FABRICATED_BUDGET_KEYS:
            assert forbidden not in fields, (
                f"{name!r} must not fabricate a measured subscription budget — "
                f"found {forbidden!r}={fields[forbidden]!r}; the real pool is "
                "unreadable from this transport (No Silent Fallbacks)"
            )
