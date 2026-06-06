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

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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


def test_intent_router_constructible_with_sdk_haiku_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-7: ``IntentRouter`` is constructible with the SDK-Haiku adapter
    (the ``build_intent_router_llm`` factory in ``llm_factory.py``).

    The adapter construction must NOT make any network calls — only
    instantiate the AsyncAnthropic client. We patch the SDK boundary so
    test runs do not require ANTHROPIC_API_KEY in the environment, then
    assert the adapter constructs and IntentRouter accepts it.
    """
    from sidequest.agents.intent_router import IntentRouter
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-only")
    with patch("anthropic.AsyncAnthropic") as sdk_class:
        sdk_class.return_value = object()
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


def test_build_intent_router_llm_fails_loud_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 fail-loud: like ``build_aside_llm`` at ``llm_factory.py:85``,
    the intent-router builder raises if ``ANTHROPIC_API_KEY`` is unset.

    Memory rule ``feedback_no_fallbacks_hard`` + CLAUDE.md "No Silent
    Fallbacks": missing config raises a typed error, never silently
    falls back to a no-op adapter.
    """
    from sidequest.agents.claude_client import LlmClientError
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LlmClientError):
        build_intent_router_llm(session_id=None)


@pytest.mark.asyncio
async def test_intent_router_sdk_adapter_calls_haiku_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 behavioral: when the adapter ``emit_tool()`` method runs, the
    underlying AsyncAnthropic ``messages.create`` is invoked with
    ``model=claude-haiku-4-5-20251001`` AND a forced ``tool_choice`` —
    proving the ADR-102 tool-use wiring is end-to-end, not just a
    constant assertion.
    """
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-only")

    fake_client_instance = AsyncMock()
    fake_response = AsyncMock()
    # Anthropic SDK response has ``.content`` as a list of blocks. Under
    # forced tool_choice the adapter extracts the ``tool_use`` block's
    # structured ``.input`` — synthesize that minimal shape.
    tool_block = type(
        "Block",
        (),
        {"type": "tool_use", "name": "emit_dispatch_package", "input": {"ok": True}},
    )()
    fake_response.content = [tool_block]
    # Story 91-1: every SDK call is cost-accounted through
    # ``_record_usage_telemetry`` — the fake must carry a real usage shape
    # and model id (an auto-mocked attribute would str() into a garbage
    # model and fail the pricing lookup loudly, by design).
    fake_response.model = "claude-haiku-4-5-20251001"
    fake_response.usage = SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    fake_client_instance.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=fake_client_instance):
        llm = build_intent_router_llm(session_id=None)
        result = await llm.emit_tool(
            system="sys",
            user="usr",
            tool_name="emit_dispatch_package",
            tool_description="desc",
            tool_schema={"type": "object", "properties": {}},
        )

    assert result == {"ok": True}
    assert fake_client_instance.messages.create.await_count == 1
    await_args = fake_client_instance.messages.create.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001", (
        f"SDK adapter must call AsyncAnthropic.messages.create with "
        f"model=claude-haiku-4-5-20251001; got model={call_kwargs.get('model')!r}"
    )
    assert call_kwargs.get("tool_choice") == {
        "type": "tool",
        "name": "emit_dispatch_package",
    }, f"adapter must force tool_choice; got {call_kwargs.get('tool_choice')!r}"
