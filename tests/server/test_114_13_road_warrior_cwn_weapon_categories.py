"""Story 114-13 — road_warrior combat classes start with CWN-category weapons.

114-5 (#871) extracted the verbatim CWN equipment catalog, which categorises
weapons as ``melee_weapon`` / ``ranged_weapon`` (CWN SRD §3.0.2/§3.0.3). But the
live road_warrior world (``the_circuit``) still ships *bespoke* personal weapons
(pistol, tire_iron, sawed_off_shotgun, crossbow, chain) under the legacy
``category: weapon`` string, and the production guards that decide "this is a
personal weapon" only recognise that one bespoke string.

This follows the epic-114 / ADR-145 doctrine — *bind the ruleset, don't author
it*: when a genre binds CWN, its gear should BE CWN gear, carrying CWN
categories. The SM ruled the "decide + optionally" fork conservatively (2026-06-15):
combat classes SHOULD start with CWN-category weapons, and the personal-weapon
guard widens to accept ``melee_weapon`` / ``ranged_weapon`` alongside the legacy
``weapon``.

RED until 114-13 lands:
  * ``test_the_circuit_combat_classes_start_with_a_cwn_category_weapon`` — strike
    classes start with ``category: weapon`` bespoke gear, not CWN categories.
  * ``test_the_circuit_has_no_legacy_bespoke_personal_weapon_category`` — the
    resolved catalog still carries damaging, non-mounted ``category: weapon``
    items.
  * ``test_narrator_minted_ranged_weapon_keeps_its_cwn_category`` /
    ``..._melee_weapon...`` — the narrator-mint guard (the one production
    ``category == "weapon"`` allowlist) demotes a CWN-category weapon to ``misc``.

The strike-damage path is deliberately NOT tested here for a category change: it
already resolves by the item's ``damage`` field / name, never by category (see
``resolve_damage_spec_from_beat_and_actor``), so it accepts CWN-category weapons
unchanged. ``test_road_warrior_combat_dispatch.py`` locks that end-to-end and
stays green through this story.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The live road_warrior world — personal weapons live at the world tier
# (ADR-145 D3 / 114-14), so the player plays against the RESOLVED catalog.
_WORLD = "the_circuit"

# The CWN SRD weapon categories 114-5 emits. After 114-13 a road_warrior
# personal weapon must carry one of these, not the bespoke legacy "weapon".
_CWN_WEAPON_CATEGORIES = frozenset({"melee_weapon", "ranged_weapon"})

# The widened personal-weapon guard: the CWN categories PLUS the legacy bespoke
# string (kept so mounted/rig gear and any un-migrated genre can still resolve).
_PERSONAL_WEAPON_CATEGORIES = frozenset({"weapon"}) | _CWN_WEAPON_CATEGORIES

# Strike beats that make a class need a personal weapon (mirrors the existing
# road_warrior combat-dispatch guard).
_STRIKE_BEATS = frozenset({"shoot", "pistol_whip"})


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_road_warrior() -> GenrePack:
    try:
        return load_genre_pack(find_pack_path("road_warrior"))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _is_mounted(item) -> bool:
    """A vehicular / rig-mounted weapon, not a personal strike weapon.

    Mounted gear (mounted_gun, ram_plow, flame_rig...) carries ``mounted`` /
    ``rig`` tags. Plan 2 (vehicular) territory — explicitly out of scope for the
    personal-weapon decision.
    """
    tags = {str(t).lower() for t in (getattr(item, "tags", None) or [])}
    return "mounted" in tags or "rig" in tags


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_the_circuit_combat_classes_start_with_a_cwn_category_weapon() -> None:
    """Every class with a strike beat must START with a non-mounted, damaging
    weapon whose category is a CWN category (``melee_weapon`` / ``ranged_weapon``).

    RED today: strike classes start with bespoke ``category: weapon`` gear
    (pistol / sawed_off_shotgun / tire_iron). Path-agnostic for the fix — Dev may
    re-categorise the bespoke world weapons in place OR repoint starting_equipment
    at the verbatim genre CWN catalog; either way the started weapon's category
    must be a CWN category.
    """
    pack = _load_road_warrior()
    resolved = resolve_inventory(pack, _WORLD)
    assert resolved is not None
    catalog_by_id = {item.id: item for item in resolved.item_catalog}
    starting = resolved.starting_equipment

    classes_needing_a_weapon = [
        cls.display_name
        for cls in pack.classes
        if _STRIKE_BEATS & set(getattr(cls, "encounter_beat_choices", []) or [])
    ]
    assert classes_needing_a_weapon, (
        f"expected at least one road_warrior class to have a strike beat ({sorted(_STRIKE_BEATS)})"
    )

    not_cwn_armed: dict[str, list[str]] = {}
    for class_name in classes_needing_a_weapon:
        loadout = starting.get(class_name, [])
        cwn_weapons = [
            item_id
            for item_id in loadout
            if (item := catalog_by_id.get(item_id)) is not None
            and not _is_mounted(item)
            and item.damage is not None
            and item.category in _CWN_WEAPON_CATEGORIES
        ]
        if not cwn_weapons:
            # Report what the class actually starts with, to make the RED legible.
            carried = [
                f"{iid}({getattr(catalog_by_id.get(iid), 'category', '?')})"
                for iid in loadout
                if catalog_by_id.get(iid) is not None
            ]
            not_cwn_armed[class_name] = carried

    assert not not_cwn_armed, (
        "every road_warrior combat class must start with a CWN-category personal "
        f"weapon ({sorted(_CWN_WEAPON_CATEGORIES)}); these do not: {not_cwn_armed}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_the_circuit_has_no_legacy_bespoke_personal_weapon_category() -> None:
    """No personal (non-mounted) weapon in the resolved catalog may keep the
    legacy bespoke ``category: weapon``.

    A damaging, non-mounted weapon is a personal strike weapon; under the CWN
    binding it must carry a CWN category. Mounted / rig weapons (vehicular) are
    out of scope and may retain whatever category they declare.

    RED today: pistol, tire_iron, sawed_off_shotgun, crossbow, chain are damaging,
    non-mounted, and ``category: weapon``.
    """
    pack = _load_road_warrior()
    resolved = resolve_inventory(pack, _WORLD)
    assert resolved is not None

    legacy_personal_weapons = [
        item.id
        for item in resolved.item_catalog
        if item.category == "weapon" and item.damage is not None and not _is_mounted(item)
    ]
    assert not legacy_personal_weapons, (
        "personal weapons must use CWN categories under the cwn binding, not the "
        f"bespoke 'weapon' string; still legacy: {legacy_personal_weapons}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_the_circuit_still_declares_personal_weapons() -> None:
    """Sanity anchor so the two guards above can't pass vacuously by the world
    simply having zero weapons: the resolved catalog must declare at least one
    non-mounted, damaging personal weapon."""
    pack = _load_road_warrior()
    resolved = resolve_inventory(pack, _WORLD)
    assert resolved is not None
    personal = [
        item for item in resolved.item_catalog if item.damage is not None and not _is_mounted(item)
    ]
    assert personal, "road_warrior the_circuit must declare personal weapons"


def _single_seat_snapshot():
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot, TurnManager

    char = Character(
        core=CreatureCore(
            name="Vex",
            description="Wheelman of the Circuit.",
            personality="reckless",
            inventory=Inventory(),
            statuses=[],
        ),
        char_class="Wheelman",
        race="Human",
        backstory="Raised on the ring road.",
    )
    snap = GameSnapshot(
        genre_slug="road_warrior",
        world_slug=_WORLD,
        location="The Pits",
        turn_manager=TurnManager(),
    )
    snap.characters.append(char)
    snap.turn_manager.record_interaction()
    return snap


def _inv_category(char, item_name: str) -> str | None:
    for it in char.core.inventory.items:
        if str(it.get("name", "")) == item_name:
            return str(it.get("category", ""))
    return None


@pytest.mark.parametrize("cwn_category", ["ranged_weapon", "melee_weapon"])
def test_narrator_minted_cwn_weapon_keeps_its_cwn_category(cwn_category: str) -> None:
    """The narrator-mint guard (``narration_apply`` items_gained allowlist) is the
    one production ``category == "weapon"`` gate. A narrator-granted CWN-category
    weapon must keep its category, not be demoted to ``misc``.

    RED today: the allowlist is ``{weapon, armor, tool, consumable, quest,
    treasure, misc}`` so ``ranged_weapon`` / ``melee_weapon`` coerce to ``misc``
    — the player's freshly looted CWN gun stops being a weapon the moment the
    narrator hands it over.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    snap = _single_seat_snapshot()
    result = NarrationTurnResult(
        narration="The scav presses the salvaged carbine into Vex's hands.",
        items_gained=[
            {
                "name": "Salvaged Carbine",
                "description": "A road-worn CWN firearm.",
                "category": cwn_category,
            }
        ],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="p1",
        room=room_for(snap),
        acting_character_name="Vex",
    )

    got = _inv_category(snap.characters[0], "Salvaged Carbine")
    assert got == cwn_category, (
        "the narrator-mint guard must accept CWN weapon categories; a "
        f"{cwn_category!r} mint was stored as {got!r} (demoted to misc?)"
    )
