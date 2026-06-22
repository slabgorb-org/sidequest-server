"""Light/torch economy (sq-playtest 2026-06-22) — content→seam contract.

The ``environment_clock`` relight handler is fully wired: lighting a torch sets
``resources.light`` to max, clears the −2 darkness penalty, and consumes one
torch. But it only fires if the torch the runtime hands the player carries the
dedicated ``light_source`` tag — ``_find_torch`` keys on that tag exactly (a
bare ``light`` tag marks light-WEIGHT weapons and must NOT qualify, or a dagger
gets eaten as fuel).

The genre-baseline ``wwn_torch`` shipped with NO tags, so the production loadout
produced a tagless torch, ``_find_torch`` returned None, every relight failed
``no_torch``, and the narrator improvised "the dark retreats" while the engine
left ``light`` at 0 and the darkness penalty in place — the GM-panel lie-detector's
headline catch this session.

The server unit suite (``tests/agents/subsystems/test_environment_clock.py``)
passed throughout because every fixture hand-built a torch WITH the tag. This is
the missing content→seam contract: load the REAL pack, take the REAL torch through
the REAL loadout item-dict builder, and assert the REAL ``_find_torch`` finds it.

Skips gracefully when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import PackNotFound, find_pack_path

TORCH_ID = "wwn_torch"
LIGHT_SOURCE_TAG = "light_source"


def _load_caverns_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        return None
    return load_genre_pack(path)


def _catalog_item(pack, item_id: str):
    assert pack.inventory is not None, "caverns_and_claudes must declare a genre-tier inventory"
    return next((c for c in pack.inventory.item_catalog if c.id == item_id), None)


def test_torch_declares_light_source_tag():
    """Content contract: the genre-baseline torch carries the ``light_source``
    tag the relight handler requires, so a tagless torch can never silently
    starve the light economy again."""
    pack = _load_caverns_pack()
    if pack is None:
        pytest.skip("caverns_and_claudes pack not on disk")

    torch = _catalog_item(pack, TORCH_ID)
    assert torch is not None, f"{TORCH_ID} must exist in the caverns inventory catalog"
    assert LIGHT_SOURCE_TAG in torch.tags, (
        f"{TORCH_ID} must carry the '{LIGHT_SOURCE_TAG}' tag so "
        "environment_clock._find_torch can find it when the player lights a torch"
    )


def test_loadout_torch_is_found_by_relight_matcher():
    """Seam contract: the torch the runtime loadout actually hands a Delver is
    recognized by the relight handler's ``_find_torch`` — bridging content
    (catalog tags) → loadout (``_item_dict_from_catalog``) → relight matcher.

    This is the assertion the unit suite could not make: it exercises the
    production item-dict builder against the production content, not a synthetic
    fixture that pre-supplies the tag.
    """
    from sidequest.agents.subsystems.environment_clock import _find_torch
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.server.dispatch.chargen_loadout import _item_dict_from_catalog

    pack = _load_caverns_pack()
    if pack is None:
        pytest.skip("caverns_and_claudes pack not on disk")

    torch = _catalog_item(pack, TORCH_ID)
    assert torch is not None

    item_dict = _item_dict_from_catalog(torch)
    assert item_dict["quantity"] >= 1, "loadout torch must ship at least one charge"

    core = CreatureCore(
        name="Delver", description="d", personality="p", inventory=Inventory()
    )
    core.inventory.items.append(item_dict)

    found = _find_torch(core)
    assert found is not None, (
        "the torch produced by the production loadout builder must be recognized "
        "as a light source by environment_clock._find_torch"
    )
    assert found["id"] == TORCH_ID
