"""Tests for sidequest/agents/orchestrator.py — Phase 1 narration pipeline.

No live Claude CLI calls. All Claude interactions are mocked via ClaudeClient
with a canned spawn_fn.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import (
    ActionRewrite,
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
    Orchestrator,
    TurnContext,
    _extract_game_patch_json,
    _strip_json_fence,
    extract_structured_from_response,
)
from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.protocol.dispatch import (
    DispatchPackage,
    NarratorDirective,
    PlayerDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Helpers — fake subprocess process and canned spawn
# ---------------------------------------------------------------------------


class FakeProcess:
    """Minimal asyncio.subprocess.Process stand-in for tests."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = b""
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def make_json_response(text: str, session_id: str = "test-session-001") -> bytes:
    """Build a minimal Claude CLI JSON envelope from a canned text response."""
    payload = {
        "result": text,
        "session_id": session_id,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    return json.dumps(payload).encode()


def make_spawn_fn(text: str, session_id: str = "test-session-001"):
    """Return a spawn_fn that returns the given canned narrator text."""

    async def spawn_fn(command: str, *args: str, env: Any = None, **kwargs: Any) -> FakeProcess:
        return FakeProcess(stdout=make_json_response(text, session_id=session_id))

    return spawn_fn


def make_canned_client(narration_text: str, session_id: str = "test-session-001") -> ClaudeClient:
    """Build a ClaudeClient whose subprocess always returns narration_text."""
    return ClaudeClient(spawn_fn=make_spawn_fn(narration_text, session_id=session_id))


# ---------------------------------------------------------------------------
# _extract_game_patch_json
# ---------------------------------------------------------------------------


def test_extract_game_patch_json_from_game_patch_fence():
    raw = '**The Tavern**\n\nSome prose.\n\n```game_patch\n{"location": "Tavern"}\n```'
    result = _extract_game_patch_json(raw)
    assert result == {"location": "Tavern"}


def test_extract_game_patch_json_from_json_fence_fallback():
    raw = '**The Tavern**\n\nSome prose.\n\n```json\n{"location": "Docks"}\n```'
    result = _extract_game_patch_json(raw)
    assert result == {"location": "Docks"}


def test_extract_game_patch_json_returns_empty_dict_on_no_fence():
    raw = "**The Tavern**\n\nSome prose with no JSON block."
    result = _extract_game_patch_json(raw)
    assert result == {}


def test_extract_game_patch_json_warns_on_malformed_json(caplog):
    import logging

    raw = "```game_patch\n{invalid json}\n```"
    with caplog.at_level(logging.WARNING, logger="sidequest.agents.orchestrator"):
        result = _extract_game_patch_json(raw)
    assert result == {}
    assert "failed to parse" in caplog.text


# ---------------------------------------------------------------------------
# _strip_json_fence
# ---------------------------------------------------------------------------


def test_strip_json_fence_removes_game_patch_block():
    raw = "**The Tavern**\n\nSome prose.\n\n```game_patch\n{}\n```"
    assert _strip_json_fence(raw) == "**The Tavern**\n\nSome prose."


def test_strip_json_fence_removes_json_block():
    raw = "Prose here.\n\n```json\n{}\n```"
    assert _strip_json_fence(raw) == "Prose here."


def test_strip_json_fence_returns_text_unchanged_if_no_fence():
    raw = "**The Tavern**\n\nSome prose only."
    assert _strip_json_fence(raw) == raw.strip()


def test_strip_json_fence_warns_and_discards_post_patch_content(caplog):
    import logging

    raw = "Prose.\n\n```game_patch\n{}\n```\n\nNote: I've been helpful."
    with caplog.at_level(logging.WARNING, logger="sidequest.agents.orchestrator"):
        result = _strip_json_fence(raw)
    assert result == "Prose."
    assert "discarding post-patch content" in caplog.text


# ---------------------------------------------------------------------------
# extract_structured_from_response
# ---------------------------------------------------------------------------


def test_extract_structured_returns_prose():
    raw = "**The Docks**\n\nThe smell of brine.\n\n```game_patch\n{}\n```"
    result = extract_structured_from_response(raw)
    assert result["prose"] == "**The Docks**\n\nThe smell of brine."


def test_extract_structured_extracts_location():
    raw = '```game_patch\n{"location": "Docks"}\n```'
    result = extract_structured_from_response(raw)
    assert result["location"] == "Docks"


def test_extract_structured_surfaces_narrator_footnotes():
    """RENDER-NO-SUBJECT (ADR-150 amendment 2026-06-20): ``footnotes`` is the
    player's knowledge/journal feed — a GENERATIVE/authorial narrator output the
    post-narration never-invent reader cannot produce. It was CARVED BACK OUT of the
    151-5 cutover to narrator-owned, so ``extract_structured_from_response`` must
    once again surface it from the game_patch (the never-invent extractor returning
    [] every turn caused known_facts=0 on mystery worlds)."""
    raw = '```game_patch\n{"footnotes": [{"summary": "The key is lost", "category": "Lore", "is_new": true}]}\n```'
    result = extract_structured_from_response(raw)
    assert result["footnotes"] == [
        {"summary": "The key is lost", "category": "Lore", "is_new": True}
    ], (
        "game_patch footnotes must be surfaced — narrator-owned again "
        "(ADR-150 amendment, RENDER-NO-SUBJECT)"
    )


def test_extract_structured_no_longer_surfaces_items_gained():
    """Story 151-4 / ADR-150 step 4: the seven transactional fields (items×4,
    gold_change, companions×2) are retired from the narrator game_patch — the
    post-narration sidecar extractor produces them now and
    ``narration_apply.merge_sidecar_extraction_transactional`` sources them onto
    the result before apply. ``extract_structured_from_response`` must no longer
    surface ``items_gained`` even when a (non-compliant) narrator still emits the
    block. Inverts the pre-151-4 ``test_extract_structured_extracts_items_gained``
    (mirrors the 151-3 action_rewrite inversion below)."""
    raw = '```game_patch\n{"items_gained": [{"name": "Rusty Key", "description": "An old key", "category": "misc"}]}\n```'
    result = extract_structured_from_response(raw)
    assert result["items_gained"] == [], (
        "game_patch items_gained must no longer be extracted — retired in 151-4 "
        "(the post-narration sidecar extractor owns it)"
    )


def test_extract_structured_extracts_beat_selections():
    raw = '```game_patch\n{"beat_selections": [{"actor": "Player", "beat_id": "attack", "target": "Goblin"}]}\n```'
    result = extract_structured_from_response(raw)
    assert len(result["beat_selections"]) == 1
    assert result["beat_selections"][0]["actor"] == "Player"


def test_extract_structured_extracts_confrontation():
    raw = '```game_patch\n{"confrontation": "combat"}\n```'
    result = extract_structured_from_response(raw)
    assert result["confrontation"] == "combat"


def test_extract_structured_drops_legacy_npcs_met_key():
    """Story 61-12 AC-1 removed the silent fallback at orchestrator.py:1003
    that aliased ``npcs_met`` → ``npcs_present``. The canonical sidecar key
    is ``npcs_present`` everywhere else in the codebase (protocol, DB,
    NarrationResult, narration_apply, emitters, telemetry); the prose
    enforces it; the parser no longer rescues the wrong spelling.
    Narrator that emits ``npcs_met`` drops to ``[]`` (the same behavior as
    any missing sidecar field) so the lie detector (OTEL spans /
    render_trigger) fires on the divergence instead of papering it over.
    """
    raw = '```game_patch\n{"npcs_met": ["Toggler"]}\n```'
    result = extract_structured_from_response(raw)
    assert result["npcs_present"] == []


def test_extract_structured_no_longer_surfaces_gold_change():
    """Story 151-4 / ADR-150 step 4: ``gold_change`` is retired from the narrator
    game_patch (extractor-sourced now). ``extract_structured_from_response`` must
    surface ``None`` even when a narrator still emits it. Inverts the pre-151-4
    ``test_extract_structured_extracts_gold_change``."""
    raw = '```game_patch\n{"gold_change": -10}\n```'
    result = extract_structured_from_response(raw)
    assert result["gold_change"] is None, (
        "game_patch gold_change must no longer be extracted — retired in 151-4"
    )


def test_extract_structured_no_longer_surfaces_action_rewrite():
    """Story 151-3 / ADR-150 step 3 (AC4): action_rewrite is retired from the
    narrator's game_patch — it is produced PRE-narrator by the IntentRouter now.
    ``extract_structured_from_response`` must no longer surface it even when a
    (non-compliant) narrator still emits the block. Inverts the pre-151-3
    ``test_extract_structured_extracts_action_rewrite``."""
    raw = '```game_patch\n{"action_rewrite": {"you": "You look around", "named": "Kael looks around", "intent": "look around"}}\n```'
    result = extract_structured_from_response(raw)
    assert result.get("action_rewrite") is None, (
        "game_patch action_rewrite must no longer be extracted — the narrator "
        "sidecar parse is retired in 151-3 (pre-pass IntentRouter owns it)"
    )


def test_extract_structured_extracts_affinity_progress():
    raw = '```game_patch\n{"affinity_progress": [{"name": "combat_mastery", "delta": 1}]}\n```'
    result = extract_structured_from_response(raw)
    assert result["affinity_progress"] == [("combat_mastery", 1)]


def test_extract_structured_empty_patch_returns_defaults():
    raw = "Some prose.\n\n```game_patch\n{}\n```"
    result = extract_structured_from_response(raw)
    assert result["footnotes"] == []
    assert result["items_gained"] == []
    assert result["beat_selections"] == []
    assert result["confrontation"] is None
    assert result["location"] is None


# ---------------------------------------------------------------------------
# NpcMention.from_value
# ---------------------------------------------------------------------------


def test_npc_mention_from_full_struct():
    npc = NpcMention.from_value({"name": "Toggler Copperjaw", "role": "blacksmith", "is_new": True})
    assert npc.name == "Toggler Copperjaw"
    assert npc.role == "blacksmith"
    assert npc.is_new is True


def test_npc_mention_from_bare_string():
    npc = NpcMention.from_value("Nub")
    assert npc.name == "Nub"
    assert npc.role == ""
    assert npc.is_new is False


def test_npc_mention_vec_mixed_formats():
    values = [{"name": "Toggler", "role": "smith"}, "Nub", {"name": "Vera"}]
    npcs = [NpcMention.from_value(v) for v in values]
    assert len(npcs) == 3
    assert npcs[0].name == "Toggler"
    assert npcs[1].name == "Nub"
    assert npcs[2].name == "Vera"


# ---------------------------------------------------------------------------
# BeatSelection.from_dict
# ---------------------------------------------------------------------------


def test_beat_selection_from_dict():
    bs = BeatSelection.from_dict({"actor": "Player", "beat_id": "attack", "target": "Goblin"})
    assert bs.actor == "Player"
    assert bs.beat_id == "attack"
    assert bs.target == "Goblin"


def test_beat_selection_no_target():
    bs = BeatSelection.from_dict({"actor": "Goblin", "beat_id": "defend"})
    assert bs.target is None


# ---------------------------------------------------------------------------
# ActionRewrite
# ---------------------------------------------------------------------------


def test_action_rewrite_from_dict():
    ar = ActionRewrite.from_dict(
        {"you": "You draw your sword", "named": "Kael draws their sword", "intent": "draw sword"}
    )
    assert ar.you == "You draw your sword"
    assert ar.intent == "draw sword"


# ---------------------------------------------------------------------------
# Orchestrator.build_narrator_prompt — structure
# ---------------------------------------------------------------------------


async def test_build_narrator_prompt_full_contains_narrator_identity():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", state_summary="You are in a tavern.")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "Game Master" in prompt


async def test_build_narrator_prompt_full_contains_output_format():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "game_patch" in prompt


async def test_build_narrator_prompt_includes_npc_intro_visual_constraint():
    """Pingpong 2026-05-03 [BUG] — render policy fired NPC_INTRO but the
    narrator emitted no ``visual_scene`` for the newly introduced NPCs.
    The fix is a standing prompt guardrail telling the narrator that
    every ``is_new: true`` entry on ``npcs_met`` requires a matching
    ``visual_scene``. This test pins the constraint into the prompt.

    Runs every turn (NOT gated on ``is_full``) because new NPCs can
    appear on any turn.
    """
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "<npc-intro-visual>" in prompt
    assert "is_new: true" in prompt
    assert "visual_scene" in prompt


async def test_npc_intro_visual_constraint_present_on_delta_tier():
    """The constraint runs on Delta tier too — new NPCs land on any turn,
    not just opening / Full-tier prompts."""
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "<npc-intro-visual>" in prompt


async def test_build_narrator_prompt_includes_confrontation_trigger_constraint():
    """Pingpong 2026-05-03 [BUG] — narrator wrote a textbook chase-firing
    beat ("patrol cutter spinning her reactor up from cold-soak. She isn't
    moving yet. She's asking the tower whether to.") but emitted
    ``confrontation=None``. No encounter fired; the lie-detector pattern
    fired in fiction without mechanical follow-through. The schema-block
    instruction in narrator.py says "MUST emit confrontation" on triggers,
    but lives deep in the System zone where attention has decayed by
    turn 20+.

    This test pins the per-turn Recency-zone Guardrail PromptSection that
    restates the rule with concrete trigger language and the
    must-not-defer constraint. Same shape as
    ``npc_intro_visual_constraint`` above.
    """
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Itchy")
    prompt, _ = await orch.build_narrator_prompt("watch the gantry", context)
    assert "<confrontation-trigger>" in prompt
    # Concrete trigger phrases the narrator just missed
    assert "spinning" in prompt
    assert "permission to engage" in prompt
    # The deferral failure mode is called out explicitly
    assert "no retroactive crediting" in prompt
    # Genre-specific encounter types are referenced so the narrator picks
    # ``ship_combat`` or ``dogfight`` rather than defaulting to ``combat``
    assert "ship_combat" in prompt
    assert "dogfight" in prompt


async def test_confrontation_trigger_constraint_present_on_delta_tier():
    """The constraint runs on Delta tier too — encounter triggers can land
    on any turn, not just opening / Full-tier prompts. The original bug
    fired at turn 20 (deep in a Delta-tier window), so the Delta-tier
    presence is the one that matters most for this fix.
    """
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Itchy")
    prompt, _ = await orch.build_narrator_prompt("watch the gantry", context)
    assert "<confrontation-trigger>" in prompt
    assert "no retroactive crediting" in prompt


async def test_build_narrator_prompt_contains_player_action():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    prompt, _ = await orch.build_narrator_prompt("examine the door", context)
    assert "examine the door" in prompt
    assert "Kael" in prompt


async def test_build_narrator_prompt_merged_actions_render_per_pc_block():
    """Multiplayer merged turn must render every PC's declaration on its own line.

    Regression: 2026-04-29 multiplayer playtest. The previous prompt format was
    ``"<one PC> says: <merged blob>"`` which both attributed every player's
    action to the dispatch winner and invited the LLM to generate dialogue
    for PCs whose players had only declared physical actions (SOUL.md
    "Agency" violation: "If a response includes the player doing something
    they didn't ask to do, it's wrong").
    """
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    # Dispatch winner (character_name) is Laverne; merged action contains
    # both PCs. The combined-action string is what the handler builds today
    # — the orchestrator must NOT use it as the source of attribution.
    context = TurnContext(
        character_name="Laverne",
        merged_player_actions=[
            ("Shirley", "I look at Laverne"),
            ("Laverne", "I look at Shirley"),
        ],
    )
    prompt, _ = await orch.build_narrator_prompt(
        "Shirley: I look at Laverne\nLaverne: I look at Shirley",
        context,
    )
    # Each PC's declaration appears on its own attributed line.
    assert "Shirley declares: I look at Laverne" in prompt
    assert "Laverne declares: I look at Shirley" in prompt
    # The merged blob is NOT wrapped under a single "Laverne says:" header.
    assert "Laverne says: Shirley:" not in prompt
    # The inline strict reminder is present adjacent to the action block.
    assert "Do NOT generate dialogue" in prompt


async def test_build_narrator_prompt_seeded_opening_marks_invitation_already_shown():
    """Pingpong 2026-06-05 [BAR-1]: a seeded opening turn passes the authored
    ``first_turn_invitation`` (already cold-opened to the player) as the
    action. The recency block must mark it already-displayed and forbid
    restating — the prior ``"<PC> says: <invitation>"`` framing cued the
    narrator's action-rewrite contract to novelize the invitation back,
    doubling every seeded opening's prose.
    """
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Groucho", opening_seed_shown=True)
    invitation = "The wind off the desert is thin and hard at this height."
    prompt, _ = await orch.build_narrator_prompt(invitation, context)
    assert "ALREADY been shown to the player" in prompt
    assert "do NOT repeat" in prompt
    assert f"<already-shown-invitation>\n{invitation}\n</already-shown-invitation>" in prompt
    # The invitation is NOT framed as the player speaking.
    assert f"Groucho says: {invitation}" not in prompt


async def test_build_narrator_prompt_solo_action_unchanged():
    """Single-player turns keep the existing 'X says: ...' framing.

    Guard against accidentally changing solo behavior while fixing the MP bug.
    """
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", merged_player_actions=None)
    prompt, _ = await orch.build_narrator_prompt(
        "look around",
        context,
    )
    assert "Kael says: look around" in prompt


def test_narrator_agency_constant_forbids_pc_dialogue_generation():
    """The strengthened agency rule must explicitly forbid PC dialogue.

    Without this string in the Primacy guardrail, the multiplayer fix is
    incomplete — the prompt structure handles attribution, but the rule
    is what stops the LLM from inventing speech for an unspoken PC.
    """
    from sidequest.agents.narrator import NARRATOR_AGENCY

    lower = NARRATOR_AGENCY.lower()
    assert "dialogue" in lower
    assert "must not" in lower or "may not" in lower
    # Specifically calls out the speaking case.
    assert "speak" in lower


async def test_build_narrator_prompt_includes_genre_identity():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", genre="caverns_and_claudes")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "caverns and claudes" in prompt


async def test_build_narrator_prompt_full_contains_verbosity_limit():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", narrator_verbosity="concise")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "400 characters" in prompt


async def test_build_narrator_prompt_full_contains_vocabulary_section():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", narrator_vocabulary="epic")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "archaic" in prompt


async def test_build_narrator_prompt_includes_state_summary():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(
        character_name="Kael",
        state_summary="You are in a dark cave. HP: 10/10.",
    )
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "dark cave" in prompt


async def test_build_narrator_prompt_includes_lore_context_when_provided():
    # Story 37-33: retrieved lore from semantic search should land in
    # the Valley zone so the narrator has canonical world detail to
    # weave in without asking the player.
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(
        character_name="Kael",
        lore_context=(
            "<lore>\n"
            "# Relevant lore retrieved for this turn\n"
            "- [history · id=castle · similarity=0.92] An ancient castle stands on the hill.\n"
            "</lore>"
        ),
    )
    prompt, _ = await orch.build_narrator_prompt("approach the castle", context)
    assert "<lore>" in prompt
    assert "ancient castle" in prompt


async def test_build_narrator_prompt_omits_lore_section_when_none():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", lore_context=None)
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "<lore>" not in prompt


async def test_build_narrator_prompt_encounter_rules_when_in_combat():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", in_combat=True)
    prompt, _ = await orch.build_narrator_prompt("attack goblin", context)
    assert "COMBAT NARRATION RULES" in prompt


async def test_build_narrator_prompt_no_encounter_rules_when_not_in_combat():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", in_combat=False, in_chase=False)
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "COMBAT NARRATION RULES" not in prompt


async def test_build_narrator_prompt_player_action_last_in_zone_order():
    """player_action (Recency) must appear after identity (Primacy) in composed output."""
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    prompt, _ = await orch.build_narrator_prompt("cast spell", context)
    identity_pos = prompt.find("Game Master")
    action_pos = prompt.find("cast spell")
    assert identity_pos < action_pos


async def test_build_narrator_prompt_trope_context_injected():
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(
        character_name="Kael",
        pending_trope_context="WEAVE THIS: The ancient curse stirs.",
    )
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "WEAVE THIS" in prompt


# ---------------------------------------------------------------------------
# Orchestrator.run_narration_turn — async turn pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_narration_turn_returns_narration():
    narration_text = (
        "**The Tavern**\n\nThe smell of stale ale fills the air.\n\n```game_patch\n{}\n```"
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", current_location="The Tavern")
    result = await orch.run_narration_turn("look around", context)
    assert "stale ale" in result.narration
    assert not result.is_degraded


@pytest.mark.asyncio
async def test_run_narration_turn_extracts_location():
    narration_text = (
        '**The Docks**\n\nThe sea glitters.\n\n```game_patch\n{"location": "The Docks"}\n```'
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    result = await orch.run_narration_turn("look around", context)
    assert result.location == "The Docks"


@pytest.mark.asyncio
async def test_run_narration_turn_extracts_confrontation():
    narration_text = (
        "**The Alley**\n\nThe bandit draws a knife.\n\n"
        '```game_patch\n{"confrontation": "combat"}\n```'
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    result = await orch.run_narration_turn("attack the bandit", context)
    assert result.confrontation == "combat"


@pytest.mark.asyncio
async def test_run_narration_turn_no_longer_sources_npcs_from_game_patch():
    """Story 151-5 / ADR-150 step 4 (cutover II): ``npcs_present`` is retired from
    the narrator game_patch — the orchestrator pipeline no longer sources it. The
    post-narration extractor produces it and
    ``narration_apply.merge_sidecar_extraction_npcs_present`` sources it onto the
    result (with engine-owned ``side``) in the WS handler, AFTER this turn. So a
    game_patch ``npcs_present`` does NOT reach ``result.npcs_present`` here. Inverts
    the pre-151-5 ``test_run_narration_turn_extracts_npcs``."""
    narration_text = (
        "**The Market**\n\nThe vendor smiles.\n\n"
        '```game_patch\n{"npcs_present": ["Nub the Vendor"]}\n```'
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    result = await orch.run_narration_turn("talk to vendor", context)
    assert result.npcs_present == [], (
        "npcs_present must no longer be sourced from the game_patch at the "
        "orchestrator level — retired in 151-5 (post-narration extractor owns it)"
    )


@pytest.mark.asyncio
async def test_run_narration_turn_degraded_on_claude_error():
    """ADR-005: CLI failure returns degraded response, not exception."""

    async def failing_spawn(command: str, *args: str, **kwargs: Any) -> Any:
        raise RuntimeError("Claude binary not found")

    client = ClaudeClient(spawn_fn=failing_spawn)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael", current_location="The Tavern")
    result = await orch.run_narration_turn("look around", context)
    assert result.is_degraded
    assert "world holds its breath" in result.narration.lower()


@pytest.mark.asyncio
async def test_run_narration_turn_records_otel_fields():
    narration_text = "**The Tavern**\n\nProse.\n\n```game_patch\n{}\n```"
    client = make_canned_client(narration_text, session_id="sess-123")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    result = await orch.run_narration_turn("look around", context)
    assert result.agent_name == "narrator"
    assert result.agent_duration_ms is not None
    assert result.token_count_in is not None
    assert result.token_count_out is not None


@pytest.mark.asyncio
async def test_run_narration_turn_no_longer_warns_action_rewrite_absent_from_extraction(caplog):
    """Story 151-3 / ADR-150 step 3 (AC4): action_rewrite is no longer a
    narrator-emitted game_patch field, so an empty game_patch is EXPECTED and
    must NOT trip the legacy 'action_rewrite absent from extraction' warning.
    The absence loud-net moves to the pre-pass ``intent_router.action_rewrite``
    span (emitted=False). Inverts the pre-151-3 warns-missing test."""
    import logging

    narration_text = "**The Tavern**\n\nProse.\n\n```game_patch\n{}\n```"
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    with caplog.at_level(logging.WARNING, logger="sidequest.agents.orchestrator"):
        await orch.run_narration_turn("look around", context)
    assert "absent from extraction" not in caplog.text, (
        "the narrator no longer owns action_rewrite — the extraction-absent "
        "warning is retired (loud net is the pre-pass span)"
    )


@pytest.mark.asyncio
async def test_run_narration_turn_no_longer_surfaces_items_gained_from_game_patch():
    """Story 151-4 / ADR-150 step 4: ``run_narration_turn`` no longer surfaces the
    transactional fields from the narrator game_patch — they are retired and
    sourced post-narration by the sidecar extractor + merge (in the WS handler,
    after this method returns). ``result.items_gained`` is therefore empty at the
    orchestrator boundary. Inverts the pre-151-4
    ``test_run_narration_turn_extracts_items_gained``."""
    narration_text = (
        "**The Chest**\n\nYou find a rusty key.\n\n"
        '```game_patch\n{"items_gained": [{"name": "Rusty Key", "description": "An old key", "category": "misc"}]}\n```'
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Kael")
    result = await orch.run_narration_turn("open chest", context)
    assert result.items_gained == [], (
        "items_gained is retired from the game_patch in 151-4 — the post-narration "
        "extractor + merge_sidecar_extraction_transactional source it now"
    )


@pytest.mark.asyncio
async def test_run_narration_turn_extracts_status_changes():
    """Wiring: status_changes from game_patch flows through to NarrationTurnResult."""
    narration_text = (
        "**The Arena**\n\nSam ducks the swing.\n\n"
        "```game_patch\n"
        '{"status_changes": [{"actor": "Sam", "status": {"text": "Bruised Ribs", "severity": "Wound"}}]}\n'
        "```"
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Sam")
    result = await orch.run_narration_turn("defend", context)
    assert result.status_changes == [
        {"actor": "Sam", "status": {"text": "Bruised Ribs", "severity": "Wound"}},
    ]


@pytest.mark.asyncio
async def test_run_narration_turn_genre_prompts_injected():
    """Genre prompts from prompts.yaml appear in the assembled prompt."""
    from sidequest.genre.models.narrative import Prompts

    narration_text = "**The Cavern**\n\nProse.\n\n```game_patch\n{}\n```"
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    context = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        genre_prompts=Prompts(
            narrator="Narrate with dungeon grit.",
            combat="Keep combat brutal.",
            npc="NPCs speak in riddles.",
            world_state="Track the dungeon state.",
        ),
    )
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "dungeon grit" in prompt
    assert "NPCs speak in riddles" in prompt


# ---------------------------------------------------------------------------
# Group A Task 2 — ActionFlags removal tests
# ---------------------------------------------------------------------------


def test_narration_turn_result_has_no_action_flags():
    """Group A Task 2 — ActionFlags dataclass is retired."""
    from dataclasses import fields

    field_names = {f.name for f in fields(NarrationTurnResult)}
    assert "action_flags" not in field_names, "action_flags still on NarrationTurnResult"


def test_action_flags_class_is_removed_from_orchestrator():
    """Group A Task 2 — ActionFlags dataclass itself is gone."""
    from sidequest.agents import orchestrator

    assert not hasattr(orchestrator, "ActionFlags"), (
        "ActionFlags dataclass still defined in orchestrator module"
    )


def test_action_flags_not_exported_from_agents_package():
    """Group A Task 2 — ActionFlags removed from agents package exports."""
    from sidequest.agents import __all__

    assert "ActionFlags" not in __all__, "ActionFlags still exported from sidequest.agents.__all__"


def test_action_rewrite_still_present():
    """Guard: ActionRewrite is LIVE — must not be touched.

    Story 151-3 retires only the game_patch *parse* of action_rewrite, not the
    field: NarrationTurnResult.action_rewrite stays (now sourced pre-pass) so
    visibility_classifier + confrontation_intent_validator + narration_apply
    keep reading it (ordering hazard closed for all)."""
    from dataclasses import fields

    from sidequest.agents.orchestrator import ActionRewrite

    assert ActionRewrite is not None
    field_names = {f.name for f in fields(NarrationTurnResult)}
    assert "action_rewrite" in field_names, (
        "action_rewrite must remain on NarrationTurnResult — not in scope for removal"
    )


# ---------------------------------------------------------------------------
# Story 151-3 / ADR-150 step 3 — action_rewrite migrates to the IntentRouter
# pre-pass. The narrator game_patch no longer feeds it; the result sources it
# from TurnContext.dispatch_package.action_rewrite (the pre-pass value).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_narration_turn_ignores_game_patch_action_rewrite_when_no_pre_pass():
    """AC4 retirement guard (result level): the narrator's game_patch
    action_rewrite is no longer parsed onto the result. With NO pre-pass package
    present, ``result.action_rewrite`` is None — the narrator can no longer drive
    the field, which is the root of the ordering hazard ADR-150 §1 closes."""
    narration_text = (
        "**Scene**\n\nKael moves.\n\n"
        '```game_patch\n{"action_rewrite": '
        '{"you": "You move", "named": "Kael moves", "intent": "move"}}\n```'
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    ctx = TurnContext(character_name="Kael", dispatch_package=None)
    result = await orch.run_narration_turn("move", ctx)
    assert result.action_rewrite is None, (
        "game_patch action_rewrite must NOT populate the result — ADR-150 step 3 "
        "retires the narrator sidecar parse; with no pre-pass it stays unset"
    )


@pytest.mark.asyncio
async def test_result_action_rewrite_sourced_from_pre_pass_not_game_patch():
    """AC2 (the wiring test): ``result.action_rewrite`` is the PRE-PASS
    IntentRouter value carried on ``TurnContext.dispatch_package``, NOT the
    narrator's game_patch. When both are present, the pre-pass WINS — proving the
    field's provenance flipped pre-narrator and every post-narrator consumer
    (visibility_classifier, confrontation_intent_validator, narration_apply) now
    reads the pre-pass value. Closes the ordering hazard for all at once."""
    # Narrator still (non-compliantly) emits an action_rewrite "lie" in game_patch.
    narration_text = (
        "**Scene**\n\nKael steps forward.\n\n"
        '```game_patch\n{"action_rewrite": '
        '{"you": "GAME_PATCH", "named": "GAME_PATCH", "intent": "game_patch"}}\n```'
    )
    client = make_canned_client(narration_text)
    orch = Orchestrator(client=client)
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[],
        cross_player=[],
        confidence_global=1.0,
        action_rewrite={
            "you": "You step forward",
            "named": "Kael steps forward",
            "intent": "advance",
        },
    )
    # A present dispatch_package implies the pre-narrator pass ran and stashed
    # its BankResult (build_narrator_prompt fails loud otherwise) — mirror that
    # production-valid state, as the narrator_directives test above does.
    ctx = TurnContext(
        character_name="Kael",
        dispatch_package=pkg,
        bank_result=await run_dispatch_bank(pkg),
    )
    result = await orch.run_narration_turn("step forward", ctx)

    assert result.action_rewrite is not None
    assert result.action_rewrite.named == "Kael steps forward", (
        "result.action_rewrite must come from the pre-pass DispatchPackage, not "
        "the narrator's retired game_patch sidecar"
    )
    assert result.action_rewrite.intent == "advance"


def test_narration_turn_result_has_no_classified_intent():
    """Group A Task 3 — classified_intent dead hardcode retired."""
    from dataclasses import fields

    field_names = {f.name for f in fields(NarrationTurnResult)}
    assert "classified_intent" not in field_names, "classified_intent still on NarrationTurnResult"


def test_orchestrator_module_has_no_classified_intent_hardcode():
    """Group A Task 3 — no classified_intent = 'exploration' assignment in source."""
    import inspect

    from sidequest.agents import orchestrator

    source = inspect.getsource(orchestrator)
    assert 'classified_intent = "exploration"' not in source, (
        'Hardcoded classified_intent = "exploration" still present'
    )
    assert "classified_intent = 'exploration'" not in source, (
        "Hardcoded classified_intent = 'exploration' still present"
    )


def test_turn_context_defaults_dispatch_package_to_none():
    """Group B Task 8 — optional field defaults to None.

    All other TurnContext fields have defaults; constructing with no args
    should succeed and dispatch_package should read as None.
    """
    tc = TurnContext()
    assert tc.dispatch_package is None


def test_turn_context_accepts_dispatch_package():
    """Group B Task 8 — the new field is populated via kwarg."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[],
        cross_player=[],
        confidence_global=1.0,
    )
    tc = TurnContext(dispatch_package=pkg)
    assert tc.dispatch_package is pkg


# ---------------------------------------------------------------------------
# Task 9 — narrator_directives section injection from DispatchPackage
# ---------------------------------------------------------------------------


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


async def test_build_narrator_prompt_registers_narrator_directives_when_present():
    """When TurnContext.dispatch_package has authored directives, the narrator
    prompt registry contains a 'narrator_directives' section and the directive
    payloads appear in the rendered prompt text."""
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="Let's go!",
                resolved=[],
                dispatch=[],
                lethality=[],
                narrator_instructions=[
                    NarratorDirective(
                        kind="must_not_narrate",
                        payload="zzz-must-not-payload-zzz",
                        visibility=_tag_all(),
                    ),
                    NarratorDirective(
                        kind="must_narrate",
                        payload="zzz-must-narrate-payload-zzz",
                        visibility=_tag_all(),
                    ),
                ],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )
    # The dispatch bank runs ONCE in the pre-narrator pass; the orchestrator
    # consumes its BankResult. Mirror that here.
    ctx = TurnContext(dispatch_package=pkg, bank_result=await run_dispatch_bank(pkg))

    prompt_text, registry = await orch.build_narrator_prompt("Let's go!", ctx)

    assert "zzz-must-not-payload-zzz" in prompt_text
    assert "zzz-must-narrate-payload-zzz" in prompt_text

    # Strong check: section is registered under the expected name.
    section_names = [s.name for s in registry.registry(orch._narrator.name())]
    assert "narrator_directives" in section_names


async def test_build_narrator_prompt_omits_narrator_directives_when_no_dispatch_package():
    """When dispatch_package is None, the prompt does NOT contain the
    narrator_directives section (no decomposer payload strings)."""
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    ctx = TurnContext(dispatch_package=None)

    prompt_text, registry = await orch.build_narrator_prompt("look around", ctx)

    assert "zzz-must-not-payload-zzz" not in prompt_text
    assert "zzz-must-narrate-payload-zzz" not in prompt_text

    section_names = [s.name for s in registry.registry(orch._narrator.name())]
    assert "narrator_directives" not in section_names


# ---------------------------------------------------------------------------
# Group G Task 5 — structural hiding in narrator prompt assembly
# ---------------------------------------------------------------------------


def _tag_redacted(who: str) -> VisibilityTag:
    return VisibilityTag(
        visible_to=[who],
        perception_fidelity={},
        secrets_for=[who],
        redact_from_narrator_canonical=True,
    )


async def test_build_narrator_prompt_strips_redacted_directive_payload():
    """A NarratorDirective flagged ``redact_from_narrator_canonical`` MUST NOT
    have its payload appear in the rendered narrator prompt — the LLM
    cannot leak what it never saw.

    Paired with the orchestrator exposing the removed entries via
    ``_last_secret_routes`` for SECRET_NOTE routing (Task 6)."""
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="poison wine",
                narrator_instructions=[
                    NarratorDirective(
                        kind="canonical_only_do_not_reveal_to_others",
                        payload="zzz-SECRET-alice-poisons-wine-zzz",
                        visibility=_tag_redacted("player:Alice"),
                    ),
                    NarratorDirective(
                        kind="must_narrate",
                        payload="zzz-PUBLIC-dogs-bark-zzz",
                        visibility=_tag_all(),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    ctx = TurnContext(dispatch_package=pkg, bank_result=await run_dispatch_bank(pkg))

    prompt_text, registry = await orch.build_narrator_prompt("poison wine", ctx)

    # The redacted payload MUST NOT appear in the prompt string.
    assert "zzz-SECRET-alice-poisons-wine-zzz" not in prompt_text
    # The open directive MUST still be present.
    assert "zzz-PUBLIC-dogs-bark-zzz" in prompt_text

    # The removed entry is exposed on the orchestrator for Task 6.
    assert len(orch._last_secret_routes) == 1
    removed = orch._last_secret_routes[0]
    assert isinstance(removed, NarratorDirective)
    assert removed.payload == "zzz-SECRET-alice-poisons-wine-zzz"


async def test_build_narrator_prompt_clears_secret_routes_when_no_dispatch_package():
    """Calls with no DispatchPackage must leave ``_last_secret_routes`` empty,
    so a previous turn's secrets never leak into a future turn's result."""
    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    # Pretend a previous turn left stale state behind.
    orch._last_secret_routes = [object()]

    ctx = TurnContext(dispatch_package=None)
    await orch.build_narrator_prompt("look", ctx)

    assert orch._last_secret_routes == []


# ---------------------------------------------------------------------------
# Group G Task 7 — canonical-leak audit wiring in run_narration_turn
# ---------------------------------------------------------------------------


async def test_run_narration_turn_emits_leak_audit_span_with_zero_leaks(
    otel_capture,
):
    """run_narration_turn must emit the ``narrator.canonical_leak_audit`` span
    for every turn that had a DispatchPackage, with ``leaks_detected=0`` when
    the canonical prose does not contain any redacted-entity tokens.

    This is the safety-net verification: structural hiding (Task 5) removed
    the redacted entry from the prompt, and the canned narration below does
    not mention the hidden target, so the audit fires clean."""
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.protocol.dispatch import SubsystemDispatch

    # Canned narrator response — no reference to the hidden target.
    client = make_canned_client("The evening wears on at the inn.")
    orch = Orchestrator(client=client)

    pkg = DispatchPackage(
        turn_id="t-audit",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak and strike",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "Rickard"},
                        idempotency_key="k1",
                        confidence=1.0,
                        visibility=_tag_redacted("player:Alice"),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    ctx = TurnContext(
        dispatch_package=pkg,
        bank_result=await run_dispatch_bank(
            pkg,
            context={
                "npc_pool": [
                    NpcPoolMember(name="Rickard", role="guard", drawn_from="world_authored")
                ]
            },
        ),
        npc_pool=[NpcPoolMember(name="Rickard", role="guard", drawn_from="world_authored")],
    )

    await orch.run_narration_turn("sneak and strike", ctx)

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.canonical_leak_audit"
    ]
    assert len(spans) == 1, (
        f"expected exactly one leak_audit span, got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("leaks_detected") == 0
    assert attrs.get("turn_id") == "t-audit"
    assert attrs.get("redact_tag_count") == 1


async def test_run_narration_turn_skips_leak_audit_when_no_dispatch_package(
    otel_capture,
):
    """With no DispatchPackage, there is nothing to audit — the span must not
    fire. Keeps the expected-zero telemetry shape meaningful: a span in the
    stream means we ran an audit, not that we shrugged."""
    client = make_canned_client("Nothing happens.")
    orch = Orchestrator(client=client)
    ctx = TurnContext(dispatch_package=None)

    await orch.run_narration_turn("look", ctx)

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.canonical_leak_audit"
    ]
    assert spans == []


# ---------------------------------------------------------------------------
# None-dispatch-package path — pins Group B / Group G guard behavior
# ---------------------------------------------------------------------------


async def test_build_narrator_prompt_with_none_dispatch_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TurnContext.dispatch_package is None, build_narrator_prompt
    must skip redact_dispatch_package, skip dispatch-bank execution,
    and produce a prompt without subsystem-injected sections."""
    import sidequest.agents.prompt_redaction as _redaction_mod
    import sidequest.agents.subsystems as _subsystems_mod

    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    # dispatch_package defaults to None — explicit for documentation clarity.
    context = TurnContext(character_name="Kael", dispatch_package=None)

    redact_called = False
    bank_called = False

    def _fake_redact(*args, **kwargs):  # pragma: no cover — must NOT be called
        nonlocal redact_called
        redact_called = True
        raise AssertionError("redact_dispatch_package called on None path")

    async def _fake_bank(*args, **kwargs):  # pragma: no cover — must NOT be called
        nonlocal bank_called
        bank_called = True
        raise AssertionError("run_dispatch_bank called on None path")

    monkeypatch.setattr(_redaction_mod, "redact_dispatch_package", _fake_redact)
    monkeypatch.setattr(_subsystems_mod, "run_dispatch_bank", _fake_bank)

    prompt_text, _registry = await orch.build_narrator_prompt(
        action="I look around.",
        context=context,
    )

    assert redact_called is False
    assert bank_called is False
    assert prompt_text  # prompt was built successfully
