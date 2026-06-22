"""Story 153-28 — beneath_sunden ships an authored surface-camp roster.

sq-playtest finding (2026-06-21/22, beneath_sunden): the world shipped ZERO
authored NPCs (`pregen.authored_npcs_seeded total_authored=0`), so the camp's
two constant fixtures — Brecca Half-Hand, who narrates ALL of chargen
(``caverns_and_claudes/char_creation.yaml``), and the winch-keeper, named in
every opening — had no anchored identity. The narrator free-invented a
replacement each turn (a hireling-tier "Ork", a "salvager" cast as winch-keeper)
and swapped the NPC the player had addressed.

Driven against the REAL pack through ``load_genre_pack`` so green proves the
production content carries the roster, not a fixture. Guards three things that,
together, are the fix:

  * the roster is non-empty (the exact ``total_authored=0`` regression);
  * Brecca Half-Hand — the canonical chargen intake — is anchored by name;
  * every camp NPC is location-tagged to ``ropefoot`` so the Monster Manual
    surfaces them only at the surface camp and never leaks them down the rope
    into the procedural deep (placement-aware selection, ADR-059).
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

SLUG = "caverns_and_claudes"
WORLD = "beneath_sunden"


def _world():
    try:
        pack = load_genre_pack(find_pack_path(SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))
    world = pack.worlds.get(WORLD)
    assert world is not None, f"{SLUG} must ship the {WORLD} world"
    return world


def test_beneath_sunden_ships_authored_camp_roster() -> None:
    """The exact regression: the world shipped zero authored NPCs."""
    world = _world()
    assert world.authored_npcs, (
        f"{WORLD} must ship an authored camp roster — it shipped zero "
        "(pregen.authored_npcs_seeded total_authored=0), forcing the narrator "
        "to invent the camp fixtures inconsistently each turn"
    )


def test_brecca_half_hand_is_anchored() -> None:
    """Brecca runs ALL of chargen (genre char_creation.yaml); she must exist in
    the runtime roster so a player who addresses her gets *her*, not a swap."""
    world = _world()
    names = {n.name for n in world.authored_npcs}
    assert "Brecca Half-Hand" in names, (
        "Brecca Half-Hand (the chargen intake fixture) must be anchored in "
        f"{WORLD}'s npcs.yaml; found {sorted(names)!r}"
    )


def test_camp_npcs_are_placed_at_ropefoot() -> None:
    """Camp cast belongs to the surface camp, never the deep. Every authored NPC
    must carry the ``ropefoot`` location tag so placement-aware selection keeps
    them at the camp and out of the procedural megadungeon graph."""
    world = _world()
    unplaced = [
        n.id
        for n in world.authored_npcs
        if "ropefoot" not in {t.casefold() for t in n.location_tags}
    ]
    assert not unplaced, (
        f"every {WORLD} camp NPC must be tagged for 'ropefoot' so it does not "
        f"leak into the deep; untagged: {unplaced!r}"
    )


def test_camp_npc_ids_are_unique() -> None:
    """Id collisions would make the loader's uniqueness validator fire; assert
    the shipped roster is clean so this stays a regression guard, not a surprise."""
    world = _world()
    ids = [n.id for n in world.authored_npcs]
    assert len(ids) == len(set(ids)), f"duplicate authored NPC ids in {WORLD}: {ids!r}"
