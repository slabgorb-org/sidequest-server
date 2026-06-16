"""RED (story 114-10, full-scope migration): the four narrative packs bind
``ruleset: fate`` AND adopt the Fate gear model — pulp_noir, spaghetti_western,
tea_and_murder, wry_whimsy.

Per the operator decision (2026-06-16) 114-10 carries the full migration the
114-9 design lists: bind ``ruleset: fate`` (absorbing 121-3/4/5 for the three
still-native packs), give archetypes the Fate shape with authored ``refresh`` +
``fate.gear`` wiring (121-7 territory), drop ``inventory.yaml``, author
``gear.yaml``, and compile gear onto the sheet at chargen.

This parametrized real-pack test pins that end-state for all four packs. It is the
generalization of ``test_121_2_pulp_noir_fate_migration.py`` (the pulp_noir pilot)
across the whole set, plus the gear-specific assertions. Tests are
``skipif``-gated per pack so the suite is honest about which content is on disk.

Per project rule, content fixtures are normally synthesised — but real-pack
*migration* acceptance is exactly what an integration test is for (the 121-2
precedent), so these load live content behind a presence gate.
"""

from __future__ import annotations

import pytest

from sidequest.game.builder import CharacterBuilder
from sidequest.game.character import Character
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import FateConfig
from tests._helpers.genre_paths import PackNotFound, find_pack_path

PACKS = ["pulp_noir", "spaghetti_western", "tea_and_murder", "wry_whimsy"]


def _pack_dir(pack: str):
    try:
        return find_pack_path(pack)
    except PackNotFound:
        return None


def _present(pack: str) -> bool:
    return _pack_dir(pack) is not None


def _load(pack: str) -> GenrePack:
    from sidequest.genre.loader import load_genre_pack

    path = _pack_dir(pack)
    if path is None:  # pragma: no cover — guarded by skipif
        pytest.skip(f"{pack} not on disk")
    return load_genre_pack(path)


def _build_character(pack: str, name: str = "Test PC") -> Character:
    """Generic choice-based chargen walk (pick 0, auto-advance) — mirrors the
    pulp_noir pilot's robust walk."""
    gpack = _load(pack)
    builder = (
        CharacterBuilder(
            scenes=list(gpack.char_creation),
            rules=gpack.rules,
            backstory_tables=gpack.backstory_tables,
        )
        .with_lobby_name(name)
        .with_classes(gpack.classes)
    )
    if gpack.equipment_tables is not None:
        builder = builder.with_equipment_tables(gpack.equipment_tables)
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            builder.apply_choice(0)
        else:
            builder.apply_auto_advance()
    return builder.build(name)


pytestmark = pytest.mark.parametrize(
    "pack",
    [
        pytest.param(p, marks=pytest.mark.skipif(not _present(p), reason=f"{p} not on disk"))
        for p in PACKS
    ],
)


# ---------------------------------------------------------------------------
# Binding — every pack is ruleset: fate with a valid fate config
# ---------------------------------------------------------------------------


class TestFateBinding:
    def test_pack_binds_fate_ruleset(self, pack: str) -> None:
        assert _load(pack).rules.ruleset == "fate", (
            f"{pack} must bind ruleset: fate (114-10 full-scope migration)"
        )

    def test_pack_authors_valid_fate_config_with_refresh_knobs(self, pack: str) -> None:
        cfg = _load(pack).rules.ruleset_config()
        assert isinstance(cfg, FateConfig), f"{pack} ruleset_config() must be a FateConfig"
        assert cfg.skills, f"{pack} fate config must author a non-empty skill list"
        # The refresh-invariant knobs the gear validator reads (114-10 delta).
        assert cfg.base_refresh >= 1
        assert cfg.free_stunts >= 0

    def test_pack_routes_to_fate_module(self, pack: str) -> None:
        module = get_ruleset_module(_load(pack).rules.ruleset)
        assert isinstance(module, FateRulesetModule), (
            f"{pack} must resolve to FateRulesetModule so actions route to fate_conflict"
        )


# ---------------------------------------------------------------------------
# Inventory dropped — no inventory.yaml under a fate pack
# ---------------------------------------------------------------------------


class TestInventoryDropped:
    def test_no_genre_tier_inventory_yaml(self, pack: str) -> None:
        path = _pack_dir(pack)
        assert path is not None
        assert not (path / "inventory.yaml").exists(), (
            f"{pack} is ruleset: fate — its genre inventory.yaml must be deleted (Fate has no economy)"
        )

    def test_no_world_tier_inventory_yaml(self, pack: str) -> None:
        path = _pack_dir(pack)
        assert path is not None
        worlds = path / "worlds"
        stray = list(worlds.glob("*/inventory.yaml")) if worlds.is_dir() else []
        assert stray == [], f"{pack} has world-tier inventory.yaml that must be deleted: {stray}"


# ---------------------------------------------------------------------------
# Gear authored — gear.yaml present and loaded
# ---------------------------------------------------------------------------


class TestGearAuthored:
    def test_genre_gear_yaml_exists(self, pack: str) -> None:
        path = _pack_dir(pack)
        assert path is not None
        assert (path / "gear.yaml").exists(), (
            f"{pack} must author a genre-tier gear.yaml (the archetypal signature gear)"
        )

    def test_pack_loads_gear_into_geardefs(self, pack: str) -> None:
        # GenrePack must expose the loaded GearDef set so chargen can resolve
        # archetype gear ids against it. (hasattr is vacuous on a pydantic model —
        # the load-bearing assertion is that at least one GearDef actually loaded.)
        gpack = _load(pack)
        assert len(gpack.gear) >= 1, f"{pack} gear.yaml must load at least one GearDef"


# ---------------------------------------------------------------------------
# Chargen compiles gear onto the sheet (end-to-end, OTEL-visible)
# ---------------------------------------------------------------------------


class TestChargenCompilesGear:
    def test_built_character_gets_fate_sheet(self, pack: str) -> None:
        character = _build_character(pack)
        assert isinstance(character.core.fate_sheet, FateSheet), (
            f"a {pack} PC built through the production builder must have a FateSheet"
        )

    def test_built_character_has_gear_sourced_entries(self, pack: str) -> None:
        # The end-to-end proof gear compiled: at least one aspect or stunt on the
        # built sheet carries source_gear (materialized from the archetype's gear).
        character = _build_character(pack)
        sheet = character.core.fate_sheet
        assert sheet is not None
        sourced = [a for a in sheet.aspects if a.source_gear] + [
            s for s in sheet.stunts if s.source_gear
        ]
        assert sourced, (
            f"a {pack} PC must carry at least one gear-sourced aspect/stunt "
            "(archetype gear compiled onto the sheet)"
        )


# ---------------------------------------------------------------------------
# Validator clean — the pack loads + passes structural validation
# ---------------------------------------------------------------------------


class TestValidatorClean:
    def test_pack_loads_without_raising(self, pack: str) -> None:
        # load_genre_pack runs RulesConfig + (114-10) fate-gear validation; a
        # paradigm-mismatched or unbalanced pack (inventory.yaml under fate, a
        # dangling default gear id, an unpaid stunt-item) fails loud here. The
        # gate is "load succeeds"; we also confirm it actually resolved the pack's
        # default gear — every cfg.gear id must exist in the loaded GearDef set,
        # i.e. the load-time dangling-gear-id validator ran and passed (distinct
        # from TestFateBinding, which never touches gear resolution).
        gpack = _load(pack)  # must not raise
        cfg = gpack.rules.ruleset_config()
        assert isinstance(cfg, FateConfig)
        available = {g.id for g in gpack.gear}
        assert set(cfg.gear) <= available, (
            f"{pack} default gear ids {sorted(cfg.gear)} must all resolve against "
            f"loaded GearDefs {sorted(available)} (no dangling id slipped the load-time check)"
        )

    def test_structural_validator_reports_no_errors(self, pack: str) -> None:
        from pathlib import Path

        from sidequest.cli.validate.pack import validate_pack_structure

        path = _pack_dir(pack)
        assert path is not None
        schema = Path(__file__).resolve().parents[3] / "sidequest-content" / "pack_schema.yaml"
        errors, _warnings = validate_pack_structure(path, schema)
        # The schema must treat inventory.yaml as conditional (absent for fate)
        # and gear.yaml as the fate-tier replacement — so a migrated fate pack
        # has ZERO structural errors.
        assert errors == [], f"{pack} structural validation must be clean: {errors}"
