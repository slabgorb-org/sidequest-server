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
    max_turns_one_stream,
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

# AC1' (Reviewer round 2) — the discriminating ``reason`` on the *query-raised*
# branch's auth event, distinct from the ``is_error`` branch's ``"is_error"``.
# A raised query() is how an absent/expired login surfaces (OQ-5); it maps to the
# typed auth error AND fires this event so the GM panel can see it.
_QUERY_RAISED_REASON = "query_raised"

# AC1' (Reviewer round 2 — the [HIGH] mislabel fix). A raised query() is *probably*
# an absent login but COULD be a transport fault — the headline must not assert
# auth as the SOLE diagnosis. The operator-facing message must acknowledge a
# transport cause too, so the lie detector never asserts a cause it cannot prove.
_NONCOMMITTAL_CAUSE_SUBSTR = "transport"

# AC1' (Reviewer round 2 — the [HIGH] mislabel fix). A non-auth error that arises
# from OUR OWN loop-body processing (a parse bug, an unexpected message shape)
# must NEVER be coerced into the auth signal. ``_is_agent_result_message`` is the
# first per-message call inside the iteration; making it raise reproduces "a
# future edit added a branch that did message.content[0] and got IndexError" —
# the exact devil's-advocate failure mode — as an unambiguously INTERNAL fault
# (not the transport failing).
_INTERNAL_FAULT_SENTINEL = "internal-parse-bug-not-an-auth-failure"

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
    must not regress the fail-loud invariant 119-3 already lands.

    Reviewer round 2 [MEDIUM]: tightened from the base ``AnthropicSdkClientError``
    to the exact ``AgentSdkAuthUnavailable`` — the base catch passed even if the
    WRONG subclass raised (``AnthropicSdkConfigError`` / ``AnthropicSdkLoopExceeded``),
    so it could not tell the auth-failure raise from any other client error."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    fake = FakeQuery(error_result_stream(subtype="error_auth"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AgentSdkAuthUnavailable):
        await _drive(_new_client())


async def test_internal_processing_error_is_not_mislabeled_as_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[HIGH] The mislabel pin. An error that arises from our OWN loop-body
    processing — NOT the transport failing — must never be coerced into the auth
    signal. Today a broad ``except Exception`` catches any in-loop error and
    re-raises it as ``AgentSdkAuthUnavailable("subscription login absent or
    expired")`` with a ``narrator.auth_unavailable`` event: a future parse bug
    (``message.content[0]`` on an empty list → ``IndexError``) would light the GM
    panel red for 'login expired' while the real fault is in our parser. The
    panel built to END 'winging it' must not assert a diagnosis it cannot
    support (CLAUDE.md OTEL doctrine; lang-review #1 'catch specifically when the
    type is known').

    We reproduce an INTERNAL fault by making ``_is_agent_result_message`` (the
    first per-message call inside the iteration) raise — a fault that is
    unambiguously ours, not the ``query()`` transport's. The honest contract:
    such an error propagates with its cause intact (raw or chained), is NOT a
    subclass of ``AgentSdkAuthUnavailable``, and emits NO auth event."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    def _raise_internal(_msg: Any) -> bool:
        raise ValueError(_INTERNAL_FAULT_SENTINEL)

    # The transport itself is healthy — it yields a normal stream. The fault is
    # entirely in OUR processing of the first yielded message.
    fake = FakeQuery(converged_text_stream(text="The door creaks open."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)
    monkeypatch.setattr(anthropic_sdk_client, "_is_agent_result_message", _raise_internal)

    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - asserted precisely below
        await _drive(_new_client())

    raised = excinfo.value
    # 1) Must NOT be coerced into the auth lie.
    assert not isinstance(raised, AgentSdkAuthUnavailable), (
        "an INTERNAL loop-body error (a parse bug, not an auth failure) must not "
        f"be re-raised as AgentSdkAuthUnavailable; got {raised!r}"
    )
    # 2) The original cause must survive — raw, or chained via ``from exc``.
    cause = getattr(raised, "__cause__", None)
    assert isinstance(raised, ValueError) or isinstance(cause, ValueError), (
        "the original ValueError must propagate (raw or cause-chained), never be "
        f"masked behind an unrelated error type; got {raised!r} (cause={cause!r})"
    )
    assert _INTERNAL_FAULT_SENTINEL in str(raised) or _INTERNAL_FAULT_SENTINEL in str(cause), (
        "the real error detail must survive to the operator, not be discarded"
    )
    # 3) The GM-panel lie detector must NOT show an auth event for a non-auth error.
    assert not any(name == _AUTH_FAILURE_EVENT for name, _ in events), (
        f"{_AUTH_FAILURE_EVENT!r} must NOT fire for an internal/non-auth error — "
        "that false signal is the exact failure mode the panel exists to prevent; "
        f"events seen: {[n for n, _ in events]!r}"
    )


async def test_raised_query_emits_auth_event_before_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[MEDIUM] The query-raised branch's event was untested — only the
    ``is_error`` branch (``test_auth_failure_emits_watcher_event_before_raise``)
    was event-verified, so a future edit could drop the ``query_raised`` emit and
    stay green. A *raised* ``query()`` (an absent/expired login, OQ-5) must emit
    ``narrator.auth_unavailable`` (reason ``query_raised``) BEFORE the loud raise.

    Round-2 honesty rider: a raised query is *probably* auth but could be
    transport, so the raised ``AgentSdkAuthUnavailable`` headline must not assert
    auth as the SOLE cause — it must acknowledge a transport fault too
    (Reviewer [HIGH]: stop the top-level label asserting a diagnosis it can't
    prove)."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = RaisingFakeQuery(RuntimeError("subscription OAuth login expired"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AgentSdkAuthUnavailable) as excinfo:
        await _drive(_new_client())

    # The query-raised branch must announce itself to the panel before raising.
    auth_events = [f for n, f in events if n == _AUTH_FAILURE_EVENT]
    assert auth_events, (
        f"the query-raised auth-failure branch must emit {_AUTH_FAILURE_EVENT!r} "
        f"before raising (GM-panel visibility); events: {[n for n, _ in events]!r}"
    )
    assert auth_events[-1].get("reason") == _QUERY_RAISED_REASON, (
        "the event must identify the query-raised branch via "
        f"reason={_QUERY_RAISED_REASON!r}; got {auth_events[-1].get('reason')!r}"
    )
    # The headline must not assert auth as the only diagnosis.
    message = str(excinfo.value).lower()
    assert _NONCOMMITTAL_CAUSE_SUBSTR in message, (
        "a raised query() could be a transport fault, not only an expired login — "
        "the failure message must acknowledge a transport cause so the panel "
        f"stops asserting auth as the sole diagnosis; got {str(excinfo.value)!r}"
    )


async def test_max_turns_does_not_emit_spurious_auth_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[LOW] NEGATIVE GUARD (passes today, must keep passing): the
    tool-loop-exceeded path (``is_error`` + ``error_max_turns``) raises
    ``AnthropicSdkLoopExceeded`` — a convergence failure, NOT an auth failure. It
    must exit BEFORE the ``is_error`` auth branch and emit NO
    ``narrator.auth_unavailable``; the panel must never cry 'login expired' when
    the model simply failed to converge."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkLoopExceeded

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    fake = FakeQuery(max_turns_one_stream())
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AnthropicSdkLoopExceeded):
        await _drive(_new_client())

    assert not any(name == _AUTH_FAILURE_EVENT for name, _ in events), (
        "error_max_turns is a convergence failure, not auth — it must not emit "
        f"{_AUTH_FAILURE_EVENT!r}; events seen: {[n for n, _ in events]!r}"
    )


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


async def test_usage_surfaces_sdk_reported_cost_none_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[LOW] No Silent Fallbacks: when the transport reports NO spend
    (``ResultMessage.total_cost_usd is None``), ``narrator.sdk.usage`` must
    surface ``sdk_reported_cost_usd=None`` — the key PRESENT and explicitly
    ``None`` — never silently defaulted to ``0.0``. A fabricated 'clean $0' would
    hide that the transport gave us no signal at all (distinct from a real
    measured zero), exactly the OQ-6 cache-split trap the story warns against."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []
    _patch_all_watchers(monkeypatch, events)

    stream = [
        FakeAssistantMessage(content=[FakeTextBlock(text="A hush settles.")]),
        FakeResultMessage(
            result="A hush settles.",
            is_error=False,
            subtype="success",
            num_turns=2,
            usage=fake_usage(),
            total_cost_usd=None,
        ),
    ]
    fake = FakeQuery(stream)
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client())

    usage = _fields_for(events, "narrator.sdk.usage")
    assert _SDK_REPORTED_COST_FIELD in usage, (
        f"{_SDK_REPORTED_COST_FIELD!r} must be PRESENT on narrator.sdk.usage even "
        "when None — dropping the key hides that the transport reported no spend"
    )
    assert usage[_SDK_REPORTED_COST_FIELD] is None, (
        "a None transport cost must surface as None, never a fabricated 0.0; "
        f"got {usage[_SDK_REPORTED_COST_FIELD]!r}"
    )


async def test_usage_surfaces_never_fabricate_subscription_budget(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
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

    # Reviewer round 2 [LOW]: the guard checked watcher events but not span
    # attributes — a fabricated budget could still ride on the llm.request span.
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert spans, "the llm.request span must fire on the inference path"
    span_attrs = dict(spans[-1].attributes or {})
    for forbidden in _FORBIDDEN_FABRICATED_BUDGET_KEYS:
        for key in (forbidden, f"llm.{forbidden}"):
            assert key not in span_attrs, (
                f"the llm.request span must not fabricate a measured subscription "
                f"budget — found {key!r}={span_attrs[key]!r} (No Silent Fallbacks)"
            )
