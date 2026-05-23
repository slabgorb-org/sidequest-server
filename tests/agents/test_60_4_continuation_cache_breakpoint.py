"""Story 60-4 — moving 1h cache_control breakpoint on tool-loop continuation.

These tests are the structural-contract gate for the 60-4 fix. The behavioral
proof (continuation `cache_creation` lands in `ephemeral_1h_input_tokens` and
the next identical continuation reads it with `write=0`) was MEASURED by 60-3
against the live Anthropic API and isolated SDK replays — see
`sprint/archive/60-3-session.md` → "Dev Diagnosis (60-3 — FINAL)". The tests
here lock in the *client-side construction* that produces that measured
behavior, so a future refactor cannot regress the marker placement without
turning these red.

The fix lives at the continuation-append site in
``AnthropicSdkClient.complete_with_tools`` (after the loop body appends a fresh
``{role:"assistant", tool_use}`` + ``{role:"user", tool_result}`` pair, before
the next ``messages.create`` call). It adds a moving
``cache_control={"type":"ephemeral", "ttl": self.cache_ttl}`` marker on the
**last content block of the newest message** and clears any stale
message-level markers from prior iterations so total request breakpoints stay
≤ 4 (system_blocks[0] + tools array already use 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

# --- SDK-shape fakes -------------------------------------------------------


@dataclass(frozen=True)
class _CacheCreation:
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: _CacheCreation | None = None


@dataclass(frozen=True)
class _TextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _ToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeSdk: out of scripted responses")
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


def _end_turn(text: str = "ok") -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=50, output_tokens=4),
        model="claude-sonnet-4-6",
    )


def _tool_use(tool_id: str, name: str = "roll_dice") -> _Resp:
    return _Resp(
        content=[_ToolUseBlock(type="tool_use", id=tool_id, name=name, input={})],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=50, output_tokens=4),
        model="claude-sonnet-4-6",
    )


def _dispatch_seventeen(block: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)


def _tools_one() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="roll_dice",
            description="Roll",
            input_schema={"type": "object"},
        )
    ]


def _last_block_has_marker(message: dict[str, Any], expected_ttl: str) -> bool:
    """True iff message.content[-1] carries cache_control{ephemeral, expected_ttl}.

    Handles both the string-content shape (legacy initial user message) and
    the list-content shape (continuation tool_result / assistant tool_use).
    """
    content = message.get("content")
    if isinstance(content, str):
        return False
    if not isinstance(content, list) or not content:
        return False
    last = content[-1]
    if not isinstance(last, dict):
        return False
    return last.get("cache_control") == {"type": "ephemeral", "ttl": expected_ttl}


def _count_message_level_markers(messages: list[dict[str, Any]]) -> int:
    """Count cache_control markers across all content blocks of all messages."""
    n = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                n += 1
    return n


# --- AC-1 / AC-2 — marker on the continuation's newest message --------------


@pytest.mark.asyncio
async def test_continuation_user_message_carries_1h_cache_control_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1: the iter-2 messages.create payload carries a
    cache_control{ephemeral, 1h} marker on the LAST content block of the
    newly-appended user (tool_result) message.

    Measured in 60-3: this single placement flips the continuation's prefix
    write from `ephemeral_5m_input_tokens` to `ephemeral_1h_input_tokens`,
    which is the entire savings of the story (~$0.08/turn).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_tool_use("toolu_1"), _end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll it")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
    )

    assert len(sdk.messages.calls) == 2, (
        f"expected one continuation call (iter 2) after the iter-1 tool_use; "
        f"got {len(sdk.messages.calls)}"
    )
    continuation = sdk.messages.calls[1]
    messages = continuation["messages"]
    # The newest message on the continuation is the user/tool_result pair the
    # loop just appended.
    assert messages[-1]["role"] == "user", (
        f"newest continuation message must be the user/tool_result pair; "
        f"got role={messages[-1]['role']!r}"
    )
    assert _last_block_has_marker(messages[-1], expected_ttl="1h"), (
        "the LAST content block of the newest continuation message MUST carry "
        "cache_control={'type':'ephemeral','ttl':'1h'}. Without it, the API "
        "re-mints the ~11.7k cached prefix at the default 5m TTL on every "
        "continuation (measured: 60-3). Got messages[-1]="
        f"{messages[-1]!r}"
    )


@pytest.mark.asyncio
async def test_continuation_marker_ttl_matches_client_5m_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1: a 5m-configured client must mark the continuation at ttl:'5m'.

    The marker must echo `self.cache_ttl` — never hardcoded to 1h — so that
    operators who explicitly opt into the 5m window keep their configured
    behavior. (No silent upgrades, per CLAUDE.md "no silent fallback".)
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_tool_use("toolu_2"), _end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="5m")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll it")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
    )

    continuation = sdk.messages.calls[1]
    assert _last_block_has_marker(continuation["messages"][-1], expected_ttl="5m"), (
        "a 5m-configured client must emit cache_control{ttl:'5m'} on the "
        "continuation — the marker must echo self.cache_ttl, not be "
        "hardcoded to 1h. Got messages[-1]="
        f"{continuation['messages'][-1]!r}"
    )


@pytest.mark.asyncio
async def test_no_continuation_marker_on_single_iter_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 edge: a turn that ends without a tool_use must NOT add any
    message-level marker. The marker is a CONTINUATION construct — it only
    exists to cover the appended tool_use/tool_result blocks.

    Regression guard: if the fix accidentally marks the original user
    message on iter-1, every turn pays an extra breakpoint and the message
    bucket grows on rebates.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="say something")],
        tools=[],
        model="claude-sonnet-4-6",
    )

    assert len(sdk.messages.calls) == 1, "expected a single end_turn call"
    call = sdk.messages.calls[0]
    markers = _count_message_level_markers(call["messages"])
    assert markers == 0, (
        "a single-iter turn must carry zero message-level cache_control "
        f"markers (no continuation happened); got {markers}. "
        "Marking the initial user message wastes a breakpoint and risks "
        "tripping the 4-breakpoint cap once tools+system are counted."
    )


# --- AC-3 — stale-marker cleanup keeps total breakpoints ≤ 4 ----------------


@pytest.mark.asyncio
async def test_deep_tool_loop_keeps_one_message_level_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: across a 3-iter tool loop, the final messages.create call must
    carry AT MOST ONE message-level cache_control marker — on the newest
    message — even though earlier iterations attached their own markers.

    Anthropic rejects >4 cache_control markers on a single request. Two
    are already in use (system_blocks[0] + last tool entry). If the
    continuation marker accumulates across iterations, a deep tool loop
    (3-4 calls is real-world possible: extraction + dice + narration tool)
    will hit the cap and 400. The fix MUST clear stale message-level
    markers from prior iterations before adding the moving one.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(
        responses=[
            _tool_use("toolu_a"),
            _tool_use("toolu_b"),
            _end_turn("done"),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll twice")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
    )

    assert len(sdk.messages.calls) == 3, (
        f"expected three calls (iter 1 tool_use, iter 2 tool_use, iter 3 "
        f"end_turn); got {len(sdk.messages.calls)}"
    )
    final = sdk.messages.calls[2]
    markers = _count_message_level_markers(final["messages"])
    assert markers <= 1, (
        f"deep tool loop must carry AT MOST one message-level cache_control "
        f"marker on the final call (combined with the 2 system+tools markers "
        f"that's ≤ 3, safe under the 4-cap); got {markers}. Stale-marker "
        f"cleanup failed — the fix is missing or appending without removing "
        f"prior continuation markers."
    )


@pytest.mark.asyncio
async def test_marker_migrates_to_newest_message_across_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 detail: the marker doesn't just stay capped — it MOVES to the
    newest message on each continuation. Verifies the moving-breakpoint
    contract: the iter-2 call marks the iter-1 tool_result; the iter-3 call
    marks the iter-2 tool_result and the iter-1 tool_result is clean.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(
        responses=[
            _tool_use("toolu_a"),
            _tool_use("toolu_b"),
            _end_turn("done"),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll twice")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
    )

    # iter-2: marker is on the iter-1 tool_result (last user message)
    iter2 = sdk.messages.calls[1]
    assert _last_block_has_marker(iter2["messages"][-1], expected_ttl="1h"), (
        "iter-2 call must mark the newly-appended iter-1 tool_result"
    )

    # iter-3: marker is on the iter-2 tool_result; the iter-1 one is clean.
    iter3 = sdk.messages.calls[2]
    assert _last_block_has_marker(iter3["messages"][-1], expected_ttl="1h"), (
        "iter-3 call must mark the newly-appended iter-2 tool_result"
    )

    # The iter-1 tool_result (now older) must have had its marker cleared.
    # In the iter-3 payload the iter-1 tool_result is messages[-3] (initial
    # user + 2× assistant+user pairs minus the latest user = idx -3).
    older_user_messages = [
        m for m in iter3["messages"][:-1] if m.get("role") == "user"
    ]
    for m in older_user_messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                assert "cache_control" not in block, (
                    f"older user message still carries a stale cache_control "
                    f"marker on iter-3 — stale-marker cleanup did not run. "
                    f"This will trip the 4-breakpoint cap on a 4+ iter loop. "
                    f"Got block={block!r}"
                )


# --- Wiring: the marker still propagates with the 1h beta header ------------


@pytest.mark.asyncio
async def test_continuation_call_still_carries_extended_cache_ttl_beta_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1h marker on the continuation is rejected by Anthropic unless the
    `extended-cache-ttl-2025-04-11` beta header rides the request. The
    existing 1h path attaches it via `extra_headers` (lines 140-142 of
    anthropic_sdk_client.py); a refactor must not drop it on continuations.

    No silent fallback to 5m if the header is missing — that would mask the
    bug 60-4 fixes.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_tool_use("toolu_c"), _end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll it")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
    )

    for idx, call in enumerate(sdk.messages.calls):
        headers = call.get("extra_headers")
        assert headers is not None, (
            f"iter-{idx + 1} 1h call is missing extra_headers entirely; "
            f"the extended-cache-ttl beta would be silently dropped"
        )
        assert headers.get("anthropic-beta") == "extended-cache-ttl-2025-04-11", (
            f"iter-{idx + 1} call must carry the extended-cache-ttl-2025-04-11 "
            f"beta header so 1h markers (including the new continuation one) "
            f"are accepted by the API; got {headers!r}"
        )
