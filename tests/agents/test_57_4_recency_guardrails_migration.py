"""Story 57-4 — Migrate recency guardrail prose into tool-use descriptions.

ADR-111 ratifies moving four Recency-zone guardrail prose blocks
(``npc_intro_visual_constraint``, ``confrontation_trigger_constraint``,
``npc_extraction_constraint``, ``location_patch_constraint``) out of the
per-turn user message on the ``anthropic_sdk`` backend and into cached
surfaces (the ``tools=`` array ``description`` field or the slimmed
sidecar prose at ``narrator_prompts/output_only_sdk.md``). On the
legacy ``claude -p`` / Ollama backends the four sections continue to
register byte-identical to pre-111 behavior — the playgroup safety net
must not drift.

These tests pin the migration contract from ADR-111 §Decision:

1. **SDK-path suppression (AC2).** On the SDK backend (a
   ``ToolingLlmClient`` such as ``AnthropicSdkClient``), the four
   Recency-zone registrations at ``orchestrator.py:1764, 1851, 1934,
   1989`` are skipped entirely — zero-byte-leak, matching the pattern
   at ``orchestrator.py:1320`` for the ``pending_trope_context`` /
   ``active_trope_summary`` registrations.

2. **Legacy-path preservation (AC3).** On a non-tooling client (default
   ``ClaudeClient``), the four sections still register and their
   ``.content`` is byte-identical to the centralized constants — the
   ``claude -p`` path the playgroup uses must not drift.

3. **Migration target presence (AC1).** For each guardrail, the prose
   appears on the SDK path's cached surface adjacent to the artifact it
   governs:

   - ``npc_intro_visual_constraint`` → ``NARRATOR_OUTPUT_ONLY_SDK``
     (slimmed sidecar, Primacy/Stable cached zone).
   - ``npc_extraction_constraint`` → ``NARRATOR_OUTPUT_ONLY_SDK``.
   - ``location_patch_constraint`` → ``apply_world_patch`` tool
     ``description`` field.
   - ``confrontation_trigger_constraint`` → at least one
     confrontation/encounter tool's ``description`` field (the live
     registry has ``generate_encounter`` and
     ``advance_confrontation`` / ``advance_encounter_beat``; ADR-111
     leaves the per-tool concretization to implementation).

4. **Single source of truth (ADR-111 §Implementation Notes).** The
   four prose constants live in one module
   (``sidequest.agents.narrator_guardrails``) and both the legacy
   Recency registration and the migrated description text reference
   these constants — no string duplication. Divergence requires
   deliberate effort, not a missed edit.

5. **OTEL emit (ADR-111 §Observability discipline).** A
   ``narrator.recency_guardrails_skipped`` span fires once per
   prompt-build, carrying ``tool_backend`` (bool),
   ``guardrails_skipped`` (the four names on SDK, empty on legacy),
   and ``bytes_saved`` (sum of ``len(prose)`` for the omitted sections).
   This is the GM-panel proof the migration is engaged on a given turn.

6. **Wiring (CLAUDE.md mandate).** A real ``AnthropicSdkClient``
   driven through ``Orchestrator.build_narrator_prompt`` demonstrates
   the suppression end-to-end — not just at the unit-method seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# Importing the tools package wires all 26 adapters onto default_registry
# so ``default_registry.tool_definitions()`` reflects production reality.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY_SDK
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import AttentionZone, SectionCategory
from sidequest.agents.tool_registry import default_registry
from sidequest.agents.tooling_protocol import ToolingLlmClient

GUARDRAIL_NAMES: tuple[str, ...] = (
    "npc_intro_visual_constraint",
    "confrontation_trigger_constraint",
    "npc_extraction_constraint",
    "location_patch_constraint",
)


# ---------------------------------------------------------------------------
# Minimal fake SDK shaped like the AsyncAnthropic surface AnthropicSdkClient
# touches. Mirrors ``test_narrator_output_format_backend_gate.py``. The
# prompt-build path never fires the SDK; the responses list stays empty.
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
    """Fake AsyncAnthropic — only ``.messages.create`` is exercised, and
    only if the test fires the SDK (the prompt-build path doesn't)."""

    def __init__(self, responses: list[_Resp] | None = None) -> None:
        self.messages = _Msgs(responses or [])


def _make_sdk_orchestrator() -> Orchestrator:
    """Build an Orchestrator whose client passes
    ``isinstance(self._client, ToolingLlmClient)`` — the SDK-path
    discriminator currently used at ``orchestrator.py:1239``."""
    sdk = _Sdk()
    client = AnthropicSdkClient(sdk=sdk)
    assert isinstance(client, ToolingLlmClient), (
        "AnthropicSdkClient must satisfy the ToolingLlmClient protocol — "
        "otherwise the backend-gate discriminator misroutes."
    )
    return Orchestrator(client=client)


def _make_legacy_orchestrator() -> Orchestrator:
    """Default ClaudeClient — NOT a ``ToolingLlmClient``."""
    orch = Orchestrator()
    assert not isinstance(orch._client, ToolingLlmClient), (
        "Default Orchestrator must use the legacy non-tooling client — "
        "otherwise this fixture cannot exercise the legacy preservation path."
    )
    return orch


def _section_by_name(registry: PromptRegistry, agent_name: str, name: str):
    for section in registry.registry(agent_name):
        if section.name == name:
            return section
    return None


def _tool_descriptions() -> dict[str, str]:
    """All registered tool descriptions, keyed by tool name. Reflects the
    live registry the SDK call's ``tools=`` array is built from."""
    return {td.name: td.description for td in default_registry.tool_definitions()}


# ---------------------------------------------------------------------------
# 1. Single source of truth: the constants module exists and exposes
#    one named constant per guardrail (ADR-111 §Implementation Notes).
# ---------------------------------------------------------------------------


def test_narrator_guardrails_module_exposes_four_named_constants() -> None:
    """ADR-111 §Implementation Notes: the four prose constants live in a
    single module so legacy Recency registration and the migrated tool
    descriptions reference the same string — no duplication.

    The constant names track the section names verbatim (uppercase)
    so future readers can grep from one to the other.
    """
    from sidequest.agents import narrator_guardrails as ng

    for name in GUARDRAIL_NAMES:
        const_name = name.upper()
        assert hasattr(ng, const_name), (
            f"narrator_guardrails module must expose {const_name} — the "
            f"single source of truth for the legacy {name!r} prose and "
            "its migrated description target."
        )
        value = getattr(ng, const_name)
        assert isinstance(value, str) and len(value) > 100, (
            f"narrator_guardrails.{const_name} must be a non-trivial "
            f"prose string (>100 chars); got {type(value).__name__} "
            f"len={len(value) if isinstance(value, str) else 'n/a'}"
        )


@pytest.mark.parametrize(
    "name, fingerprint",
    [
        # Distinctive phrases drawn from each Recency-zone prose block at
        # orchestrator.py:1764/1851/1934/1989. ADR-111 §Alternatives B
        # rejected compression because these specifics ARE the regression
        # fingerprints — so they must survive verbatim into the constants.
        ("npc_intro_visual_constraint", "Recurring NPCs"),
        ("confrontation_trigger_constraint", "Do NOT defer it to the"),
        ("npc_extraction_constraint", "Patients on a sickbed count"),
        ("location_patch_constraint", "State must not lag prose"),
    ],
)
def test_each_constant_carries_its_load_bearing_fingerprint(
    name: str, fingerprint: str
) -> None:
    """Each constant must contain the phrase that earned the original
    Recency-zone block its place. If the implementer compressed the
    prose into a one-line rule (ADR-111 §Alternatives B, rejected),
    this assertion fails — the bug-report specificity is the
    regression detector, not stylistic flavor.
    """
    from sidequest.agents import narrator_guardrails as ng

    const = getattr(ng, name.upper())
    assert fingerprint in const, (
        f"narrator_guardrails.{name.upper()} lost its load-bearing "
        f"fingerprint {fingerprint!r}. ADR-111 §Alternatives B "
        "rejected the compression path explicitly — concrete "
        "examples in the prose are the regression detector."
    )


# ---------------------------------------------------------------------------
# 2. Legacy path: byte-identical preservation (AC3).
#    Same backend (no client argument → default ClaudeClient).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", GUARDRAIL_NAMES)
@pytest.mark.asyncio
async def test_legacy_backend_registers_all_four_guardrails(
    name: str, simple_turn_context_turn_three
) -> None:
    """AC3: on the legacy ``claude -p`` backend, every one of the four
    Recency-zone Guardrails MUST still register, in the Recency zone,
    with the Guardrail category — the playgroup playback path stays
    byte-identical to pre-111 behavior."""
    orch = _make_legacy_orchestrator()
    _, registry = await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
    section = _section_by_name(registry, orch._narrator.name(), name)
    assert section is not None, (
        f"Legacy backend must still register Recency-zone {name!r} — "
        "ADR-111 backend-gates on the SDK path only, so the legacy "
        "registration site must remain reachable. Recency-zone "
        f"siblings actually registered: "
        f"{[s.name for s in registry.registry(orch._narrator.name()) if s.zone == AttentionZone.Recency]}"
    )
    assert section.zone == AttentionZone.Recency, (
        f"Legacy {name!r} must stay in Recency (got {section.zone}); "
        "moving it would alter prompt structure on the legacy path."
    )
    assert section.category == SectionCategory.Guardrail, (
        f"Legacy {name!r} must stay categorized Guardrail (got {section.category})."
    )


@pytest.mark.parametrize("name", GUARDRAIL_NAMES)
@pytest.mark.asyncio
async def test_legacy_backend_prose_equals_centralized_constant(
    name: str, simple_turn_context_turn_three
) -> None:
    """ADR-111 §Implementation Notes mandates one source of truth. The
    legacy Recency-zone registration MUST reference the centralized
    constant from ``sidequest.agents.narrator_guardrails`` — no string
    duplication. This pin is what prevents future edits from drifting
    one side without the other.
    """
    from sidequest.agents import narrator_guardrails as ng

    orch = _make_legacy_orchestrator()
    _, registry = await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
    section = _section_by_name(registry, orch._narrator.name(), name)
    assert section is not None
    expected = getattr(ng, name.upper())
    assert section.content == expected, (
        f"Legacy-path section {name!r} content drifted from the "
        f"narrator_guardrails.{name.upper()} constant. ADR-111 "
        "Implementation Notes forbids string duplication — the "
        "registration must reference the constant verbatim, otherwise "
        "a future edit will silently apply only on one path."
    )


# ---------------------------------------------------------------------------
# 3. SDK path: suppression (AC2). Zero-byte-leak.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", GUARDRAIL_NAMES)
@pytest.mark.asyncio
async def test_sdk_backend_suppresses_recency_guardrail(
    name: str, simple_turn_context_turn_three
) -> None:
    """AC2: on the SDK backend, the four Recency-zone registrations are
    skipped entirely. ADR-111 §Decision: zero-byte-leak discipline
    matching ``orchestrator.py:1320`` for the trope-context skip
    pattern. The prose lives at its migration target, not in the
    per-turn user message."""
    orch = _make_sdk_orchestrator()
    _, registry = await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
    section = _section_by_name(registry, orch._narrator.name(), name)
    assert section is None, (
        f"SDK backend must NOT register Recency-zone {name!r}. The "
        "section appeared in the prompt registry — backend-gating "
        "either was not applied at this registration site, or the "
        "ToolingLlmClient discriminator at orchestrator.py:1239 "
        "isn't reaching this branch. "
        "ADR-111 §Decision: zero-byte-leak on the SDK path."
    )


@pytest.mark.asyncio
async def test_sdk_prompt_text_does_not_contain_any_guardrail_section_marker(
    simple_turn_context_turn_three,
) -> None:
    """Assembled SDK prompt MUST NOT contain the section-marker tags from
    the four Recency-zone blocks. Each registration wraps its prose
    in an XML-style sentinel (``<npc-intro-visual>``,
    ``<confrontation-trigger>``, ``<npc-extraction>``,
    ``<location-patch>``); the markers are the cheapest grep target.

    A regression that re-registers a Recency-zone block via a copy of
    the prose (instead of importing the centralized constant) would
    pass the per-section ``_section_by_name`` check above if the new
    section name differs — this catch-all grep is the safety net for
    that drift mode.
    """
    orch = _make_sdk_orchestrator()
    prompt, _ = await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
    for marker in (
        "<npc-intro-visual>",
        "<confrontation-trigger>",
        "<npc-extraction>",
        "<location-patch>",
    ):
        assert marker not in prompt, (
            f"SDK-path assembled prompt contains the Recency-zone marker "
            f"{marker!r}. ADR-111 routes this prose to the cached "
            "``tools=`` / Primacy surface — the per-turn user message "
            "must be clean of all four guardrail blocks."
        )


# ---------------------------------------------------------------------------
# 4. Migration target presence (AC1).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "npc_intro_visual_constraint",
        "npc_extraction_constraint",
    ],
)
def test_sidecar_targets_carry_their_guardrail_prose(name: str) -> None:
    """AC1: the two sidecar-owned guardrails migrate into
    ``NARRATOR_OUTPUT_ONLY_SDK`` (the slimmed-sidecar prose at
    ``narrator_prompts/output_only_sdk.md``). Per ADR-111 §Decision
    routing rule: sidecar-field guardrails go to the sidecar SDK prose
    in the Primacy/Stable cached zone.

    The check uses the load-bearing fingerprint phrase rather than the
    full constant body so the implementer has latitude to introduce a
    new subsection header / restructure the prose for the new home.
    The fingerprint itself is the regression detector (see
    ``test_each_constant_carries_its_load_bearing_fingerprint``).
    """
    fingerprints = {
        "npc_intro_visual_constraint": "Recurring NPCs",
        "npc_extraction_constraint": "Patients on a sickbed count",
    }
    fp = fingerprints[name]
    assert fp in NARRATOR_OUTPUT_ONLY_SDK, (
        f"NARRATOR_OUTPUT_ONLY_SDK is missing the migrated fingerprint "
        f"{fp!r} from {name!r}. ADR-111 routing rule: this guardrail "
        "governs a sidecar field (visual_scene / npcs_present) and "
        "must move into the slimmed-sidecar Primacy-cached prose."
    )


def test_apply_world_patch_tool_description_carries_location_guardrail() -> None:
    """AC1: ``location_patch_constraint`` migrates into the
    ``apply_world_patch`` tool's ``description`` field. ADR-111 §Decision
    routing table: ``game_patch.location`` is owned by the
    ``apply_world_patch`` write tool, so its rule-of-use travels with
    the tool definition into the cache key root.

    The check uses the fingerprint phrase ``"State must not lag prose"``
    so the implementer may append the rule cleanly to the existing
    one-line description (no XML wrapper required in a tool
    description).
    """
    descriptions = _tool_descriptions()
    assert "apply_world_patch" in descriptions, (
        "apply_world_patch tool not registered — cannot migrate the "
        "location_patch_constraint into a tool that doesn't exist."
    )
    desc = descriptions["apply_world_patch"]
    assert "State must not lag prose" in desc, (
        "apply_world_patch.description is missing the migrated "
        "location_patch_constraint fingerprint. ADR-111 routing "
        "table: this guardrail must travel with the tool that owns "
        "the artifact (``game_patch.location``)."
    )


def test_confrontation_guardrail_migrates_into_an_encounter_tool() -> None:
    """AC1: ``confrontation_trigger_constraint`` migrates into the
    description of the tool that fires a confrontation. ADR-111
    §Decision routing table: trigger-fire prose goes to the start-
    confrontation tool description. The live registry today exposes
    ``generate_encounter``, ``advance_confrontation``, and
    ``advance_encounter_beat`` — ADR-111 leaves the per-tool
    concretization to implementation, so this test pins the contract
    ("the prose lives on at least one of the confrontation/encounter
    tools") without over-specifying which.
    """
    descriptions = _tool_descriptions()
    candidate_names = {
        n for n in descriptions if "confront" in n or "encounter" in n
    }
    assert candidate_names, (
        "No confrontation/encounter tools registered — cannot host the "
        "confrontation_trigger_constraint migration."
    )
    fingerprint = "Do NOT defer it to the"
    hits = {n for n in candidate_names if fingerprint in descriptions[n]}
    assert hits, (
        f"None of the confrontation/encounter tools ({sorted(candidate_names)}) "
        f"carry the migrated fingerprint {fingerprint!r} from "
        "confrontation_trigger_constraint. ADR-111 §Decision: the "
        "trigger-fire-this-turn invariant must live on the start-"
        "confrontation tool description so the model reads it whenever "
        "it weighs the call."
    )


# ---------------------------------------------------------------------------
# 5. OTEL emit (ADR-111 §Observability discipline).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_path_emits_recency_guardrails_skipped_span(
    simple_turn_context_turn_three, otel_capture
) -> None:
    """ADR-111 §Observability: a ``narrator.recency_guardrails_skipped``
    span fires on the SDK path with ``tool_backend=true``,
    ``guardrails_skipped`` listing the four names, and ``bytes_saved``
    summing the prose lengths.

    This is the GM-panel proof per repo CLAUDE.md "OTEL Observability
    Principle" — if the migration silently un-engages on a future
    refactor, the span goes missing and the dashboard catches it
    before a playtest does.
    """
    orch = _make_sdk_orchestrator()
    await orch.build_narrator_prompt("act", simple_turn_context_turn_three)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "narrator.recency_guardrails_skipped"
    ]
    assert len(spans) == 1, (
        f"Expected exactly one narrator.recency_guardrails_skipped span on "
        f"the SDK path, got {len(spans)}. ADR-111 §Observability mandates "
        "one emit per prompt-build."
    )
    span = spans[0]
    attrs = dict(span.attributes or {})
    assert attrs.get("tool_backend") is True, (
        f"Span tool_backend attr must be True on SDK path, got {attrs.get('tool_backend')!r}"
    )

    skipped = attrs.get("guardrails_skipped")
    # OTEL attributes coerce list values to tuples on read; accept either.
    skipped_list = list(skipped) if skipped is not None else []
    assert set(skipped_list) == set(GUARDRAIL_NAMES), (
        f"guardrails_skipped attr must list the four migrated names, got {skipped_list!r}"
    )

    bytes_saved = attrs.get("bytes_saved")
    assert isinstance(bytes_saved, int) and bytes_saved > 0, (
        f"bytes_saved must be a positive int (sum of len(prose) for the "
        f"four skipped sections); got {bytes_saved!r}"
    )


@pytest.mark.asyncio
async def test_legacy_path_emits_span_with_empty_skipped_list(
    simple_turn_context_turn_three, otel_capture
) -> None:
    """The span fires on every prompt-build, regardless of backend. On
    the legacy path it reports ``tool_backend=False``, an empty
    ``guardrails_skipped`` list, and ``bytes_saved=0``. The constant-
    emit shape lets the GM panel diff per-turn behaviour across
    backends without conditional span presence.
    """
    orch = _make_legacy_orchestrator()
    await orch.build_narrator_prompt("act", simple_turn_context_turn_three)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "narrator.recency_guardrails_skipped"
    ]
    assert len(spans) == 1, (
        f"Expected one narrator.recency_guardrails_skipped span on the "
        f"legacy path, got {len(spans)}. The span must fire on every "
        "prompt-build so the GM panel can diff backends without "
        "conditional presence."
    )
    span = spans[0]
    attrs = dict(span.attributes or {})
    assert attrs.get("tool_backend") is False, (
        f"Span tool_backend attr must be False on legacy path, "
        f"got {attrs.get('tool_backend')!r}"
    )
    skipped = attrs.get("guardrails_skipped")
    skipped_list = list(skipped) if skipped is not None else []
    assert skipped_list == [], (
        f"Legacy path emits zero skipped guardrails, got {skipped_list!r}"
    )
    assert attrs.get("bytes_saved") == 0, (
        f"Legacy path bytes_saved must be 0, got {attrs.get('bytes_saved')!r}"
    )


# ---------------------------------------------------------------------------
# 6. End-to-end wiring (CLAUDE.md mandate).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wiring_sdk_orchestrator_assembled_prompt_drops_all_four_sections(
    simple_turn_context_turn_three,
) -> None:
    """Integration / wiring test (repo CLAUDE.md "Every Test Suite Needs
    a Wiring Test"). Drive a real ``AnthropicSdkClient`` through
    ``Orchestrator.build_narrator_prompt`` and assert that NONE of the
    four section names appear in the registered section list. Mirrors
    the wiring pattern used by ``test_narrator_output_format_backend_gate.
    test_build_narrator_prompt_uses_sdk_prose_when_tooling_client``.
    """
    orch = _make_sdk_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "look around", simple_turn_context_turn_three
    )
    registered_names = {s.name for s in registry.registry(orch._narrator.name())}
    leaked = registered_names & set(GUARDRAIL_NAMES)
    assert not leaked, (
        f"SDK-path build_narrator_prompt leaked guardrail sections {sorted(leaked)} "
        "into the registry. The four registration sites at "
        "orchestrator.py:1764/1851/1934/1989 must be gated behind a "
        "``not isinstance(self._client, ToolingLlmClient)`` (or equivalent "
        "context.tool_backend flag) check — see ADR-111 §Backend-gated "
        "dual path. Currently-registered Recency-zone sections: "
        f"{[s.name for s in registry.registry(orch._narrator.name()) if s.zone == AttentionZone.Recency]}"
    )


@pytest.mark.asyncio
async def test_wiring_legacy_orchestrator_assembled_prompt_keeps_all_four_sections(
    simple_turn_context_turn_three,
) -> None:
    """Sibling wiring test: the legacy path MUST still expose all four
    guardrail sections — the playgroup's safety net. ADR-111 §Decision
    explicitly preserves the legacy registration code paths via
    backend-gating; deletion or accidental gating would break the
    ``claude -p`` opt-in backend.
    """
    orch = _make_legacy_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "look around", simple_turn_context_turn_three
    )
    registered_names = {s.name for s in registry.registry(orch._narrator.name())}
    missing = set(GUARDRAIL_NAMES) - registered_names
    assert not missing, (
        f"Legacy backend dropped guardrail sections {sorted(missing)} — "
        "the ``claude -p`` path must stay byte-identical to pre-111 "
        "behavior per ADR-111 §Decision."
    )
