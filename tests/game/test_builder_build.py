"""Tests for sidequest.game.builder — Slice 4: build() finalizer.

Covers the Character composition path:
- Phase guard (must be Confirmation)
- Numeric-name guard (Story 30-1)
- Race/class resolution from accumulated + rules defaults
- HP resolution: hp_formula vs class_hp_bases fallback vs default_hp
- Backstory composition: fragments / tables / mechanical / fallback
- Ability resolution with AbilitySource tags
- Inventory composition: item_hints first, equipment_tables opt-in
- EdgeConfig seeding vs placeholder pool
- Story 39-4 Fighter +2 Edge stub
- Archetype resolution (jungian + rpg_role pairing)
- Hook filtering (excludes race_hint / class_hint / personality_trait)
- Anchor auto-fill for missing faction/npc/location
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from sidequest.game.ability import AbilitySource
from sidequest.game.builder import (
    CharacterBuilder,
    HpConfigMissingClassError,
    NumericNameError,
    WrongPhaseError,
)
from sidequest.genre.models.character import (
    BackstoryTables,
    CharCreationChoice,
    CharCreationScene,
    EquipmentTables,
    MechanicalEffects,
)
from sidequest.genre.models.rules import (
    EdgeConfig,
    EdgeThresholdDecl,
    RulesConfig,
)

ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_choice(label: str, description: str = "desc", **fx: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(**fx),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    choices: list[CharCreationChoice] | None = None,
    allows_freeform: bool | None = None,
    mechanical_effects: MechanicalEffects | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title="T",
        narration="N",
        choices=choices or [],
        allows_freeform=allows_freeform,
        mechanical_effects=mechanical_effects,
    )


def base_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(ABILITY_NAMES),
        point_buy_budget=27,
        default_class="Fighter",
        default_race="Human",
    )


def minimal_happy_path_builder() -> CharacterBuilder:
    """Build a builder that's one scene from confirmation."""
    scenes = [
        make_scene(
            "origin",
            choices=[
                make_choice(
                    "Mutant",
                    description="A wanderer born in the ash.",
                    race_hint="Mutant",
                    personality_trait="wary",
                ),
            ],
        ),
        make_scene(
            "class",
            choices=[
                make_choice(
                    "Ranger",
                    description="A hunter who reads the land.",
                    class_hint="Ranger",
                ),
            ],
        ),
    ]
    b = CharacterBuilder(scenes=scenes, rules=base_rules())
    b.apply_choice(0)
    b.apply_choice(0)
    assert b.is_confirmation()
    return b


# ===========================================================================
# Phase + name guards
# ===========================================================================


class TestBuildGuards:
    def test_wrong_phase_raises(self) -> None:
        """Calling build() outside Confirmation is a caller bug."""
        scenes = [make_scene("only", choices=[make_choice("Go")])]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        with pytest.raises(WrongPhaseError) as excinfo:
            b.build("Kara")
        assert excinfo.value.expected == "Confirmation"

    def test_numeric_name_raises(self) -> None:
        """Story 30-1: purely-numeric name means a UI index leaked into
        the name fallback."""
        b = minimal_happy_path_builder()
        with pytest.raises(NumericNameError) as excinfo:
            b.build("7")
        assert excinfo.value.name == "7"

    def test_numeric_name_with_whitespace_raises(self) -> None:
        b = minimal_happy_path_builder()
        with pytest.raises(NumericNameError):
            b.build("  42  ")

    def test_blank_name_bubbles_up_as_pydantic_validation(self) -> None:
        """Blank names are caught by Character / CreatureCore's non-blank
        validators — not by NumericNameError (which only fires for digits)."""
        b = minimal_happy_path_builder()
        with pytest.raises(ValidationError):  # pydantic ValidationError
            b.build("   ")


# ===========================================================================
# Race / class resolution
# ===========================================================================


class TestRaceClassResolution:
    def test_uses_accumulated_hints_when_set(self) -> None:
        b = minimal_happy_path_builder()
        char = b.build("Kara")
        assert char.race == "Mutant"
        assert char.char_class == "Ranger"

    def test_falls_back_to_rules_defaults_when_accumulated_empty(self) -> None:
        scenes = [make_scene("empty", choices=[make_choice("Go")])]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Anon")
        assert char.race == "Human"  # default_race
        assert char.char_class == "Fighter"  # default_class

    def test_hardcoded_defaults_when_rules_have_none(self) -> None:
        """With neither accumulated nor rules-level defaults,
        build() uses Rust's hardcoded 'Human' / 'Fighter' fallbacks."""
        rules = RulesConfig(
            stat_generation="standard_array",
            ability_score_names=list(ABILITY_NAMES),
            point_buy_budget=27,
        )
        scenes = [make_scene("empty", choices=[make_choice("Go")])]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)
        char = b.build("Anon")
        assert char.race == "Human"
        assert char.char_class == "Fighter"


# ===========================================================================
# Backstory composition
# ===========================================================================


class TestBackstoryComposition:
    def test_fragments_path(self) -> None:
        """Accumulated backstory_fragments join with spaces."""
        b = minimal_happy_path_builder()
        char = b.build("Kara")
        # Two choices, both with descriptions (see fixture).
        assert char.backstory == "A wanderer born in the ash. A hunter who reads the land."

    def test_tables_path(self) -> None:
        """With backstory_tables set and no fragments, the template is
        populated and unmatched placeholders stripped."""
        tables = BackstoryTables(
            template="Former {job}. {unknown}. Now a drifter.",
            tables={"job": ["ratcatcher"]},
        )
        # A pronoun-only choice produces no backstory fragment.
        scenes = [
            make_scene(
                "pronoun",
                choices=[
                    make_choice(
                        "He.",
                        description="He.",
                        pronoun_hint="he/him",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(
            scenes=scenes,
            rules=base_rules(),
            backstory_tables=tables,
            rng=random.Random(0),
        )
        b.apply_choice(0)
        char = b.build("Anon")
        # Orphan {unknown} is stripped along with its trailing ". ".
        assert "unknown" not in char.backstory
        assert "ratcatcher" in char.backstory

    def test_mechanical_fallback(self) -> None:
        """No fragments, no tables → background + personality labels.

        To reach this path we need background/personality set but no
        choice description flowing into fragments. Scene-level effects
        on a freeform (name-entry) scene satisfy both: apply_freeform
        records scene-level effects with no choice_description.
        """
        scenes = [
            make_scene(
                "name",
                allows_freeform=True,
                mechanical_effects=MechanicalEffects(
                    background="Postwar drifter.",
                    personality_trait="wary",
                ),
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_freeform("Anon")
        char = b.build("Anon")
        assert "Postwar drifter" in char.backstory
        assert "wary" in char.backstory

    def test_hardcoded_fallback_when_nothing_authored(self) -> None:
        """No fragments, no tables, no background, no personality."""
        scenes = [
            make_scene(
                "pronoun",
                choices=[
                    make_choice("He.", description="He.", pronoun_hint="he/him"),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Anon")
        assert char.backstory == "A wanderer with a mysterious past"


# ===========================================================================
# Ability resolution
# ===========================================================================


class TestAbilityResolution:
    def test_mutation_hint_produces_race_ability(self) -> None:
        scenes = [
            make_scene(
                "mutation",
                choices=[
                    make_choice(
                        "Stone Skin",
                        description="Your skin is stone-hard.",
                        mutation_hint="stone_skin",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert len(char.abilities) == 1
        ab = char.abilities[0]
        assert ab.source == AbilitySource.Race
        assert ab.name == "Stone Skin"
        assert ab.mechanical_effect == "stone_skin"
        assert ab.genre_description == "Your skin is stone-hard."

    def test_affinity_hint_produces_class_ability(self) -> None:
        scenes = [
            make_scene(
                "affinity",
                choices=[
                    make_choice(
                        "Fire",
                        description="An affinity for flame.",
                        affinity_hint="fire",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert len(char.abilities) == 1
        assert char.abilities[0].source == AbilitySource.Class

    def test_training_hint_produces_class_ability(self) -> None:
        scenes = [
            make_scene(
                "training",
                choices=[
                    make_choice(
                        "Swordplay",
                        description="Drilled in the blade.",
                        training_hint="swordplay",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert len(char.abilities) == 1
        assert char.abilities[0].source == AbilitySource.Class

    def test_mutation_none_sentinel_produces_no_ability(self) -> None:
        scenes = [
            make_scene(
                "mutation",
                choices=[make_choice("No mutation", mutation_hint="none")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert char.abilities == []

    def test_precedence_mutation_over_affinity(self) -> None:
        """A choice that sets BOTH mutation and affinity prefers mutation
        (Race source) over affinity (Class source) — matches Rust
        if/elif ordering."""
        scenes = [
            make_scene(
                "combo",
                choices=[
                    make_choice(
                        "Both",
                        mutation_hint="stone_skin",
                        affinity_hint="fire",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert len(char.abilities) == 1
        assert char.abilities[0].source == AbilitySource.Race


# ===========================================================================
# Inventory composition
# ===========================================================================


class TestInventoryComposition:
    def test_item_hints_populate_inventory(self) -> None:
        scenes = [
            make_scene(
                "kit",
                choices=[
                    make_choice(
                        "Crowbar kit",
                        item_hint="crowbar",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert len(char.core.inventory.items) == 1
        item = char.core.inventory.items[0]
        assert item["id"] == "crowbar"
        assert item["name"] == "Crowbar"
        assert item["equipped"] is True

    def test_item_hint_none_sentinel_filtered(self) -> None:
        scenes = [
            make_scene(
                "kit",
                choices=[make_choice("Empty", item_hint="none")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert char.core.inventory.items == []

    def test_equipment_tables_opt_in_via_scene_directive(self) -> None:
        """equipment_generation=random_table + tables wired → rolls fire."""
        scenes = [
            make_scene(
                "gear",
                choices=[
                    make_choice(
                        "Pick from kit",
                        equipment_generation="random_table",
                    ),
                ],
            ),
        ]
        tables = EquipmentTables(
            tables={"weapon": ["revolver"], "armor": ["leather_duster"]},
            rolls_per_slot={"weapon": 1, "armor": 1},
        )
        b = CharacterBuilder(
            scenes=scenes, rules=base_rules(), rng=random.Random(0)
        ).with_equipment_tables(tables)
        b.apply_choice(0)
        char = b.build("Kara")
        ids = [item["id"] for item in char.core.inventory.items]
        assert "revolver" in ids
        assert "leather_duster" in ids

    def test_equipment_tables_missing_when_directive_present_produces_empty(self) -> None:
        """equipment_generation=random_table + no tables wired → empty
        inventory (misconfiguration surfaced via OTEL)."""
        scenes = [
            make_scene(
                "gear",
                choices=[
                    make_choice("Kit", equipment_generation="random_table"),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert char.core.inventory.items == []

    def test_item_hints_come_before_random_tables(self) -> None:
        scenes = [
            make_scene(
                "hint",
                choices=[make_choice("Crowbar", item_hint="crowbar")],
            ),
            make_scene(
                "random",
                choices=[make_choice("Roll", equipment_generation="random_table")],
            ),
        ]
        tables = EquipmentTables(
            tables={"weapon": ["revolver"]},
            rolls_per_slot={"weapon": 1},
        )
        b = CharacterBuilder(
            scenes=scenes, rules=base_rules(), rng=random.Random(0)
        ).with_equipment_tables(tables)
        b.apply_choice(0)
        b.apply_choice(0)
        char = b.build("Kara")
        ids = [item["id"] for item in char.core.inventory.items]
        assert ids[0] == "crowbar"  # item_hint first
        assert "revolver" in ids[1:]  # random_table after


# ===========================================================================
# EdgePool seeding
# ===========================================================================


class TestEdgeSeeding:
    def test_placeholder_when_no_edge_config(self) -> None:
        b = minimal_happy_path_builder()
        char = b.build("Kara")
        # Placeholder edge pool → base_max == PLACEHOLDER_EDGE_BASE_MAX (10)
        # Ranger is NOT Fighter, so no +2 stub.
        assert char.core.hp.base_max == 10
        assert char.core.hp.max == 10
        assert char.core.hp.current == 10

    def test_edge_config_path(self) -> None:
        # Story 39-10: edge_pool_from_config now applies a CON modifier
        # ((CON-10)//2, floored at 1). Pin CON to 10 with roll_3d6_strict
        # + _rolled_stats so the modifier is 0 and the test's intent
        # (base_max comes from edge_config) is preserved without
        # entangling this test in the new CON math (which has its own
        # dedicated suite in test_chargen_edge_con_modifier.py).
        rules = RulesConfig(
            stat_generation="roll_3d6_strict",
            ability_score_names=list(ABILITY_NAMES),
            default_class="Ranger",
            default_race="Human",
        )
        rules.edge_config = EdgeConfig(
            base_max_by_class={"Ranger": 6, "Fighter": 8},
            thresholds=[
                EdgeThresholdDecl(at=3, event_id="edge_strained", narrator_hint="Fraying."),
            ],
        )
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Ranger", class_hint="Ranger")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b._rolled_stats = [
            ("STR", 10),
            ("DEX", 10),
            ("CON", 10),
            ("INT", 10),
            ("WIS", 10),
            ("CHA", 10),
        ]
        b.apply_choice(0)
        char = b.build("Kara")
        # Ranger base_max = 6 from edge_config. CON 10 mod = 0. No +2 stub.
        assert char.core.hp.base_max == 6
        assert char.core.hp.max == 6
        # ADR-114: HpPool has no thresholds — threshold assertions dropped.

    def test_edge_config_missing_class_raises(self) -> None:
        """Class declared via chargen but absent from base_max_by_class →
        loud failure, not silent fallback to placeholder. Story 39-3."""
        rules = base_rules()
        rules.edge_config = EdgeConfig(
            base_max_by_class={"Fighter": 8},  # no Ranger
        )
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Ranger", class_hint="Ranger")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)
        with pytest.raises(HpConfigMissingClassError) as excinfo:
            b.build("Kara")
        assert excinfo.value.class_name == "Ranger"

    def test_fighter_plus_two_stub_retired(self) -> None:
        """Story 39-10: the Story 39-4 Fighter +2 stub is retired. With
        CON 10 (mod 0) the seeded Edge should equal the class base
        verbatim — no class-specific bonus.

        Detailed CON math lives in test_chargen_edge_con_modifier.py;
        this test just guards against silent resurrection of the stub.
        """
        rules = RulesConfig(
            stat_generation="roll_3d6_strict",
            ability_score_names=list(ABILITY_NAMES),
            default_class="Fighter",
            default_race="Human",
        )
        rules.edge_config = EdgeConfig(
            base_max_by_class={"Fighter": 8},
        )
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Fighter", class_hint="Fighter")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b._rolled_stats = [
            ("STR", 10),
            ("DEX", 10),
            ("CON", 10),
            ("INT", 10),
            ("WIS", 10),
            ("CHA", 10),
        ]
        b.apply_choice(0)
        char = b.build("Arc")
        # Base 8 + CON mod 0 = 8. Was 10 with the +2 stub.
        assert char.core.hp.base_max == 8
        assert char.core.hp.max == 8
        assert char.core.hp.current == 8

    def test_non_fighter_placeholder_unaffected(self) -> None:
        """Placeholder edge pool (no edge_config) path: CON modifier
        applies only via edge_pool_from_config, so the placeholder
        branch is unchanged. Ranger keeps base 10."""
        b = minimal_happy_path_builder()
        char = b.build("Kara")
        # Ranger, placeholder base 10, no stub → still 10.
        assert char.core.hp.max == 10


# ===========================================================================
# Archetype resolution
# ===========================================================================


class TestArchetypeResolution:
    def test_paired_hints_produce_resolved_archetype(self) -> None:
        scenes = [
            make_scene(
                "arch",
                choices=[
                    make_choice(
                        "Warrior+Hero",
                        jungian_hint="warrior",
                        rpg_role_hint="hero",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert char.resolved_archetype == "warrior/hero"

    def test_single_hint_leaves_archetype_none(self) -> None:
        scenes = [
            make_scene(
                "half",
                choices=[make_choice("Warrior", jungian_hint="warrior")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        assert char.resolved_archetype is None


# ===========================================================================
# Hook filtering and anchor auto-fill
# ===========================================================================


class TestHooksAndAnchors:
    def test_excluded_mechanical_keys_filtered(self) -> None:
        """race_hint / class_hint / personality_trait hooks do NOT appear in
        Character.hooks — they're already represented on the sheet."""
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Mutant",
                        race_hint="Mutant",
                        class_hint="Ranger",
                        personality_trait="wary",
                        goals="find her sister",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        hook_texts = [h for h in char.hooks if not h.endswith("auto-filled from genre pack")]
        assert not any("Origin:" in h for h in hook_texts)
        assert not any("Class:" in h for h in hook_texts)
        assert not any("Personality:" in h for h in hook_texts)
        # Goal survives filtering.
        assert any("Goal: find her sister" in h for h in hook_texts)

    def test_anchor_auto_fill_for_missing_types(self) -> None:
        """With no relationship effects, no NPC anchors are created, so
        the auto-fill note appears."""
        b = minimal_happy_path_builder()
        char = b.build("Kara")
        note_types = set()
        for h in char.hooks:
            if h.endswith("auto-filled from genre pack"):
                note_types.add(h.split(":")[0])
        assert note_types == {"faction", "npc", "location"}

    def test_relationship_suppresses_npc_autofill(self) -> None:
        """A relationship effect produces an NPC anchor, so no auto-fill
        note is added for the npc type."""
        scenes = [
            make_scene(
                "rel",
                choices=[
                    make_choice(
                        "Sister",
                        relationship="Thessa, sister missing",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Kara")
        auto_fills = {
            h.split(":")[0] for h in char.hooks if h.endswith("auto-filled from genre pack")
        }
        assert auto_fills == {"faction", "location"}  # npc covered by anchor
