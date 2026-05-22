"""Task E1.5-A — backend-gate the narrator output-format instruction.

E1.5-B made the SDK narration path a hybrid split: the 26 WRITE tools own
+ persist 8 state categories during the SDK tool-dispatch loop, while the
~12 no-tool presentation fields are still parsed from a sidecar
``game_patch`` block. But the narrator prompt UNCONDITIONALLY injected the
legacy ``narrator_output_only`` prose, which tells the model to emit a
FULL ``game_patch`` covering ALL fields — including the 8 categories
E1.5-B deliberately zeros. So on the SDK path the model was double-
instructed: emit sidecar state the path ignores, and it could hedge (emit
the sidecar field, skip the tool → state silently lost).

The fix: ``NarratorAgent.build_output_format`` takes ``tool_backend``.
``Orchestrator.build_narrator_prompt`` passes
``tool_backend=isinstance(self._client, ToolingLlmClient)``. On the SDK
path the section body is the slimmed-sidecar + tool-routing prose
(``NARRATOR_OUTPUT_ONLY_SDK``); on the ``claude -p`` path it stays the
legacy prose, byte-identical.

Tests:

* Wiring (CLAUDE.md mandate): with a real ``ToolingLlmClient``
  (``AnthropicSdkClient`` + fake SDK) the orchestrator's
  ``build_narrator_prompt`` registers ``narrator_output_only`` with the
  SDK prose; with a non-tooling client it registers the legacy prose.
* Anti-drift contract: every tool-owned category in
  ``_SDK_TOOL_OWNED_FIELDS`` is represented in the SDK prose by its
  successor tool name, the SDK prose instructs the slimmed sidecar to
  carry the kept presentation fields, and it does NOT carry the legacy
  full-sidecar emission instruction. This guard fails if the prose and
  the E1.5-B partition silently diverge.
* Legacy-unchanged: ``tool_backend=False`` registers a body that equals
  ``<critical>\\n{NARRATOR_OUTPUT_ONLY}\\n</critical>`` exactly — the
  ``claude -p`` path the playgroup uses must not drift.

The fake-SDK shape mirrors ``test_narrator_uses_sdk_client.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.narrator_prompts import (
    NARRATOR_OUTPUT_ONLY,
    NARRATOR_OUTPUT_ONLY_SDK,
)
from sidequest.agents.orchestrator import (
    _SDK_TOOL_OWNED_FIELDS,
    Orchestrator,
    TurnContext,
)
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import AttentionZone, SectionCategory
from sidequest.agents.tooling_protocol import ToolingLlmClient

# ---------------------------------------------------------------------------
# Sentinels. Verified mutually exclusive across the two prose files:
#   - SDK_SENTINEL appears ONLY in output_only_sdk.md
#   - LEGACY_SENTINEL appears ONLY in output_only.md
# The contract test below re-asserts mutual exclusivity so a future edit
# that bleeds one phrase into the other file fails loudly.
# ---------------------------------------------------------------------------
SDK_SENTINEL = "TOOL-OWNED MECHANICS — CALL THE TOOL, DO NOT PUT THESE IN game_patch"
LEGACY_SENTINEL = "emit a fenced JSON block labeled game_patch containing mechanical intents"

# Distinct successor tool name(s) the SDK prose MUST reference for each
# tool-owned field in the E1.5-B partition. Keys MUST stay in lockstep
# with _SDK_TOOL_OWNED_FIELDS — the contract test asserts the key SETS
# match so a new partition row without a prose mapping fails loudly.
#
# SCOPE: this map guards prose↔partition KEY-SET drift only — does the SDK
# prose mention a routing tool for every tool-owned category. It does NOT
# verify the tool actually OWNS that category at dispatch time; that
# map↔dispatch tool-ownership contract is the complementary guard in
# tests/agents/test_sidecar_coverage_map.py (COVERAGE_MAP). The tool tuples
# here are documentation of the routing the prose advertises, not an
# assertion that dispatch honors them.
_FIELD_TO_TOOLS: dict[str, tuple[str, ...]] = {
    "status_changes": ("apply_status", "apply_damage"),
    "location": ("apply_world_patch",),
    "magic_working": ("apply_spell_effect", "update_resource_pool"),
    # Story 59-1: ``confrontation`` (encounter START) is no longer a zeroed,
    # tool-owned field — begin_confrontation is a signal and the assembler sets
    # result.confrontation from the ledger; narration_apply creates the
    # encounter on the canonical snapshot. So it is absent from both this map
    # and _SDK_TOOL_OWNED_FIELDS (the two key-sets must stay equal).
    "beat_selections": ("advance_encounter_beat", "advance_confrontation"),
    "days_advanced": ("tick_tropes",),
    "affinity_progress": ("update_resource_pool", "update_npc_disposition"),
    "game_patch_dict": ("apply_world_patch", "update_npc_disposition"),
}

# No-tool presentation fields E1.5-B keeps sidecar-sourced on the SDK path
# (NOT in _SDK_TOOL_OWNED_FIELDS). The SDK prose must still instruct the
# model to emit these in game_patch.
_KEPT_SIDECAR_FIELDS = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
    "gold_change",
    "companions_added",
    "companions_dismissed",
    "npcs_met",
    "mood",
    "visual_scene",
    "footnotes",
    "action_rewrite",
)


# ---------------------------------------------------------------------------
# Minimal fake SDK shaped like the AsyncAnthropic surface AnthropicSdkClient
# touches. Mirrors test_narrator_uses_sdk_client.py.
# ---------------------------------------------------------------------------
@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
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

    async def create(self, **kwargs: Any) -> _Resp:
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


def _section_body(registry: PromptRegistry) -> str:
    section = next(s for s in registry.registry("narrator") if s.name == "narrator_output_only")
    return section.content


# ---------------------------------------------------------------------------
# Wiring test (CLAUDE.md mandate) — the gate is reachable from the real
# Orchestrator.build_narrator_prompt seam, not just the unit method.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_build_narrator_prompt_uses_sdk_prose_when_tooling_client() -> None:
    """A ToolingLlmClient (AnthropicSdkClient) → SDK output-format prose."""
    sdk = _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text="unused")],
                stop_reason="end_turn",
                usage=_Usage(),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)
    assert isinstance(client, ToolingLlmClient)
    orch = Orchestrator(client=client)

    ctx = TurnContext(character_name="Kael", genre="caverns_and_claudes", turn_number=1)
    _prompt, registry = await orch.build_narrator_prompt("look around", ctx)

    body = _section_body(registry)
    assert SDK_SENTINEL in body, "SDK path must inject the slimmed-sidecar prose"
    assert LEGACY_SENTINEL not in body, "SDK path must NOT inject the legacy full-sidecar prose"


@pytest.mark.asyncio
async def test_build_narrator_prompt_uses_legacy_prose_when_non_tooling_client() -> None:
    """A non-tooling client (default ClaudeClient) → legacy prose."""
    orch = Orchestrator()  # default ClaudeClient — not a ToolingLlmClient
    assert not isinstance(orch._client, ToolingLlmClient)

    ctx = TurnContext(character_name="Kael", genre="caverns_and_claudes", turn_number=1)
    _prompt, registry = await orch.build_narrator_prompt("look around", ctx)

    body = _section_body(registry)
    assert LEGACY_SENTINEL in body, "claude -p path must inject the legacy prose"
    assert SDK_SENTINEL not in body, "claude -p path must NOT inject the SDK slimmed-sidecar prose"


# ---------------------------------------------------------------------------
# Anti-drift contract test — the prose and the E1.5-B partition cannot
# silently diverge. Pure (no orchestrator), modeled on
# tests/agents/test_sidecar_coverage_map.py.
# ---------------------------------------------------------------------------
def test_field_to_tools_keys_match_sdk_tool_owned_partition() -> None:
    """The prose↔tool map keys must equal _SDK_TOOL_OWNED_FIELDS keys.

    This is a KEY-SET tripwire only: if E1.5-B adds or removes a
    tool-owned field, this fails until the SDK prose mapping is updated,
    so the prose can't silently miss (or stale-list) a tool-owned
    category. It does NOT verify which tool actually owns a category at
    dispatch time — that map↔dispatch tool-ownership contract lives in
    tests/agents/test_sidecar_coverage_map.py (COVERAGE_MAP). A future
    maintainer changing tool ownership must look there, not here.
    """
    assert set(_FIELD_TO_TOOLS) == set(_SDK_TOOL_OWNED_FIELDS), (
        "SDK prose tool-map drifted from _SDK_TOOL_OWNED_FIELDS: "
        f"map={sorted(_FIELD_TO_TOOLS)} partition={sorted(_SDK_TOOL_OWNED_FIELDS)}"
    )


def test_sdk_prose_routes_every_tool_owned_category_to_its_tool() -> None:
    """Every tool-owned category is represented in the SDK prose by its
    successor tool name (rendered as a backticked tool token)."""
    for field, tools in _FIELD_TO_TOOLS.items():
        for tool in tools:
            assert f"`{tool}`" in NARRATOR_OUTPUT_ONLY_SDK, (
                f"SDK prose must route tool-owned category {field!r} via {tool!r}"
            )


def test_sdk_prose_keeps_presentation_fields_in_sidecar() -> None:
    """The slimmed sidecar must still carry every no-tool presentation
    field E1.5-B keeps sidecar-sourced.

    Anchored on the ``<field>:`` instruction label rather than a bare
    substring. Short tokens like ``mood`` are substrings of unrelated
    prose (scene-mood, the visual_scene JSON's quoted ``"mood":`` key,
    melancholic-adjacent tag text), so ``"mood" in prose`` would pass
    vacuously even if the standalone ``mood:`` field instruction were
    deleted. Requiring ``"mood:"`` (the line-label form the prose uses
    for every kept field) tests the actual field instruction while
    staying robust to benign rewording of the description text.
    """
    for field in _KEPT_SIDECAR_FIELDS:
        assert f"{field}:" in NARRATOR_OUTPUT_ONLY_SDK, (
            f"SDK prose must still instruct the sidecar to carry {field!r} "
            f"via a '{field}:' field instruction"
        )


def test_sdk_prose_does_not_carry_legacy_full_sidecar_instruction() -> None:
    """The SDK prose must NOT tell the model to emit the legacy FULL
    game_patch (the one that enumerates tool-owned fields like
    confrontation/location/status_changes/days_advanced in the sidecar
    field list). That instruction is exactly what E1.5-B ignores."""
    assert LEGACY_SENTINEL not in NARRATOR_OUTPUT_ONLY_SDK
    # The legacy "Valid fields:" enumeration lists tool-owned sidecar
    # fields; it must not survive into the SDK prose.
    assert "Valid fields: confrontation, items_gained" not in NARRATOR_OUTPUT_ONLY_SDK


def test_sentinels_are_mutually_exclusive_across_prose_files() -> None:
    """Guard the sentinels the wiring tests rely on: each appears in
    exactly one prose file."""
    assert SDK_SENTINEL in NARRATOR_OUTPUT_ONLY_SDK
    assert SDK_SENTINEL not in NARRATOR_OUTPUT_ONLY
    assert LEGACY_SENTINEL in NARRATOR_OUTPUT_ONLY
    assert LEGACY_SENTINEL not in NARRATOR_OUTPUT_ONLY_SDK


# ---------------------------------------------------------------------------
# Legacy-unchanged regression — the claude -p path the playgroup plays on
# must be byte-identical to pre-E1.5-A behavior.
# ---------------------------------------------------------------------------
def test_tool_backend_false_body_is_byte_identical_to_legacy() -> None:
    """Default / tool_backend=False registers exactly the legacy body."""
    agent = NarratorAgent()

    registry_default = PromptRegistry()
    agent.build_output_format(registry_default)

    registry_explicit = PromptRegistry()
    agent.build_output_format(registry_explicit, tool_backend=False)

    expected = f"<critical>\n{NARRATOR_OUTPUT_ONLY}\n</critical>"
    assert _section_body(registry_default) == expected
    assert _section_body(registry_explicit) == expected


def test_tool_backend_true_body_wraps_sdk_prose() -> None:
    """tool_backend=True registers the SDK prose in the same wrapper."""
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_output_format(registry, tool_backend=True)
    expected = f"<critical>\n{NARRATOR_OUTPUT_ONLY_SDK}\n</critical>"
    assert _section_body(registry) == expected


def test_section_name_zone_category_stable_across_backends() -> None:
    """Both backends register one section named 'narrator_output_only' in
    the Primacy/Guardrail zone — downstream lookups by name keep working."""
    for tool_backend in (False, True):
        agent = NarratorAgent()
        registry = PromptRegistry()
        agent.build_output_format(registry, tool_backend=tool_backend)
        sections = registry.get_sections(
            "narrator",
            zone=AttentionZone.Primacy,
            category=SectionCategory.Guardrail,
        )
        assert [s.name for s in sections if s.name == "narrator_output_only"] == [
            "narrator_output_only"
        ]
