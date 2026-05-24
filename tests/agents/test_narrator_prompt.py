from __future__ import annotations

import pytest

from sidequest.agents.narrator import NARRATOR_OUTPUT_ONLY


def test_prompt_documents_npc_side_field():
    # Closed enum surface — narrator must emit `side`.
    assert "side" in NARRATOR_OUTPUT_ONLY
    assert "player" in NARRATOR_OUTPUT_ONLY
    assert "opponent" in NARRATOR_OUTPUT_ONLY
    assert "neutral" in NARRATOR_OUTPUT_ONLY


def test_prompt_documents_beat_outcome_tiers():
    for tier in ("CritFail", "Fail", "Tie", "Success", "CritSuccess"):
        assert tier in NARRATOR_OUTPUT_ONLY


def test_prompt_documents_status_changes_field():
    """Story 61-9 / ADR-101 amendment: the SDK narrator records status
    changes by CALLING ``apply_status`` (a tool); the legacy
    ``status_changes`` sidecar field is gone. The severity enumeration
    must still appear in the prose so the narrator picks the right tier.
    """
    assert "apply_status" in NARRATOR_OUTPUT_ONLY
    for sev in ("Scratch", "Wound", "Scar", "Boon"):
        assert sev in NARRATOR_OUTPUT_ONLY


def test_prompt_documents_boon_for_temporary_buffs():
    """Boon was added 2026-04-30 — give the narrator a slot for prose-described
    magical effects from consumables/scrolls/potions/artifacts (Mira pouch
    playtest gap). Without an explicit rule, the narrator silently dropped
    "the torchlight gets clearer" (a real perception buff) into prose with
    no schema slot.
    """
    # Boon severity is documented and contextualized.
    assert "Boon" in NARRATOR_OUTPUT_ONLY
    # The CRITICAL MAGIC EFFECT RULE wires the Boon-emit obligation to the
    # prose patterns the narrator was previously dropping silently.
    assert "CRITICAL MAGIC EFFECT RULE" in NARRATOR_OUTPUT_ONLY
    # Boon is described as scene-bounded (matches status_clear.py wiring).
    assert (
        "scene end" in NARRATOR_OUTPUT_ONLY.lower()
        or "scene-bounded" in NARRATOR_OUTPUT_ONLY.lower()
    )


def test_active_encounter_zone_renders_both_dials_and_tags(monkeypatch, build_registry):
    from sidequest.agents.narrator import NarratorAgent
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.game.encounter_tag import EncounterTag
    from sidequest.game.status import Status, StatusSeverity
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
    )

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=4, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=7, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
        tags=[
            EncounterTag(
                text="Off-Balance",
                created_by="Sam",
                target="Promo",
                leverage=1,
                fleeting=False,
                created_turn=3,
            )
        ],
    )
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            )
        ],
    )
    statuses_by_actor = {
        "Sam": [
            Status(
                text="Bruised Ribs",
                severity=StatusSeverity.Wound,
                absorbed_shifts=0,
                created_turn=2,
                created_in_encounter="combat",
            )
        ]
    }

    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        statuses_by_actor=statuses_by_actor,
    )

    rendered = registry.render_for("narrator")
    assert "Player metric: 4 / 10" in rendered
    assert "Opponent metric: 7 / 10" in rendered
    assert "Off-Balance" in rendered
    assert "Bruised Ribs" in rendered
    assert "Wound" in rendered
    assert "side=player" in rendered
    assert "side=opponent" in rendered


def test_resolved_encounter_short_circuits_to_resolution_zone(build_registry):
    from sidequest.agents.narrator import NarratorAgent
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.game.resolution_signal import ResolutionSignal
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
    )

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=4, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=11, starting=0, threshold=10),
        actors=[EncounterActor(name="Sam", role="combatant", side="player")],
        resolved=True,
        outcome="opponent_victory",
    )
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            )
        ],
    )
    signal = ResolutionSignal(
        encounter_type="combat",
        outcome="opponent_victory",
        final_player_metric=4,
        final_opponent_metric=11,
    )

    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        statuses_by_actor={},
        resolution_signal=signal,
    )

    rendered = registry.render_for("narrator")
    assert "[ENCOUNTER RESOLVED]" in rendered
    assert "outcome: opponent_victory" in rendered
    assert "final_player_metric: 4" in rendered
    assert "final_opponent_metric: 11" in rendered
    # The active-encounter live zone is NOT rendered.
    assert "Available beats" not in rendered


def test_resolved_encounter_yielded_branch_renders_actors_and_edge(build_registry):
    """Yielded-branch prose includes yielded_actors and edge_refreshed.

    Phase 2 reviewer flagged this as deferred coverage; now asserted here.
    """
    from sidequest.agents.narrator import NarratorAgent
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.game.resolution_signal import ResolutionSignal
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
    )

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=4, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=7, starting=0, threshold=10),
        actors=[EncounterActor(name="Sam", role="combatant", side="player")],
        resolved=True,
        outcome="yielded",
    )
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            )
        ],
    )
    signal = ResolutionSignal(
        encounter_type="combat",
        outcome="yielded",
        final_player_metric=4,
        final_opponent_metric=7,
        yielded_actors=("Sam",),
        edge_refreshed=3,
    )

    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        statuses_by_actor={},
        resolution_signal=signal,
    )

    rendered = registry.render_for("narrator")
    assert "[ENCOUNTER RESOLVED]" in rendered
    assert "outcome: yielded" in rendered
    assert "yielded_actors: [Sam]" in rendered
    assert "edge_refreshed: 3" in rendered
    # The active-encounter live zone is NOT rendered for a resolved encounter.
    assert "Available beats" not in rendered


# ──────────────────────────────────────────────────────────────────────────
# Per-PC class-filtered beat menus (Task 7, C&C B/X class beats).
#
# Two assertions:
#  - Fighter section of the prompt must NOT list cast_spell.
#  - Mage section of the prompt with full slots MUST list cast_spell.
# Tests build a ConfrontationDef with one universal and one Mage-only beat,
# pass per-PC ClassDef + spell_slots through build_encounter_context, and
# inspect the rendered prompt text. Mirrors the test_active_encounter_zone…
# style above (inline factories, no fixtures beyond build_registry).
# ──────────────────────────────────────────────────────────────────────────


def _two_pc_class_filter_setup():
    """Build the encounter / cdef / pc_classes_by_name triple used by
    both per-class tests below. Two PCs (Fighter Sam + Mage Aldous), one
    universal beat (attack), one Mage-only beat (cast_spell)."""
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.genre.models.character import ClassDef
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
    )

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Aldous", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="combatant", side="opponent"),
        ],
    )
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ),
            BeatDef.model_validate(
                {
                    "id": "cast_spell",
                    "label": "Cast Spell",
                    "kind": "strike",
                    "base": 3,
                    "stat_check": "INT",
                    "class_filter": ["Mage"],
                }
            ),
        ],
    )
    fighter = ClassDef.model_validate(
        {
            "id": "fighter",
            "display_name": "Fighter",
            "rpg_role": "tank",
            "jungian_default": "hero",
            "prime_requisite": "STR",
            "minimum_score": 9,
            "kit_table": "fighter_kit",
            "encounter_beat_choices": ["attack"],
        }
    )
    mage = ClassDef.model_validate(
        {
            "id": "mage",
            "display_name": "Mage",
            "rpg_role": "caster",
            "jungian_default": "sage",
            "prime_requisite": "INT",
            "minimum_score": 9,
            "kit_table": "mage_kit",
            "encounter_beat_choices": ["attack", "cast_spell"],
            "magic_access": "arcane",
        }
    )
    pc_classes_by_name = {
        "Sam": (fighter, 0.0),
        "Aldous": (mage, 1.0),
    }
    return enc, cdef, pc_classes_by_name


def test_narrator_prompt_omits_other_class_beats(build_registry):
    """Fighter line of the per-PC menu must not list cast_spell."""
    from sidequest.agents.narrator import NarratorAgent

    enc, cdef, pc_classes_by_name = _two_pc_class_filter_setup()
    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        pc_classes_by_name=pc_classes_by_name,
    )
    rendered = registry.render_for("narrator")

    # Locate the Fighter (Sam) line; verify cast_spell is absent from THAT line
    # (the Mage line legitimately mentions cast_spell, so a global "not in" check
    # would be wrong — the prompt MUST contain cast_spell for Aldous).
    fighter_lines = [line for line in rendered.splitlines() if "Fighter (Sam) can:" in line]
    assert fighter_lines, "Fighter PC menu line missing from rendered prompt"
    fighter_line = fighter_lines[0]
    assert "cast_spell" not in fighter_line
    assert "attack" in fighter_line


def test_narrator_prompt_includes_class_signature(build_registry):
    """Mage line of the per-PC menu at full slots must list cast_spell."""
    from sidequest.agents.narrator import NarratorAgent

    enc, cdef, pc_classes_by_name = _two_pc_class_filter_setup()
    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        pc_classes_by_name=pc_classes_by_name,
    )
    rendered = registry.render_for("narrator")

    mage_lines = [line for line in rendered.splitlines() if "Mage (Aldous) can:" in line]
    assert mage_lines, "Mage PC menu line missing from rendered prompt"
    mage_line = mage_lines[0]
    assert "cast_spell" in mage_line
    assert "attack" in mage_line


def test_narrator_prompt_filters_cast_spell_when_mage_has_nothing_prepared(build_registry):
    """Story 47-10 wiring test (AC4): a Mage with slots remaining but
    nothing prepared MUST see cast_spell filtered out of the prompt menu.

    This guards against the wiring gap the architect flagged in spec-check:
    even if the prepared-list gate exists in beats_available_for, the
    narrator must thread prepared_spells through the call. Prior to the
    fix this test failed because narrator.py:723 called the helper with
    only spell_slots_remaining.
    """
    from sidequest.agents.narrator import NarratorAgent

    enc, cdef, pc_classes_by_name = _two_pc_class_filter_setup()
    # Reshape Aldous's entry into the new 3-tuple form: slots remain, but
    # prepared_spells is an empty dict (nothing memorized this morning).
    pc_classes_by_name = dict(pc_classes_by_name)
    fighter, _ = pc_classes_by_name["Sam"]
    mage, _ = pc_classes_by_name["Aldous"]
    pc_classes_by_name["Sam"] = (fighter, 0.0, None)
    pc_classes_by_name["Aldous"] = (mage, 1.0, {})  # slots OK, prepared empty

    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        pc_classes_by_name=pc_classes_by_name,
    )
    rendered = registry.render_for("narrator")

    mage_lines = [line for line in rendered.splitlines() if "Mage (Aldous) can:" in line]
    assert mage_lines, "Mage PC menu line missing from rendered prompt"
    mage_line = mage_lines[0]
    assert "cast_spell" not in mage_line, (
        f"With slots=1 and prepared_spells={{}}, cast_spell must be filtered "
        f"out (rejected_unprepared). Got: {mage_line!r}"
    )
    assert "attack" in mage_line


def test_narrator_prompt_includes_cast_spell_when_mage_has_prepared(build_registry):
    """Story 47-10 wiring test (AC4 happy path): a Mage with both slots AND
    a prepared spell sees cast_spell in their menu."""
    from sidequest.agents.narrator import NarratorAgent

    enc, cdef, pc_classes_by_name = _two_pc_class_filter_setup()
    pc_classes_by_name = dict(pc_classes_by_name)
    fighter, _ = pc_classes_by_name["Sam"]
    mage, _ = pc_classes_by_name["Aldous"]
    pc_classes_by_name["Sam"] = (fighter, 0.0, None)
    # Slots = 1.0, prepared = {1: ["sleep"]} -> cast_spell allowed.
    pc_classes_by_name["Aldous"] = (mage, 1.0, {1: ["sleep"]})

    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        pc_classes_by_name=pc_classes_by_name,
    )
    rendered = registry.render_for("narrator")

    mage_lines = [line for line in rendered.splitlines() if "Mage (Aldous) can:" in line]
    assert mage_lines
    assert "cast_spell" in mage_lines[0]


def test_narrator_prompt_omits_pc_block_when_no_classes_supplied(build_registry):
    """Backward compatibility — calling without pc_classes_by_name keeps the
    legacy single beat list (no per-PC block at all)."""
    from sidequest.agents.narrator import NarratorAgent

    enc, cdef, _ = _two_pc_class_filter_setup()
    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
    )
    rendered = registry.render_for("narrator")
    # The all-beats listing still ships (opponent-side selection needs it).
    assert "attack: Attack" in rendered
    # But there is no per-PC class-distinct block.
    assert "Player-character beat menus" not in rendered
    assert "Fighter (Sam) can:" not in rendered


def test_narrator_per_turn_prompt_includes_action_invariant(build_registry):
    """The per-turn prompt zone tells the narrator not to perform unlisted actions."""
    from sidequest.agents.narrator import NarratorAgent

    enc, cdef, pc_classes_by_name = _two_pc_class_filter_setup()
    registry = build_registry()
    NarratorAgent().build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
        pc_classes_by_name=pc_classes_by_name,
    )
    rendered = registry.render_for("narrator")
    assert "Do not narrate actions outside that list as performed" in rendered


# ──────────────────────────────────────────────────────────────────────────
# ADR-098 stateless narrator: build_narrator_prompt signature + tier gates
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_narrator_prompt_signature_no_tier_param(simple_turn_context):
    """build_narrator_prompt no longer accepts `tier` or `rebuild_header`.

    ADR-098 removes the Full/Delta tier system. Every turn builds the
    same shape; no per-turn gating beyond turn_number.
    """
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator()
    # The new signature accepts only (action, context). Calling with
    # `tier=` must fail at the call site (TypeError) and the kwargs-free
    # path must succeed.
    prompt_text, registry = await orch.build_narrator_prompt("test action", simple_turn_context)
    assert isinstance(prompt_text, str)
    assert len(registry.registry(orch._narrator.name())) > 0


@pytest.mark.asyncio
async def test_opening_scene_constraint_fires_only_on_turn_zero(
    simple_turn_context, simple_turn_context_turn_three
):
    """Formerly Full-tier-only; now gated by context.turn_number == 0."""
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator()
    _, registry_t0 = await orch.build_narrator_prompt("hi", simple_turn_context)
    _, registry_t3 = await orch.build_narrator_prompt("hi", simple_turn_context_turn_three)

    names_t0 = {s.name for s in registry_t0.registry(orch._narrator.name())}
    names_t3 = {s.name for s in registry_t3.registry(orch._narrator.name())}
    assert "opening_scene_constraint" in names_t0
    assert "opening_scene_constraint" not in names_t3


@pytest.mark.asyncio
async def test_narrator_identity_fires_every_turn(
    simple_turn_context_turn_three,
):
    """Formerly Full-tier-only; now fires unconditionally."""
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("hi", simple_turn_context_turn_three)
    names = {s.name for s in registry.registry(orch._narrator.name())}
    assert "narrator_identity" in names
    assert "narrator_dialogue_rules" in names
