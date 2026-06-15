"""RED (ADR-144 F4b / story 121-2): pulp_noir Fate Core migration — the pilot.

F4a (story 121-1) built the engine spine: ``FateConfig`` schema, the
``seed_chargen_resources`` apply layer, builder ``fate_sheet`` attach, and the
``fate.chargen.seeded`` span — proven end-to-end against a *synthetic* fate pack.
F4a's reviewer explicitly deferred the **real-pack** integration test to F4b:

    "121-2 (F4b) should add a skipif-gated real-pack chargen integration test
     (mirror test_real_neon_character_gets_strain_pool)."

This file is that test. It drives the **real** ``pulp_noir`` content pack through
the production loader + builder and pins story 121-2's acceptance criteria:

- AC1  pack loads and authors a valid ``fate:`` config (``ruleset: fate``)
- AC2  a created PC has a populated ``FateSheet`` (skills + HC/trouble aspects + refresh)
- AC3  a Fate action routes to the Fate engine (``fate_conflict``), not native
- AC4  native d20 config is stripped (power_tiers gone, no ability scores)
- AC5  the F4a wiring contract holds with pulp_noir as the *real* test pack

121-2 is **content authoring** — these tests are RED today because ``pulp_noir`` is
still native-bound (``ruleset`` defaults to ``"native"``, no ``fate:`` block). They go
GREEN when the pack is migrated; **no engine change is in scope** (if an AC can't be
met without touching the engine, that is an F4a gap — escalate, don't patch from F4b).

Pattern mirrored: ``tests/game/test_builder_seeds_strain.py::test_real_neon_character_gets_strain_pool``
(real-pack load + production CharacterBuilder walk + CreatureCore assertions) and
``tests/game/ruleset/test_121_1_fate_chargen_seed.py`` (the F4a fate_sheet/routing contract).
"""

from __future__ import annotations

import pytest

from sidequest.game.builder import CharacterBuilder
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.genre.models.rules import FateConfig
from tests._helpers.genre_paths import PackNotFound, find_pack_path

PACK = "pulp_noir"


def _has_pulp_noir_content() -> bool:
    try:
        find_pack_path(PACK)
        return True
    except PackNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _has_pulp_noir_content(), reason="pulp_noir pack not on disk"
)


def _load_pack() -> object:
    """Load the real pulp_noir pack through the production loader."""
    from sidequest.genre.loader import load_genre_pack

    try:
        pack_path = find_pack_path(PACK)
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))
    return load_genre_pack(pack_path)


def _build_pulp_noir_character(name: str = "Sam Spade") -> object:
    """Walk pulp_noir's full choice-based chargen and build a PC.

    Generic walk (pick choice 0, auto-advance choiceless scenes) mirrors the
    neon real-pack test — robust to the scene changes the Fate migration makes.
    """
    pack = _load_pack()
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),  # type: ignore[attr-defined]
            rules=pack.rules,  # type: ignore[attr-defined]
            backstory_tables=pack.backstory_tables,  # type: ignore[attr-defined]
        )
        .with_lobby_name(name)
        .with_equipment_tables(pack.equipment_tables)  # type: ignore[attr-defined]
        .with_classes(pack.classes)  # type: ignore[attr-defined]
    )
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            builder.apply_choice(0)
        else:
            builder.apply_auto_advance()
    return builder.build(name)


# ---------------------------------------------------------------------------
# AC1 — pack loads + authors a valid fate config (ruleset: fate)
# ---------------------------------------------------------------------------


class TestAC1PackLoadsAsFate:
    def test_pulp_noir_binds_fate_ruleset(self) -> None:
        pack = _load_pack()
        assert pack.rules.ruleset == "fate", (  # type: ignore[attr-defined]
            "pulp_noir must bind ruleset: fate (defaults to 'native' pre-migration)"
        )

    def test_pulp_noir_authors_a_valid_fate_block(self) -> None:
        # load_genre_pack() runs RulesConfig validation; a ruleset: fate pack with
        # a missing/invalid fate block fails loud at load (_validate_fate). Reaching
        # a populated FateConfig here IS the AC1 "sidequest-validate passes" proof
        # for the fate semantics (the `validate pack` CLI only checks dir structure).
        cfg = _load_pack().rules.ruleset_config()  # type: ignore[attr-defined]
        assert isinstance(cfg, FateConfig), (
            "ruleset_config() must return a FateConfig for a fate-bound pack"
        )
        assert cfg.skills, "pulp_noir fate config must author a non-empty skill list"
        assert cfg.refresh >= 1, "refresh is a positive resource count"
        assert cfg.default_high_concept, "fate config must seed a default high concept"
        assert cfg.default_trouble, "fate config must seed a default trouble"

    def test_pulp_noir_authors_hardboiled_core_skills(self) -> None:
        # The genre's signature skills must be present (story 121-2 skill list).
        cfg = _load_pack().rules.ruleset_config()  # type: ignore[attr-defined]
        assert isinstance(cfg, FateConfig)
        core = {"Investigate", "Contacts", "Deceive", "Shoot", "Notice", "Rapport"}
        missing = core - set(cfg.skills)
        assert not missing, f"pulp_noir fate skills missing hard-boiled core: {sorted(missing)}"


# ---------------------------------------------------------------------------
# AC2 — a created PC has a populated FateSheet
# ---------------------------------------------------------------------------


class TestAC2CharacterGetsFateSheet:
    def test_real_pulp_noir_character_gets_fate_sheet(self) -> None:
        character = _build_pulp_noir_character()
        assert character.core.fate_sheet is not None, (  # type: ignore[attr-defined]
            "a ruleset: fate pulp_noir PC built through the production CharacterBuilder "
            "must have a populated FateSheet on CreatureCore.fate_sheet"
        )
        assert isinstance(character.core.fate_sheet, FateSheet)  # type: ignore[attr-defined]

    def test_fate_sheet_skills_match_pack_config(self) -> None:
        pack = _load_pack()
        character = _build_pulp_noir_character()
        assert character.core.fate_sheet.skills == pack.rules.fate.skills, (  # type: ignore[attr-defined]
            "the seeded fate sheet skills must match the pack's authored fate.skills"
        )

    def test_fate_sheet_has_high_concept_and_trouble_aspects(self) -> None:
        character = _build_pulp_noir_character()
        sheet = character.core.fate_sheet  # type: ignore[attr-defined]
        kinds = [a.kind for a in sheet.aspects]
        assert kinds.count("high_concept") == 1, "exactly one high-concept aspect seeded"
        assert kinds.count("trouble") == 1, "exactly one trouble aspect seeded"

    def test_fate_sheet_refresh_and_starting_fate_points(self) -> None:
        pack = _load_pack()
        character = _build_pulp_noir_character()
        sheet = character.core.fate_sheet  # type: ignore[attr-defined]
        assert sheet.refresh == pack.rules.fate.refresh, (  # type: ignore[attr-defined]
            "fate sheet refresh must match the pack's authored refresh"
        )
        assert sheet.refresh >= 1
        # SRD: a character starts a session with fate points == refresh.
        assert sheet.fate_points == sheet.refresh


# ---------------------------------------------------------------------------
# AC3 — a Fate action routes to the Fate engine (fate_conflict), not native
# ---------------------------------------------------------------------------


class TestAC3RoutesToFateNotNative:
    def test_pulp_noir_resolves_to_fate_module(self) -> None:
        # dispatch_fate_action() gates on isinstance(module, FateRulesetModule)
        # (fate_conflict.py) — so binding ruleset: fate is exactly what routes a
        # combat/social action to the Fate engine instead of the native dial path.
        # Behavior-based wiring proof (NOT a source grep), mirroring F4a's
        # test_fate_pack_routes_to_fate_module_not_native.
        pack = _load_pack()
        module = get_ruleset_module(pack.rules.ruleset)  # type: ignore[attr-defined]
        assert isinstance(module, FateRulesetModule), (
            "a fate-bound pulp_noir must resolve to FateRulesetModule so dispatch "
            "routes actions to fate_conflict, not the native engine"
        )

    def test_native_module_is_not_the_fate_module(self) -> None:
        # Paired negative: proves the isinstance gate actually discriminates.
        assert not isinstance(get_ruleset_module("native"), FateRulesetModule)


# ---------------------------------------------------------------------------
# AC4 — native d20 config is stripped
# ---------------------------------------------------------------------------


class TestAC4NativeConfigStripped:
    def test_power_tiers_removed(self) -> None:
        # power_tiers.yaml is loaded optionally into pack.power_tiers; a Fate pack
        # has no native power tiers, so the migrated pack must carry an empty map.
        pack = _load_pack()
        assert pack.power_tiers == {}, (  # type: ignore[attr-defined]
            "native power_tiers config must be stripped from a fate-bound pulp_noir"
        )

    def test_no_d20_ability_scores(self) -> None:
        # The de-d20 invariant (F4a base.py guard): a Fate pack declares no ability
        # scores, so the builder generates no attribute pool. Story 121-2 AC4: the
        # pack loads without d20-shaped attributes.
        pack = _load_pack()
        assert not pack.rules.ability_score_names, (  # type: ignore[attr-defined]
            "a fate-bound pulp_noir must not declare d20 ability_score_names"
        )


# ---------------------------------------------------------------------------
# AC5 — the F4a wiring contract holds with pulp_noir as the REAL test pack
# ---------------------------------------------------------------------------


class TestAC5RealPackWiring:
    def test_f4a_wiring_holds_for_real_pulp_noir(self) -> None:
        # The single end-to-end claim F4a proved synthetically and AC5 requires on
        # real content: a fate-bound pack, through the production builder, yields a
        # populated FateSheet AND resolves to the Fate engine (combat → fate_conflict).
        pack = _load_pack()
        character = _build_pulp_noir_character()

        assert character.core.fate_sheet is not None  # type: ignore[attr-defined]
        assert character.core.fate_sheet.skills, "real pulp_noir PC has seeded skills"  # type: ignore[attr-defined]
        assert isinstance(get_ruleset_module(pack.rules.ruleset), FateRulesetModule)  # type: ignore[attr-defined]
