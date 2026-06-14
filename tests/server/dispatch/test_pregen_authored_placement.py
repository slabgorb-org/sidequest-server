"""Loader → Monster-Manual wiring for placement-aware authored NPCs.

wry_whimsy/oz bug (2026-06-14): the canonical companions (Scarecrow, Tin
Woodman, Cowardly Lion) are richly authored in ``worlds/oz/npcs.yaml`` but never
surfaced on the Yellow Brick Road — the Monster Manual offered generic generated
walk-ons instead, because authored placement was ignored.

This is the end-to-end wiring test: a world-shaped fixture whose authored NPC
carries ``location_tags`` flows through ``_seed_authored_npcs`` into the Manual
and is surfaced by ``format_nearby_npcs`` at the matching location — proving the
chain ``AuthoredNpc.location_tags → ManualNpc.location_tags → selection`` is
connected, not just the pure function in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.monster_manual import MonsterManual
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.server.dispatch.pregen import _seed_authored_npcs


def _world_with_authored(*npcs: AuthoredNpc) -> SimpleNamespace:
    """A world stand-in exposing the ``authored_npcs`` field the loader populates
    (``World.authored_npcs``, asserted real in test_authored_npc.py). Avoids
    constructing the heavyweight ``World`` aggregate (config/lore/cartography)
    when only the authored roster is under test."""
    return SimpleNamespace(authored_npcs=list(npcs))


class _Pack:
    """Stand-in pack exposing only ``worlds`` — the field ``_seed_authored_npcs`` reads."""

    def __init__(self, world: SimpleNamespace) -> None:
        self.worlds = {"oz": world}


def test_authored_location_tags_flow_through_to_selection() -> None:
    scarecrow = AuthoredNpc(
        id="scarecrow",
        name="Scarecrow",
        role="companion",
        location_tags=["yellow brick road", "cornfield"],
    )
    guard = AuthoredNpc(
        id="throne_guard",
        name="Throne Guard",
        role="sentry",
        location_tags=["emerald city"],
    )
    pack = _Pack(_world_with_authored(scarecrow, guard))
    manual = MonsterManual(genre="wry_whimsy", world="oz")

    added = _seed_authored_npcs(pack, "oz", manual)
    assert added == 2

    # The tags survived the loader→Manual hop.
    placed = {n.name: n.location_tags for n in manual.npcs}
    assert placed["Scarecrow"] == ["yellow brick road", "cornfield"]

    # And drive the selector: Scarecrow surfaces on the road, the Emerald City
    # guard does not.
    on_road = manual.format_nearby_npcs("The Yellow Brick Road — Morning")
    assert "Scarecrow" in on_road
    assert "Throne Guard" not in on_road

    # In the Emerald City the placement flips.
    in_city = manual.format_nearby_npcs("The Emerald City — Throne Room")
    assert "Throne Guard" in in_city
    assert "Scarecrow" not in in_city


def test_authored_npc_without_tags_is_unplaced() -> None:
    """An authored NPC with no ``location_tags`` is eligible everywhere (legacy
    walk-on behavior preserved)."""
    wanderer = AuthoredNpc(id="wanderer", name="Old Wanderer", role="hermit")
    pack = _Pack(_world_with_authored(wanderer))
    manual = MonsterManual(genre="wry_whimsy", world="oz")

    _seed_authored_npcs(pack, "oz", manual)
    assert "Old Wanderer" in manual.format_nearby_npcs("Anywhere At All")


def test_seed_authored_npcs_tolerates_pack_without_worlds() -> None:
    """A stub pack with no ``worlds`` attribute (legacy seed_manual callers) is a
    clean no-op, not a crash."""
    manual = MonsterManual(genre="g", world="w")
    assert _seed_authored_npcs(object(), "w", manual) == 0
    assert manual.npcs == []
