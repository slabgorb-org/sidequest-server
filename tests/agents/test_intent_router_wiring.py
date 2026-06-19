"""Wiring tests for IntentRouter — Story 59-2 RED phase.

CLAUDE.md "Every Test Suite Needs a Wiring Test": unit tests prove a
component works in isolation, but the wiring test verifies the component
is reachable from production-shaped code paths.

Story 59-2 ACs covered here:

* AC-7 (wiring placeholder): ``IntentRouter`` is importable from
  ``sidequest.agents.intent_router`` and is constructible with the
  SDK-Haiku adapter. **LIVE pipeline integration lands in Story 59-4**
  — this wiring test is the producer-side proof; 59-4 wires the producer
  into the orchestrator turn pipeline pre-narrator and adds the
  end-to-end wiring test.
* AC-3 (SDK-Haiku adapter): the adapter exists in
  ``sidequest/agents/llm_factory.py`` and routes
  ``claude-haiku-4-5-20251001`` via ``CallType.CLASSIFICATION``, mirroring
  the ``AsideResolver`` / ``_ASIDE_MODEL`` pattern at ``llm_factory.py:88``.

Design constraint (CLAUDE.md "No Source-Text Wiring Tests"): we DO NOT
grep production source files. Wiring is proved by runtime imports,
runtime-type interrogation, and behavioral mocks against the SDK boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.agents.fakes.fake_agent_sdk import FakeQuery, structured_output_stream


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 119-3: the agent-SDK transport draws the Max subscription only
    with both PAYG credentials UNSET — a set key re-routes to PAYG and raises
    ``AgentSdkAuthUnavailable`` at call time. Pin the subscription world."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# AC-7: IntentRouter is importable + constructible.
# ---------------------------------------------------------------------------


def test_intent_router_importable_from_agents_module() -> None:
    """AC-7: ``IntentRouter`` is importable from the agents subpackage."""
    from sidequest.agents.intent_router import IntentRouter

    # Constructor accepts a Protocol-conforming LLM adapter. We pass an
    # AsyncMock so the constructor exercises its injection path without
    # hitting the SDK or the network.
    llm = AsyncMock()
    llm.emit_tool = AsyncMock(return_value={})  # never actually called here
    router = IntentRouter(llm=llm)
    assert router is not None


def test_intent_router_constructible_with_sdk_haiku_adapter() -> None:
    """AC-7: ``IntentRouter`` is constructible with the SDK-Haiku adapter
    (the ``build_intent_router_llm`` factory in ``llm_factory.py``).

    Story 119-3: the adapter construction must NOT make any network calls and
    must NOT require ``ANTHROPIC_API_KEY`` — the agent-SDK transport runs on
    the Max subscription (credentials UNSET, pinned by ``_subscription_env``).
    Build is late-bound: the ``query`` seam is only touched at call time, so
    construction is hermetic. Assert the adapter constructs and IntentRouter
    accepts it.
    """
    from sidequest.agents.intent_router import IntentRouter
    from sidequest.agents.llm_factory import build_intent_router_llm

    llm = build_intent_router_llm(session_id=None)
    router = IntentRouter(llm=llm)
    assert router is not None


def test_intent_router_re_exported_from_agents_package_root() -> None:
    """AC-7: ``IntentRouter`` is re-exported from the ``sidequest.agents``
    package root so other layers (orchestrator, server) can use the
    package-root import style. Mirrors the ``LocalDM`` re-export tested
    pre-rename in tests/agents/test_local_dm.py::test_local_dm_importable_from_package_root.
    """
    from sidequest.agents import IntentRouter as IntentRouterFromRoot
    from sidequest.agents.intent_router import IntentRouter

    assert IntentRouterFromRoot is IntentRouter, (
        "sidequest.agents must re-export IntentRouter so callers can use "
        "the package-root import (same shape as the LocalDM re-export)"
    )


# ---------------------------------------------------------------------------
# AC-3: SDK-Haiku adapter mirrors ``AsideResolver`` / ``_ASIDE_MODEL``
#       pattern; uses CallType.CLASSIFICATION; targets
#       claude-haiku-4-5-20251001.
# ---------------------------------------------------------------------------


def test_intent_router_model_constant_targets_haiku() -> None:
    """AC-3: ``_INTENT_ROUTER_MODEL`` constant exists in
    ``llm_factory.py`` and targets the Haiku 4.5 model id, mirroring the
    ``_ASIDE_MODEL`` pattern at ``llm_factory.py:88``.

    Runtime-type interrogation (legitimate per CLAUDE.md "No Source-Text
    Wiring Tests"): we import the symbol and assert its value, rather
    than grepping the module source.
    """
    from sidequest.agents import llm_factory

    assert hasattr(llm_factory, "_INTENT_ROUTER_MODEL"), (
        "llm_factory.py must define _INTENT_ROUTER_MODEL (mirrors "
        "_ASIDE_MODEL at llm_factory.py:88)"
    )
    assert llm_factory._INTENT_ROUTER_MODEL == "claude-haiku-4-5-20251001", (
        f"_INTENT_ROUTER_MODEL must target claude-haiku-4-5-20251001 "
        f"(CallType.CLASSIFICATION default); "
        f"got {llm_factory._INTENT_ROUTER_MODEL!r}"
    )


def test_intent_router_model_resolves_via_call_type_classification() -> None:
    """AC-3: ``_INTENT_ROUTER_MODEL`` matches what
    ``resolve_model(CallType.CLASSIFICATION)`` returns by default.

    This pins the wiring contract — if the per-call-type ladder ever
    changes the CLASSIFICATION default, the constant must move with it
    (or the test catches the drift)."""
    from sidequest.agents import llm_factory
    from sidequest.agents.model_routing import CallType, resolve_model

    resolved = resolve_model(CallType.CLASSIFICATION)
    assert resolved == llm_factory._INTENT_ROUTER_MODEL, (
        f"_INTENT_ROUTER_MODEL ({llm_factory._INTENT_ROUTER_MODEL!r}) "
        f"must equal resolve_model(CallType.CLASSIFICATION) "
        f"({resolved!r})"
    )


@pytest.mark.asyncio
async def test_build_intent_router_llm_fails_loud_when_payg_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 fail-loud (Story 119-3 INVERSION): the old contract raised when
    ``ANTHROPIC_API_KEY`` was UNSET. The agent-SDK transport inverts it — the
    Max subscription needs the PAYG credentials UNSET, and a SET key silently
    re-routes to the metered PAYG ledger (the 119-1 NO-GO). So a set key must
    fail loud with ``AgentSdkAuthUnavailable`` (a typed ``LlmClientError``),
    never a silent PAYG fallback.

    The raise fires at CALL time inside ``build_agent_sdk_options`` (build is
    late-bound), so drive ``emit_tool`` to provoke it. Memory rule
    ``feedback_no_fallbacks_hard`` + CLAUDE.md "No Silent Fallbacks".
    """
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable
    from sidequest.agents.claude_client import LlmClientError
    from sidequest.agents.llm_factory import build_intent_router_llm

    assert issubclass(AgentSdkAuthUnavailable, LlmClientError)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-only")
    llm = build_intent_router_llm(session_id=None)
    with pytest.raises(AgentSdkAuthUnavailable):
        await llm.emit_tool(
            system="sys",
            user="usr",
            tool_name="emit_dispatch_package",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
        )


@pytest.mark.asyncio
async def test_intent_router_sdk_adapter_calls_haiku_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 behavioral (Story 119-3 transport): when the adapter ``emit_tool()``
    runs, it drives the module-level ``query`` seam with
    ``ClaudeAgentOptions(model=claude-haiku-4-5-20251001, max_turns=4,
    output_format={json_schema})`` — the VERIFIED Path A surface (no
    ``tool_choice``; the Agent SDK has none). The adapter returns the dict from
    ``ResultMessage.structured_output`` where the forced tool's ``.input`` went.
    Proves the ADR-102/119-3 structured-output wiring is end-to-end, not just a
    constant assertion.
    """
    from sidequest.agents import llm_factory

    payload = {"ok": True}
    fake = FakeQuery(structured_output_stream(payload))
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)

    llm = llm_factory.build_intent_router_llm(session_id=None)
    result = await llm.emit_tool(
        system="sys",
        user="usr",
        tool_name="emit_dispatch_package",
        tool_description="desc",
        tool_schema={"type": "object", "properties": {}},
    )

    assert result == payload
    assert len(fake.calls) == 1, "the adapter must drive query() exactly once"
    opts = fake.last_options
    assert getattr(opts, "model", None) == "claude-haiku-4-5-20251001", (
        f"adapter must request the Haiku 4.5 model; got model={getattr(opts, 'model', None)!r}"
    )
    # The Agent SDK has no tool_choice; the forced-extraction surface is
    # output_format JSON-schema. ``2`` is the mandatory FLOOR (the +1 finalize
    # turn means max_turns=1 fails closed); the structured-output choke point
    # (_call_haiku_sdk) raises the value to 4 for headroom against intermittent
    # error_max_turns at mt=2 (2026-06-19 playtest).
    assert getattr(opts, "max_turns", None) == 4, (
        f"forced extraction must run at max_turns=4 (2 is the floor; raised for headroom); "
        f"got {getattr(opts, 'max_turns', None)!r}"
    )
    output_format = getattr(opts, "output_format", None)
    assert isinstance(output_format, dict) and output_format.get("type") == "json_schema", (
        f"adapter must force structured extraction via output_format; got {output_format!r}"
    )
