"""Shared pytest fixtures for sidequest-server tests."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Refresh recorded SVG snapshots in tests/orbital/snapshots/.",
    )


@pytest.fixture(scope="session")
def content_dir() -> Path:
    """Path to the sidequest-content repo (genre packs, worlds)."""
    return Path(__file__).resolve().parent.parent.parent / "sidequest-content"


@pytest.fixture
def tmp_save_dir(tmp_path: Path) -> Path:
    """Temporary save directory per test."""
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    return save_dir


@pytest.fixture
async def initialized_tracer() -> AsyncIterator[None]:
    """Initialize OTEL tracer for the duration of a test."""
    from sidequest.telemetry import init_tracer

    init_tracer(service_name="sidequest-server-test")
    yield


# --- caverns_sunden deprecation: skip world-coupled tests -----------------
# caverns_sunden was deprecated in favor of beneath_sunden and relocated to
# genre_workshopping/ (sidequest-content PR #228). Every test below binds to
# that now-removed world (in-memory snapshots, fixtures, or on-disk world
# loads). They are SKIPPED — deliberately and visibly — pending a re-point
# to beneath_sunden or a dedicated test-fixture world. This single block is
# the reversible, documented record of that debt; nothing is buried.
# NOTE (2026-05-17, [BS-BUG-LOW]): agents/test_pov_swap.py was REMOVED
# from this set. It is a pure unit suite for swap_to_second_person — a
# world-agnostic string transform with generic names and no genre/world
# fixtures, snapshots, or on-disk world loads. Its only caverns_sunden
# tie is a docstring noting where the bug was originally found; PR #312's
# name-grep swept it in over-broadly. pov_swap is live in the
# beneath_sunden playtest right now, so its regression coverage must run
# (CLAUDE.md: no skipping tests for live subsystems). Re-included
# deliberately and visibly, in the spirit of PR #312's own reversible-
# with-reason record.
_CAVERNS_SUNDEN_DEPRECATED_TESTS = frozenset(
    {
        "audio/test_library_backend_r2_only.py",
        "cli/test_encountergen.py",
        "game/test_disposition_call_site_migration.py",
        "game/test_room_file_loader.py",
        "genre/test_beneath_sunden_world_load.py",
        "genre/test_models/test_pack_integration.py",
        "genre/test_visual_style_lora_removal_wiring.py",
        "genre/test_world_items_loader.py",
        "integration/test_cavern_static_mount.py",
        "integration/test_room_enter_cavern.py",
        "magic/test_47_9_innate_proactive.py",
        "magic/test_e2e_cnc_memorization.py",
        "magic/test_state.py",
        "protocol/test_models.py",
        "server/dispatch/test_pregen.py",
        "server/test_adr105_b1_secret_invariant_wiring.py",
        "server/test_chargen_arrange_dispatch.py",
        "server/test_chargen_dispatch.py",
        "server/test_chargen_persist_and_play.py",
        "server/test_chargen_story_dispatch.py",
        "server/test_confrontation_mp_broadcast.py",
        "server/test_confrontation_per_pc_projection.py",
        "server/test_dice_throw_session_wiring.py",
        "server/test_magic_init_caverns_and_claudes.py",
        "server/test_magic_init_mp_second_commit.py",
        "server/test_magic_init.py",
        "server/test_merged_mp_emitter_projection.py",
        "server/test_narration_pov_emission.py",
        "server/test_opening_turn_bootstrap.py",
        "server/test_persistence_otel_wiring.py",
        "server/test_region_init.py",
        "server/test_resource_deltas.py",
        "server/test_rest_hub_endpoint.py",
        "server/test_room_graph_init.py",
        "server/test_yield_dispatch.py",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    tests_root = Path(__file__).parent
    skip = pytest.mark.skip(
        reason="caverns_sunden deprecated → genre_workshopping "
        "(sidequest-content PR #228); test world binding pending migration"
    )
    for item in items:
        try:
            rel = item.path.relative_to(tests_root).as_posix()
        except ValueError:
            continue
        if rel in _CAVERNS_SUNDEN_DEPRECATED_TESTS:
            item.add_marker(skip)


# --- Shared synthetic-pack fixture (visible to all test packages) --------
# Relocated from tests/genre/conftest.py (Story 50-13): the disposition
# threshold loader/OTEL wiring suite lives under tests/game/ and needs the
# same clone-a-fixture-pack helper the genre suite uses. conftest fixtures
# are directory-scoped, so a single definition at the tests/ root is the
# correct home — one definition, visible to genre/, game/, and beyond.

# Canonical minimal pack fixture used as the base for clone-based tests.
_FIXTURE_PACK = Path(__file__).resolve().parent / "fixtures" / "packs" / "test_genre"


class MinimalPack:
    """A temporary copy of the test_genre fixture pack with mutable YAML overrides.

    Usage::

        pack = minimal_pack_factory(tmp_path)
        pack.set_rules_yaml(confrontations=[...], allowed_classes=["Fighter"])
        pack.set_classes_yaml([{"id": "fighter", ...}])
        load_genre_pack(pack.path)
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def set_rules_yaml(
        self,
        *,
        confrontations: list[dict[str, Any]],
        allowed_classes: list[str],
    ) -> None:
        """Write a minimal rules.yaml with the given confrontations and allowed_classes.

        All other required RulesConfig fields are set to safe defaults.
        """
        data: dict[str, Any] = {
            "tone": "test",
            "lethality": "low",
            "magic_level": "none",
            "stat_generation": "point_buy",
            "point_buy_budget": 27,
            "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            "allowed_classes": allowed_classes,
            "allowed_races": [],
            "confrontations": confrontations,
        }
        rules_path = self.path / "rules.yaml"
        with rules_path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def set_classes_yaml(self, classes: list[dict[str, Any]]) -> None:
        """Write classes.yaml with the given class definition dicts."""
        classes_path = self.path / "classes.yaml"
        with classes_path.open("w", encoding="utf-8") as f:
            yaml.dump(classes, f, default_flow_style=False, sort_keys=False)

    def create_spells_dir(self) -> None:
        """Create a minimal spells/ directory at the pack root.

        This simulates a pack that ships a spell catalog, which triggers
        the saving_throws validator. The YAML content is a minimal valid
        SpellCatalog entry (all required fields present).
        """
        spells_dir = self.path / "spells"
        spells_dir.mkdir(exist_ok=True)
        stub_catalog = {
            "version": "1.0",
            "genre": "test_pack",
            "tradition": "arcane",
            "level": 1,
            "spells": [
                {
                    "id": "magic_missile",
                    "name": "Magic Missile",
                    "level": 1,
                    "tradition": "arcane",
                    "range": "near",
                    "target": "single",
                    "duration": "instant",
                    "save": {"stat": None, "effect": "none"},
                    "effect_template": "Auto-hit bolt of force.",
                    "components": {"verbal": True, "somatic": True},
                    "backlash": None,
                    "narrator_register": "A bolt of force streaks unerringly.",
                    "domain": "force",
                }
            ],
        }
        catalog_path = spells_dir / "arcane_l1.yaml"
        with catalog_path.open("w", encoding="utf-8") as f:
            yaml.dump(stub_catalog, f, default_flow_style=False, sort_keys=False)


@pytest.fixture
def minimal_pack_factory():
    """Factory fixture: call with (tmp_path) to get a MinimalPack.

    The returned pack is a full clone of tests/fixtures/packs/test_genre with
    its lethality_policy.yaml genre_key updated to match the tmp directory name.
    Call set_rules_yaml() / set_classes_yaml() to inject test-specific YAML.
    """

    def _factory(tmp_path: Path) -> MinimalPack:
        dest = tmp_path / "test_pack"
        shutil.copytree(_FIXTURE_PACK, dest)
        # Update lethality_policy.yaml genre_key to match the new directory name.
        lethality_yaml = dest / "lethality_policy.yaml"
        if lethality_yaml.exists():
            with lethality_yaml.open("r", encoding="utf-8") as f:
                policy_data = yaml.safe_load(f) or {}
            policy_data["genre_key"] = dest.name
            with lethality_yaml.open("w", encoding="utf-8") as f:
                yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)
        return MinimalPack(dest)

    return _factory
