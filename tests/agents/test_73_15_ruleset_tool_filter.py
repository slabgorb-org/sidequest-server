"""RED tests for Story 73-15 — filter narrator tool_definitions by bound ruleset.

ADR-117 tightening. The narrator's advertised tool list (the
``default_registry.tool_definitions()`` call sites in ``orchestrator.py``,
~3904 diagnostic + ~4103 the real ``tools=`` array) currently exposes ALL
registered tools regardless of the pack's bound ruleset. So the WWN-only tools
(``commit_effort``, ``long_rest``, ``veterans_luck``) and CWN-only tools
(``adjust_system_strain``, ``stabilize_mortal_injury``) appear on EVERY pack,
including ``native`` ones. They self-guard fail-loud (handler raises
``ValueError`` when ``pack.rules.ruleset`` != the required slug), so this is a
*tightening*, not a correctness bug — the win is tool-budget + mis-attempt
avoidance: the narrator should not see tools it can only fail to use.

Desired contract (what these tests pin):

* ``Registry.tool_definitions(ruleset=<slug>)`` returns only tools whose
  declared ruleset is ``None`` (agnostic) OR ``== <slug>``.
* ``Registry.tool_definitions()`` / ``(ruleset=None)`` returns ALL tools —
  back-compat. Legacy/fixture/pack-less narration paths (``context.pack is
  None``) are unchanged, and the self-guard backstop still sees the full set.
* The ``@tool`` decorator carries a ``ruleset: str | None = None`` declaration;
  the five gated tools declare it (``"wwn"`` / ``"cwn"``). Filtering is
  DECLARATION-driven, not a hardcoded per-tool name allowlist duplicated from
  the tool modules (AC-4).
* The per-tool ``ValueError`` self-guard REMAINS as a backstop (AC-3).
* The production narration assembly path filters by ``context.pack.rules.ruleset``
  and emits a ``narrator.tools.ruleset_filter`` OTEL span (the GM-panel lie
  detector — CLAUDE.md OTEL Observability Principle).

All tests in this module FAIL until Story 73-15 is implemented: today the
decorator/`tool_definitions` reject the ``ruleset=`` kwarg (TypeError) and the
narration path advertises the gated tools on every pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

# Importing the tools package wires every adapter onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import (
    Registry,
    ToolCategory,
    ToolContext,
    ToolResult,
    default_registry,
    tool,
)
from sidequest.agents.tooling_protocol import ToolUseBlock

GATED_WWN = {"commit_effort", "long_rest", "veterans_luck"}
GATED_CWN = {"adjust_system_strain", "stabilize_mortal_injury"}
GATED_ALL = GATED_WWN | GATED_CWN


class _NoArgs(BaseModel):
    pass


def _names(defs: list[Any]) -> set[str]:
    return {d.name for d in defs}


def _sample_registry() -> Registry:
    """A fresh registry with one agnostic tool + one WWN tool + one CWN tool.

    The ``ruleset=`` kwarg on ``@tool`` does not exist until 73-15 ships — these
    decorator calls raise ``TypeError`` today, which is the RED signal.
    """
    reg = Registry()

    @tool(name="agnostic_read", description="x", category=ToolCategory.READ, registry=reg)
    async def _a(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    @tool(
        name="wwn_only",
        description="x",
        category=ToolCategory.WRITE,
        registry=reg,
        ruleset="wwn",
    )
    async def _w(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    @tool(
        name="cwn_only",
        description="x",
        category=ToolCategory.WRITE,
        registry=reg,
        ruleset="cwn",
    )
    async def _c(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    return reg


# ---------------------------------------------------------------------------
# Registry.tool_definitions(ruleset=...) — unit filtering behavior (AC-1/AC-2)
# ---------------------------------------------------------------------------


def test_tool_definitions_unfiltered_returns_all_back_compat() -> None:
    """No slug (or None) → every tool, regardless of declared ruleset.

    This is the back-compat contract the diagnostic token-estimate call site
    and the pack-less legacy/fixture paths rely on."""
    reg = _sample_registry()
    assert _names(reg.tool_definitions()) == {"agnostic_read", "wwn_only", "cwn_only"}
    assert _names(reg.tool_definitions(ruleset=None)) == {
        "agnostic_read",
        "wwn_only",
        "cwn_only",
    }


def test_tool_definitions_native_excludes_every_ruleset_gated_tool() -> None:
    reg = _sample_registry()
    assert _names(reg.tool_definitions(ruleset="native")) == {"agnostic_read"}


def test_tool_definitions_wwn_keeps_wwn_and_agnostic_drops_cwn() -> None:
    reg = _sample_registry()
    names = _names(reg.tool_definitions(ruleset="wwn"))
    assert names == {"agnostic_read", "wwn_only"}
    assert "cwn_only" not in names


def test_tool_definitions_cwn_keeps_cwn_and_agnostic_drops_wwn() -> None:
    reg = _sample_registry()
    names = _names(reg.tool_definitions(ruleset="cwn"))
    assert names == {"agnostic_read", "cwn_only"}
    assert "wwn_only" not in names


def test_filter_is_declaration_driven_not_a_name_allowlist() -> None:
    """AC-4: filtering must read each tool's DECLARED ruleset, not a hardcoded
    set of the five known gated names.

    A novel tool name the implementer could not have listed proves the filter
    is data-driven. An allowlist keyed on the real tool names would wrongly keep
    this novel WWN tool on a native pack."""
    reg = Registry()

    @tool(
        name="totally_novel_wwn_widget",
        description="x",
        category=ToolCategory.WRITE,
        registry=reg,
        ruleset="wwn",
    )
    async def _novel(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    assert "totally_novel_wwn_widget" not in _names(reg.tool_definitions(ruleset="native"))
    assert "totally_novel_wwn_widget" in _names(reg.tool_definitions(ruleset="wwn"))


def test_agnostic_tool_survives_every_ruleset_filter() -> None:
    reg = Registry()

    @tool(name="ubiquitous", description="x", category=ToolCategory.READ, registry=reg)
    async def _u(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    for slug in ("native", "wwn", "cwn", "swn"):
        assert "ubiquitous" in _names(reg.tool_definitions(ruleset=slug))


# ---------------------------------------------------------------------------
# The real production registry — the five gated tools declare their ruleset,
# and the partition is correct (the actual bug this story closes).
# ---------------------------------------------------------------------------


def test_default_registry_native_excludes_the_five_gated_tools() -> None:
    names = _names(default_registry.tool_definitions(ruleset="native"))
    assert GATED_ALL.isdisjoint(names), (
        f"native pack still advertises gated tools: {GATED_ALL & names}"
    )
    # A ruleset-agnostic tool must survive.
    assert "roll_dice" in names


def test_default_registry_wwn_partition() -> None:
    names = _names(default_registry.tool_definitions(ruleset="wwn"))
    assert names >= GATED_WWN, f"missing WWN tools: {GATED_WWN - names}"
    assert GATED_CWN.isdisjoint(names), f"WWN pack leaked CWN tools: {GATED_CWN & names}"
    assert "roll_dice" in names


def test_default_registry_cwn_partition() -> None:
    names = _names(default_registry.tool_definitions(ruleset="cwn"))
    assert names >= GATED_CWN, f"missing CWN tools: {GATED_CWN - names}"
    assert GATED_WWN.isdisjoint(names), f"CWN pack leaked WWN tools: {GATED_WWN & names}"
    assert "roll_dice" in names


def test_default_registry_unfiltered_still_lists_all_gated_tools() -> None:
    """Back-compat: the no-arg call still returns the full catalog, so the
    diagnostic call site and the self-guard backstop are unaffected, and the
    tools remain dispatchable."""
    names = _names(default_registry.tool_definitions())
    assert names >= GATED_ALL


# ---------------------------------------------------------------------------
# AC-3 — the per-tool fail-loud self-guard REMAINS as a backstop.
# (Filtering the advertised list does not remove the in-handler guard.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_effort_self_guard_still_raises_on_native_pack() -> None:
    """Even if the filter regresses and commit_effort is dispatched on a native
    pack, the handler must still fail loud (rendered as an error ToolResult)."""
    repository = MagicMock()
    repository.load.return_value = MagicMock()  # truthy session — get past the load guard
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Kael",
        turn_number=1,
        repository=repository,
        otel_span=MagicMock(),
        perception_filter=_NoopFilter(),
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="native")),
    )
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t1", name="commit_effort", arguments={"actor": "Kael", "source": "channeler"}
        ),
        ctx,
    )
    assert out.is_error is True
    assert "wwn-only" in out.content.lower()


class _NoopFilter:
    def filter_result(
        self,
        *,
        tool_name: str,
        category: ToolCategory,
        result: ToolResult,
        perspective_pc: str | None,
    ) -> ToolResult:
        return result


# ---------------------------------------------------------------------------
# Wiring — the PRODUCTION narration assembly path filters by the bound ruleset.
#
# Harness mirrors tests/agents/test_narrator_uses_sdk_client.py: an in-memory
# fake SDK captures the ``tools=`` array handed to ``complete_with_tools`` so we
# assert the real ``run_narration_turn`` path (not just the registry unit).
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _TextBlock:
    type: str
    text: str


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


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


def _single_prose_sdk() -> _Sdk:
    return _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text="The salt flats shimmer.")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=200, output_tokens=24),
                model="claude-sonnet-4-6",
            )
        ]
    )


def _pack(ruleset: str) -> SimpleNamespace:
    # Minimal stand-in for the loaded GenrePack: the assembly site reads
    # ``context.pack.rules.ruleset``. seed_tropes kept empty for any incidental
    # getattr during the (bypassed) prompt build.
    return SimpleNamespace(rules=SimpleNamespace(ruleset=ruleset), seed_tropes=())


def _bypass_prompt_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)


async def _run_turn(monkeypatch: pytest.MonkeyPatch, ruleset: str) -> _Sdk:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _bypass_prompt_builder(monkeypatch)
    sdk = _single_prose_sdk()
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))
    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=2,
        pack=_pack(ruleset),
    )
    await orch.run_narration_turn("look around", ctx)
    return sdk


@pytest.mark.asyncio
async def test_native_pack_narration_excludes_gated_tools_from_sdk_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = await _run_turn(monkeypatch, "native")
    sent = {t["name"] for t in sdk.messages.calls[0]["tools"]}
    assert GATED_ALL.isdisjoint(sent), f"native narration leaked gated tools: {GATED_ALL & sent}"
    assert "roll_dice" in sent  # agnostic tool still advertised


@pytest.mark.asyncio
async def test_wwn_pack_narration_includes_wwn_excludes_cwn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = await _run_turn(monkeypatch, "wwn")
    sent = {t["name"] for t in sdk.messages.calls[0]["tools"]}
    assert sent >= GATED_WWN, f"WWN narration missing WWN tools: {GATED_WWN - sent}"
    assert GATED_CWN.isdisjoint(sent), f"WWN narration leaked CWN tools: {GATED_CWN & sent}"


@pytest.mark.asyncio
async def test_narration_emits_ruleset_filter_otel_span(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """OTEL Observability Principle: the filter decision is a subsystem decision
    and MUST emit a span so the GM panel can verify the tightening engaged."""
    await _run_turn(monkeypatch, "native")
    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.tools.ruleset_filter"
    ]
    assert len(spans) >= 1, "no narrator.tools.ruleset_filter span emitted"
    attrs = dict(spans[0].attributes or {})
    assert attrs["tools.bound_ruleset"] == "native"
    # Ten gated tools dropped on a native pack: the original six (five CWN/WWN
    # tools + use_mutation/awn) plus the four WN-family contract tools (102-5:
    # wn_attack/wn_skill_check/wn_save/wn_adjudicate_dead_premise), all hidden
    # from a native narrator.
    assert attrs["tools.excluded_count"] == 10
    # The advertised count is the full catalog minus the ten gated tools.
    expected_advertised = len(default_registry.tool_definitions()) - 10
    assert attrs["tools.advertised_count"] == expected_advertised
