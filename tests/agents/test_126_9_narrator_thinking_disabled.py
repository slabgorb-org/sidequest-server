"""Story 126-9 RED — the narrator tool-loop must run with extended thinking DISABLED.

Regression guard for the Jun-17 ~3x narrator-latency regression. The 119-3 port
(commit f970091e, PR #908) moved the narrator off the ``anthropic`` Messages SDK
(thinking OFF unless a budget is passed) onto the ``claude-agent-sdk`` ``query()``
loop, whose ``claude`` CLI defaults thinking ON ("adaptive").
``complete_with_tools`` builds its options with neither ``thinking`` nor
``output_format`` (anthropic_sdk_client.py:438), so the ``build_agent_sdk_options``
auto-disable — scoped to ``output_format`` calls (lines 262-263) — never fires.
sonnet-4.6 then runs an adaptive thinking pass before EACH of up to 8 tool-loop
iterations, tripling ``agent_duration_ms`` (~16s -> ~50-57s; same-world proof
wry_whimsy/oz 15.9s -> 56.7s).

These tests pin AC #3: the narrator's ``complete_with_tools`` call MUST build
options with ``thinking={"type": "disabled"}`` so the regression cannot silently
return. They drive the REAL ``AnthropicSdkClient.complete_with_tools`` with only
the transport ``query`` seam faked (OQ-9) and assert on the ``ClaudeAgentOptions``
actually handed to ``query()`` — a behaviour assertion on the production call
site, never a source-text grep (CLAUDE.md "No Source-Text Wiring Tests").

Wiring: that a production narration turn reaches ``complete_with_tools`` is held by
``test_narrator_uses_sdk_client.py::test_orchestrator_routes_narration_through_sdk``.

Scope. The narrator-aside (``aside_resolver.py:302`` — ``caller="aside"``,
``tool_choice={"type": "none"}``, no ``tool_dispatch``) shares this exact call
site, so it is covered here too (AC #2 parenthetical). The *Haiku* aside adapter
(``llm_factory.build_aside_llm`` -> ``.complete()``, a different call site and a
cheap single-shot model) is NOT the regression and is left untouched —
``test_119_3_haiku_port.py::test_aside_leaves_thinking_unset`` still holds for it.
The Fate ``intent_router_pass`` prompt-bloat spike is a separate mechanism tracked
in 126-10; thinking-off does not fix it and it is out of scope here (AC #5).
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.tooling_protocol import CacheableBlock, Message
from tests.agents.fakes.fake_agent_sdk import FakeQuery, converged_text_stream

_SONNET = "claude-sonnet-4-6"
_DISABLED = {"type": "disabled"}


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_agent_sdk_options`` asserts the no-PAYG-cred invariant (a SET key
    re-routes to PAYG — the 119-1 NO-GO). Clear both so the subscription-auth
    assert passes without a live login."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _new_client() -> Any:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    return AnthropicSdkClient()


async def test_narrator_tool_loop_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrator tool-loop call builds options with extended thinking DISABLED.

    Fails on the f970091e regression: ``complete_with_tools`` leaves ``thinking``
    ``None``, so the ``claude`` CLI adaptive default (ON) applies and the model
    thinks before each of up to 8 iterations.
    """
    from sidequest.agents import anthropic_sdk_client

    fake = FakeQuery(converged_text_stream(text="You glance around the clearing."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _new_client().complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="look around")],
        [],
        None,
        model=_SONNET,
    )

    assert getattr(fake.last_options, "thinking", "MISSING") == _DISABLED, (
        "the narrator tool-loop MUST disable extended thinking — the agent-SDK "
        "query() loop defaults thinking ON ('adaptive') and runs a pass before "
        "each of up to 8 iterations, tripling agent_duration_ms (the Jun-17 "
        f"regression); got thinking={getattr(fake.last_options, 'thinking', None)!r}"
    )


async def test_narrator_aside_caller_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrator-aside (caller='aside', tool_choice none, no dispatch —
    aside_resolver.py:302) shares the ``complete_with_tools`` call site and so MUST
    also run with thinking disabled (AC #2 parenthetical). This guards against a
    future 'exclude the aside' branch silently re-enabling thinking on that path.
    """
    from sidequest.agents import anthropic_sdk_client

    fake = FakeQuery(converged_text_stream(text="A whisper, off to the side."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _new_client().complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="(aside) what do I smell?")],
        [],
        None,
        model=_SONNET,
        tool_choice={"type": "none"},
        caller="aside",
    )

    assert getattr(fake.last_options, "thinking", "MISSING") == _DISABLED, (
        "the narrator-aside shares the narrator tool-loop call site and must also "
        f"run with thinking disabled; got thinking={getattr(fake.last_options, 'thinking', None)!r}"
    )
