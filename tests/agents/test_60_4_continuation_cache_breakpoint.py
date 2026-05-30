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
async def test_continuation_user_message_carries_volatile_5m_cache_control_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 (TIER SUPERSEDED BY 61-19): the iter-2 messages.create payload
    carries a cache_control marker on the LAST content block of the
    newly-appended user (tool_result) message — at the VOLATILE 5m tier.

    60-4 originally placed this marker at 1h to keep the continuation from
    re-minting the stable prefix at 5m. Story 61-19 (2026-05-30) proved via
    empirical probe (`probe_61_19_cache_tier.py`) that the marker's TTL can
    drop to 5m WITHOUT re-minting the prefix: the stable system prefix has
    its OWN 1h breakpoint (`system_blocks[0]`), so on warm turns it still
    reads at 1h (`1h_write=0`, `cache_read~=prefix`) while only the volatile
    per-turn tail writes — now at the cheaper 5m tier (1.25x vs 2x). The
    marker's PRESENCE prevents the re-mint; its TTL only sets the tail tier.
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
    assert _last_block_has_marker(messages[-1], expected_ttl="5m"), (
        "the LAST content block of the newest continuation message MUST carry "
        "cache_control={'type':'ephemeral','ttl':'5m'} (61-19 volatile tier). "
        "The marker's PRESENCE prevents the 60-3 prefix re-mint; 61-19's probe "
        "proved the prefix still reads at 1h via its own breakpoint while this "
        "volatile tail writes at the cheaper 5m tier. Got messages[-1]="
        f"{messages[-1]!r}"
    )


@pytest.mark.asyncio
async def test_continuation_splits_tiers_volatile_5m_message_over_1h_system_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 (61-19 tier split — the non-trivial invariant). On a **1h**-
    configured client, a continuation call MUST emit the 61-19 tier split:
    the volatile message tail at ttl:'5m' while the STABLE system prefix
    stays at ttl:'1h'.

    Why this replaces the old `..._matches_client_5m_configuration` test:
    after 61-19 the message marker is the hardcoded `_VOLATILE_CACHE_TTL`
    (5m) regardless of `self.cache_ttl`, so a 5m-client/5m-assert test passed
    trivially (5m==5m) and could no longer catch a regression that reverted
    the message marker to `self.cache_ttl`. This 1h-client test IS that
    regression guard: if the message marker ever echoes `self.cache_ttl`
    again, it would read '1h' here and fail. It also pins that the split is
    real — system prefix 1h, message tail 5m, in the same request.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_tool_use("toolu_2"), _end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll it")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
    )

    continuation = sdk.messages.calls[1]
    # Volatile message tail → 5m (NOT self.cache_ttl, which is 1h here).
    assert _last_block_has_marker(continuation["messages"][-1], expected_ttl="5m"), (
        "61-19: the continuation's newest (tool_result) message MUST carry "
        "cache_control{ttl:'5m'} even on a 1h-configured client — the volatile "
        "tier is hardcoded, not echoing self.cache_ttl. A '1h' here means the "
        "message marker regressed to self.cache_ttl. Got messages[-1]="
        f"{continuation['messages'][-1]!r}"
    )
    # Stable system prefix → still 1h (the amortizing half is untouched).
    sys_block = continuation["system"][0]
    assert sys_block.get("cache_control") == {"type": "ephemeral", "ttl": "1h"}, (
        "61-19: the stable system prefix MUST keep ttl:'1h' on a 1h client — "
        "only the volatile message tail moved to 5m. Got system[0]="
        f"{sys_block!r}"
    )


@pytest.mark.asyncio
async def test_single_iter_turn_marks_initial_user_message_at_volatile_5m_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 (60-7 amendment; TIER SUPERSEDED BY 61-19). A turn that ends
    without a tool_use MUST mark the initial user message's last content
    block on iter=1 — at the VOLATILE 5m tier (61-19), NOT `self.cache_ttl`.

    60-7 (2026-05-24) added the iter=1 marker (the prior assertion "zero
    message-level markers" codified the auto-5m-then-1h-displacement bug).
    61-19 (2026-05-30) then proved the marker should be 5m, not 1h: the user
    message is volatile (it changes every turn), so the 1h tier's 2x premium
    is wasted on it. The marker still EXISTS (overriding Anthropic's auto-5m
    default and pinning the tail to a single explicit 5m write per turn); only
    its TTL value changed. The stable prefix keeps 1h via `system_blocks[0]`.

    The 4-breakpoint budget still holds: system_blocks[0] + tools[-1] +
    iter=1 user message = 3 markers on a single-iter turn (safe).
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
    assert _last_block_has_marker(call["messages"][-1], expected_ttl="5m"), (
        "a single-iter turn MUST mark the newest user message's last content "
        "block with cache_control={'type':'ephemeral','ttl':'5m'} (61-19 "
        "volatile tier). The marker's presence pins the volatile tail to one "
        "explicit 5m write/turn (overriding Anthropic's auto-5m default); its "
        f"TTL is 5m, not 1h. Got messages[-1]={call['messages'][-1]!r}"
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

    # iter-2: marker is on the iter-1 tool_result (last user message), at the
    # 61-19 volatile 5m tier.
    iter2 = sdk.messages.calls[1]
    assert _last_block_has_marker(iter2["messages"][-1], expected_ttl="5m"), (
        "iter-2 call must mark the newly-appended iter-1 tool_result (5m tier)"
    )

    # iter-3: marker is on the iter-2 tool_result; the iter-1 one is clean.
    iter3 = sdk.messages.calls[2]
    assert _last_block_has_marker(iter3["messages"][-1], expected_ttl="5m"), (
        "iter-3 call must mark the newly-appended iter-2 tool_result (5m tier)"
    )

    # The iter-1 tool_result (now older) must have had its marker cleared.
    # In the iter-3 payload the iter-1 tool_result is messages[-3] (initial
    # user + 2× assistant+user pairs minus the latest user = idx -3).
    older_user_messages = [m for m in iter3["messages"][:-1] if m.get("role") == "user"]
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
