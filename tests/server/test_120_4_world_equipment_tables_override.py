"""Story 120-4 (RED) — world-tier equipment_tables override.

ADR-140 layering for chargen kits: a world may ship `worlds/<w>/equipment_tables.yaml`
to extend the genre's chargen kits with world-flavor gear. The loader loads it into a new
`World.equipment_tables` field, and a `resolve_equipment_tables(pack, world_slug)` resolver
(mirroring `resolve_inventory` / `resolve_classes`) merges world-over-genre:

  * class_tables: per-slot APPEND within each kit (genre items first, then the world's);
    a slot present only in the world is added; a kit present only in the world is added.
  * guaranteed_grants: APPEND by kit_id.
  * rolls_per_slot / tables: world overrides per key (genre carries through otherwise).

ADDITIVE: a world that ships no equipment_tables.yaml resolves to the genre tier unchanged
(`World.equipment_tables is None`), so this lands green without touching existing packs.

Fixtures (added to wwn_test_pack — it has NO equipment_generation scene, so the new genre
file is inert for existing chargen tests):
  * genre `equipment_tables.yaml`: warrior_kit {weapon:[genre_blade], utility:[genre_rope]},
    expert_kit {weapon:[genre_blade]}, guaranteed_grants warrior_kit:[genre_tonic].
  * world `worlds/test_world/equipment_tables.yaml`: warrior_kit {utility:[world_lockpick],
    light:[world_torch]}, expert_kit {utility:[world_lockpick]}, grants warrior_kit:[world_charm].

RED today:
  * model field + loader: `World.equipment_tables` doesn't exist / isn't loaded.
  * resolver: `resolve_equipment_tables` doesn't exist.
GREEN-guard (additive proof — must hold after the feature lands):
  * a world without the file resolves to the genre tier unchanged.
"""

from __future__ import annotations

import random

from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    ClassDef,
    EquipmentTables,
    MechanicalEffects,
)
from sidequest.genre.models.pack import World
from sidequest.genre.models.rules import RulesConfig
from tests._helpers.fixture_packs import load_fixture_pack

WWN = "wwn_test_pack"
SWN = "swn_test_pack"  # a fixture pack whose world ships NO equipment_tables.yaml
TEST_WORLD = "test_world"


# ---------------------------------------------------------------------------
# 1. Model field
# ---------------------------------------------------------------------------
def test_world_model_declares_equipment_tables_field() -> None:
    """RED: the World model must DECLARE an `equipment_tables` field (not rely on
    extra='allow' to stash it). Mirrors World.inventory / World.classes."""
    assert "equipment_tables" in World.model_fields, (
        "World must declare an `equipment_tables` field (EquipmentTables | None), "
        "parallel to World.inventory — extra='allow' stashing is not a declared field"
    )


# ---------------------------------------------------------------------------
# 2-3. Loader
# ---------------------------------------------------------------------------
def test_loader_loads_world_equipment_tables() -> None:
    """RED: the loader must read `worlds/<w>/equipment_tables.yaml` into
    World.equipment_tables (mirrors the world inventory.yaml load)."""
    pack = load_fixture_pack(WWN)
    world = pack.worlds[TEST_WORLD]
    et = world.equipment_tables
    assert isinstance(et, EquipmentTables), (
        f"{WWN}/{TEST_WORLD} ships worlds/{TEST_WORLD}/equipment_tables.yaml; the loader "
        f"must load it into World.equipment_tables, got {et!r}"
    )
    assert et.class_tables.get("warrior_kit", {}).get("utility") == ["world_lockpick"]
    assert et.class_tables.get("warrior_kit", {}).get("light") == ["world_torch"]
    grants = et.guaranteed_grants.get("warrior_kit", [])
    assert [g.item for g in grants] == ["world_charm"]


def test_loader_world_without_equipment_tables_is_none() -> None:
    """Additive no-op proof: a world that ships no equipment_tables.yaml resolves to
    World.equipment_tables == None (no behavior change for unmigrated worlds)."""
    pack = load_fixture_pack(SWN)
    assert pack.worlds, f"{SWN} must ship at least one world"
    for slug, world in pack.worlds.items():
        assert world.equipment_tables is None, (
            f"{SWN}/{slug} ships no equipment_tables.yaml; World.equipment_tables must be None, "
            f"got {world.equipment_tables!r}"
        )


# ---------------------------------------------------------------------------
# 4-6. Resolver (resolve_equipment_tables) — the merge contract
# ---------------------------------------------------------------------------
def test_resolve_equipment_tables_appends_world_slots() -> None:
    """RED: resolve_equipment_tables(pack, world) merges class_tables per-slot APPEND
    (genre first, then world); world-only slots/kits are added; genre-only slots stay."""
    from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables

    pack = load_fixture_pack(WWN)
    merged = resolve_equipment_tables(pack, TEST_WORLD)
    assert merged is not None
    warrior = merged.class_tables["warrior_kit"]
    assert warrior["weapon"] == ["genre_blade"], "genre-only slot must carry through unchanged"
    assert warrior["utility"] == ["genre_rope", "world_lockpick"], "per-slot APPEND, genre first"
    assert warrior["light"] == ["world_torch"], "world-only slot must be added"
    # expert_kit has no genre `utility` slot — the world adds one.
    assert merged.class_tables["expert_kit"]["utility"] == ["world_lockpick"]
    assert merged.class_tables["expert_kit"]["weapon"] == ["genre_blade"]
    # rolls_per_slot carries through from the genre tier (world set none here).
    assert merged.rolls_per_slot.get("weapon") == 1


def test_resolve_equipment_tables_appends_guaranteed_grants_by_kit_id() -> None:
    """RED: guaranteed_grants merge by kit_id — APPEND, not replace."""
    from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables

    pack = load_fixture_pack(WWN)
    merged = resolve_equipment_tables(pack, TEST_WORLD)
    assert merged is not None
    items = [g.item for g in merged.guaranteed_grants.get("warrior_kit", [])]
    assert items == ["genre_tonic", "world_charm"], (
        f"warrior_kit grants must append world over genre by kit_id, got {items}"
    )


def test_resolve_equipment_tables_no_world_override_is_genre() -> None:
    """GREEN-guard (additive): with no world override the resolver returns the genre tables
    unchanged. Checked two ways: world_slug=None, and a world that ships no override file."""
    from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables

    pack = load_fixture_pack(WWN)
    genre_only = resolve_equipment_tables(pack, None)
    assert genre_only is not None
    warrior = genre_only.class_tables["warrior_kit"]
    assert warrior["utility"] == ["genre_rope"], "no world → genre utility unchanged"
    assert "light" not in warrior, "no world → no world-only `light` slot"
    assert [g.item for g in genre_only.guaranteed_grants.get("warrior_kit", [])] == ["genre_tonic"]

    # A real pack/world that ships no override file must also be a no-op.
    swn = load_fixture_pack(SWN)
    swn_world = next(iter(swn.worlds))
    swn_resolved = resolve_equipment_tables(swn, swn_world)
    swn_genre = resolve_equipment_tables(swn, None)
    assert swn_resolved == swn_genre, "a world with no equipment_tables.yaml is a no-op merge"


# ---------------------------------------------------------------------------
# 7. OTEL — the merge decision emits a span (OTEL Observability Principle)
# ---------------------------------------------------------------------------
def test_resolve_equipment_tables_emits_otel_on_world_merge(monkeypatch) -> None:
    """RED: when a world override merges, the resolver emits a watcher event so the GM panel
    can prove the world tier engaged (mirrors resolve_inventory's `state_transition`/`merged`).
    The pure-genre path must NOT emit a `merged` event."""
    from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables

    captured: list[tuple[str, dict]] = []

    def _capture(event_type: str, fields: dict, *args, **kwargs) -> None:
        captured.append((event_type, fields))

    monkeypatch.setattr("sidequest.telemetry.watcher_hub.publish_event", _capture)

    pack = load_fixture_pack(WWN)
    resolve_equipment_tables(pack, TEST_WORLD)
    merged_events = [
        f
        for (_t, f) in captured
        if f.get("field") == "resolved_equipment_tables" and f.get("op") == "merged"
    ]
    assert merged_events, (
        "the world-merge path must emit a state_transition with "
        "field='resolved_equipment_tables', op='merged' (OTEL Observability Principle); "
        f"captured: {captured}"
    )
    assert any(f.get("world_slug") == TEST_WORLD for f in merged_events)

    captured.clear()
    resolve_equipment_tables(pack, None)
    assert not [
        f
        for (_t, f) in captured
        if f.get("field") == "resolved_equipment_tables" and f.get("op") == "merged"
    ], "the pure-genre path must not emit a 'merged' event"


# ---------------------------------------------------------------------------
# 8. Wiring — the merged tables flow through the builder into the rolled kit
# ---------------------------------------------------------------------------
def _class_kit_scenes() -> list[CharCreationScene]:
    return [
        CharCreationScene(
            id="class_choice",
            title="Choose",
            narration="What are you?",
            choices=[
                CharCreationChoice(
                    label="Warrior",
                    description="A fighter.",
                    mechanical_effects=MechanicalEffects(class_hint="Warrior"),
                ),
            ],
        ),
        CharCreationScene(
            id="the_kit",
            title="Gear",
            narration="Here is your kit.",
            mechanical_effects=MechanicalEffects(equipment_generation="class_kit"),
        ),
    ]


def test_world_equipment_flows_through_resolver_into_rolled_kit() -> None:
    """Wiring: feeding the builder the resolver's world-merged tables (the data path
    connect.py uses) yields a kit containing the world-tier gear. world_torch (sole `light`
    item) and world_charm (a guaranteed grant) are deterministic, so the world override
    provably reached the character — not just the genre tier."""
    from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables

    pack = load_fixture_pack(WWN)
    merged = resolve_equipment_tables(pack, TEST_WORLD)
    assert merged is not None

    rules = RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
    )
    classes = [
        ClassDef(
            id="warrior",
            display_name="Warrior",
            rpg_role="tank",
            jungian_default="hero",
            prime_requisite="STR",
            minimum_score=9,
            kit_table="warrior_kit",
        )
    ]
    builder = (
        CharacterBuilder(_class_kit_scenes(), rules, rng=random.Random(42))
        .with_equipment_tables(merged)
        .with_classes(classes)
    )
    builder.apply_choice(0)  # Warrior
    builder.apply_auto_advance()  # the_kit (class_kit roll)
    assert builder.is_confirmation()
    character = builder.build("KitTest")

    item_ids = [item["id"] for item in character.core.inventory.items]
    assert "world_torch" in item_ids, (
        f"world-tier `light` item must reach the kit via the merge, got {item_ids}"
    )
    assert "world_charm" in item_ids, (
        f"world-tier guaranteed grant must reach the kit via the merge, got {item_ids}"
    )


# Late import so the model/loader tests above fail on their own assertions rather than a
# collection-time ImportError. CharacterBuilder is import-safe today.
from sidequest.game.builder import CharacterBuilder  # noqa: E402
