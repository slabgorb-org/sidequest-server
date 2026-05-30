"""Story 60-7 — iter=1 cache_control marker on the newest user message.

These tests are the RED-phase gate for the 60-7 fix. The fix lives in
``AnthropicSdkClient._build_messages_payload`` (sidequest-server/sidequest/
agents/anthropic_sdk_client.py lines 897-928): the ``is_continuation`` guard
that short-circuits before adding a marker on iter=1 must be dropped, so the
newest user message's last content block carries
``cache_control={"type":"ephemeral", "ttl": self.cache_ttl}`` on every iter
(including iter=1).

Background — empirically measured on probe ``probe/60-7-single-iter-prose``
(commits 11e3b6e + 1b70578), live save
``~/.sidequest/saves/games/2026-05-24-coyote_star/save.db``, server PID 19391:

- Steady-state pre-fix: iter=1 ALWAYS writes ~17K to ``ephemeral_5m`` because
  the user message + recency-zone deltas (~17K tok) sit AFTER the
  1h-marked system_blocks+tools prefix with no explicit cache_control marker,
  so Anthropic auto-caches the tail at the default 5m TTL.
- Steady-state pre-fix: iter=2 then writes the same content at 1h via the
  60-4 continuation marker, displacing the 5m write seconds later — pure
  waste.
- Post-fix probe: per-turn cost dropped from $0.137 → $0.096 (30% savings);
  iter=2 ``cache_write_5m`` collapsed from ~13K to <350 tok.

Source diff to consult (DO NOT cherry-pick; carries the option-A prompt
banner that's been ruled out): commit ``1b70578`` on probe branch.

Also covers AC-5 OTEL: ``narrator.cache.both_writes_fired`` WARN watcher
event that fires whenever a single iter reports
``cache_write_5m > 0 AND cache_write_1h > 0`` — the lie-detector for
future regressions of this class.
"""

from __future__ import annotations

import asyncio
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
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub

# --- SDK-shape fakes (mirror tests/agents/test_60_4_continuation_cache_breakpoint.py) ---


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


def _end_turn(
    text: str = "ok",
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
) -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=50,
            output_tokens=4,
            cache_read_input_tokens=cache_read,
            cache_creation=_CacheCreation(
                ephemeral_5m_input_tokens=cache_write_5m,
                ephemeral_1h_input_tokens=cache_write_1h,
            ),
        ),
        model="claude-sonnet-4-6",
    )


def _tool_use(
    tool_id: str,
    name: str = "roll_dice",
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> _Resp:
    return _Resp(
        content=[_ToolUseBlock(type="tool_use", id=tool_id, name=name, input={})],
        stop_reason="tool_use",
        usage=_Usage(
            input_tokens=50,
            output_tokens=4,
            cache_creation=_CacheCreation(
                ephemeral_5m_input_tokens=cache_write_5m,
                ephemeral_1h_input_tokens=cache_write_1h,
            ),
        ),
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


class _FakeSocket:
    """Minimal `_Sendable` for watcher_hub subscription. Collects every
    published event so tests can assert delivery to the GM-panel transport
    (not just `logger.warning`). Same pattern as 61-3 / 61-4 tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test event loop and clear subscribers.
    Mirrors `tests/agents/test_61_4_cost_runaway_alarm.py::bound_hub`."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


# --- AC-3 / AC-6 — iter=1 cache_control marker (the fix) -------------------


def test_build_messages_payload_marks_iter1_user_message_at_volatile_5m() -> None:
    """AC-3 / AC-6 (direct, unit; TIER SUPERSEDED BY 61-19). Calling
    `_build_messages_payload` with `is_continuation=False` (the iter=1 path)
    on a 1h-configured client MUST return a payload where the LAST content
    block of the LAST message carries
    `cache_control={"type": "ephemeral", "ttl": "5m"}` — the 61-19 volatile
    tier, NOT 1h.

    Why the marker exists (60-7): Anthropic auto-caches content past the last
    explicit breakpoint at the default 5m. Leaving the iter=1 tail unmarked
    let the API auto-mint it, and the 60-4 continuation marker then displaced
    it — burning a write. An explicit marker pins the tail to one write/turn.

    Why the TTL is 5m, not 1h (61-19, 2026-05-30): the user-message tail is
    VOLATILE (changes every turn), so 1h's 2x premium is wasted on it. An
    empirical probe confirmed the stable prefix still reads at 1h via its own
    `system_blocks[0]` breakpoint while only this tail writes — at 5m. The
    marker's PRESENCE is the 60-7 fix; its TTL VALUE is the 61-19 fix.
    """
    sdk = _Sdk(responses=[])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "say something"}]}
    ]

    payload = client._build_messages_payload(  # noqa: SLF001 — testing the helper directly
        running_messages,
        is_continuation=False,
    )

    assert payload, "payload must not be empty"
    last_msg = payload[-1]
    content = last_msg.get("content")
    assert isinstance(content, list) and content, (
        f"newest message must carry a non-empty list content; got {content!r}"
    )
    last_block = content[-1]
    assert isinstance(last_block, dict), (
        f"newest message's last content block must be a dict; got {last_block!r}"
    )
    assert last_block.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}, (
        "iter=1 (is_continuation=False) MUST mark the newest user message's "
        "last content block with cache_control={'type':'ephemeral','ttl':'5m'} "
        "(61-19 volatile tier) — the marker overrides Anthropic's auto-5m "
        "default and pins the volatile tail to a single 5m write/turn, while "
        "the stable prefix keeps 1h via system_blocks[0]. "
        f"Got last_block={last_block!r}."
    )


def test_build_messages_payload_marks_iter1_at_configured_5m_ttl() -> None:
    """AC-3 / AC-6. A 5m-configured client MUST mark iter=1 at ttl:'5m'
    (echoes `self.cache_ttl` verbatim — no silent upgrade to 1h, no
    hardcode). Mirrors the 60-4 `test_continuation_marker_ttl_matches_client_5m_configuration`
    discipline: the marker echoes the configured TTL, period.
    """
    sdk = _Sdk(responses=[])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="5m")

    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]}
    ]

    payload = client._build_messages_payload(  # noqa: SLF001
        running_messages,
        is_continuation=False,
    )

    last_block = payload[-1]["content"][-1]
    assert last_block.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}, (
        "5m-configured client must emit cache_control{ttl:'5m'} on iter=1 — "
        "no silent upgrade to 1h, no hardcoded value. "
        f"Got last_block={last_block!r}."
    )


def test_build_messages_payload_promotes_bare_string_content_to_block_list() -> None:
    """AC-6 (bare-string promotion path). When the newest message's
    `content` is a bare string (the legacy initial-user-message shape),
    `_build_messages_payload(is_continuation=False)` MUST promote it to a
    single-text-block list with the cache_control marker attached to that
    block.

    Why: `cache_control` is a content-block attribute. Bare strings have no
    block-level addressable structure, so the marker has nowhere to land.
    Promotion to `[{"type":"text","text":<the-string>, "cache_control":{...}}]`
    is the only way to keep the legacy entry shape live without silently
    dropping the marker.

    Regression guard for the bare-string promotion path introduced by 60-7.
    See the companion `test_..._bare_string_at_5m_ttl` test for the
    TTL-echo half of the same path.
    """
    sdk = _Sdk(responses=[])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "say something"},
    ]

    payload = client._build_messages_payload(  # noqa: SLF001
        running_messages,
        is_continuation=False,
    )

    last_msg = payload[-1]
    content = last_msg.get("content")
    assert isinstance(content, list), (
        "bare-string content on the newest message MUST be promoted to a list "
        "so cache_control has a content-block to attach to. "
        f"Got content={content!r} (type={type(content).__name__})."
    )
    assert len(content) == 1, (
        f"bare-string promotion should produce exactly one text block; got {content!r}"
    )
    block = content[0]
    assert isinstance(block, dict), f"promoted block must be a dict; got {block!r}"
    assert block.get("type") == "text", (
        f"promoted block must carry type='text'; got {block!r}"
    )
    assert block.get("text") == "say something", (
        f"promoted block must preserve the original string; got text={block.get('text')!r}"
    )
    assert block.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}, (
        "promoted text block MUST carry cache_control{'type':'ephemeral','ttl':'5m'} "
        f"on iter=1 (61-19 volatile tier); got {block!r}"
    )


def test_build_messages_payload_promotes_bare_string_at_5m_ttl() -> None:
    """AC-6 (bare-string promotion path) — TTL-echo half. A 5m-configured
    client with bare-string content on the newest user message MUST promote
    the string to a single text block AND attach cache_control with
    `ttl: '5m'` — not hardcoded `'1h'`.

    Why this test exists: the companion `..._bare_string_content_to_block_list`
    test only covers `cache_ttl='1h'`. If the promotion path hardcodes the
    TTL on the marker (instead of echoing `self.cache_ttl`), the existing
    `..._marks_iter1_at_configured_5m_ttl` test would still pass because it
    uses list-content (which exercises a different code path than the
    bare-string promotion). This test closes the seam.
    """
    sdk = _Sdk(responses=[])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="5m")

    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
    ]

    payload = client._build_messages_payload(  # noqa: SLF001
        running_messages,
        is_continuation=False,
    )

    last_msg = payload[-1]
    content = last_msg.get("content")
    assert isinstance(content, list) and len(content) == 1, (
        "bare-string content MUST promote to a single-text-block list on the "
        f"5m path too; got content={content!r}"
    )
    block = content[0]
    assert isinstance(block, dict), f"promoted block must be a dict; got {block!r}"
    assert block.get("text") == "go", (
        f"promoted block must preserve the original string; got text={block.get('text')!r}"
    )
    assert block.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}, (
        "5m-configured client MUST emit cache_control{ttl:'5m'} on the "
        "promoted bare-string block — no hardcoded '1h', no silent upgrade. "
        f"Got block={block!r}"
    )


# --- AC-3 / AC-6 — integration through `complete_with_tools` ---------------


@pytest.mark.asyncio
async def test_single_iter_turn_carries_iter1_volatile_5m_cache_control_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 / AC-6 (integration; TIER SUPERSEDED BY 61-19). A turn that ends
    without a tool_use (single `messages.create` call, iter=1 only) MUST send
    a payload whose newest user message's last content block carries
    `cache_control={'type':'ephemeral','ttl':'5m'}` (the 61-19 volatile tier)
    on a 1h-configured client.

    This is the inversion of 60-4's
    `test_no_continuation_marker_on_single_iter_turn` (which was edited in
    the same RED phase to flip its assertion). That earlier test codified
    the bug ("a single-iter turn must carry zero message-level
    cache_control markers"); the 60-7 probe proved the unmarked tail costs
    a wasted 5m write per turn.
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
    messages = call["messages"]
    last_msg = messages[-1]
    content = last_msg.get("content")
    assert isinstance(content, list) and content, (
        "single-iter turn must produce a list-content newest message (bare "
        "strings get promoted by _build_messages_payload). "
        f"Got content={content!r}"
    )
    last_block = content[-1]
    assert isinstance(last_block, dict), (
        f"newest message's last content block must be a dict; got {last_block!r}"
    )
    assert last_block.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}, (
        "single-iter turn (iter=1 only) MUST carry cache_control "
        "{'type':'ephemeral','ttl':'5m'} on the newest user message's last "
        "content block (61-19 volatile tier). The marker's presence overrides "
        "the API auto-5m default and pins one 5m write/turn; the stable prefix "
        "keeps 1h via system_blocks[0]. "
        f"Got last_block={last_block!r}."
    )


@pytest.mark.asyncio
async def test_continuation_still_carries_marker_on_final_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 + 60-4 compat. Adding the iter=1 marker MUST NOT break the
    iter=2 marker that 60-4 added. The continuation call's newest user
    message (the tool_result append) still carries the 1h marker on its
    last content block.

    Regression guard: a naive 'add a marker on iter=1' refactor that
    forgets to keep the iter=2+ behavior would silently undo 60-4.
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
        f"expected iter-1 tool_use then iter-2 end_turn; got {len(sdk.messages.calls)} calls"
    )
    continuation = sdk.messages.calls[1]
    messages = continuation["messages"]
    assert messages[-1]["role"] == "user", (
        f"newest continuation message must be the appended user/tool_result; "
        f"got role={messages[-1]['role']!r}"
    )
    last_block = messages[-1]["content"][-1]
    assert isinstance(last_block, dict), (
        f"continuation newest-message last block must be a dict; got {last_block!r}"
    )
    assert last_block.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}, (
        "continuation marker MUST still land on iter=2's newest message "
        "(61-19 volatile 5m tier) after 60-7 adds the iter=1 marker; "
        f"got {last_block!r}"
    )


@pytest.mark.asyncio
async def test_iter1_marker_does_not_inflate_total_breakpoint_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 / 60-4 compat. Adding the iter=1 marker on the newest user
    message MUST keep the total per-request cache_control breakpoint count
    inside Anthropic's hard cap of 4.

    Budget on a 2-iter turn:
      - system_blocks[0]:                            1 breakpoint
      - tools[-1] (cached):                          1 breakpoint
      - iter=1 user message tail (60-7):             1 breakpoint
      - iter=2 newest tool_result message (60-4):    1 breakpoint
      = 4. At the cap, not over.

    This test asserts the iter=1 payload carries AT MOST ONE message-level
    marker (no stale markers from prior fake-history bleed into the
    iter=1 call), and the iter=2 payload still carries AT MOST ONE
    message-level marker (stale-marker cleanup from 60-4 still works).
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
        f"expected three calls (iter-1 + iter-2 tool_use, iter-3 end_turn); "
        f"got {len(sdk.messages.calls)}"
    )

    def _count_markers(call: dict[str, Any]) -> int:
        n = 0
        for m in call["messages"]:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    n += 1
        return n

    iter1_markers = _count_markers(sdk.messages.calls[0])
    iter2_markers = _count_markers(sdk.messages.calls[1])
    final_markers = _count_markers(sdk.messages.calls[2])

    assert iter1_markers == 1, (
        f"iter=1 payload must carry exactly one message-level cache_control "
        f"marker (on the newest user message); got {iter1_markers}."
    )
    # AC-3 / 4-cap guard (added in 60-7 review): the iter=2 payload sits
    # mid-loop between the iter=1 stamp and the iter=3 newest-message
    # stamp. If stale-marker cleanup misses iter=1's marker before the
    # iter=2 newest-message stamp lands, the iter=2 payload carries 2
    # message-level markers (combined with system_blocks[0] + tools[-1]
    # = 4 — exactly at Anthropic's hard cap, no headroom). Counting only
    # iter=1 and the final iter misses this exact regression mode.
    assert iter2_markers <= 1, (
        f"iter=2 payload must carry AT MOST one message-level cache_control "
        f"marker. A count > 1 means stale-marker cleanup failed before the "
        f"iter=2 newest-message stamp landed — combined with system+tools "
        f"that pushes the request to or past Anthropic's 4-breakpoint cap. "
        f"Got {iter2_markers}."
    )
    assert final_markers <= 1, (
        f"final iter payload must carry at most one message-level marker "
        f"(stale-marker cleanup must remove prior continuation markers); "
        f"got {final_markers}. If >1, the 60-4 cleanup regressed when "
        f"the 60-7 iter=1 marker was added."
    )


# --- AC-5 — `narrator.cache.both_writes_fired` WARN watcher event ---------


@pytest.mark.asyncio
async def test_both_writes_fired_event_emits_when_5m_and_1h_both_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-5 (lie-detector). When a single iter reports
    `cache_creation.ephemeral_5m_input_tokens > 0` AND
    `cache_creation.ephemeral_1h_input_tokens > 0`, the client MUST
    publish exactly one `narrator.cache.both_writes_fired` watcher event
    with `severity="warn"`.

    Rationale: this is the lie-detector signature for "the cache fix isn't
    working." A healthy turn writes to exactly one tier per iter (the
    iter=1 write at 1h, then iter=2 reads it). Both > 0 in a single iter
    means a breakpoint defaulted to 5m while another explicit 1h marker
    fired on overlapping content — the same waste pattern 60-7 fixed. If
    this ever fires post-60-7, a regression of the same class is live.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # Synthesize the pre-60-7 steady-state shape: iter=1 writes ~17K at 5m
    # AND ~17K at 1h (the exact double-billing pattern from probe evidence).
    sdk = _Sdk(responses=[_end_turn("done", cache_write_5m=17_000, cache_write_1h=17_000)])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="say something")],
        tools=[],
        model="claude-sonnet-4-6",
        session_id="60-7-lie-detector-test",
    )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "narrator.cache.both_writes_fired"]
    assert len(events) == 1, (
        "Exactly one narrator.cache.both_writes_fired event must reach "
        f"watcher subscribers when a single iter writes both 5m AND 1h; got "
        f"{len(events)}. All events: {[e.get('event_type') for e in sock.events]}"
    )
    event = events[0]
    assert event.get("severity") == "warn", (
        "narrator.cache.both_writes_fired MUST use severity='warn' (lie-"
        "detector, not hard error — the call already succeeded; the "
        f"observation is the waste). Got severity={event.get('severity')!r}."
    )
    fields = event.get("fields", {})
    for key in ("iteration", "cache_write_5m_tokens", "cache_write_1h_tokens", "model"):
        assert key in fields, (
            f"GM panel needs '{key}' on narrator.cache.both_writes_fired "
            f"fields; got fields={list(fields)}."
        )
    assert fields["cache_write_5m_tokens"] == 17_000, fields
    assert fields["cache_write_1h_tokens"] == 17_000, fields


@pytest.mark.asyncio
async def test_both_writes_fired_event_does_not_emit_when_only_one_tier_writes(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-5 negative case. A healthy turn writes to exactly one tier per
    iter. The watcher event MUST NOT fire when one of the writes is zero.

    Three scripted shapes — each should produce zero events:
      (a) cold start: 1h-only on a fresh prefix
      (b) cache-read-only: both writes zero (cache hit)
      (c) 5m-only: a 5m-configured client writing only to 5m
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # (a) 1h-only write
    sdk_1h = _Sdk(responses=[_end_turn("done", cache_write_5m=0, cache_write_1h=18_000)])
    client_1h = AnthropicSdkClient(sdk=sdk_1h, cache_ttl="1h")
    await client_1h.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="cold")],
        tools=[],
        model="claude-sonnet-4-6",
        session_id="60-7-negative-1h",
    )

    # (b) cache-read-only (both writes zero)
    sdk_read = _Sdk(responses=[_end_turn("done", cache_write_5m=0, cache_write_1h=0, cache_read=11_988)])
    client_read = AnthropicSdkClient(sdk=sdk_read, cache_ttl="1h")
    await client_read.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="warm")],
        tools=[],
        model="claude-sonnet-4-6",
        session_id="60-7-negative-read",
    )

    # (c) 5m-only write on a 5m-configured client
    sdk_5m = _Sdk(responses=[_end_turn("done", cache_write_5m=12_000, cache_write_1h=0)])
    client_5m = AnthropicSdkClient(sdk=sdk_5m, cache_ttl="5m")
    await client_5m.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="5m mode")],
        tools=[],
        model="claude-sonnet-4-6",
        session_id="60-7-negative-5m",
    )

    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "narrator.cache.both_writes_fired"]
    assert len(events) == 0, (
        "narrator.cache.both_writes_fired MUST NOT emit when only one cache "
        "tier writes (or when neither writes — a cache hit). Got "
        f"{len(events)} false-positive event(s); all event types: "
        f"{[e.get('event_type') for e in sock.events]}"
    )


@pytest.mark.asyncio
async def test_both_writes_fired_event_emits_per_offending_iter_in_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-5 + AC-7 wiring. In a multi-iter tool loop, the watcher event
    MUST fire ONCE per iter that exhibits the double-write pattern (not
    aggregated to a single per-turn emit) — so the GM panel can see
    exactly which iteration in the loop is wasting writes.

    Setup: iter=1 reports double-write; iter=2 reports clean 1h-only.
    Expected: exactly one event from iter=1, none from iter=2.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(
        responses=[
            _tool_use("toolu_x", cache_write_5m=17_000, cache_write_1h=17_000),
            _end_turn("done", cache_write_5m=0, cache_write_1h=300),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model="claude-sonnet-4-6",
        session_id="60-7-per-iter",
    )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "narrator.cache.both_writes_fired"]
    assert len(events) == 1, (
        "Exactly one narrator.cache.both_writes_fired event must fire — one "
        "per offending iter, not aggregated. iter=1 was offending (17K+17K), "
        f"iter=2 was clean. Got {len(events)} events."
    )
    fields = events[0].get("fields", {})
    assert fields.get("iteration") == 1, (
        "The fired event MUST report iteration=1 (the offending iter), not "
        f"iteration=2 (the clean one). Got iteration={fields.get('iteration')!r}."
    )
