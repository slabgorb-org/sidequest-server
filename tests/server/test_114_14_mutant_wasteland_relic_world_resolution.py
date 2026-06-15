"""Story 114-14 (guards) — mutant_wasteland relics live at the world tier and still
resolve + wire at chargen for BOTH worlds.

After the 6 pre-war relics move off the genre baseline (ADR-145 D3), each world must
still surface them: ``flickering_reach`` (which gains a NEW ``inventory.yaml``) and
``seaboard_of_saints`` (which already ships one). These guard:
  * the power_glove fallback-to-weapon regression class (relics must resolve), and
  * the chargen item_hint -> catalog-category upgrade (datapad must show as a tool,
    not the category=weapon stub — the 2026-04-11 bug), and
  * the world-replaces-genre starting_equipment trap (a new world inventory.yaml
    must carry the kits or chargen ships empty).

Driven against the REAL pack through ``resolve_inventory`` and the real
``apply_starting_loadout`` seam (Verify Wiring, Not Just Existence).
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.chargen_loadout import apply_starting_loadout
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_PACK = "mutant_wasteland"
_WORLDS = ["flickering_reach", "seaboard_of_saints"]
_RELICS = [
    "power_glove",
    "datapad",
    "growth_wand",
    "purifier",
    "mystery_compass",
    "ancient_artifact",
]
# char_creation.yaml item_hint artifacts and the catalog category each must resolve
# to. power_glove is the only weapon (it hits); the rest are tools (sensor/agritech/
# water/navigation) — the "datapad shows as a weapon" stub-fallback bug guards here.
_ARTIFACT_CATEGORY = {
    "growth_wand": "tool",
    "datapad": "tool",
    "purifier": "tool",
    "mystery_compass": "tool",
    "power_glove": "weapon",
}


def _load():
    try:
        return load_genre_pack(find_pack_path(_PACK))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def _builder_stub(item_id: str) -> dict:
    """The shape CharacterBuilder emits for a scene ``item_hint`` BEFORE the catalog
    upgrade: a hardcoded ``category: weapon`` stub (chargen_loadout.py:86)."""
    return {
        "id": item_id,
        "name": item_id.replace("_", " ").title(),
        "description": f"Starting equipment: {item_id}",
        "category": "weapon",
        "value": 0,
        "weight": 1.0,
        "rarity": "common",
        "narrative_weight": 0.2,
        "tags": [],
        "equipped": False,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


def _make_character(items: list[dict], *, char_class: str = "Scavenger"):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name="Vex",
        description="A wastelander who found a thing.",
        personality="curious",
        inventory=Inventory(items=list(items)),
    )
    return Character(core=core, char_class=char_class, race="Human", backstory="Born in the wastes.")


@pytest.mark.parametrize("world", _WORLDS)
def test_all_relics_resolve_for_world(world: str) -> None:
    """Every relic must appear in the resolved (genre baseline ∪ world) catalog for
    each world — a regression guard against the relics vanishing when they leave the
    genre tier (flickering_reach must gain its own inventory.yaml)."""
    pack = _load()
    resolved = resolve_inventory(pack, world)
    assert resolved is not None, f"{world} must resolve an inventory"
    ids = {item.id for item in resolved.item_catalog}
    missing = [r for r in _RELICS if r not in ids]
    assert not missing, (
        f"{world} resolved catalog must carry all relics (ADR-145 D3 non-droppable "
        f"merge / new world inventory); missing: {missing}"
    )


@pytest.mark.parametrize("world", _WORLDS)
def test_chargen_artifacts_upgrade_to_catalog_category(world: str) -> None:
    """Wiring: a builder item_hint stub (category=weapon) for each chargen artifact
    must upgrade to its catalog category via the real ``apply_starting_loadout``
    against the resolved per-world catalog — datapad/etc -> tool, power_glove ->
    weapon. Guards the 'datapad shows as a weapon' stub fallback after the relics
    move to the world tier."""
    pack = _load()
    resolved = resolve_inventory(pack, world)
    character = _make_character([_builder_stub(a) for a in _ARTIFACT_CATEGORY])

    apply_starting_loadout(character, resolved, genre=_PACK, world=world)

    by_id = {it["id"]: it for it in character.core.inventory.items}
    for artifact, expected in _ARTIFACT_CATEGORY.items():
        assert artifact in by_id, f"{world}: chargen artifact {artifact!r} missing from inventory"
        assert by_id[artifact]["category"] == expected, (
            f"{world}: {artifact!r} must upgrade to catalog category {expected!r}, got "
            f"{by_id[artifact]['category']!r} (category=weapon stub fallback = the bug)"
        )


@pytest.mark.parametrize("world", _WORLDS)
def test_world_starting_equipment_resolves(world: str) -> None:
    """The world-replaces-genre trap: a world that ships its OWN inventory.yaml takes
    starting_equipment/gold/currency wholesale. flickering_reach gains a NEW
    inventory.yaml for the relics, so it MUST also carry the genre kits or every
    class ships empty. Assert kits are non-empty and every kit id resolves in the
    merged catalog (no chargen.starting_equipment_missing / minimal stub)."""
    pack = _load()
    resolved = resolve_inventory(pack, world)
    assert resolved is not None and resolved.starting_equipment, (
        f"{world}: resolved inventory must declare starting_equipment kits — a new "
        f"world inventory.yaml must copy the genre kits (world-replaces-genre)"
    )
    catalog_ids = {item.id for item in resolved.item_catalog}
    for klass, ids in resolved.starting_equipment.items():
        for item_id in ids:
            assert item_id in catalog_ids, (
                f"{world}: class {klass!r} kit id {item_id!r} does not resolve in the "
                f"merged catalog (would hit the category=equipment minimal stub)"
            )
