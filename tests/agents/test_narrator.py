"""Tests for sidequest/agents/narrator.py.

Port of sidequest-agents/src/agents/narrator.rs tests.
All assertions are against prompt structure, not LLM output.
No live Claude CLI calls.
"""

from __future__ import annotations

import pytest

from sidequest.agents.narrator import (
    NARRATOR_AGENCY,
    NARRATOR_CHASE_RULES,
    NARRATOR_COMBAT_RULES,
    NARRATOR_CONSTRAINTS,
    NARRATOR_DIALOGUE_RULES,
    NARRATOR_IDENTITY,
    NARRATOR_OUTPUT_ONLY,
    NARRATOR_OUTPUT_STYLE,
    NARRATOR_REFERRAL_RULE,
    NarratorAgent,
    narrator_output_format_text,
)
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import AttentionZone, SectionCategory

# ---------------------------------------------------------------------------
# NarratorAgent — construction
# ---------------------------------------------------------------------------


def test_narrator_agent_name():
    agent = NarratorAgent()
    assert agent.name() == "narrator"


def test_narrator_agent_system_prompt_is_identity():
    agent = NarratorAgent()
    assert agent.system_prompt() == NARRATOR_IDENTITY


def test_narrator_output_format_text_matches_constant():
    assert narrator_output_format_text() == NARRATOR_OUTPUT_ONLY


# ---------------------------------------------------------------------------
# NarratorAgent.build_context — section registration
# ---------------------------------------------------------------------------


def _build_registry(agent: NarratorAgent) -> PromptRegistry:
    registry = PromptRegistry()
    agent.build_context(registry)
    return registry


def test_build_context_registers_identity_section():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    sections = registry.registry("narrator")
    names = [s.name for s in sections]
    assert "narrator_identity" in names


def test_build_context_identity_in_primacy_zone():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    identity_sections = registry.get_sections(
        "narrator", zone=AttentionZone.Primacy, category=SectionCategory.Identity
    )
    assert any(s.name == "narrator_identity" for s in identity_sections)


def test_build_context_registers_constraints_guardrail():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    sections = registry.get_sections(
        "narrator", zone=AttentionZone.Primacy, category=SectionCategory.Guardrail
    )
    names = [s.name for s in sections]
    assert "narrator_constraints" in names


def test_build_context_registers_agency_guardrail():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    sections = registry.get_sections(
        "narrator", zone=AttentionZone.Primacy, category=SectionCategory.Guardrail
    )
    names = [s.name for s in sections]
    assert "narrator_agency" in names


def test_build_context_registers_consequences_guardrail():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    sections = registry.get_sections(
        "narrator", zone=AttentionZone.Primacy, category=SectionCategory.Guardrail
    )
    names = [s.name for s in sections]
    assert "narrator_consequences" in names


def test_build_context_registers_output_style_in_early_zone():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    sections = registry.get_sections("narrator", zone=AttentionZone.Early)
    names = [s.name for s in sections]
    assert "narrator_output_style" in names


def test_build_context_registers_referral_rule_in_early_zone():
    agent = NarratorAgent()
    registry = _build_registry(agent)
    sections = registry.get_sections("narrator", zone=AttentionZone.Early)
    names = [s.name for s in sections]
    assert "narrator_referral_rule" in names


def test_build_context_does_not_register_output_only():
    """narrator_output_only is injected by build_output_format, not build_context."""
    agent = NarratorAgent()
    registry = _build_registry(agent)
    names = [s.name for s in registry.registry("narrator")]
    assert "narrator_output_only" not in names


# ---------------------------------------------------------------------------
# NarratorAgent.build_output_format
# ---------------------------------------------------------------------------


def test_build_output_format_registers_section():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_output_format(registry)
    names = [s.name for s in registry.registry("narrator")]
    assert "narrator_output_only" in names


def test_build_output_format_in_primacy_zone():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_output_format(registry)
    sections = registry.get_sections(
        "narrator", zone=AttentionZone.Primacy, category=SectionCategory.Guardrail
    )
    assert any(s.name == "narrator_output_only" for s in sections)


def test_build_output_format_content_contains_game_patch():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_output_format(registry)
    section = next(s for s in registry.registry("narrator") if s.name == "narrator_output_only")
    assert "game_patch" in section.content


def test_narrator_output_format_retires_npcs_present_contract():
    """Story 151-5 / ADR-150 step 4 (cutover II): ``npcs_present`` is RETIRED from
    the narrator output contract. The post-narration extractor reads NPCs from prose
    and the engine seats combatant ``side`` (the IntentRouter seats opponents
    pre-narrator); the per-turn catch-loops (``_detect_missed_recurring_npcs`` /
    ``_auto_mint_prose_only_npcs``) remain the loud net for the old
    "confrontation panel has no combatants" regression. The narrator no longer emits
    ``npcs_present``, so its CRITICAL ADVERSARY RULE is gone from the contract.

    Forward regression guard: it must NOT come back here — a future "optimization"
    sweeping ``npcs_present`` back onto the narrator would reintroduce the Opus
    attention cost ADR-150 removed. Inverts the pre-151-5
    ``test_narrator_output_format_requires_adversaries_in_npcs_present`` (the 151-3
    ``retires_action_rewrite`` pattern)."""
    assert "npcs_present" not in NARRATOR_OUTPUT_ONLY, (
        "npcs_present is retired from the narrator contract in 151-5 — the "
        "post-narration extractor owns it; it must not be re-instructed here"
    )
    assert "CRITICAL ADVERSARY RULE" not in NARRATOR_OUTPUT_ONLY, (
        "the CRITICAL ADVERSARY RULE was the npcs_present emission guardrail — "
        "retired with the field in 151-5"
    )


# ---------------------------------------------------------------------------
# NarratorAgent.build_encounter_context
# ---------------------------------------------------------------------------


def test_build_encounter_context_registers_section():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_encounter_context(registry)
    names = [s.name for s in registry.registry("narrator")]
    assert "narrator_encounter_rules" in names


def test_build_encounter_context_contains_combat_rules():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_encounter_context(registry)
    section = next(s for s in registry.registry("narrator") if s.name == "narrator_encounter_rules")
    assert "COMBAT NARRATION RULES" in section.content


def test_build_encounter_context_contains_chase_rules():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_encounter_context(registry)
    section = next(s for s in registry.registry("narrator") if s.name == "narrator_encounter_rules")
    assert "CHASE NARRATION RULES" in section.content


def test_build_encounter_context_in_early_zone():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_encounter_context(registry)
    sections = registry.get_sections("narrator", zone=AttentionZone.Early)
    names = [s.name for s in sections]
    assert "narrator_encounter_rules" in names


# ---------------------------------------------------------------------------
# NarratorAgent.build_dialogue_context
# ---------------------------------------------------------------------------


def test_build_dialogue_context_registers_section():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_dialogue_context(registry)
    names = [s.name for s in registry.registry("narrator")]
    assert "narrator_dialogue_rules" in names


def test_build_dialogue_context_content_contains_dialogue_rules():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_dialogue_context(registry)
    section = next(s for s in registry.registry("narrator") if s.name == "narrator_dialogue_rules")
    assert "DIALOGUE NARRATION RULES" in section.content


def test_build_dialogue_context_in_early_zone():
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_dialogue_context(registry)
    sections = registry.get_sections("narrator", zone=AttentionZone.Early)
    names = [s.name for s in sections]
    assert "narrator_dialogue_rules" in names


# ---------------------------------------------------------------------------
# Composed prompt ordering
# ---------------------------------------------------------------------------


def test_compose_with_output_format_has_game_patch_before_player_action():
    """Output format section (Primacy) must appear before player action (Recency)."""
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_context(registry)
    agent.build_output_format(registry)
    registry.register_section(
        "narrator",
        __import__(
            "sidequest.agents.prompt_framework.types",
            fromlist=["PromptSection"],
        ).PromptSection.new(
            "player_action",
            "Player says: look around",
            AttentionZone.Recency,
            SectionCategory.Action,
        ),
    )
    composed = registry.compose("narrator")
    game_patch_pos = composed.find("game_patch")
    player_action_pos = composed.find("Player says: look around")
    assert game_patch_pos < player_action_pos


def test_build_context_wrong_type_raises():
    agent = NarratorAgent()
    with pytest.raises(TypeError, match="Expected PromptRegistry"):
        agent.build_context("not a registry")  # type: ignore[arg-type]


def test_build_output_format_wrong_type_raises():
    agent = NarratorAgent()
    with pytest.raises(TypeError, match="Expected PromptRegistry"):
        agent.build_output_format("not a registry")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Prompt constant content (spot checks for porting correctness)
# ---------------------------------------------------------------------------


def test_narrator_identity_is_gm_of_collaborative_rpg():
    assert "Game Master" in NARRATOR_IDENTITY
    assert "collaborative RPG" in NARRATOR_IDENTITY


def test_narrator_constraints_mentions_never_acknowledge():
    assert "NEVER acknowledge" in NARRATOR_CONSTRAINTS


def test_narrator_agency_mentions_player_controls():
    assert "player controls" in NARRATOR_AGENCY


def test_narrator_output_style_mentions_brevity():
    assert "BREVITY" in NARRATOR_OUTPUT_STYLE


def test_narrator_referral_rule_mentions_never_send_back():
    assert "NEVER send the player back" in NARRATOR_REFERRAL_RULE


def test_narrator_combat_rules_mentions_beat_selections():
    assert "beat_selections" in NARRATOR_COMBAT_RULES


def test_narrator_chase_rules_mentions_beat_selections():
    assert "beat_selections" in NARRATOR_CHASE_RULES


def test_narrator_dialogue_rules_mentions_npc_talk_only():
    assert "NEVER speak for the player character" in NARRATOR_DIALOGUE_RULES


# ---------------------------------------------------------------------------
# action_flags removal (dead-code demolition)
# ---------------------------------------------------------------------------


def test_narrator_output_format_does_not_contain_action_flags_token():
    """action_flags is write-only — never read by server/UI/daemon.
    This test ensures it's been removed from the prompt."""
    assert "action_flags" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must not contain 'action_flags' token"
    )


def test_narrator_output_format_does_not_contain_is_power_grab():
    """is_power_grab is a dead action_flags field."""
    assert "is_power_grab" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must not contain 'is_power_grab'"
    )


def test_narrator_output_format_does_not_contain_references_inventory():
    """references_inventory is a dead action_flags field."""
    assert "references_inventory" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must not contain 'references_inventory'"
    )


def test_narrator_output_format_does_not_contain_references_npc():
    """references_npc is a dead action_flags field."""
    assert "references_npc" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must not contain 'references_npc'"
    )


def test_narrator_output_format_does_not_contain_references_ability():
    """references_ability is a dead action_flags field."""
    assert "references_ability" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must not contain 'references_ability'"
    )


def test_narrator_output_format_does_not_contain_references_location():
    """references_location is a dead action_flags field."""
    assert "references_location" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must not contain 'references_location'"
    )


def test_narrator_output_format_retires_action_rewrite():
    """Story 151-3 (ADR-150 step 3): action_rewrite is retired from the narrator
    output contract — produced by the pre-narrator IntentRouter now, not the
    narrator game_patch. It must NOT remain in the prompt."""
    assert "action_rewrite" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must no longer contain 'action_rewrite' — retired "
        "to the IntentRouter pre-pass in 151-3"
    )


# ---------------------------------------------------------------------------
# Story 45-53: Recurring NPC presence — prompt content
#
# Playtest 3 (2026-04-19) and follow-up sessions surfaced a recurring failure:
# named NPCs (allies, merchants, quest-givers, bystanders) introduced in turn
# N would vanish from ``npcs_met`` on turns N+1..N+k even when narration prose
# clearly placed them onstage. The CRITICAL ADVERSARY RULE only forces
# emission for confrontation adversaries; outside combat the existing prompt
# language ("every named NPC ... the player encounters") is ambiguous about
# recurring presence. James (narrative-first) and Sebastien (mechanical lie-
# detector) both feel the gap — once an NPC drops out of npcs_met the
# narrator prompt and the GM panel both stop knowing the NPC is in the
# scene, breaking encounter continuity and NPC-centric narrative arcs.
# ---------------------------------------------------------------------------


def test_narrator_output_format_retires_recurring_presence_guardrails():
    """Story 151-5 / ADR-150 step 4 (cutover II): the npcs_present recurring-presence
    guardrails (the 'every turn' / 'onstage' / 'passing mention' / RECURRING-block
    rules added for the James+Sebastien continuity gap) are RETIRED with the field.
    The narrator no longer emits ``npcs_present``; the post-narration extractor reads
    NPCs from prose and the catch-loops (``_detect_missed_recurring_npcs`` /
    ``_auto_mint_prose_only_npcs``) are the loud net for recurring-NPC continuity.

    Forward regression guard: these guardrails must NOT return to the narrator
    contract. Inverts the three pre-151-5 recurring-presence guardrail tests (the
    151-3 ``retires_action_rewrite`` pattern)."""
    text = NARRATOR_OUTPUT_ONLY.lower()
    assert "passing mention" not in text, (
        "the 'passing mention' recurring-presence guardrail was an npcs_present "
        "emission rule — retired with the field in 151-5"
    )
    assert "onstage" not in text, (
        "the 'onstage' recurring-presence guardrail was an npcs_present emission "
        "rule — retired with the field in 151-5"
    )
    assert "RECURRING" not in NARRATOR_OUTPUT_ONLY, (
        "the RECURRING PRESENCE RULE block was the npcs_present emission guardrail — "
        "retired with the field in 151-5"
    )
    # The recurring rule must contain the word 'MANDATORY' to mirror the
    # CRITICAL ADVERSARY RULE's strength (it currently uses MANDATORY too).
    # We don't reassert MANDATORY here — the AC1 'every turn' / 'onstage'
    # tests above are the operational guard. This test is purely about the
    # rule having its own labeled block.
