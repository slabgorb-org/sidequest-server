"""Story 114-14 (guards) — neon_dystopia + road_warrior declared-bespoke items move
to their single world and still resolve, with chargen kits intact.

Both packs are single-world (``franchise_nations`` / ``the_circuit``) with NO world
inventory.yaml today, so they currently resolve to the pure genre baseline. Moving
the declared-bespoke items down creates a NEW world inventory.yaml → triggers the
world-replaces-genre starting_equipment/gold/currency trap, which these guards catch.

road_warrior: only the 5 DECLARED-bespoke move here; its 25 unprovenanced items stay
at the genre tier (epic 120, verbatim-only sweep) and are not this story's concern.

Driven against the REAL packs through ``resolve_inventory``.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import PackNotFound, find_pack_path

# (pack, single-world slug, the declared-bespoke ids that must resolve for that world)
_CASES = [
    (
        "neon_dystopia",
        "franchise_nations",
        ["smart_pistol", "katana", "mantis_blades", "cyberdeck", "optical_camo", "data_chip"],
    ),
    (
        "road_warrior",
        "the_circuit",
        ["tire_iron", "chain", "sawed_off_shotgun", "crossbow", "pistol"],
    ),
]


def _load(slug: str):
    try:
        return load_genre_pack(find_pack_path(slug))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


@pytest.mark.parametrize("slug,world,moved_ids", _CASES)
def test_moved_bespoke_ids_resolve_for_world(slug: str, world: str, moved_ids: list[str]) -> None:
    """The declared-bespoke items must still resolve for the pack's single world
    after they move from the genre tier to that world (non-droppable merge) — guards
    against them vanishing at chargen."""
    pack = _load(slug)
    resolved = resolve_inventory(pack, world)
    assert resolved is not None, f"{slug}/{world} must resolve an inventory"
    ids = {item.id for item in resolved.item_catalog}
    missing = [m for m in moved_ids if m not in ids]
    assert not missing, (
        f"{slug}/{world}: moved-to-world bespoke ids must still resolve "
        f"(create worlds/{world}/inventory.yaml carrying them); missing: {missing}"
    )


@pytest.mark.parametrize("slug,world,moved_ids", _CASES)
def test_world_starting_equipment_resolves(slug: str, world: str, moved_ids: list[str]) -> None:
    """The world-replaces-genre trap: creating worlds/<world>/inventory.yaml for the
    moved items takes starting_equipment/gold/currency from the world WHOLESALE, so
    the new world inventory MUST copy the genre kits or every class ships an empty
    loadout. Assert kits are non-empty and every kit id resolves in the merged
    catalog (covers both the still-genre ids and the moved-to-world ids)."""
    pack = _load(slug)
    resolved = resolve_inventory(pack, world)
    assert resolved is not None and resolved.starting_equipment, (
        f"{slug}/{world}: resolved inventory must declare starting_equipment kits — "
        f"copy the genre starting_equipment/gold/currency into the new world inventory"
    )
    catalog_ids = {item.id for item in resolved.item_catalog}
    for klass, ids in resolved.starting_equipment.items():
        for item_id in ids:
            assert item_id in catalog_ids, (
                f"{slug}/{world}: class {klass!r} kit id {item_id!r} does not resolve "
                f"in the merged catalog (category=equipment minimal-stub fallback)"
            )
