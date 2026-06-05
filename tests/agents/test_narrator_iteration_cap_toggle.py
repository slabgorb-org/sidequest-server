"""Story 82-9 (RED) — operator toggle for the narrator iteration_cap (AC4).

71-40 added the ``iteration_cap`` kwarg to ``complete_with_tools`` but left it
with NO production caller — no operator can switch it on without a code change
(Reviewer Gap finding). This story adds an operator toggle
(``SIDEQUEST_NARRATOR_ITERATION_CAP``) that resolves the cap and is forwarded by
the narrator turn path into ``complete_with_tools``.

Fail-loud discipline (CLAUDE.md "No Silent Fallbacks", mirroring the
``SIDEQUEST_SESSION_COST_CEILING_USD`` parser): unset → ``None`` (no cap);
a valid positive int → that cap; a non-int or non-positive value RAISES rather
than silently disabling the throttle.

Coverage:
* ``resolve_narrator_iteration_cap`` env-parsing contract (unset / valid /
  invalid / non-positive);
* a REAL narrator turn (``Orchestrator.run_narration_turn``) with the env set
  fires ``narrator.tool_loop.cap_hit`` — proving the toggle reaches the
  production ``complete_with_tools`` call site, not just a standalone parser.

RED: ``resolve_narrator_iteration_cap`` does not exist and the orchestrator never
sets ``iteration_cap``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.agents.tools  # noqa: F401 — wires tool adapters onto default_registry
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock

_ENV = "SIDEQUEST_NARRATOR_ITERATION_CAP"
_CAP_HIT_SPAN = "narrator.tool_loop.cap_hit"


@pytest.fixture(autouse=True)
def _clear_iteration_cap_cache():
    """Story 82-11: the resolver is memoized (``functools.cache``) because a
    running server's env is fixed at boot. Tests in this file mutate the env
    per-test, so the cache must be cleared around each one — before (so a
    value cached by an earlier test in the worker doesn't leak in) and after
    (so this file's env values don't leak out to other narrator-turn tests)."""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    resolve_narrator_iteration_cap.cache_clear()
    yield
    resolve_narrator_iteration_cap.cache_clear()


# --- AC4a: the env-parsing contract -----------------------------------------


def test_resolve_iteration_cap_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset toggle → ``None`` (no cap): the default behavior is unchanged, the
    loop runs to the hard ``max_iterations`` ceiling with no soft cap."""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    monkeypatch.delenv(_ENV, raising=False)
    assert resolve_narrator_iteration_cap() is None


def test_resolve_iteration_cap_valid_positive_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid positive int is returned as the cap the narrator forwards."""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    monkeypatch.setenv(_ENV, "5")
    assert resolve_narrator_iteration_cap() == 5


def test_resolve_iteration_cap_non_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer value RAISES rather than silently disabling the cap — a
    typo'd toggle must fail loud, not vanish (No Silent Fallbacks)."""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    monkeypatch.setenv(_ENV, "lots")
    with pytest.raises(ValueError):
        resolve_narrator_iteration_cap()


@pytest.mark.parametrize("bad", ["0", "-3"])
def test_resolve_iteration_cap_non_positive_raises(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A non-positive cap is meaningless (a cap of 0 would "throttle" before the
    first iteration) — it must raise, not silently clamp. Mirrors the cost-
    ceiling parser's ``parsed <= 0`` rejection."""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    monkeypatch.setenv(_ENV, bad)
    with pytest.raises(ValueError):
        resolve_narrator_iteration_cap()


# --- Story 82-11: parse-once memoization contract ----------------------------


def test_resolve_iteration_cap_is_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 82-11 AC1: the resolver parses the env ONCE and caches. A
    subsequent env mutation in the same process does NOT change the resolved
    value — the toggle is operator/startup config, not a per-turn input. (In a
    real server the env never changes post-boot; this test pins the parse-once
    semantics so a future edit doesn't silently reintroduce the per-turn read.)"""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    monkeypatch.setenv(_ENV, "5")
    assert resolve_narrator_iteration_cap() == 5
    monkeypatch.setenv(_ENV, "9")
    assert resolve_narrator_iteration_cap() == 5, (
        "memoized resolver must not re-read the env on a later call"
    )
    # cache_clear() is the explicit re-read seam (used by tests/tooling only).
    resolve_narrator_iteration_cap.cache_clear()
    assert resolve_narrator_iteration_cap() == 9


def test_resolve_iteration_cap_invalid_value_raises_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 82-11 AC2: ``functools.cache`` does not cache exceptions, so an
    invalid value fails loud on EVERY call (not one-shot) — the typo cannot
    vanish after first raise (No Silent Fallbacks)."""
    from sidequest.agents.narrator import resolve_narrator_iteration_cap

    monkeypatch.setenv(_ENV, "lots")
    with pytest.raises(ValueError):
        resolve_narrator_iteration_cap()
    with pytest.raises(ValueError):
        resolve_narrator_iteration_cap()


# --- AC4b: the toggle reaches the real narrator call site --------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: Any = None


@dataclass
class _TextBlock:
    type: str
    text: str


@dataclass
class _ToolUseSdkBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


def _tool_use_resp() -> _Resp:
    return _Resp(
        content=[_ToolUseSdkBlock(type="tool_use", id="t", name="roll_dice", input={"sides": 20})],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=30, output_tokens=4),
        model="claude-sonnet-4-6",
    )


def _text_resp() -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text="The dice settle.")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=30, output_tokens=8),
        model="claude-sonnet-4-6",
    )


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


@pytest.mark.asyncio
async def test_env_toggle_drives_cap_hit_on_real_narrator_turn(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """Wiring: with ``SIDEQUEST_NARRATOR_ITERATION_CAP=2`` set, a real narrator
    turn that requests a tool twice before converging (iterations 1,2,3) must
    fire a ``narrator.tool_loop.cap_hit`` span. That span only fires when the
    cap reaches ``complete_with_tools`` — so its presence proves the operator
    toggle is wired end-to-end through ``run_narration_turn``, not stranded in a
    parser."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(_ENV, "2")

    # tool, tool, text → 3 iterations; cap=2 is crossed on iteration 2.
    sdk = _Sdk(responses=[_tool_use_resp(), _tool_use_resp(), _text_resp()])
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="11", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(self: Orchestrator, action: str, context: TurnContext):
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kael", genre="caverns_and_claudes", turn_number=3)
    await orch.run_narration_turn("roll to climb", ctx)

    cap_hits = [s for s in otel_capture.get_finished_spans() if s.name == _CAP_HIT_SPAN]
    assert cap_hits, (
        "with the operator toggle set, the narrator turn must forward iteration_cap "
        "into complete_with_tools — the cap_hit span proves the wiring; got none"
    )
    attrs = dict(cap_hits[0].attributes or {})
    assert attrs.get("iteration_cap") == 2, (
        f"the cap_hit span must record the operator's cap value (2); got {attrs}"
    )


@pytest.mark.asyncio
async def test_no_toggle_means_no_cap_hit_on_real_narrator_turn(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """Negative wiring: with the toggle UNSET, the same tool-heavy turn fires NO
    cap_hit span — the narrator forwards ``iteration_cap=None``, so the soft cap
    stays off and the signal remains meaningful (it only appears when an operator
    opted in)."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(_ENV, raising=False)

    sdk = _Sdk(responses=[_tool_use_resp(), _tool_use_resp(), _text_resp()])
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="11", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(self: Orchestrator, action: str, context: TurnContext):
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kael", genre="caverns_and_claudes", turn_number=3)
    await orch.run_narration_turn("roll to climb", ctx)

    cap_hits = [s for s in otel_capture.get_finished_spans() if s.name == _CAP_HIT_SPAN]
    assert not cap_hits, (
        "with no operator toggle, no cap is forwarded and no cap_hit span should "
        f"fire; got {len(cap_hits)}"
    )
