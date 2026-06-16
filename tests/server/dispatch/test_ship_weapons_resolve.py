"""Story 114-15 — the genre-tier ``ship_weapons`` collection: model field,
resolver carry-through, and D3-validator exemption.

The space_opera dogfight ship weapon ``multifocal_laser`` moves OUT of the
personal ``item_catalog`` (where ADR-145 D3 forbids a genre-tier ``bespoke``
item, forcing the byte-identical triplication across three worlds) into a
dedicated genre-tier ``ship_weapons`` collection on ``InventoryConfig``. It is
native-subsystem config — not personal SRD gear — so a ``bespoke`` ship weapon
is legitimate at the genre tier and EXEMPT from
``_validate_genre_baseline_no_bespoke`` (which scans ``item_catalog`` only).

These synthetic tests isolate the mechanism (no content load):
  - ``InventoryConfig`` carries a ``ship_weapons`` list (AC1 substrate).
  - ``resolve_inventory`` threads the genre-tier ``ship_weapons`` through the
    WORLD-MERGE path unchanged — the load-bearing AC3 guard: a world that ships
    its OWN ``inventory.yaml`` hits the merge path, which ``model_copy``s the
    result from the WORLD config; if ``ship_weapons`` isn't threaded from the
    genre baseline it is silently dropped and the dogfight weapon can't resolve
    for any world that owns an inventory.
  - the D3 validator stays blind to ``ship_weapons`` (AC4): a bespoke ship
    weapon is fine; a bespoke ``item_catalog`` item still fails loud.
"""

from __future__ import annotations

from typing import cast

import pytest

from sidequest.genre.loader import PackError, _validate_genre_baseline_no_bespoke
from sidequest.genre.models.inventory import (
    CatalogItem,
    CurrencyConfig,
    DamageSpec,
    InventoryConfig,
    ItemProvenance,
)
from sidequest.genre.models.pack import GenrePack, World
from sidequest.server.dispatch.inventory_resolve import resolve_inventory


def _ship_weapon(
    item_id: str = "multifocal_laser", *, ap: int = 20, dice: str = "1d4"
) -> CatalogItem:
    """A bespoke ship weapon (mirrors the multifocal_laser shape)."""
    return CatalogItem(
        id=item_id,
        name=item_id.replace("_", " ").title(),
        description="A strike-fighter ship weapon.",
        category="weapon",
        tags=["ranged", "ship-weapon"],
        damage=DamageSpec(dice=dice, armor_piercing=ap),
        provenance=ItemProvenance(mode="bespoke"),
    )


def _personal_item(item_id: str) -> CatalogItem:
    return CatalogItem(id=item_id, name=item_id, description="d", category="tool")


def _inv(
    *,
    catalog: list[str] | None = None,
    ship_weapons: list[CatalogItem] | None = None,
    currency: str | None = None,
) -> InventoryConfig:
    return InventoryConfig(
        currency=CurrencyConfig(name=currency) if currency else None,
        item_catalog=[_personal_item(i) for i in (catalog or [])],
        ship_weapons=list(ship_weapons or []),
    )


def _make_pack(
    *,
    genre_inventory: InventoryConfig | None,
    worlds: dict[str, InventoryConfig | None],
) -> GenrePack:
    world_objs: dict[str, World] = {
        slug: cast(World, World.model_construct(inventory=inv)) for slug, inv in worlds.items()
    }
    return cast(GenrePack, GenrePack.model_construct(inventory=genre_inventory, worlds=world_objs))


class TestInventoryConfigShipWeapons:
    def test_inventory_config_carries_ship_weapons(self) -> None:
        inv = _inv(ship_weapons=[_ship_weapon()])
        assert [w.id for w in inv.ship_weapons] == ["multifocal_laser"]
        assert inv.ship_weapons[0].damage is not None
        assert inv.ship_weapons[0].damage.armor_piercing == 20

    def test_ship_weapons_defaults_to_empty_list(self) -> None:
        # A pack with no ship weapons is the common case; the field must default to
        # an empty list (not None) so call sites can iterate unconditionally.
        assert _inv().ship_weapons == []


class TestResolveInventoryCarriesShipWeapons:
    def test_genre_ship_weapons_survive_world_merge_path(self) -> None:
        # AC3 (load-bearing). The world ships its OWN inventory.yaml (two distinct
        # personal items) so resolve_inventory takes the MERGE path, which builds
        # the result by model_copy-ing the WORLD config. ``ship_weapons`` is
        # genre-tier-only — it must be threaded through from the genre baseline or
        # it is dropped and the dogfight weapon can't resolve for a world that owns
        # an inventory. This is exactly the space_opera worlds' situation.
        pack = _make_pack(
            genre_inventory=_inv(ship_weapons=[_ship_weapon()]),
            worlds={"aureate_span": _inv(catalog=["mirrorsilk_mantle", "house_chit"])},
        )
        resolved = resolve_inventory(pack, "aureate_span")
        assert resolved is not None
        assert [w.id for w in resolved.ship_weapons] == ["multifocal_laser"]
        assert resolved.ship_weapons[0].damage is not None
        assert resolved.ship_weapons[0].damage.armor_piercing == 20
        # Sanity: the world's own personal gear still merged in (the merge ran).
        assert {i.id for i in resolved.item_catalog} == {"mirrorsilk_mantle", "house_chit"}

    def test_genre_ship_weapons_present_on_pure_genre_path(self) -> None:
        # World ships NO inventory → pure-genre path returns pack.inventory, which
        # already carries ship_weapons. Guards the no-world / world-empty branch.
        pack = _make_pack(
            genre_inventory=_inv(ship_weapons=[_ship_weapon()]),
            worlds={"w": None},
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        assert [w.id for w in resolved.ship_weapons] == ["multifocal_laser"]

    def test_world_with_empty_ship_weapons_does_not_shadow_genre(self) -> None:
        # ship_weapons is genre-tier-only in v1; a world that ships none must not
        # blank out the genre collection on the merge path.
        pack = _make_pack(
            genre_inventory=_inv(ship_weapons=[_ship_weapon()]),
            worlds={"coyote_star": _inv(catalog=["jump_key"], ship_weapons=[])},
        )
        resolved = resolve_inventory(pack, "coyote_star")
        assert resolved is not None
        assert [w.id for w in resolved.ship_weapons] == ["multifocal_laser"]


class TestD3ValidatorIgnoresShipWeapons:
    def test_bespoke_ship_weapon_is_exempt_from_d3(self) -> None:
        # AC4. The genre item_catalog is clean (no bespoke), but a bespoke ship
        # weapon lives in ship_weapons. The D3 validator scans item_catalog only,
        # so this must NOT raise.
        inv = _inv(ship_weapons=[_ship_weapon()])  # item_catalog empty/clean
        # Precondition the no-raise is meaningful: the ship weapon really IS bespoke
        # (so passing proves the validator saw a bespoke item in ship_weapons and
        # deliberately ignored it — not that there was nothing bespoke to find).
        assert inv.ship_weapons[0].provenance is not None
        assert inv.ship_weapons[0].provenance.mode == "bespoke"
        assert inv.item_catalog == []
        _validate_genre_baseline_no_bespoke("swn", inv)  # must not raise

    def test_bespoke_item_catalog_item_still_fails_loud(self) -> None:
        # Control: a bespoke item in item_catalog (NOT ship_weapons) still trips the
        # D3 validator — proves the exemption is scoped to ship_weapons, not a
        # blanket relaxation that would let bespoke personal gear back into a WN
        # genre baseline.
        bespoke = _ship_weapon("contraband")  # category=weapon, provenance=bespoke
        inv = InventoryConfig(item_catalog=[bespoke], ship_weapons=[])
        with pytest.raises(PackError, match="contraband"):
            _validate_genre_baseline_no_bespoke("swn", inv)
