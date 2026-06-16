"""Story 119-3 RED — AC2: subscription auth, fail-loud, no PAYG fallback.

The port inverts the auth contract (spec §6.1, §7.5):

* ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` both **unset** → the Agent
  SDK resolves the subscription login. No-key construction must SUCCEED (the
  legacy "key required" check is replaced).
* A **set** ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` on the SDK path
  must RAISE ``AgentSdkAuthUnavailable`` — a set key silently re-routes to the
  empty PAYG ledger (the exact 119-1 NO-GO). This is the inverse of today's
  check.
* A failed query / ``ResultMessage(is_error=True)`` must RAISE, never return a
  degraded-but-successful result (No Silent Fallbacks).

Uniform across the narrator and all four Haiku sites (one choke point, one
contract). Tests import ``AgentSdkAuthUnavailable`` *before* touching the
transport so RED fails at the missing-symbol import (no network), and drive the
fake ``query`` seam (OQ-9) so no live subscription is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.claude_client import LlmClientError
from sidequest.agents.tooling_protocol import CacheableBlock, Message, ToolDefinition
from tests.agents.fakes.fake_agent_sdk import (
    FakeQuery,
    converged_text_stream,
    error_result_stream,
    structured_output_stream,
)

_SONNET = "claude-sonnet-4-6"


async def _drive_narrator(client: Any) -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="go")],
        [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})],
        None,
        model=_SONNET,
    )


# ===========================================================================
# Narrator path
# ===========================================================================


@pytest.mark.parametrize("cred", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
async def test_narrator_raises_when_payg_cred_set(
    monkeypatch: pytest.MonkeyPatch, cred: str
) -> None:
    """A set PAYG credential re-routes to the metered ledger (the 119-1 NO-GO).
    The narrator path must raise ``AgentSdkAuthUnavailable`` rather than spend
    on PAYG — the inverse of the legacy "key required" check."""
    # Import first so RED fails at the missing symbol, before any transport.
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv(cred, "sk-should-not-be-set-on-agent-sdk-path")

    fake = FakeQuery(converged_text_stream(text="never reached"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AgentSdkAuthUnavailable):
        client = anthropic_sdk_client.AnthropicSdkClient()
        await _drive_narrator(client)


async def test_narrator_auth_absent_raises_not_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal ``ResultMessage(is_error=True)`` (a failed query — how an
    absent subscription surfaces, OQ-5) must RAISE, never return a
    degraded-success ToolingResult that masks the missing credential."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClientError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    fake = FakeQuery(error_result_stream(subtype="error_auth"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = anthropic_sdk_client.AnthropicSdkClient()
    with pytest.raises(AnthropicSdkClientError):
        await _drive_narrator(client)


async def test_narrator_both_creds_unset_resolves_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both creds unset → no-key construction succeeds and the turn converges
    via the subscription transport, with no api_key/auth_token plumbed into
    the SDK options."""
    from sidequest.agents import anthropic_sdk_client

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    fake = FakeQuery(converged_text_stream(text="The door creaks open."))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = anthropic_sdk_client.AnthropicSdkClient()
    result = await _drive_narrator(client)

    assert result.text == "The door creaks open.", (
        "both-creds-unset must resolve the subscription and converge — the "
        "subscription pool is the whole point of the port"
    )
    opts = fake.last_options
    assert not getattr(opts, "api_key", None), (
        "no api_key may be plumbed into the SDK options — the subscription "
        "login is resolved by the CLI, and a key would re-route to PAYG"
    )
    assert not getattr(opts, "auth_token", None), "no auth_token may be plumbed in"


# ===========================================================================
# Haiku single-shot sites (uniform contract — spec §6.1)
# ===========================================================================


def _build_router(monkeypatch: pytest.MonkeyPatch) -> Any:
    from sidequest.agents import llm_factory

    return llm_factory.build_intent_router_llm(session_id=None)


def _build_aside(monkeypatch: pytest.MonkeyPatch) -> Any:
    from sidequest.agents import llm_factory

    return llm_factory.build_aside_llm(session_id=None)


def _build_classifier(monkeypatch: pytest.MonkeyPatch) -> Any:
    from sidequest.agents import llm_factory

    return llm_factory.build_unseeded_objective_classifier_llm(session_id=None)


async def _drive_emit_tool(adapter: Any) -> Any:
    return await adapter.emit_tool(
        system="S",
        user="U",
        tool_name="emit_dispatch_package",
        tool_description="d",
        tool_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )


_HAIKU_SITES = ("router", "aside", "classifier")


async def _drive_site(name: str, adapter: Any) -> Any:
    if name == "aside":
        return await adapter.complete(system="S", user="U")
    return await _drive_emit_tool(adapter)


@pytest.mark.parametrize("site", _HAIKU_SITES)
async def test_haiku_site_raises_when_api_key_set(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """A set ``ANTHROPIC_API_KEY`` on any Haiku site re-routes to PAYG — it
    must raise ``AgentSdkAuthUnavailable`` (uniform choke-point contract)."""
    from sidequest.agents import llm_factory
    from sidequest.agents.anthropic_sdk_client import AgentSdkAuthUnavailable

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-set")

    fake = FakeQuery(structured_output_stream({"intent": "x"}))
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)

    builders = {
        "router": _build_router,
        "aside": _build_aside,
        "classifier": _build_classifier,
    }
    with pytest.raises(AgentSdkAuthUnavailable):
        adapter = builders[site](monkeypatch)
        await _drive_site(site, adapter)


@pytest.mark.parametrize("site", _HAIKU_SITES)
async def test_haiku_site_auth_absent_raises_not_silent(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """An auth-absent failure (``ResultMessage(is_error=True)``) on a Haiku
    site must raise loudly — never silently return an empty/``None`` payload a
    consumer could mistake for a legitimate "nothing to infer"."""
    from sidequest.agents import llm_factory

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    fake = FakeQuery(error_result_stream(subtype="error_auth"))
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)

    builders = {
        "router": _build_router,
        "aside": _build_aside,
        "classifier": _build_classifier,
    }
    # Construct OUTSIDE pytest.raises: in RED the legacy ctor raises
    # LlmClientError on the missing key, which would false-green this test if
    # caught here. The raise under test is the auth-absent QUERY result.
    adapter = builders[site](monkeypatch)
    with pytest.raises(LlmClientError):
        await _drive_site(site, adapter)
