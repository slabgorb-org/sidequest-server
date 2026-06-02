"""Narrator-gained items resolve against the authored item catalog.

Playtest 2026-06-02 (Silver Shoes GAP, crunch-scoped resolution): the
item-gain path always minted a bare ``narrator:{slug}`` dict, so a gained
weapon arrived with no ``damage`` and an id that ``damage_roll``'s
catalog-by-id lookup could never match — mechanically inert. These tests
pin the resolution: a gained item whose name/id matches an authored
``CatalogItem`` lands with the authored id + mechanical fields; a
non-match still mints bare (current behaviour preserved).

Includes the required wiring test: drives the real
``_apply_narration_result_to_snapshot`` with a pack catalog and asserts the
production path performs the resolution (not just the unit helper).
"""

from __future__ import annotations

import copy

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.item_catalog_resolution import resolve_gained_item_dict
from sidequest.genre.models.inventory import CatalogItem, InventoryConfig
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for


def _blaster() -> CatalogItem:
    return CatalogItem(
        id="blaster_rifle",
        name="Blaster Rifle",
        description="A standard-issue energy carbine.",
        category="weapon",
        value=50,
        weight=3.0,
        rarity="standard",
        tags=["ranged", "energy"],
        # pydantic coerces the dict into the DamageSpec the field declares.
        damage={"dice": "1d8", "bonus": 1},
    )


# ---------------------------------------------------------------------------
# Unit: resolve_gained_item_dict
# ---------------------------------------------------------------------------


def test_resolve_matches_by_name_and_carries_mechanics():
    out = resolve_gained_item_dict({"name": "Blaster Rifle"}, [_blaster()])
    assert out is not None
    # Authored id, NOT a ``narrator:`` mint — this is what unlocks the
    # damage_roll Priority-3 catalog-by-id lookup.
    assert out["id"] == "blaster_rifle"
    # damage is the full serialised DamageSpec; assert the load-bearing dice.
    assert out["damage"]["dice"] == "1d8"
    assert out["damage"]["bonus"] == 1
    assert out["tags"] == ["ranged", "energy"]
    assert out["rarity"] == "standard"
    assert out["category"] == "weapon"
    assert out["quantity"] == 1
    assert out["equipped"] is False


def test_resolve_matches_case_insensitively_and_by_narrator_slug_id():
    # Case-folded name match.
    assert resolve_gained_item_dict({"name": "blaster rifle"}, [_blaster()]) is not None
    # Slug derived from the name matches the authored id.
    assert resolve_gained_item_dict({"name": "Blaster  Rifle"}, [_blaster()]) is not None
    # An explicit narrator:-prefixed id is stripped and matched.
    by_id = resolve_gained_item_dict(
        {"name": "Blaster Rifle", "id": "narrator:blaster_rifle"}, [_blaster()]
    )
    assert by_id is not None and by_id["id"] == "blaster_rifle"


def test_resolve_preserves_requested_quantity():
    out = resolve_gained_item_dict({"name": "Blaster Rifle", "quantity": 3}, [_blaster()])
    assert out is not None and out["quantity"] == 3


def test_resolve_returns_none_on_no_match_or_empty_catalog():
    # No catalog entry by that name → bare-mint signalled.
    assert resolve_gained_item_dict({"name": "Mysterious Trinket"}, [_blaster()]) is None
    # Empty / absent catalog.
    assert resolve_gained_item_dict({"name": "Blaster Rifle"}, []) is None
    assert resolve_gained_item_dict({"name": "Blaster Rifle"}, None) is None
    # Conservative: a partial/looser name must NOT bind (no fuzzy matching).
    assert resolve_gained_item_dict({"name": "Blaster"}, [_blaster()]) is None


# ---------------------------------------------------------------------------
# Wiring: the production item-gain path performs catalog resolution
# ---------------------------------------------------------------------------


def test_apply_narration_resolves_gained_item_against_pack_catalog(
    snapshot_with_pack, character_named_sam
):
    snap, base_pack = snapshot_with_pack
    snap.characters.append(character_named_sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.inventory = InventoryConfig(item_catalog=[_blaster()])

    result = NarrationTurnResult(
        narration="You snatch the carbine off the rack.",
        items_gained=[{"name": "Blaster Rifle"}],
    )
    _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))

    items = snap.characters[0].core.inventory.items
    rifle = next((it for it in items if it.get("name") == "Blaster Rifle"), None)
    assert rifle is not None, "gained item was not added to inventory"
    # Production path resolved against the catalog: authored id + damage block.
    assert rifle["id"] == "blaster_rifle"
    assert rifle.get("damage", {}).get("dice") == "1d8"
    assert "ranged" in rifle["tags"]


def test_apply_narration_mints_bare_when_no_catalog_match(
    snapshot_with_pack, character_named_sam
):
    snap, base_pack = snapshot_with_pack
    snap.characters.append(character_named_sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.inventory = InventoryConfig(item_catalog=[_blaster()])

    result = NarrationTurnResult(
        narration="You pocket a curious bauble.",
        items_gained=[{"name": "Curious Bauble", "category": "treasure"}],
    )
    _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))

    items = snap.characters[0].core.inventory.items
    bauble = next((it for it in items if it.get("name") == "Curious Bauble"), None)
    assert bauble is not None
    # No catalog match → bare narrator mint (current behaviour preserved).
    assert bauble["id"] == "narrator:curious_bauble"
    assert "damage" not in bauble
