"""Builder seeds SystemStrainPool at chargen for cwn packs (Task 6).

Two tests:
1. Synthetic cwn RulesConfig + synthetic builder → pool seeded with
   max == Body score, current == 0, permanent == 0.
2. Synthetic non-cwn RulesConfig → system_strain is None.
3. Real neon_dystopia pack integration build → same contract.

Test approach: primarily synthetic (pattern from test_character_chargen_fields.py),
plus one real-pack integration test via load_genre_pack(neon_dystopia).
Synthetic tests are faster, self-contained, and exercise seed_system_strain()
directly as the unit-testable helper while the real-pack test proves end-to-end
wiring is correct.
"""

from __future__ import annotations

import pytest

from sidequest.game.builder import CharacterBuilder, seed_system_strain
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import CwnConfig, RulesConfig
from tests._helpers.genre_paths import PackNotFound, find_pack_path

CWN_ABILITY_NAMES = ["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"]
NATIVE_ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]

CWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}


def _has_neon_content() -> bool:
    try:
        find_pack_path("neon_dystopia")
        return True
    except PackNotFound:
        return False


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
    mechanical_effects: MechanicalEffects | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title="T",
        narration="N",
        choices=choices or [],
        mechanical_effects=mechanical_effects,
    )


def cwn_rules() -> RulesConfig:
    """Minimal valid cwn RulesConfig with Body mapped to CONSTITUTION."""
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(CWN_ABILITY_NAMES),
        point_buy_budget=27,
        default_class="Solo",
        default_race="Street",
        ruleset="cwn",
        cwn=CwnConfig(attribute_map=CWN_ATTRIBUTE_MAP),
    )


def native_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(NATIVE_ABILITY_NAMES),
        point_buy_budget=27,
        default_class="Fighter",
        default_race="Human",
    )


# ---------------------------------------------------------------------------
# Unit tests for seed_system_strain helper
# ---------------------------------------------------------------------------


class TestSeedSystemStrainHelper:
    def test_cwn_returns_pool_maxed_at_body_score(self) -> None:
        rules = cwn_rules()
        stats = {"Brawn": 10, "Reflex": 12, "Body": 14, "Tech": 8, "Instinct": 11, "Cool": 13}
        pool = seed_system_strain(rules, stats)
        assert pool is not None
        assert pool.max == 14
        assert pool.current == 0
        assert pool.permanent == 0

    def test_cwn_uses_max_1_floor(self) -> None:
        rules = cwn_rules()
        # Body = 0 is edge-case; floor must clamp to 1.
        stats = {"Brawn": 10, "Reflex": 10, "Body": 0, "Tech": 10, "Instinct": 10, "Cool": 10}
        pool = seed_system_strain(rules, stats)
        assert pool is not None
        assert pool.max == 1

    def test_non_cwn_returns_none(self) -> None:
        rules = native_rules()
        stats = {k: 10 for k in NATIVE_ABILITY_NAMES}
        result = seed_system_strain(rules, stats)
        assert result is None


# ---------------------------------------------------------------------------
# Integration: synthetic builder build (cwn pack)
# ---------------------------------------------------------------------------


def test_cwn_character_gets_strain_pool_maxed_at_body_score() -> None:
    """End-to-end synthetic builder: cwn pack → system_strain seeded.

    Uses a single-choice scene so build() runs through the full
    _build_character path and seeds the SystemStrainPool from stats.
    standard_array maps the 6 values [15,14,13,12,10,8] to
    [Brawn,Reflex,Body,Tech,Instinct,Cool] in declaration order,
    so Body gets index-2 value = 13.
    """
    rules = cwn_rules()
    scenes = [
        make_scene(
            "origins",
            choices=[
                make_choice(
                    "The Streets",
                    description="You grew up in the concrete.",
                    race_hint="Street",
                    background="Street Rat",
                )
            ],
        ),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=rules)
    builder.apply_choice(0)
    character = builder.build("Kira Vex")

    # standard_array = [15,14,13,12,10,8] mapped to [Brawn,Reflex,Body,Tech,Instinct,Cool]
    body_score = character.stats["Body"]
    assert body_score == 13  # index-2 of standard_array

    assert character.core.system_strain is not None
    assert character.core.system_strain.max == max(1, body_score)
    assert character.core.system_strain.current == 0
    assert character.core.system_strain.permanent == 0


# ---------------------------------------------------------------------------
# Integration: synthetic builder build (non-cwn pack)
# ---------------------------------------------------------------------------


def test_non_cwn_character_has_no_strain_pool() -> None:
    """A non-cwn pack character should have system_strain=None."""
    rules = native_rules()
    scenes = [
        make_scene(
            "noop",
            choices=[make_choice("Go", description="A blank slate.")],
        ),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=rules)
    builder.apply_choice(0)
    character = builder.build("Arven Steel")
    assert character.core.system_strain is None


# ---------------------------------------------------------------------------
# Real-pack integration: neon_dystopia (skipped if content not on disk)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_neon_content(), reason="neon_dystopia pack not on disk")
def test_real_neon_character_gets_strain_pool() -> None:
    """Drive neon_dystopia's full choice-based flow and verify strain seeding.

    neon_dystopia flow (6 scenes, point_buy stats):
      0. origins — race/background choice
      1. pronouns — pronoun_hint choice
      2. crucible — class choice
      3. connection — relationship/personality choice
      4. drive — goals/emotional_state choice
      5. confirmation — no-choice, auto-confirmation

    The builder becomes confirmable after all non-confirmation scenes are
    chosen (choice-based packs advance on apply_choice, no auto_advance
    needed). After all 5 non-confirmation scenes, build() is available.
    """
    from sidequest.genre.loader import load_genre_pack

    try:
        pack_path = find_pack_path("neon_dystopia")
    except PackNotFound as exc:  # pragma: no cover — guard above should prevent this
        pytest.skip(str(exc))

    pack = load_genre_pack(pack_path)

    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),  # type: ignore[attr-defined]
            rules=pack.rules,  # type: ignore[attr-defined]
            backstory_tables=pack.backstory_tables,  # type: ignore[attr-defined]
        )
        .with_lobby_name("Zara Kade")
        .with_equipment_tables(pack.equipment_tables)  # type: ignore[attr-defined]
        .with_classes(pack.classes)  # type: ignore[attr-defined]
    )

    # Walk through all non-confirmation scenes by picking choice 0 each time.
    # confirmation scene has no choices; the builder becomes confirmable once all
    # prior scenes are processed.
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            builder.apply_choice(0)
        else:
            builder.apply_auto_advance()

    character = builder.build("Zara Kade")

    body_score = character.stats.get("Body", 0)
    assert character.core.system_strain is not None, (
        "neon_dystopia (cwn) character must have a SystemStrainPool"
    )
    assert character.core.system_strain.max == max(1, body_score), (
        f"strain max ({character.core.system_strain.max}) must equal Body score ({body_score})"
    )
    assert character.core.system_strain.current == 0
    assert character.core.system_strain.permanent == 0
