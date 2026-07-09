"""165-3 REWORK (ADR-096 v2, Track C2) — direct unit coverage for
``resolve_weapon_range_band_from_beat_and_actor``.

Reviewer HIGH [TEST]: this helper feeds the reach gate the weapon's range band, and
it shipped with ZERO unit tests — every branch (empty inventory, ``pack`` None,
catalog miss, first-band-None-then-real, empty catalog) was unexercised. A bug here
silently reclassifies a ranged weapon as melee (or vice-versa), which the reach
gate then adjudicates against the wrong reach. These are characterization tests of
the current (correct) behavior — they lock each branch so a future regression fails
loudly instead of resolving to the wrong band.

Contract (from the current impl + docstring): returns the FIRST inventory item that
has a catalog ``range_band``; ``None`` (melee) when the actor has no inventory, no
pack is available, the catalog is absent/empty, or no held item carries a band.
``resolve_inventory`` is imported lazily inside the helper, so it is stubbed at its
source module ``sidequest.game.inventory_resolve``.
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.ruleset.combat_rules import (
    resolve_weapon_range_band_from_beat_and_actor,
)

# The helper never reads ``beat`` (see the MEDIUM Delivery Finding re: dead param).
# A bare sentinel documents that and keeps the tests decoupled from BeatDef.
_BEAT = object()


def _actor_with_items(items: list[dict] | None):
    """An actor_core stand-in whose ``.inventory.items`` is ``items``."""
    if items is None:
        return SimpleNamespace(inventory=None)
    return SimpleNamespace(inventory=SimpleNamespace(items=items))


class _CatItem:
    def __init__(self, item_id: str, range_band: str | None):
        self.id = item_id
        self.range_band = range_band


def _stub_catalog(monkeypatch, catalog):
    """Stub the lazily-imported ``resolve_inventory`` to return a config carrying
    ``catalog`` as its ``item_catalog`` (or None to simulate no config)."""
    inv_config = None if catalog is None else SimpleNamespace(item_catalog=catalog)
    monkeypatch.setattr(
        "sidequest.game.inventory_resolve.resolve_inventory",
        lambda pack, world_slug=None: inv_config,
    )


def test_empty_inventory_resolves_melee_none():
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT, actor_core=_actor_with_items([]), pack=object(), world_slug="w"
    )
    assert band is None


def test_no_actor_core_resolves_melee_none():
    """A natural/unarmed strike carries no actor inventory at all."""
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT, actor_core=None, pack=object(), world_slug="w"
    )
    assert band is None


def test_pack_none_resolves_melee_none():
    """No pack → no catalog to resolve against → melee (None), even with items."""
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT,
        actor_core=_actor_with_items([{"id": "laser_pistol"}]),
        pack=None,
        world_slug="w",
    )
    assert band is None


def test_absent_catalog_resolves_melee_none(monkeypatch):
    """resolve_inventory yields a config with no item_catalog → None."""
    _stub_catalog(monkeypatch, None)
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT,
        actor_core=_actor_with_items([{"id": "laser_pistol"}]),
        pack=object(),
        world_slug="w",
    )
    assert band is None


def test_catalog_miss_resolves_melee_none(monkeypatch):
    """Held item id is not in the catalog → no band found → melee (None). Guards
    against a stray truthy default masking an unknown item."""
    _stub_catalog(monkeypatch, [_CatItem("known_blade", "10/100")])
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT,
        actor_core=_actor_with_items([{"id": "unknown_relic"}]),
        pack=object(),
        world_slug="w",
    )
    assert band is None


def test_skips_bandless_item_and_returns_first_banded(monkeypatch):
    """First held item (a melee blade) has no band; the second (a pistol) does —
    the helper must skip the bandless item and return the pistol's band, not stop
    at the first weapon and false-resolve melee."""
    _stub_catalog(
        monkeypatch,
        [_CatItem("iron_blade", None), _CatItem("laser_pistol", "100/300")],
    )
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT,
        actor_core=_actor_with_items([{"id": "iron_blade"}, {"id": "laser_pistol"}]),
        pack=object(),
        world_slug="w",
    )
    assert band == "100/300"


def test_ignores_items_without_id(monkeypatch):
    """An inventory row missing an ``id`` is skipped, not crashed on."""
    _stub_catalog(monkeypatch, [_CatItem("laser_pistol", "10/100")])
    band = resolve_weapon_range_band_from_beat_and_actor(
        beat=_BEAT,
        actor_core=_actor_with_items([{"qty": 1}, {"id": "laser_pistol"}]),
        pack=object(),
        world_slug="w",
    )
    assert band == "10/100"
