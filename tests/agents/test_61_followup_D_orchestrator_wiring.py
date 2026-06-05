"""Story 61-followup-D — orchestrator wiring tests for mitigation C.

Two cross-cutting wiring concerns the unit tests in
``test_61_followup_D_session_cost_ceiling.py`` cannot prove on their own:

1. **``session_id`` plumbing.** ``TurnContext.session_id`` MUST reach
   ``ToolingLlmClient.complete_with_tools`` via the orchestrator call
   site at ``sidequest/agents/orchestrator.py:3663``. Without this
   plumbing, the cumulative-cost tracker has nothing to key on and the
   hard kill never engages in production.

2. **Exception propagation.** When ``AnthropicSdkCostCeilingExceeded``
   raises out of ``complete_with_tools``, the orchestrator MUST NOT
   swallow it (no silent fallback per CLAUDE.md). The exception
   propagates upward so the WS handler can broadcast the typed
   ``session.cost_ceiling_exceeded`` message and terminate the turn.

Pattern: fixture-driven, behavior-based — drive the real
``Orchestrator.run_narration_turn`` against a recording spy / raising
spy. Mirrors ``tests/agents/test_61_3_hard_cap_oversized_canary.py``
(the canonical refuse-the-SDK-call wiring test).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import AnthropicSdkCostCeilingExceeded
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolingResult,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass
class _SessionIdRecordingSpy:
    """Spy that captures the ``session_id`` kwarg passed to
    ``complete_with_tools``. The recorded value is the RED gate: until
    Dev wires ``session_id`` through orchestrator.py:3663 → protocol →
    here, the recorded list stays empty.
    """

    recorded_session_ids: list[Any] = field(default_factory=list)
    raise_on_call: BaseException | None = None

    async def complete_with_tools(
        self,
        system_blocks: list[CacheableBlock],
        messages: list[Message],
        tools: list[ToolDefinition],
        tool_dispatch: Callable[[ToolUseBlock], Awaitable[ToolResultBlock] | ToolResultBlock]
        | None = None,
        *,
        model: str,
        max_iterations: int = 8,
        max_tokens: int = 4096,
        on_text_delta: Callable[[str], None] | None = None,
        session_id: str | None = None,
        # Absorb forward-added client kwargs (Story 82-9: iteration_cap, caller)
        # so this orchestrator-wiring double tracks the real signature.
        **_kwargs: object,
    ) -> ToolingResult:
        self.recorded_session_ids.append(session_id)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        # Minimal valid result so the orchestrator continues normally.
        return ToolingResult(
            text='{"narration": "ok"}',
            stop_reason="end_turn",
            input_tokens=1_000,
            output_tokens=50,
            cached_input_read_tokens=0,
            cached_input_write_tokens=0,
            model=model,
            tool_calls=[],
            cumulative_cost_usd=compute_cost_usd(
                input_tokens=1_000,
                output_tokens=50,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model=model,
            ),
            cached_input_write_5m_tokens=0,
            cached_input_write_1h_tokens=0,
        )


# ---------------------------------------------------------------------------
# 1. Protocol signature — ToolingLlmClient.complete_with_tools accepts
#    session_id as a keyword-only parameter
# ---------------------------------------------------------------------------


def test_tooling_protocol_accepts_session_id_kwarg() -> None:
    """Contract test for ``ToolingLlmClient`` Protocol surface. The
    ``session_id`` parameter MUST be part of the Protocol so any
    backend (Anthropic SDK, Ollama, claude -p) is held to the same
    signature. Backends that don't track per-session cost still get
    the kwarg (and can ignore it); the contract is uniform.
    """
    from sidequest.agents.tooling_protocol import ToolingLlmClient

    sig = inspect.signature(ToolingLlmClient.complete_with_tools)
    assert "session_id" in sig.parameters, (
        "ToolingLlmClient.complete_with_tools MUST accept a "
        "'session_id' keyword. Got parameters: "
        f"{list(sig.parameters.keys())}"
    )
    param = sig.parameters["session_id"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
        "session_id MUST be keyword-only (the protocol uses a *-bar to "
        "keep positional args stable across backends). Got "
        f"kind={param.kind!r}"
    )
    # The protocol allows None to keep non-narrator callsites simple
    # (see context-story-61-followup-D.md §C.1).
    assert param.default is None, (
        "session_id default MUST be None so non-narrator callsites "
        "(claude -p curate, tests without a session) don't need to "
        f"thread one. Got default={param.default!r}"
    )


# ---------------------------------------------------------------------------
# 2. Orchestrator propagates context.session_id to complete_with_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_passes_session_id_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring teeth: when ``TurnContext.session_id`` is set, the
    orchestrator's narrator-turn path MUST forward that value to
    ``complete_with_tools(..., session_id=...)``. Without this, every
    SDK call appears to come from the same nameless session and the
    per-session cumulative collapses to a single global counter.

    This test fails until Dev adds ``session_id=session_id`` (or
    equivalent) to the kwargs at orchestrator.py:3663.
    """
    spy = _SessionIdRecordingSpy()
    orch = Orchestrator(client=spy)

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=0,
        session_id="2026-05-23-glenross-mp",
        world_id="glenross",
    )

    await orch.run_narration_turn("look around", ctx)

    assert spy.recorded_session_ids, (
        "Orchestrator MUST call complete_with_tools at least once. Got "
        "no recorded calls — narration path bypassed the SDK client."
    )
    assert spy.recorded_session_ids[0] == "2026-05-23-glenross-mp", (
        "Orchestrator MUST forward TurnContext.session_id to "
        "complete_with_tools(session_id=...). Got "
        f"recorded session_id={spy.recorded_session_ids[0]!r}, "
        f"expected '2026-05-23-glenross-mp'."
    )


# ---------------------------------------------------------------------------
# 3. AnthropicSdkCostCeilingExceeded propagates out of run_narration_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_ceiling_exception_propagates_out_of_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story §C.2 + CLAUDE.md "No Silent Fallbacks": when the SDK
    client raises ``AnthropicSdkCostCeilingExceeded``, the orchestrator
    MUST NOT swallow it. The exception propagates out of
    ``run_narration_turn`` so the WS handler can:

    - broadcast the typed cost-ceiling message to clients,
    - mark the turn refused,
    - and stop the dispatch chain.

    If the orchestrator catches and returns a degraded result instead,
    the player gets a fake "ok" narration while the server has decided
    the session is dead. That's the worst-of-both — the kill becomes
    silent.
    """
    spy = _SessionIdRecordingSpy(
        raise_on_call=AnthropicSdkCostCeilingExceeded(
            "Session 'live-fire-test' has exceeded its $10.00 ceiling.",
            session_id="live-fire-test",
            cumulative_cost_usd=10.05,
            ceiling_usd=10.0,
        ),
    )
    orch = Orchestrator(client=spy)

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=0,
        session_id="live-fire-test",
        world_id="glenross",
    )

    with pytest.raises(AnthropicSdkCostCeilingExceeded) as excinfo:
        await orch.run_narration_turn("look around", ctx)

    err = excinfo.value
    assert err.session_id == "live-fire-test", (
        f"Propagated exception MUST preserve session_id. Got {err.session_id!r}"
    )
    assert err.ceiling_usd == pytest.approx(10.0), (
        f"Propagated exception MUST preserve ceiling_usd. Got {err.ceiling_usd!r}"
    )
    assert err.cumulative_cost_usd >= 10.0, (
        "Propagated exception MUST preserve cumulative_cost_usd ≥ "
        f"ceiling. Got {err.cumulative_cost_usd!r}"
    )


# ---------------------------------------------------------------------------
# 4. AnthropicSdkCostCeilingExceeded carries actionable fields
# ---------------------------------------------------------------------------


def test_cost_ceiling_exception_constructor_shape() -> None:
    """Story §C.2 locks the typed exception's shape. Construction MUST
    accept ``session_id``, ``cumulative_cost_usd``, and ``ceiling_usd``
    as named arguments AND expose them as attributes. The WS handler /
    broadcast layer reads these to build the typed message; missing
    fields would force the handler to grovel at strings.
    """
    err = AnthropicSdkCostCeilingExceeded(
        "Session 'sid' has exceeded its $10.00 ceiling.",
        session_id="sid",
        cumulative_cost_usd=10.5,
        ceiling_usd=10.0,
    )
    assert err.session_id == "sid"
    assert err.cumulative_cost_usd == pytest.approx(10.5)
    assert err.ceiling_usd == pytest.approx(10.0)

    # Hierarchy — the exception MUST inherit from a stable base so a
    # catch-all SDK-error handler still catches it without enumerating.
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClientError

    assert isinstance(err, AnthropicSdkClientError), (
        "AnthropicSdkCostCeilingExceeded MUST inherit from "
        f"AnthropicSdkClientError. MRO: {type(err).__mro__!r}"
    )


# ---------------------------------------------------------------------------
# 5. session_id None reaches the client when TurnContext has no session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_passes_none_session_id_when_context_lacks_one() -> None:
    """A TurnContext without a session_id (legacy callsites, tests)
    MUST forward ``session_id=None`` to the client — NOT a string like
    "adhoc" or "unknown". Per §C.1, the client uses ``None`` as the
    tracker-bypass sentinel: a string sentinel collides with real
    session_ids and corrupts the per-session cumulative.
    """
    spy = _SessionIdRecordingSpy()
    orch = Orchestrator(client=spy)

    # No session_id set — TurnContext defaults to None.
    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=0,
    )

    await orch.run_narration_turn("look around", ctx)

    assert spy.recorded_session_ids, "complete_with_tools MUST be called."
    assert spy.recorded_session_ids[0] is None, (
        "When TurnContext.session_id is None, the orchestrator MUST "
        "forward session_id=None to the client (NOT a string sentinel "
        "like 'adhoc' or 'unknown'). String sentinels collide with "
        "real session_ids and corrupt the cumulative tracker. Got "
        f"session_id={spy.recorded_session_ids[0]!r}"
    )
