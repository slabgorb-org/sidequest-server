"""ADR-143 Task 10 — end-to-end wiring test for chargen contribution methods.

Drives a synthetic WWN pack (background + focus + class + scene flow) through the
REAL CharacterBuilder.build() and asserts:

  1. Background skills land on Character.skills (max-of semantics).
  2. Focus skills land on Character.skills (max-of semantics).
  3. Scene skill_grants from AccumulatedChoices also merge (max-of).
  4. Character.foci == [focus_id].
  5. Focus ability appears on Character.abilities as AbilityDefinition with
     source=AbilitySource.Class.
  6. {slug}.chargen.background_skills span fired.
  7. {slug}.chargen.foci_applied span fired.
  8. Prime-aware stat placement still fires (Task 4 regression guard).

No Claude calls — synthetic fixtures drive the real production build path.
Span assertions use the monkeypatched-tracer pattern from test_stock_apply.py.
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.game.ability import AbilitySource
from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import (
    Background,
    CharCreationChoice,
    CharCreationScene,
    ClassAbilityDef,
    ClassDef,
    Focus,
    FocusLevel,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Synthetic WWN fixtures
# ---------------------------------------------------------------------------

_WWN_ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]

_WWN_ATTRIBUTE_MAP = {
    "STRENGTH": "STR",
    "DEXTERITY": "DEX",
    "CONSTITUTION": "CON",
    "INTELLIGENCE": "INT",
    "WISDOM": "WIS",
    "CHARISMA": "CHA",
}


def _wwn_rules() -> RulesConfig:
    return RulesConfig.model_validate(
        {
            "ruleset": "wwn",
            "stat_generation": "standard_array",
            "standard_array": [14, 12, 11, 10, 9, 7],
            "ability_score_names": _WWN_ABILITY_NAMES,
            "wwn": {"attribute_map": _WWN_ATTRIBUTE_MAP},
        }
    )


def _warrior_class() -> ClassDef:
    """Minimal WWN Warrior class — STR prime, no magic."""
    return ClassDef(
        id="warrior",
        display_name="Warrior",
        rpg_role="fighter",
        jungian_default="Hero",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="warrior_kit",
    )


def _thief_background() -> Background:
    """Background granting Sneak (free) + Exert (quick) at level 0."""
    return Background(
        id="thief",
        display_name="Thief",
        description="You used to pick pockets.",
        free_skill="Sneak",
        quick_skills=["Exert"],
    )


def _die_hard_focus() -> Focus:
    """Focus granting Endure skill (level 1) + a signature ability."""
    return Focus(
        id="die_hard",
        display_name="Die Hard",
        description="You endure beyond mortal limits.",
        levels=[
            FocusLevel(
                skills={"Endure": 1},
                abilities=[
                    ClassAbilityDef(
                        name="Ignore Death",
                        genre_description="You can shrug off wounds that would fell others.",
                        mechanical_effect="die_hard_ignore_death",
                        involuntary=False,
                    )
                ],
            )
        ],
    )


def _chargen_scenes() -> list[CharCreationScene]:
    """Minimal two-scene flow: class choice + background+focus grant.

    Scene 1: pick Warrior class.
    Scene 2: auto-advance grants a skill + selects background + selects focus.
    """
    return [
        CharCreationScene(
            id="the_calling",
            title="What is your calling?",
            narration="Choose your path.",
            choices=[
                CharCreationChoice(
                    label="Warrior",
                    description="A fighter.",
                    mechanical_effects=MechanicalEffects(class_hint="Warrior"),
                ),
            ],
        ),
        # Scene 2: auto-advance with scene-level mechanical_effects that
        # grants a skill directly AND selects background + focus.
        CharCreationScene(
            id="the_background",
            title="Your background",
            narration="Your past defines you.",
            mechanical_effects=MechanicalEffects(
                background="thief",
                focus_id="die_hard",
                # Scene-level skill grant for merge-semantics verification.
                # Sneak is ALSO granted by the background at level 0; scene
                # grants Sneak at 0 too — result stays 0 (max-of, no additive).
                skill_grants={"Sneak": 0},
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# OTEL span capture helper (pattern from tests/mutation/test_stock_apply.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route Span.open through an in-memory exporter for this test."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.chargen_seam_wiring")
    monkeypatch.setattr(spans_module, "tracer", lambda: tracer)
    return exporter


def _span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


# ---------------------------------------------------------------------------
# The wiring test
# ---------------------------------------------------------------------------


def test_chargen_seam_wiring_full(
    span_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real CharacterBuilder.build() with background + focus + class.

    Assertions:
      A1  Background skills land on Character.skills (Sneak=0, Exert=0).
      A2  Focus skills land on Character.skills (Endure=1).
      A3  Scene skill_grants merge with max-of semantics (Sneak stays 0).
      A4  Character.foci == ["die_hard"].
      A5  Focus ability is on Character.abilities as AbilityDefinition,
          source=AbilitySource.Class, with the correct name.
      A6  wwn.chargen.background_skills span fired.
      A7  wwn.chargen.foci_applied span fired.
      A8  wwn.chargen.attributes_assigned span fired (prime-aware, Task 4).
    """
    rules = _wwn_rules()
    warrior = _warrior_class()
    background = _thief_background()
    focus = _die_hard_focus()

    builder = (
        CharacterBuilder(
            scenes=_chargen_scenes(),
            rules=rules,
            rng=random.Random(42),
        )
        .with_classes([warrior])
        .with_chargen_defs(
            backgrounds={"thief": background},
            foci={"die_hard": focus},
        )
    )

    # Drive through the two scenes.
    # Scene 0 (the_calling): player picks Warrior (choice index 0).
    builder.apply_choice(0)
    # Scene 1 (the_background): auto-advance applies the scene-level effects.
    builder.apply_auto_advance()

    assert builder.is_confirmation(), (
        f"Builder should be in Confirmation after all scenes. Phase: {builder._phase}"
    )

    # Build the character — this is the REAL production build() path.
    character = builder.build("Gareth")

    # A1 + A3: background grants Sneak=0, Exert=0; scene also grants Sneak=0.
    # Max-of keeps Sneak at 0 (0 vs 0 = 0).
    assert "Sneak" in character.skills, (
        f"Sneak not found in character.skills; got {character.skills}"
    )
    assert character.skills["Sneak"] == 0, (
        f"Sneak should be 0 (background grant, max-of scene grant); got {character.skills['Sneak']}"
    )
    assert "Exert" in character.skills, (
        f"Exert not found in character.skills; got {character.skills}"
    )
    assert character.skills["Exert"] == 0, (
        f"Exert should be 0 (background quick skill); got {character.skills['Exert']}"
    )

    # A2: focus grants Endure=1.
    assert "Endure" in character.skills, (
        f"Endure not found in character.skills; got {character.skills}"
    )
    assert character.skills["Endure"] == 1, (
        f"Endure should be 1 (focus level-1 grant); got {character.skills['Endure']}"
    )

    # A4: focus ID is in Character.foci.
    assert character.foci == ["die_hard"], (
        f"Character.foci should be ['die_hard']; got {character.foci}"
    )

    # A5: focus ability is on Character.abilities with correct type and source.
    focus_abilities = [a for a in character.abilities if a.name == "Ignore Death"]
    assert len(focus_abilities) == 1, (
        f"Expected 1 'Ignore Death' ability; got {[a.name for a in character.abilities]}"
    )
    focus_ability = focus_abilities[0]
    assert focus_ability.source == AbilitySource.Class, (
        f"Focus ability source should be Class; got {focus_ability.source!r}"
    )
    assert focus_ability.mechanical_effect == "die_hard_ignore_death", (
        f"Unexpected mechanical_effect: {focus_ability.mechanical_effect!r}"
    )

    # A6: background_skills span fired.
    names = _span_names(span_exporter)
    assert "wwn.chargen.background_skills" in names, (
        f"wwn.chargen.background_skills span missing; fired spans: {names}"
    )

    # A7: foci_applied span fired.
    assert "wwn.chargen.foci_applied" in names, (
        f"wwn.chargen.foci_applied span missing; fired spans: {names}"
    )

    # A8: attributes_assigned span fired (prime-aware Task 4 regression guard).
    assert "wwn.chargen.attributes_assigned" in names, (
        f"wwn.chargen.attributes_assigned span missing; fired spans: {names}"
    )

    # Verify the class is what we expect (basic sanity).
    assert character.char_class == "Warrior", f"Expected Warrior; got {character.char_class!r}"


def test_chargen_seam_no_background_def_gives_no_skills(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When background ID doesn't match any def, no background skills are granted.

    DD-5: free-text prose background (no matching def) grants nothing — this is
    documented-correct behavior, NOT a silent fallback masking an error. The
    {slug}.chargen.background_skills span STILL fires (empty skills + a reason)
    so the GM panel can distinguish "evaluated, prose background, no skills" from
    "never called" (OTEL lie-detector principle).
    """
    rules = _wwn_rules()
    warrior = _warrior_class()
    focus = _die_hard_focus()

    # Scene grants background="unknown_prose" which won't match any loaded def.
    scenes = [
        CharCreationScene(
            id="the_calling",
            title="T",
            narration="N",
            choices=[
                CharCreationChoice(
                    label="Warrior",
                    description="A fighter.",
                    mechanical_effects=MechanicalEffects(class_hint="Warrior"),
                ),
            ],
        ),
        CharCreationScene(
            id="the_bg",
            title="T2",
            narration="N2",
            mechanical_effects=MechanicalEffects(
                background="unknown_prose",
                focus_id="die_hard",
            ),
        ),
    ]

    builder = (
        CharacterBuilder(scenes=scenes, rules=rules, rng=random.Random(1))
        .with_classes([warrior])
        .with_chargen_defs(
            backgrounds={},  # no background defs loaded — simulates prose path
            foci={"die_hard": focus},
        )
    )
    builder.apply_choice(0)
    builder.apply_auto_advance()
    character = builder.build("Rux")

    # No Sneak or Exert — the background def was unmatched.
    assert "Sneak" not in character.skills
    assert "Exert" not in character.skills

    # Focus skills still apply.
    assert character.skills.get("Endure") == 1

    # background_skills span DOES fire even on the no-matching-def skip, carrying
    # empty skills + the skip reason (OTEL lie-detector: the decision is visible).
    finished = span_exporter.get_finished_spans()
    names = [s.name for s in finished]
    assert "wwn.chargen.background_skills" in names, (
        f"background_skills span should fire even on the prose-background skip; got {names}"
    )
    bg_span = next(s for s in finished if s.name == "wwn.chargen.background_skills")
    # skills is JSON-serialized in the span attributes — empty dict for the skip.
    assert bg_span.attributes["skills"] == "{}", (
        f"skip-path span should carry empty skills; got {bg_span.attributes['skills']!r}"
    )
    assert bg_span.attributes["reason"] == "no_matching_background_def", (
        f"skip-path span should carry the skip reason; got {bg_span.attributes.get('reason')!r}"
    )
    # foci_applied span DOES fire (focus was matched).
    assert "wwn.chargen.foci_applied" in names


def test_chargen_seam_max_of_skill_merge(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Higher-of (max) semantics: focus Endure=1 wins over a scene Endure=0 grant."""
    rules = _wwn_rules()
    warrior = _warrior_class()
    focus = _die_hard_focus()

    scenes = [
        CharCreationScene(
            id="the_calling",
            title="T",
            narration="N",
            choices=[
                CharCreationChoice(
                    label="Warrior",
                    description="Fighter.",
                    mechanical_effects=MechanicalEffects(class_hint="Warrior"),
                ),
            ],
        ),
        CharCreationScene(
            id="the_bg",
            title="T2",
            narration="N2",
            # Scene grants Endure=0 — focus grants Endure=1 — result MUST be 1.
            mechanical_effects=MechanicalEffects(
                focus_id="die_hard",
                skill_grants={"Endure": 0},
            ),
        ),
    ]

    builder = (
        CharacterBuilder(scenes=scenes, rules=rules, rng=random.Random(7))
        .with_classes([warrior])
        .with_chargen_defs(backgrounds={}, foci={"die_hard": focus})
    )
    builder.apply_choice(0)
    builder.apply_auto_advance()
    character = builder.build("Alex")

    # max(scene=0, focus=1) → 1.
    assert character.skills.get("Endure") == 1, (
        f"Endure should be 1 (max of scene=0 and focus=1); got {character.skills}"
    )


def test_chargen_seam_base_defaults_return_empty() -> None:
    """Dial ruleset contribute_* methods return empty dicts / FociContribution."""
    from sidequest.game.ruleset import get_ruleset_module

    dial = get_ruleset_module("dial")
    bg_skills = dial.contribute_background_skills(background_def=None, rng=random.Random(1))
    assert bg_skills == {}

    foci_contrib = dial.contribute_foci(focus_defs=[])
    assert foci_contrib.skills == {}
    assert foci_contrib.abilities == []


def test_chargen_seam_wn_background_skills_with_none_def() -> None:
    """WN-core contribute_background_skills returns {} when background_def is None."""
    from sidequest.game.ruleset import get_ruleset_module

    wwn = get_ruleset_module("wwn")
    result = wwn.contribute_background_skills(background_def=None, rng=random.Random(1))
    assert result == {}


def test_chargen_seam_foci_contribution_type() -> None:
    """FociContribution.abilities is typed as list[ClassAbilityDef]."""
    from sidequest.game.chargen_contribution import FociContribution

    ca = ClassAbilityDef(
        name="Test",
        genre_description="desc",
        mechanical_effect="eff",
    )
    contrib = FociContribution(skills={"Power": 1}, abilities=[ca])
    assert len(contrib.abilities) == 1
    assert contrib.abilities[0].name == "Test"
    assert contrib.abilities[0].mechanical_effect == "eff"
