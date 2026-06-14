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
    """A stub pack with no ``worlds`` attribute (a pack that failed to load →
    ``None``, or a legacy stub) is a clean no-op, not a crash."""
    manual = MonsterManual(genre="g", world="w")
    assert _seed_authored_npcs(object(), "w", manual) == 0
    assert manual.npcs == []


def test_seed_authored_npcs_warns_on_world_not_found(caplog) -> None:  # type: ignore[no-untyped-def]
    """H3 (No Silent Fallbacks): a world key that isn't in ``pack.worlds`` is a
    config/wiring error — WARN loudly and return 0, never silently swallow it.

    The original bug class: ``_seed_authored_npcs`` returned 0 with no signal on
    a world-key mismatch, so the authored cast vanished and nobody could tell
    whether the world had no roster or the key was wrong.
    """
    pack = _Pack(_world_with_authored(AuthoredNpc(id="x", name="X")))  # only "oz" exists
    manual = MonsterManual(genre="wry_whimsy", world="kansas")

    import logging

    with caplog.at_level(logging.WARNING):
        added = _seed_authored_npcs(pack, "kansas", manual)

    assert added == 0
    assert manual.npcs == []
    assert any("world_not_found" in r.message for r in caplog.records)


def test_seed_authored_npcs_logs_unconditionally(caplog) -> None:  # type: ignore[no-untyped-def]
    """H3: the seeding outcome is logged on EVERY successful read — including a
    world that exists but authors an empty roster (count=0) — so the GM/dev can
    distinguish "world has no authored cast" from "the read never happened"."""
    pack = _Pack(_world_with_authored())  # world exists, roster empty
    manual = MonsterManual(genre="wry_whimsy", world="oz")

    import logging

    with caplog.at_level(logging.INFO):
        added = _seed_authored_npcs(pack, "oz", manual)

    assert added == 0
    assert any("authored_npcs_seeded" in r.message for r in caplog.records)


def test_seed_authored_npcs_upserts_stale_tags() -> None:
    """M4: a re-seed must refresh placement tags on an already-present NPC.

    ``add_npc`` dedups by name and returns early, so without an upsert a stale
    on-disk Manual keeps its old (or empty) ``location_tags`` forever — exactly
    the wry_whimsy/oz recurrence on an existing save. Re-seeding with new tags
    updates the existing entry in place and the change counts toward the return
    (so the caller knows to persist).
    """
    manual = MonsterManual(genre="wry_whimsy", world="oz")
    # First seed: Scarecrow present but with NO placement (stale-cache shape).
    first = _Pack(_world_with_authored(AuthoredNpc(id="scarecrow", name="Scarecrow")))
    assert _seed_authored_npcs(first, "oz", manual) == 1
    assert manual.npcs[0].location_tags == []

    # Re-seed with the corrected placement.
    second = _Pack(
        _world_with_authored(
            AuthoredNpc(id="scarecrow", name="Scarecrow", location_tags=["yellow brick road"])
        )
    )
    changed = _seed_authored_npcs(second, "oz", manual)

    assert len(manual.npcs) == 1  # no duplicate
    assert manual.npcs[0].location_tags == ["yellow brick road"]  # tags refreshed
    assert changed == 1  # the upsert is reported so the caller saves


def test_seed_authored_npc_inserts_despite_substring_walkon() -> None:
    """REJECT-B1: an authored NPC whose name is a SUBSTRING of an existing
    generated walk-on must still be inserted as its own entry — and must NOT
    overwrite the walk-on's placement tags.

    The fuzzy ``find_npc_by_name`` collapses "Lion" into a pre-seeded "Cowardly
    Lion": the authored NPC would either vanish (silent drop — the very bug this
    story fixes, via a rarer path) or write its tags onto the walk-on. Authored
    dedup must be EXACT.
    """
    manual = MonsterManual(genre="wry_whimsy", world="oz")
    # A generated walk-on seeded first (no placement).
    manual.add_npc({"name": "Cowardly Lion", "role": "beast", "culture": "wild"}, [])

    # Authored "Lion" with its own placement.
    pack = _Pack(
        _world_with_authored(AuthoredNpc(id="lion", name="Lion", location_tags=["forest"]))
    )
    added = _seed_authored_npcs(pack, "oz", manual)

    assert added == 1
    names = {n.name for n in manual.npcs}
    assert names == {"Cowardly Lion", "Lion"}  # authored NPC inserted, not swallowed
    # The walk-on's (empty) placement was NOT corrupted by the authored tags.
    walkon = next(n for n in manual.npcs if n.name == "Cowardly Lion")
    assert walkon.location_tags == []
    lion = next(n for n in manual.npcs if n.name == "Lion")
    assert lion.location_tags == ["forest"]


def test_seed_authored_npc_inserts_despite_substring_walkon_inverse() -> None:
    """REJECT-B1 (inverse direction): an authored NPC whose name CONTAINS an
    existing walk-on's name as a substring must also insert exactly, not collide.
    """
    manual = MonsterManual(genre="wry_whimsy", world="oz")
    manual.add_npc({"name": "Lion", "role": "beast", "culture": "wild"}, [])

    pack = _Pack(
        _world_with_authored(
            AuthoredNpc(id="cowardly_lion", name="Cowardly Lion", location_tags=["timber"])
        )
    )
    added = _seed_authored_npcs(pack, "oz", manual)

    assert added == 1
    assert {n.name for n in manual.npcs} == {"Lion", "Cowardly Lion"}


def test_seed_authored_npcs_reordered_tags_is_not_a_change() -> None:
    """REJECT-B2: the upsert dirty-check must be order-INSENSITIVE. A re-seed
    whose ``location_tags`` carry the same tags in a different order is NOT a
    change — it must not report a refresh (which would trigger a spurious save
    and an OTEL backfill span every load)."""
    manual = MonsterManual(genre="wry_whimsy", world="oz")
    first = _Pack(
        _world_with_authored(
            AuthoredNpc(
                id="scarecrow", name="Scarecrow", location_tags=["yellow brick road", "cornfield"]
            )
        )
    )
    assert _seed_authored_npcs(first, "oz", manual) == 1

    # Same tags, reversed order — semantically identical placement.
    second = _Pack(
        _world_with_authored(
            AuthoredNpc(
                id="scarecrow", name="Scarecrow", location_tags=["cornfield", "yellow brick road"]
            )
        )
    )
    changed = _seed_authored_npcs(second, "oz", manual)
    assert changed == 0  # no spurious refresh
    assert len(manual.npcs) == 1


def test_authored_npcs_flow_through_real_loader(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """H2: the REAL loader path, end to end — no ``SimpleNamespace`` stub.

    Copies a real fixture pack, writes a ``npcs.yaml`` carrying ``location_tags``,
    loads it through the production ``load_genre_pack``, and asserts the chain
    ``npcs.yaml → AuthoredNpc.model_validate → World.authored_npcs.location_tags
    → _seed_authored_npcs → selection`` is connected. The prior wiring test
    stubbed the loader with ``SimpleNamespace``, so nothing proved the YAML key
    actually binds through ``World``.
    """
    import shutil

    from sidequest.genre.loader import load_genre_pack
    from tests._helpers.fixture_packs import fixture_pack_path

    pack_dir = tmp_path / "wwn_test_pack"
    shutil.copytree(fixture_pack_path("wwn_test_pack"), pack_dir)
    world_dir = pack_dir / "worlds" / "test_world"
    (world_dir / "npcs.yaml").write_text(
        "npcs:\n"
        "  - id: scarecrow\n"
        "    name: Scarecrow\n"
        "    role: companion\n"
        "    location_tags:\n"
        "      - yellow brick road\n"
        "      - cornfield\n"
        "  - id: throne_guard\n"
        "    name: Throne Guard\n"
        "    role: sentry\n"
        "    location_tags:\n"
        "      - emerald city\n",
        encoding="utf-8",
    )

    pack = load_genre_pack(pack_dir)
    world = pack.worlds["test_world"]

    # The YAML key bound through the real model into World.authored_npcs.
    by_name = {n.name: n for n in world.authored_npcs}
    assert by_name["Scarecrow"].location_tags == ["yellow brick road", "cornfield"]

    # Drive the seam: seed from the loaded pack, then assert placement-aware
    # surfacing at a matching vs a non-matching location.
    manual = MonsterManual(genre="wwn_test_pack", world="test_world")
    added = _seed_authored_npcs(pack, "test_world", manual)
    assert added == 2

    on_road = manual.format_nearby_npcs("The Yellow Brick Road — Morning")
    assert "Scarecrow" in on_road
    assert "Throne Guard" not in on_road

    in_city = manual.format_nearby_npcs("The Emerald City — Throne Room")
    assert "Throne Guard" in in_city
    assert "Scarecrow" not in in_city
