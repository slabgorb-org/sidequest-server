"""Story 119-3 RED — AC1: Agent SDK context isolation (the landmine).

``claude-agent-sdk`` inherits project context from ``cwd`` /
``setting_sources`` — the 119-3 subscription spike replied **in an SM persona**
because it absorbed the repo ``CLAUDE.md``. The narrator (and every Haiku
single-shot) MUST pin ``system_prompt`` (plain string, no ``claude_code``
preset) + ``cwd`` (a neutral non-repo dir) + ``setting_sources=[]`` +
``add_dirs=[]`` on the ``ClaudeAgentOptions`` it builds (spec §6.3).

Two gates (spec §9, AC1):

* ``test_narrator_options_pin_isolation`` — the **structural** unit gate:
  inspect the exact ``ClaudeAgentOptions`` the port hands ``query()``.
* ``test_narrator_output_not_contaminated_by_repo_persona`` — the
  **behavioural** spike-regression gate: a fake transport that absorbs the
  repo persona *unless* isolation is pinned, proving the pins actually defeat
  the contamination.

All tests drive the fake ``query`` seam (OQ-9): no live subscription. RED:
``AnthropicSdkClient()`` with both creds unset raises today (the old
"key required" check) and the port's options builder does not exist, so these
fail. GREEN: no-key construction resolves the subscription and the pinned
options ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.agents.tooling_protocol import CacheableBlock, Message, ToolDefinition
from tests.agents.fakes.fake_agent_sdk import (
    ContaminatingFakeQuery,
    FakeQuery,
    converged_text_stream,
    options_pin_isolation,
)

_SONNET = "claude-sonnet-4-6"

# The server repo root — the directory whose CLAUDE.md the spike absorbed.
_SERVER_REPO_ROOT = Path(__file__).resolve().parents[2]

# Persona tells the spike emitted; none may appear in clean narration.
_PERSONA_TELLS = ("Morpheus", "Scrum Master", "SideQuest orchestrator", "Architect")


def _new_narrator_client() -> Any:
    """Construct the narrator client on the subscription path (both creds
    unset, no ``sdk=`` injection). RED: the legacy ctor raises here."""
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    return AnthropicSdkClient()


async def _drive_turn(client: Any, *, system_text: str = "NARRATOR-RULES") -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text=system_text, cache=True)],
        [Message(role="user", content="look around")],
        [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})],
        None,
        model=_SONNET,
    )


@pytest.fixture(autouse=True)
def _both_creds_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


async def test_narrator_options_pin_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``ClaudeAgentOptions`` the port hands ``query()`` must pin every
    isolation lever — ``setting_sources=[]``, ``add_dirs=[]``, a non-repo
    ``cwd``, and a plain-string ``system_prompt`` carrying our assembled
    narrator text (no ``preset``)."""
    from sidequest.agents import anthropic_sdk_client

    fake = FakeQuery(converged_text_stream(text="The cavern yawns."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = _new_narrator_client()
    await _drive_turn(client, system_text="SOUL+RULES+TONE")

    opts = fake.last_options

    assert getattr(opts, "setting_sources", "MISSING") == [], (
        "setting_sources must be [] — load no user/project/local config tier "
        "(no CLAUDE.md, no settings). This is the landmine (spec §6.3)."
    )
    assert getattr(opts, "add_dirs", "MISSING") == [], "add_dirs must be []"

    cwd = getattr(opts, "cwd", None)
    assert cwd is not None, "cwd must be pinned to a neutral non-repo dir, not left to default"
    assert Path(cwd).resolve() != _SERVER_REPO_ROOT, (
        f"cwd must NOT be the server repo root ({_SERVER_REPO_ROOT}) — that is "
        "exactly the dir whose CLAUDE.md the spike absorbed"
    )
    assert not (Path(cwd) / "CLAUDE.md").exists(), (
        f"cwd ({cwd}) must contain no CLAUDE.md to absorb"
    )

    system_prompt = getattr(opts, "system_prompt", None)
    assert isinstance(system_prompt, str), (
        "system_prompt must be a plain replacement string, never the "
        "{'type':'preset','preset':'claude_code',...} form (which injects "
        f"Claude-Code scaffolding); got {type(system_prompt).__name__}"
    )
    assert "SOUL+RULES+TONE" in system_prompt, (
        f"the assembled narrator system text must be the system_prompt — got {system_prompt!r}"
    )


async def test_narrator_output_not_contaminated_by_repo_persona(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spike-regression: a transport that absorbs the repo SM persona unless
    isolation is pinned must yield CLEAN narration — proving the §6.3 pins
    actually defeat the contamination, not just that they are present."""
    from sidequest.agents import anthropic_sdk_client

    clean = "Rain hisses on the neon-slick street."
    persona = "As Morpheus, the Scrum Master of the SideQuest orchestrator, I advise you to..."
    fake = ContaminatingFakeQuery(clean_text=clean, persona_text=persona)
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = _new_narrator_client()
    result = await _drive_turn(client)

    assert result.text == clean, (
        "narration was contaminated — the options the port built did not pin "
        f"context isolation, so the fake absorbed the repo persona: {result.text!r}"
    )
    for tell in _PERSONA_TELLS:
        assert tell not in result.text, (
            f"repo-context tell {tell!r} leaked into narration: {result.text!r}"
        )


async def test_haiku_router_options_pin_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The isolation pins apply uniformly to the Haiku sites too (spec §6.1):
    an Intent-Router classifier that absorbed the repo CLAUDE.md would
    misclassify on persona-tainted context. Pin the router's options."""
    from sidequest.agents import llm_factory
    from tests.agents.fakes.fake_agent_sdk import structured_output_stream

    fake = FakeQuery(structured_output_stream({"intent": "attack", "confidence": 0.9}))
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)

    adapter = llm_factory.build_intent_router_llm(session_id=None)
    await adapter.emit_tool(
        system="ROUTER-SYS",
        user="attack the goblin",
        tool_name="emit_dispatch_package",
        tool_description="d",
        tool_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )

    opts = fake.last_options
    assert options_pin_isolation(opts), (
        "the Intent Router's ClaudeAgentOptions must pin setting_sources=[] + "
        "add_dirs=[] + a plain-string system_prompt — a classifier absorbing "
        "the repo CLAUDE.md misclassifies on tainted context (spec §6.1)"
    )
    assert "ROUTER-SYS" in getattr(opts, "system_prompt", ""), (
        "the router's own system text must be the system_prompt"
    )
