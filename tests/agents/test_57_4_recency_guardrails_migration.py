"""Story 57-4 — Migrate recency guardrail prose into tool-use descriptions.

ADR-111 ratifies moving four Recency-zone guardrail prose blocks
(``npc_intro_visual_constraint``, ``confrontation_trigger_constraint``,
``npc_extraction_constraint``, ``location_patch_constraint``) out of the
per-turn user message on the ``anthropic_sdk`` backend and into cached
surfaces (the ``tools=`` array ``description`` field or the slimmed
sidecar prose at ``narrator_prompts/output_only.md``).

Story 61-9 / ADR-101 amendment retired the legacy ``claude -p`` / Ollama
narrator path. The dual-path tests this file used to host (legacy
preservation, tool_backend OTEL-attr assertions) were deleted with the
backend gate; the surviving SDK contract is pinned here.

These tests pin the migration contract from ADR-111 §Decision:

1. **Suppression (AC2).** On the SDK backend (a ``ToolingLlmClient``
   such as ``AnthropicSdkClient``), the four Recency-zone registration
   sites are skipped entirely — zero-byte-leak, matching the pattern at
   ``orchestrator.py:1320`` for the ``pending_trope_context`` /
   ``active_trope_summary`` registrations.

2. **Migration target presence (AC1).** For each guardrail, the prose
   appears on the SDK path's cached surface adjacent to the artifact it
   governs:

   - ``npc_intro_visual_constraint`` → ``NARRATOR_OUTPUT_ONLY``
     (slimmed sidecar, Primacy/Stable cached zone).
   - ``npc_extraction_constraint`` → ``NARRATOR_OUTPUT_ONLY``.
   - ``location_patch_constraint`` → ``apply_world_patch`` tool
     ``description`` field.
   - ``confrontation_trigger_constraint`` → at least one
     confrontation/encounter tool's ``description`` field (the live
     registry has ``generate_encounter`` and
     ``advance_confrontation`` / ``advance_encounter_beat``; ADR-111
     leaves the per-tool concretization to implementation).

3. **Single source of truth (ADR-111 §Implementation Notes).** The
   four prose constants live in one module
   (``sidequest.agents.narrator_guardrails``) so the migrated
   description text references these constants — no string duplication.
   Divergence requires deliberate effort, not a missed edit.

4. **OTEL emit (ADR-111 §Observability discipline; 61-9 Option a).** A
   ``narrator.recency_guardrails_skipped`` span fires once per
   prompt-build with ``guardrails_skipped`` (the four names) and
   ``bytes_saved`` (sum of ``len(prose)``) hard-wired as constants. The
   pre-61-9 ``tool_backend`` attribute is gone — only the SDK path is
   wired, so the attribute carried no information.

5. **Wiring (CLAUDE.md mandate).** A real ``AnthropicSdkClient``
   driven through ``Orchestrator.build_narrator_prompt`` demonstrates
   the suppression end-to-end — not just at the unit-method seam.
"""

from __future__ import annotations

import pytest

# Importing the tools package wires all 26 adapters onto default_registry
# so ``default_registry.tool_definitions()`` reflects production reality.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY
from sidequest.agents.orchestrator import Orchestrator
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.tool_registry import default_registry
from sidequest.agents.tooling_protocol import ToolingLlmClient

GUARDRAIL_NAMES: tuple[str, ...] = (
    "npc_intro_visual_constraint",
    "confrontation_trigger_constraint",
    "npc_extraction_constraint",
    "location_patch_constraint",
)


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 119-3: the claude-agent-sdk transport runs over the Max
    subscription pool. Both PAYG credentials must be UNSET — a SET key now
    re-routes to PAYG and raises ``AgentSdkAuthUnavailable`` at call time.
    Construction reads no env, but pin the absence so a polluted environment
    can't leak into the prompt-build path."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _make_sdk_orchestrator() -> Orchestrator:
    """Build an Orchestrator whose client passes
    ``isinstance(self._client, ToolingLlmClient)`` — the SDK-path
    discriminator. Post-story-61-9 this is the only viable narrator
    backend.

    Story 119-3: ``AnthropicSdkClient()`` takes no args — the legacy ``sdk=``
    injection is gone (the transport is the late-bound module-level ``query``
    seam). The prompt-build path these tests drive never fires ``query``, so
    no fake stream is installed here."""
    client = AnthropicSdkClient()
    assert isinstance(client, ToolingLlmClient), (
        "AnthropicSdkClient must satisfy the ToolingLlmClient protocol — "
        "otherwise the backend-gate discriminator misroutes."
    )
    return Orchestrator(client=client)


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
    single module so the migrated tool descriptions reference the same
    string — no duplication.

    The constant names track the section names verbatim (uppercase)
    so future readers can grep from one to the other.
    """
    from sidequest.agents import narrator_guardrails as ng

    for name in GUARDRAIL_NAMES:
        const_name = name.upper()
        assert hasattr(ng, const_name), (
            f"narrator_guardrails module must expose {const_name} — the "
            f"single source of truth for the {name!r} prose and its "
            "migrated description target."
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
def test_each_constant_carries_its_load_bearing_fingerprint(name: str, fingerprint: str) -> None:
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
# 2. SDK path: suppression (AC2). Zero-byte-leak.
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
        "ToolingLlmClient discriminator isn't reaching this branch. "
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
# 3. Migration target presence (AC1).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "npc_intro_visual_constraint",
        "npc_extraction_constraint",
    ],
)
def test_sidecar_targets_guardrail_prose_retired_with_their_fields(name: str) -> None:
    """Story 151-5 / ADR-150 step 4 (cutover II) SUPERSEDES the ADR-111 placement of
    these two guardrails. ADR-111 migrated the ``npc_intro_visual_constraint`` /
    ``npc_extraction_constraint`` prose INTO ``NARRATOR_OUTPUT_ONLY`` because they
    govern the sidecar fields ``visual_scene`` / ``npcs_present``. ADR-150 RETIRES
    those fields from the narrator contract (extractor-sourced now), so their
    guardrail prose is retired with them.

    The guardrail CONSTANTS still exist (dormant) — ``test_each_constant_carries_its
    _load_bearing_fingerprint`` still pins them — but they are no longer injected into
    output_only.md. Forward regression guard: the fingerprint must NOT reappear in the
    narrator contract. Inverts the pre-151-5 ``test_sidecar_targets_carry_their
    _guardrail_prose``."""
    fingerprints = {
        "npc_intro_visual_constraint": "Recurring NPCs",
        "npc_extraction_constraint": "Patients on a sickbed count",
    }
    fp = fingerprints[name]
    assert fp not in NARRATOR_OUTPUT_ONLY, (
        f"NARRATOR_OUTPUT_ONLY still carries the {fp!r} guardrail from {name!r}; "
        f"151-5 retired the npcs_present / visual_scene fields it governs (ADR-150 "
        "supersedes the ADR-111 placement — the extractor owns these fields now)."
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


# Story 59-10: test_confrontation_guardrail_migrates_into_an_encounter_tool
# removed. The begin_confrontation tool was retired in Story 59-4 (ADR-113);
# confrontation engagement is now router-driven. The guardrail migration
# target no longer exists.


# ---------------------------------------------------------------------------
# 4. OTEL emit (ADR-111 §Observability discipline; 61-9 Option a).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_path_emits_recency_guardrails_skipped_span(
    simple_turn_context_turn_three, otel_capture
) -> None:
    """ADR-111 §Observability: a ``narrator.recency_guardrails_skipped``
    span fires on the SDK path with ``guardrails_skipped`` listing the
    four names, and ``bytes_saved`` summing the prose lengths.

    Story 61-9 Option a: the pre-existing ``tool_backend`` attribute
    is dropped — only SDK is wired, so the attribute carried no
    information.

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


# ---------------------------------------------------------------------------
# 5. End-to-end wiring (CLAUDE.md mandate).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wiring_sdk_orchestrator_assembled_prompt_drops_all_four_sections(
    simple_turn_context_turn_three,
) -> None:
    """Integration / wiring test (repo CLAUDE.md "Every Test Suite Needs
    a Wiring Test"). Drive a real ``AnthropicSdkClient`` through
    ``Orchestrator.build_narrator_prompt`` and assert that NONE of the
    four section names appear in the registered section list.
    """
    orch = _make_sdk_orchestrator()
    _, registry = await orch.build_narrator_prompt("look around", simple_turn_context_turn_three)
    registered_names = {s.name for s in registry.registry(orch._narrator.name())}
    leaked = registered_names & set(GUARDRAIL_NAMES)
    assert not leaked, (
        f"SDK-path build_narrator_prompt leaked guardrail sections {sorted(leaked)} "
        "into the registry. The four registration sites at "
        "orchestrator.py:1764/1851/1934/1989 must stay gated behind a "
        "``not isinstance(self._client, ToolingLlmClient)`` check."
    )
