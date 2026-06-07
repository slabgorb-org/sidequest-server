"""Aside-rides-the-narrator-cache — unit tests (playtest 2026-06-07).

The ADR-107 re-scope: instead of a parallel thin read-view, the aside
re-presents the narrator's EXACT cached prompt artifacts (system blocks +
tools + model, stashed per SDK turn) with the OOC question as the user
turn and ``tool_choice={"type": "none"}``.

Pins, per ``resolve_aside_on_narrator_cache``:

* happy path — valid compact-JSON answer parses into an AsideResolution,
  and the cache-read token count passes through for the span's
  ``cache_hit`` lie-detector;
* the stash artifacts are forwarded VERBATIM (byte-identity is the cache
  key — any rebuild risks drift) with ``tool_choice={"type":"none"}``,
  ``max_iterations=1``, the session id, and the question in the user turn;
* malformed JSON degrades to the loud ``resolver_error`` outcome (shared
  ``_parse_resolution`` contract), never raises;
* an LLM call failure degrades to ``resolver_error`` with 0 cache-read
  tokens (spec §6 parity with the legacy path).
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.aside_resolver import (
    AsidePromptStash,
    resolve_aside_on_narrator_cache,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    ToolDefinition,
    ToolingLlmClient,
    ToolingResult,
)

_ANSWER_JSON = (
    '{"answer":"It is the morning of the 18th of October.",'
    '"outcome":"answered","grounded_on":["calendar","recent_narration"]}'
)


def _tooling_result(text: str, *, cache_read: int = 27_000) -> ToolingResult:
    return ToolingResult(
        text=text,
        stop_reason="end_turn",
        input_tokens=27_400,
        output_tokens=60,
        cached_input_read_tokens=cache_read,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )


class _FakeToolingClient:
    """Records the complete_with_tools call; absorbs extra kwargs like the
    shared production-shaped fakes do (``caller`` rides beyond the
    Protocol surface, mirroring the orchestrator's own call)."""

    def __init__(
        self,
        result: ToolingResult | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    async def complete_with_tools(
        self,
        system_blocks: list[CacheableBlock],
        messages: list[Any],
        tools: list[ToolDefinition],
        tool_dispatch: Any = None,
        *,
        model: str,
        max_iterations: int = 8,
        max_tokens: int = 4096,
        session_id: str | None = None,
        tool_choice: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolingResult:
        self.calls.append(
            {
                "system_blocks": system_blocks,
                "messages": messages,
                "tools": tools,
                "tool_dispatch": tool_dispatch,
                "model": model,
                "max_iterations": max_iterations,
                "session_id": session_id,
                "tool_choice": tool_choice,
                **kwargs,
            }
        )
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _stash() -> AsidePromptStash:
    return AsidePromptStash(
        system_blocks=[
            CacheableBlock(text="stable narrator prefix", cache=True),
            CacheableBlock(text="valley state", cache=False),
        ],
        tools=[
            ToolDefinition(
                name="roll_dice",
                description="Roll dice.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        model="claude-sonnet-4-6",
    )


def test_fake_satisfies_tooling_protocol() -> None:
    # The runtime_checkable Protocol is the handler's path gate
    # (Orchestrator.aside_cache_client) — the fake must match it or the
    # tests below test a shape production would reject.
    assert isinstance(_FakeToolingClient(), ToolingLlmClient)


@pytest.mark.asyncio
async def test_happy_path_parses_answer_and_passes_cache_read_through() -> None:
    client = _FakeToolingClient(result=_tooling_result(_ANSWER_JSON, cache_read=27_000))
    stash = _stash()

    res, cache_read = await resolve_aside_on_narrator_cache(
        client=client,
        stash=stash,
        question="what day is it?",
        session_id="2026-06-07-blackthorn_moor",
    )

    assert res.outcome == "answered"
    assert res.answer == "It is the morning of the 18th of October."
    assert res.grounded_on == ("calendar", "recent_narration")
    assert cache_read == 27_000


@pytest.mark.asyncio
async def test_stash_forwarded_verbatim_with_tool_choice_none() -> None:
    client = _FakeToolingClient(result=_tooling_result(_ANSWER_JSON))
    stash = _stash()

    await resolve_aside_on_narrator_cache(
        client=client,
        stash=stash,
        question="what day is it?",
        session_id="slug-1",
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    # Byte-identity: the SAME objects the narrator turn shipped — identity,
    # not equality, is the contract (a rebuild risks cache-busting drift).
    assert call["system_blocks"] is stash.system_blocks
    assert call["tools"] is stash.tools
    assert call["model"] == stash.model
    # Read-only: tools presented (cache-prefix preservation) but forbidden.
    assert call["tool_choice"] == {"type": "none"}
    assert call["tool_dispatch"] is None
    assert call["max_iterations"] == 1
    # ADR-134: the spend keys into the same per-session pot as the narrator.
    assert call["session_id"] == "slug-1"
    assert call["caller"] == "aside"
    # The OOC contract + question ride the USER turn — the system blocks
    # must not change or the cache busts.
    [message] = call["messages"]
    assert message.role == "user"
    assert "PLAYER ASIDE: what day is it?" in message.content
    assert "OUT-OF-CHARACTER" in message.content


@pytest.mark.asyncio
async def test_malformed_json_degrades_to_resolver_error() -> None:
    client = _FakeToolingClient(result=_tooling_result("the model rambled, no JSON"))

    res, cache_read = await resolve_aside_on_narrator_cache(
        client=client,
        stash=_stash(),
        question="what day is it?",
        session_id="slug-1",
    )

    assert res.outcome == "resolver_error"
    assert res.answer  # loud, non-empty "ask again" message
    assert res.grounded_on == ()
    # The call DID happen — cache-read passthrough still reports honestly.
    assert cache_read == 27_000


@pytest.mark.asyncio
async def test_llm_call_failure_degrades_to_resolver_error_zero_tokens() -> None:
    client = _FakeToolingClient(exc=TimeoutError("anthropic timed out"))

    res, cache_read = await resolve_aside_on_narrator_cache(
        client=client,
        stash=_stash(),
        question="what day is it?",
        session_id="slug-1",
    )

    assert res.outcome == "resolver_error"
    assert res.answer
    assert cache_read == 0
