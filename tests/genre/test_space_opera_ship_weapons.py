"""Story 114-15 — space_opera ship-weapon de-duplication, end-to-end against the
real pack (the WIRING test).

Before: ``multifocal_laser`` (bespoke, 1d4/AP20) was triplicated byte-identical
across the three space_opera worlds' ``inventory.yaml`` ``item_catalog`` because
ADR-145 D3 forbids a bespoke item at the genre tier. After 114-15 it lives ONCE in
the genre-tier ``ship_weapons`` collection and the dogfight resolves it from there
for every world — including a world that ships its own ``inventory.yaml`` (the
merge path) and no copy of the weapon.

Loads the production space_opera pack, so it is the integration guard that the
mechanism is wired to real content. Skips when sidequest-content is not checked out
alongside sidequest-server (matches tests/genre/test_dogfight_content_loading.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.dogfight_shot import build_dogfight_weapon_lookup
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

SHIP_WEAPON_ID = "multifocal_laser"
SPACE_OPERA_WORLDS = ("aureate_span", "coyote_star", "perseus_cloud")


def _has_real_content() -> bool:
    return (CONTENT_ROOT / "space_opera").is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


@pytest.fixture(scope="module")
def space_opera_pack() -> GenrePack:
    # AC4: load_genre_pack succeeds (the D3 validator passes — no bespoke in the
    # genre item_catalog; the bespoke ship weapon lives in ship_weapons).
    return load_genre_pack(CONTENT_ROOT / "space_opera")


def _dogfight_cdef(pack: GenrePack):
    assert pack.rules is not None, "space_opera has no rules.yaml"
    matches = [c for c in pack.rules.confrontations if c.confrontation_type == "dogfight"]
    assert len(matches) == 1, f"expected exactly one dogfight cdef, got {len(matches)}"
    return matches[0]


class TestGenreShipWeaponHome:
    def test_multifocal_laser_lives_once_in_genre_ship_weapons(
        self, space_opera_pack: GenrePack
    ) -> None:
        # AC1: defined exactly once, at the genre tier, in ship_weapons.
        assert space_opera_pack.inventory is not None
        ship = [w for w in space_opera_pack.inventory.ship_weapons if w.id == SHIP_WEAPON_ID]
        assert len(ship) == 1, "multifocal_laser must appear exactly once in genre ship_weapons"
        weapon = ship[0]
        assert weapon.damage is not None
        assert weapon.damage.armor_piercing == 20, "AP 20 is load-bearing for the dogfight"
        assert weapon.provenance is not None and weapon.provenance.mode == "bespoke"

    def test_multifocal_laser_not_in_genre_item_catalog(self, space_opera_pack: GenrePack) -> None:
        # AC1: the ship weapon is NOT on the personal item_catalog surface.
        assert space_opera_pack.inventory is not None
        ids = {i.id for i in space_opera_pack.inventory.item_catalog}
        assert SHIP_WEAPON_ID not in ids


class TestWorldsAreDeDuplicated:
    @pytest.mark.parametrize("world", SPACE_OPERA_WORLDS)
    def test_world_item_catalog_has_no_ship_weapon_copy(
        self, space_opera_pack: GenrePack, world: str
    ) -> None:
        # AC1: zero copies remain in the worlds' resolved item_catalogs.
        resolved = resolve_inventory(space_opera_pack, world)
        assert resolved is not None, f"{world} resolved to no inventory"
        ids = {i.id for i in resolved.item_catalog}
        assert SHIP_WEAPON_ID not in ids, (
            f"{world} still carries a duplicated {SHIP_WEAPON_ID} in item_catalog"
        )

    def test_worlds_keep_their_distinct_gear(self, space_opera_pack: GenrePack) -> None:
        # AC1 (only the laser was removed, not the world files): each world keeps its
        # two world-distinct personal items.
        expected = {
            "aureate_span": {"mirrorsilk_mantle", "house_chit"},
            "coyote_star": {"jump_key", "claim_beacon"},
            "perseus_cloud": {"ion_compass", "driftrunner_charm"},
        }
        for world, want in expected.items():
            resolved = resolve_inventory(space_opera_pack, world)
            assert resolved is not None
            ids = {i.id for i in resolved.item_catalog}
            assert want <= ids, f"{world} lost its distinct gear; have {ids}"


class TestDogfightResolvesShipWeaponPerWorld:
    @pytest.mark.parametrize("world", SPACE_OPERA_WORLDS)
    def test_dogfight_weapon_resolves_with_ap_from_genre(
        self, space_opera_pack: GenrePack, world: str
    ) -> None:
        # AC2 + AC3 (the de-dup proof): every world — each of which ships its OWN
        # inventory.yaml (the merge path) and NO copy of the ship weapon — still
        # resolves the dogfight weapon with armor_piercing == 20 from the single
        # genre-tier source.
        resolved = resolve_inventory(space_opera_pack, world)
        lookup = build_dogfight_weapon_lookup(resolved)
        item = lookup(SHIP_WEAPON_ID)
        assert item is not None, f"{world} could not resolve the dogfight ship weapon"
        assert item.damage is not None
        assert item.damage.armor_piercing == 20

    def test_dogfight_cdef_weapon_ids_resolve(self, space_opera_pack: GenrePack) -> None:
        # AC2 + guards a content rename: the dogfight cdef's player/opponent weapon
        # ids must point at the ship weapon AND resolve through the production lookup.
        cdef = _dogfight_cdef(space_opera_pack)
        assert cdef.player_weapon == SHIP_WEAPON_ID
        assert cdef.opponent_weapon == SHIP_WEAPON_ID

        resolved = resolve_inventory(space_opera_pack, "aureate_span")
        lookup = build_dogfight_weapon_lookup(resolved)
        for weapon_id in (cdef.player_weapon, cdef.opponent_weapon):
            item = lookup(weapon_id)
            assert item is not None and item.damage is not None
            assert item.damage.armor_piercing == 20
