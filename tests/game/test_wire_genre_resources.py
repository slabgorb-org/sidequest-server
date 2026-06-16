"""Story 42-2 (port of Story 16-12): Wire genre resources — Luck, Humanity, Heat,
Fuel-at-rest as ResourcePool instances.

RED phase tests. Verify genre-specific resource declarations in rules.yaml load
correctly, initialize ResourcePools, and wire through the full pipeline.

ACs tested:
  AC1: spaghetti_western declares Luck (0-6, voluntary, thresholds at 1 and 0)
  AC2: neon_dystopia declares Humanity (0-100, involuntary, thresholds at 50/25/0)
  AC3: pulp_noir declares Heat (0-5, involuntary, decay 0.1/turn)
  AC4: road_warrior declares Fuel (0-100, transfer to RigStats on confrontation)
  AC5: Genre loader parses and inits ResourcePools on GameSnapshot
  AC6: Bounds validation per genre
  AC7: Integration: load → init → patch → threshold → LoreStore

Port discipline: Rust source at
``sidequest-api/crates/sidequest-game/tests/wire_genre_resources_story_16_12_tests.rs``
is the behavioural contract. Each Rust `#[test]` becomes one pytest function
with the same name (snake_case matches).

Port translations:
  Rust ``load_rules_config`` (private loader used by tests) → Python
    ``load_genre_pack(path).rules`` (public full-pack loader that resolves
    ``_from`` pointers identically).
  Rust ``ResourcePatchOp::Add`` → Python ``ResourcePatchOp.Add`` (PascalCase
    preserved — StrEnum members match Rust variant names per architect pre-red
    assessment; wire values are lowercase per ``serde(rename_all = "lowercase")``).
  Rust ``apply_resource_patch_by_name(...) -> Result`` → Python
    ``apply_resource_patch_by_name(...)`` raising on error.
  Rust ``process_resource_patch_with_lore(...).unwrap()`` → Python same
    (raises on error).

Known Delivery Finding (logged in .session/42-2-session.md): four of the five
genre packs referenced by this file (``neon_dystopia``, ``pulp_noir``,
``road_warrior``, ``low_fantasy``) were moved from ``genre_packs/`` to
``genre_workshopping/`` after the Rust tests were authored. Tests depending
on those packs will RED with "pack not found" until the packs are restored
or the paths are updated. This is a CONTENT-side decision, not a test-port
decision — surfacing to Dev/Architect/team-lead for resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.lore_store import LoreStore
from sidequest.game.resource_pool import ResourcePatchOp, ResourcePool
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResourceDeclaration, RulesConfig
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# ═══════════════════════════════════════════════════════════
# Test helpers (port of Rust helpers)
# ═══════════════════════════════════════════════════════════


def genre_pack_path(genre: str) -> Path:
    """Path to a genre pack on disk (resolves either content root).

    Port of Rust ``genre_pack_path(genre)``. Routes through
    :func:`tests._helpers.genre_paths.find_pack_path` so packs resolve
    correctly whether they live under ``genre_packs/`` (production) or
    ``genre_workshopping/`` (in development).

    For packs that aren't on disk under either root, returns a sentinel
    non-existent path so existing ``.exists()`` skip-marker checks still
    behave as before (False → skip the test).
    """
    try:
        return find_pack_path(genre)
    except PackNotFound:
        # Sentinel: a non-existent path so callers' .exists() checks fail.
        root = Path(__file__).resolve().parents[3]
        return root / "sidequest-content" / "genre_packs" / genre


def load_rules_yaml(genre: str) -> RulesConfig:
    """Load and return the rules block of a genre pack.

    Port of Rust ``load_rules_yaml(genre)``. Rust called private
    ``load_rules_config`` directly; Python routes through the public full-pack
    loader which resolves ``_from`` pointers before returning
    ``pack.rules``.
    """
    pack_path = genre_pack_path(genre)
    if not pack_path.exists():
        raise AssertionError(f"Failed to find genre pack at {pack_path}")
    return load_genre_pack(pack_path).rules


def find_resource(rules: RulesConfig, name: str) -> ResourceDeclaration:
    """Find a resource declaration by name in a rules block.

    Port of Rust ``find_resource(rules, name)``.
    """
    for r in rules.resources:
        if r.name == name:
            return r
    raise AssertionError(f"resource '{name}' not found in rules.yaml")


# ═══════════════════════════════════════════════════════════
# Conditional skip markers — workshopping packs
#
# Four of the five genre packs referenced by this file (``neon_dystopia``,
# ``pulp_noir``, ``road_warrior``, ``low_fantasy``) were moved from
# ``sidequest-content/genre_packs/`` to ``sidequest-content/genre_workshopping/``
# in content commit ``acc89a3`` ("chore: move incomplete genre packs to
# genre_workshopping"). Team-lead + architect decision (2026-04-21): tests
# depending on those packs get conditional skips that auto-unskip when a
# pack graduates back to ``genre_packs/``. ``spaghetti_western`` stays
# canonical and is NEVER skipped — it's the anchor fixture for 42-2's
# pack-integration coverage, and for the 42-4 pack-integration smoke test.
#
# Grep recipe at pack-promotion time:
#     grep -rn 'genre_workshopping/' tests/
# ═══════════════════════════════════════════════════════════


def _pack_missing_reason(name: str) -> str:
    return (
        f"pack '{name}' relocated to sidequest-content/genre_workshopping/ "
        f"(commit acc89a3); re-enable when promoted back to genre_packs/"
    )


# NOTE: skip markers deliberately check GENRE_PACKS_DIR (production root)
# rather than genre_pack_path() — the latter resolves to genre_workshopping/
# too, but the documented contract here ("auto-unskip when a pack graduates
# back to genre_packs/") is specifically about *graduation to production*.
# A pack still in workshopping must remain skipped even though it's on disk.
def _is_promoted(name: str) -> bool:
    return (GENRE_PACKS_DIR / name / "pack.yaml").is_file()


_requires_neon_dystopia = pytest.mark.skipif(
    not _is_promoted("neon_dystopia"),
    reason=_pack_missing_reason("neon_dystopia"),
)
_requires_pulp_noir = pytest.mark.skipif(
    not _is_promoted("pulp_noir"),
    reason=_pack_missing_reason("pulp_noir"),
)
_requires_road_warrior = pytest.mark.skipif(
    not _is_promoted("road_warrior"),
    reason=_pack_missing_reason("road_warrior"),
)
_requires_low_fantasy = pytest.mark.skipif(
    not _is_promoted("low_fantasy"),
    reason=_pack_missing_reason("low_fantasy"),
)


# ═══════════════════════════════════════════════════════════
# AC1: spaghetti_western — Luck (0-6, voluntary, thresholds at 1 and 0)
# ═══════════════════════════════════════════════════════════


def test_spaghetti_western_has_luck_resource():
    rules = load_rules_yaml("spaghetti_western")
    luck = find_resource(rules, "luck")

    assert luck.label == "Luck"
    assert abs(luck.min - 0.0) < 1e-9, "luck min should be 0"
    assert abs(luck.max - 6.0) < 1e-9, "luck max should be 6"
    assert luck.voluntary, "luck should be voluntary (player-spendable)"
    assert abs(luck.decay_per_turn - 0.0) < 1e-9, "luck should not decay"


def test_spaghetti_western_luck_starting_value():
    rules = load_rules_yaml("spaghetti_western")
    luck = find_resource(rules, "luck")

    assert luck.min <= luck.starting <= luck.max, (
        f"starting value {luck.starting} should be in [{luck.min}, {luck.max}]"
    )


def test_spaghetti_western_luck_has_threshold_at_1():
    rules = load_rules_yaml("spaghetti_western")
    luck = find_resource(rules, "luck")

    assert any(abs(t.at - 1.0) < 1e-9 for t in luck.thresholds), (
        "luck should have a threshold at 1.0"
    )


def test_spaghetti_western_luck_has_threshold_at_0():
    rules = load_rules_yaml("spaghetti_western")
    luck = find_resource(rules, "luck")

    assert any(abs(t.at - 0.0) < 1e-9 for t in luck.thresholds), (
        "luck should have a threshold at 0.0"
    )


def test_spaghetti_western_luck_thresholds_have_event_ids():
    rules = load_rules_yaml("spaghetti_western")
    luck = find_resource(rules, "luck")

    for threshold in luck.thresholds:
        assert threshold.event_id, "every threshold should have a non-empty event_id"
        assert threshold.narrator_hint, "every threshold should have a non-empty narrator_hint"


# ═══════════════════════════════════════════════════════════
# AC2: neon_dystopia — Humanity (0-100, involuntary, thresholds at 50/25/0)
# ═══════════════════════════════════════════════════════════


@_requires_neon_dystopia
def test_neon_dystopia_has_humanity_resource():
    rules = load_rules_yaml("neon_dystopia")
    humanity = find_resource(rules, "humanity")

    assert humanity.label == "Humanity"
    assert abs(humanity.min - 0.0) < 1e-9
    assert abs(humanity.max - 100.0) < 1e-9
    assert not humanity.voluntary, "humanity should be involuntary"


@_requires_neon_dystopia
def test_neon_dystopia_humanity_has_threshold_at_50():
    rules = load_rules_yaml("neon_dystopia")
    humanity = find_resource(rules, "humanity")

    assert any(abs(t.at - 50.0) < 1e-9 for t in humanity.thresholds), (
        "humanity should have a threshold at 50"
    )


@_requires_neon_dystopia
def test_neon_dystopia_humanity_has_threshold_at_25():
    rules = load_rules_yaml("neon_dystopia")
    humanity = find_resource(rules, "humanity")

    assert any(abs(t.at - 25.0) < 1e-9 for t in humanity.thresholds), (
        "humanity should have a threshold at 25"
    )


@_requires_neon_dystopia
def test_neon_dystopia_humanity_has_threshold_at_0():
    rules = load_rules_yaml("neon_dystopia")
    humanity = find_resource(rules, "humanity")

    assert any(abs(t.at - 0.0) < 1e-9 for t in humanity.thresholds), (
        "humanity should have a threshold at 0"
    )


@_requires_neon_dystopia
def test_neon_dystopia_humanity_thresholds_have_narrator_hints():
    rules = load_rules_yaml("neon_dystopia")
    humanity = find_resource(rules, "humanity")

    assert len(humanity.thresholds) >= 3, "humanity should have at least 3 thresholds (50, 25, 0)"
    for threshold in humanity.thresholds:
        assert threshold.narrator_hint, f"threshold at {threshold.at} should have a narrator_hint"


# ═══════════════════════════════════════════════════════════
# AC3: pulp_noir — Heat (0-5, involuntary, decay 0.1/turn)
# ═══════════════════════════════════════════════════════════


@_requires_pulp_noir
def test_pulp_noir_has_heat_resource():
    rules = load_rules_yaml("pulp_noir")
    heat = find_resource(rules, "heat")

    assert heat.label == "Heat"
    assert abs(heat.min - 0.0) < 1e-9
    assert abs(heat.max - 5.0) < 1e-9
    assert not heat.voluntary, "heat should be involuntary"


@_requires_pulp_noir
def test_pulp_noir_heat_has_decay():
    rules = load_rules_yaml("pulp_noir")
    heat = find_resource(rules, "heat")

    assert abs(heat.decay_per_turn - (-0.1)) < 1e-9, (
        f"heat should decay by 0.1 per turn, got: {heat.decay_per_turn}"
    )


@_requires_pulp_noir
def test_pulp_noir_heat_starts_at_zero():
    rules = load_rules_yaml("pulp_noir")
    heat = find_resource(rules, "heat")

    assert abs(heat.starting - 0.0) < 1e-9, (
        "heat should start at 0 (you earn heat, not start with it)"
    )


# ═══════════════════════════════════════════════════════════
# AC4: road_warrior — Fuel (0-100, resource-at-rest → RigStats transfer)
# ═══════════════════════════════════════════════════════════


@_requires_road_warrior
def test_road_warrior_has_fuel_resource():
    rules = load_rules_yaml("road_warrior")
    fuel = find_resource(rules, "fuel")

    assert fuel.label == "Fuel"
    assert abs(fuel.min - 0.0) < 1e-9
    assert abs(fuel.max - 100.0) < 1e-9
    assert not fuel.voluntary, "fuel should be involuntary (consumed by driving)"


@_requires_road_warrior
def test_road_warrior_fuel_starting_value():
    rules = load_rules_yaml("road_warrior")
    fuel = find_resource(rules, "fuel")

    assert fuel.starting > 0.0, "fuel should have a positive starting value"
    assert fuel.starting <= fuel.max, "fuel starting should not exceed max"


# ═══════════════════════════════════════════════════════════
# AC5: Genre loader parses and inits ResourcePools on GameSnapshot
# ═══════════════════════════════════════════════════════════


def test_genre_loader_parses_spaghetti_western_resources():
    path = genre_pack_path("spaghetti_western")
    assert path.exists(), f"spaghetti_western genre pack not found at {path}"
    pack = load_genre_pack(path)

    luck = next((r for r in pack.rules.resources if r.name == "luck"), None)
    assert luck is not None, "loader should parse luck resource from spaghetti_western"


@_requires_neon_dystopia
def test_genre_loader_parses_neon_dystopia_resources():
    path = genre_pack_path("neon_dystopia")
    pack = load_genre_pack(path)

    humanity = next((r for r in pack.rules.resources if r.name == "humanity"), None)
    assert humanity is not None, "loader should parse humanity resource from neon_dystopia"


def test_init_pools_from_spaghetti_western_declarations():
    rules = load_rules_yaml("spaghetti_western")
    snap = GameSnapshot()

    snap.init_resource_pools(rules.resources)

    assert "luck" in snap.resources, (
        "luck pool should be initialized from spaghetti_western declarations"
    )
    pool = snap.resources["luck"]
    assert abs(pool.max - 6.0) < 1e-9
    assert pool.voluntary


@_requires_neon_dystopia
def test_init_pools_from_neon_dystopia_declarations():
    rules = load_rules_yaml("neon_dystopia")
    snap = GameSnapshot()

    snap.init_resource_pools(rules.resources)

    assert "humanity" in snap.resources
    pool = snap.resources["humanity"]
    assert abs(pool.max - 100.0) < 1e-9
    assert not pool.voluntary
    assert len(pool.thresholds) >= 3, "humanity pool should have at least 3 thresholds from YAML"


@_requires_pulp_noir
def test_init_pools_from_pulp_noir_declarations():
    rules = load_rules_yaml("pulp_noir")
    snap = GameSnapshot()

    snap.init_resource_pools(rules.resources)

    assert "heat" in snap.resources
    pool = snap.resources["heat"]
    assert abs(pool.decay_per_turn - (-0.1)) < 1e-9


@_requires_road_warrior
def test_init_pools_from_road_warrior_declarations():
    rules = load_rules_yaml("road_warrior")
    snap = GameSnapshot()

    snap.init_resource_pools(rules.resources)

    assert "fuel" in snap.resources
    pool = snap.resources["fuel"]
    assert abs(pool.max - 100.0) < 1e-9


# ═══════════════════════════════════════════════════════════
# AC6: Bounds validation per genre
# ═══════════════════════════════════════════════════════════


def test_spaghetti_western_luck_validates_bounds():
    rules = load_rules_yaml("spaghetti_western")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    # Try to exceed luck max (6.0) — should clamp, not raise
    snap.apply_resource_patch_by_name("luck", ResourcePatchOp.Add, 100.0)
    assert snap.resources["luck"].current <= 6.0, "luck should clamp to max 6.0"


@_requires_neon_dystopia
def test_neon_dystopia_humanity_validates_bounds():
    rules = load_rules_yaml("neon_dystopia")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    # Try to go below humanity min (0.0) — should clamp, not raise
    snap.apply_resource_patch_by_name("humanity", ResourcePatchOp.Subtract, 999.0)
    assert snap.resources["humanity"].current >= 0.0, "humanity should clamp to min 0.0"


@_requires_pulp_noir
def test_pulp_noir_heat_validates_bounds():
    rules = load_rules_yaml("pulp_noir")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    # Try to exceed heat max (5.0) — should clamp, not raise
    snap.apply_resource_patch_by_name("heat", ResourcePatchOp.Add, 100.0)
    assert snap.resources["heat"].current <= 5.0, "heat should clamp to max 5.0"


@_requires_road_warrior
def test_road_warrior_fuel_validates_bounds():
    rules = load_rules_yaml("road_warrior")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    # Try to go below fuel min (0.0) — should clamp, not raise
    snap.apply_resource_patch_by_name("fuel", ResourcePatchOp.Subtract, 999.0)
    assert snap.resources["fuel"].current >= 0.0, "fuel should clamp to min 0.0"


# ═══════════════════════════════════════════════════════════
# AC7: Integration — load → init → patch → threshold → LoreStore
# ═══════════════════════════════════════════════════════════


def test_spaghetti_western_luck_threshold_fires_known_fact():
    rules = load_rules_yaml("spaghetti_western")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    store = LoreStore()

    # Drain luck from starting to below threshold at 1.0
    starting = snap.resources["luck"].current
    drain = starting  # drain all luck to 0
    snap.process_resource_patch_with_lore("luck", ResourcePatchOp.Subtract, drain, store, 10)

    assert len(store) > 0, "draining luck past thresholds should mint KnownFacts"


@_requires_neon_dystopia
def test_neon_dystopia_humanity_threshold_fires_known_fact():
    rules = load_rules_yaml("neon_dystopia")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    store = LoreStore()

    # Drop humanity from 100 to 40 — should cross threshold at 50
    snap.process_resource_patch_with_lore("humanity", ResourcePatchOp.Subtract, 60.0, store, 15)

    assert len(store) > 0, "dropping humanity below 50 should mint a KnownFact"


@_requires_pulp_noir
def test_pulp_noir_heat_decay_integration():
    rules = load_rules_yaml("pulp_noir")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)

    # Add some heat first
    snap.apply_resource_patch_by_name("heat", ResourcePatchOp.Add, 3.0)
    assert abs(snap.resources["heat"].current - 3.0) < 1e-9

    # Apply decay — should reduce by 0.1
    snap.apply_pool_decay()
    assert abs(snap.resources["heat"].current - 2.9) < 1e-9, (
        f"heat should decay by 0.1, got: {snap.resources['heat'].current}"
    )


# ═══════════════════════════════════════════════════════════
# ResourceDeclaration now requires thresholds field
# ═══════════════════════════════════════════════════════════


def test_resource_declaration_with_thresholds_deserializes():
    import yaml as _yaml

    yaml_text = """
name: luck
label: Luck
min: 0
max: 6
starting: 3
voluntary: true
decay_per_turn: 0.0
thresholds:
  - at: 1
    event_id: luck_critical
    narrator_hint: "Nearly out of luck."
  - at: 0
    event_id: luck_depleted
    narrator_hint: "Completely out of luck."
"""

    data = _yaml.safe_load(yaml_text)
    decl = ResourceDeclaration.model_validate(data)
    assert decl.name == "luck"
    assert len(decl.thresholds) == 2
    assert decl.thresholds[0].event_id == "luck_critical"
    assert abs(decl.thresholds[0].at - 1.0) < 1e-9


def test_resource_declaration_without_thresholds_defaults_empty():
    import yaml as _yaml

    yaml_text = """
name: heat
label: Heat
min: 0
max: 5
starting: 0
voluntary: false
decay_per_turn: -0.1
"""

    data = _yaml.safe_load(yaml_text)
    decl = ResourceDeclaration.model_validate(data)
    assert decl.thresholds == [], "missing thresholds field should default to empty list"


def test_rules_config_resources_with_thresholds_parses():
    import yaml as _yaml

    yaml_text = """
stat_generation: point_buy
point_buy_budget: 27
magic_level: none
default_class: Drifter
default_race: "Frontier Born"
default_location: "A nameless border town"
default_time_of_day: high_noon

resources:
  - name: luck
    label: Luck
    min: 0
    max: 6
    starting: 3
    voluntary: true
    decay_per_turn: 0.0
    thresholds:
      - at: 1
        event_id: luck_critical
        narrator_hint: "Nearly out of luck."
      - at: 0
        event_id: luck_depleted
        narrator_hint: "Completely out of luck."
"""

    data = _yaml.safe_load(yaml_text)
    rules = RulesConfig.model_validate(data)
    assert len(rules.resources) == 1
    assert len(rules.resources[0].thresholds) == 2


# ═══════════════════════════════════════════════════════════
# Edge: genres without resources still load fine
# ═══════════════════════════════════════════════════════════


@_requires_low_fantasy
def test_genre_without_resources_loads_empty():
    # low_fantasy doesn't declare resources
    rules = load_rules_yaml("low_fantasy")
    assert rules.resources == [], "genres without resource declarations should have empty list"


@_requires_low_fantasy
def test_init_pools_from_empty_declarations_no_crash():
    rules = load_rules_yaml("low_fantasy")
    snap = GameSnapshot()
    snap.init_resource_pools(rules.resources)
    assert snap.resources == {}


# ═══════════════════════════════════════════════════════════
# Upsert semantics — story consolidation phase 1a (2026-04)
#
# init_resource_pools must be idempotent and must preserve `current`
# when called a second time with the same declarations. This is what
# makes old-save migration work: the deserializer creates minimal
# pools with the saved `current`, then init_resource_pools populates
# genre-pack metadata without clobbering the player's progress.
# ═══════════════════════════════════════════════════════════


def test_init_resource_pools_preserves_current_on_second_call():
    decl = ResourceDeclaration(
        name="luck",
        label="Luck",
        min=0.0,
        max=10.0,
        starting=5.0,
        voluntary=True,
        decay_per_turn=0.0,
        thresholds=[],
    )
    snap = GameSnapshot()

    # First call — creates pool at starting value.
    snap.init_resource_pools([decl])
    assert abs(snap.resources["luck"].current - 5.0) < 1e-9

    # Simulate gameplay: player's current drops to 2.
    snap.resources["luck"].current = 2.0

    # Second call (e.g., after save/load re-runs session init).
    snap.init_resource_pools([decl])

    # Current MUST be preserved — not reset to starting.
    assert abs(snap.resources["luck"].current - 2.0) < 1e-9, (
        f"init_resource_pools must preserve existing current on upsert; "
        f"got {snap.resources['luck'].current} "
        f"(expected 2.0 — was reset to starting 5.0?)"
    )


def test_init_resource_pools_populates_label_from_declaration():
    decl = ResourceDeclaration(
        name="heat",
        label="Heat",
        min=0.0,
        max=5.0,
        starting=0.0,
        voluntary=False,
        decay_per_turn=-0.1,
        thresholds=[],
    )
    snap = GameSnapshot()
    snap.init_resource_pools([decl])

    assert snap.resources["heat"].label == "Heat", (
        "label must be populated from genre pack declaration"
    )


def test_init_resource_pools_updates_bounds_but_reclamps_current():
    decl_wide = ResourceDeclaration(
        name="fuel",
        label="Fuel",
        min=0.0,
        max=100.0,
        starting=50.0,
        voluntary=True,
        decay_per_turn=0.0,
        thresholds=[],
    )
    snap = GameSnapshot()
    snap.init_resource_pools([decl_wide])

    # Player has 80 fuel.
    snap.resources["fuel"].current = 80.0

    # Genre pack is re-loaded with narrower bounds (e.g., mod balance patch).
    decl_narrow = ResourceDeclaration(
        name="fuel",
        label="Fuel",
        min=0.0,
        max=50.0,
        starting=25.0,
        voluntary=True,
        decay_per_turn=0.0,
        thresholds=[],
    )
    snap.init_resource_pools([decl_narrow])

    # Current re-clamps to the new max (80 > 50 → 50).
    assert abs(snap.resources["fuel"].current - 50.0) < 1e-9, (
        f"current must re-clamp when bounds narrow; got {snap.resources['fuel'].current}"
    )
    assert abs(snap.resources["fuel"].max - 50.0) < 1e-9


def test_resource_pool_label_serde_defaults_empty():
    # Old saves predating the label field should deserialize with label = "".
    import json as _json

    payload = _json.loads("""{
        "name": "luck",
        "current": 3.0,
        "min": 0.0,
        "max": 10.0,
        "voluntary": true,
        "decay_per_turn": 0.0
    }""")
    pool = ResourcePool.model_validate(payload)
    assert pool.label == "", "old saves without label should deserialize with empty label"
    assert abs(pool.current - 3.0) < 1e-9


# ═══════════════════════════════════════════════════════════
# Phase 4 — GameSnapshot migration from legacy resource_state
# ═══════════════════════════════════════════════════════════


def test_old_save_with_resource_state_migrates_to_resources_map():
    # Minimal save JSON shaped like a pre-phase-4 persistence file:
    # resource_state is populated, resources is absent.
    import json as _json

    payload = _json.loads("""{
        "genre_slug": "spaghetti_western",
        "world_slug": "border_town",
        "resource_state": { "luck": 2.5, "heat": 3.0 },
        "resource_declarations": [
            { "name": "luck", "label": "Luck", "min": 0.0, "max": 6.0,
              "starting": 3.0, "voluntary": true, "decay_per_turn": 0.0 },
            { "name": "heat", "label": "Heat", "min": 0.0, "max": 5.0,
              "starting": 0.0, "voluntary": false, "decay_per_turn": -0.1 }
        ]
    }""")

    snap = GameSnapshot.model_validate(payload)

    # Migration populated the resources map.
    assert len(snap.resources) == 2
    assert abs(snap.resources["luck"].current - 2.5) < 1e-9, (
        f"luck.current must be preserved from resource_state, got {snap.resources['luck'].current}"
    )
    assert abs(snap.resources["heat"].current - 3.0) < 1e-9, "heat.current must be preserved"
    # Labels and bounds came from resource_declarations.
    assert snap.resources["luck"].label == "Luck"
    assert snap.resources["heat"].label == "Heat"
    assert abs(snap.resources["luck"].max - 6.0) < 1e-9
    assert abs(snap.resources["heat"].decay_per_turn - (-0.1)) < 1e-9


def test_new_save_with_resources_takes_precedence_over_legacy_fields():
    # Both resources and resource_state are present. The new field wins.
    import json as _json

    payload = _json.loads("""{
        "genre_slug": "test",
        "world_slug": "test",
        "resource_state": { "luck": 9.9 },
        "resources": {
            "luck": {
                "name": "luck",
                "label": "Luck",
                "current": 4.0,
                "min": 0.0,
                "max": 6.0,
                "voluntary": true,
                "decay_per_turn": 0.0
            }
        }
    }""")

    snap = GameSnapshot.model_validate(payload)
    assert abs(snap.resources["luck"].current - 4.0) < 1e-9, (
        "resources field (4.0) must take precedence over legacy resource_state (9.9)"
    )


def test_migration_without_declarations_produces_minimal_pool():
    # Very old save with resource_state but no resource_declarations
    # (e.g., a save from before story 16-1 completed). Migration should
    # still produce a usable pool with unbounded defaults; the next
    # init_resource_pools() call will populate metadata.
    import json as _json

    payload = _json.loads("""{
        "genre_slug": "test",
        "world_slug": "test",
        "resource_state": { "mana": 7.0 }
    }""")

    snap = GameSnapshot.model_validate(payload)
    assert len(snap.resources) == 1
    mana = snap.resources["mana"]
    assert abs(mana.current - 7.0) < 1e-9
    assert mana.label == "", "no declaration → empty label for upsert to fill"
    assert mana.name == "mana"


def test_migration_raises_on_malformed_legacy_declaration():
    """CLAUDE.md No Silent Fallbacks: a non-dict entry in
    ``resource_declarations`` must raise with context, not be silently
    filtered out (which previously synthesized unbounded defaults for
    the matching ``resource_state`` entry and lost all metadata)."""
    import json as _json

    import pytest
    from pydantic import ValidationError

    payload = _json.loads("""{
        "genre_slug": "test",
        "world_slug": "test",
        "resource_state": { "luck": 2.0 },
        "resource_declarations": [
            "not a dict — malformed legacy save",
            { "name": "luck", "label": "Luck", "min": 0.0, "max": 6.0,
              "starting": 3.0, "voluntary": true, "decay_per_turn": 0.0 }
        ]
    }""")

    with pytest.raises((ValueError, ValidationError)) as excinfo:
        GameSnapshot.model_validate(payload)
    assert "malformed legacy resource_declaration" in str(excinfo.value)


def test_migration_raises_on_non_numeric_legacy_current():
    """CLAUDE.md No Silent Fallbacks: a non-numeric value in
    ``resource_state`` must raise with the pool name in the error
    message, not silently crash with a bare ``TypeError``."""
    import json as _json

    import pytest
    from pydantic import ValidationError

    payload = _json.loads("""{
        "genre_slug": "test",
        "world_slug": "test",
        "resource_state": { "luck": "not a number" }
    }""")

    with pytest.raises((ValueError, ValidationError)) as excinfo:
        GameSnapshot.model_validate(payload)
    assert "luck" in str(excinfo.value)
    assert "numeric" in str(excinfo.value)


def test_migration_then_init_populates_metadata_without_resetting_current():
    # End-to-end migration + upsert test: load an old save with minimal
    # pool data, then run init_resource_pools with the genre pack
    # declarations. Current should be preserved; metadata should be
    # populated from the pack.
    import json as _json

    payload = _json.loads("""{
        "genre_slug": "test",
        "world_slug": "test",
        "resource_state": { "luck": 1.5 }
    }""")
    snap = GameSnapshot.model_validate(payload)

    # Simulate session load calling init_resource_pools with genre pack decls.
    decl = ResourceDeclaration(
        name="luck",
        label="Luck",
        min=0.0,
        max=6.0,
        starting=3.0,
        voluntary=True,
        decay_per_turn=0.0,
        thresholds=[],
    )
    snap.init_resource_pools([decl])

    luck = snap.resources["luck"]
    assert abs(luck.current - 1.5) < 1e-9, (
        "saved current (1.5) must survive the init_resource_pools upsert"
    )
    assert luck.label == "Luck", "label populated by upsert"
    assert abs(luck.max - 6.0) < 1e-9, "max populated by upsert"
    assert luck.voluntary, "voluntary populated by upsert"
