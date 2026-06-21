"""Tests — Seam 2 sub-part A: generated walk-on origin-stamp (story 157-3).

Design: ``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``
(§ "Seam 2 — NPCs", first bullet) + implementation plan Task 3.

The pre-seeded namegen pool (``pregen`` calls ``manual.add_npc(data, [])``) is
runtime filler, not authored content, so the load validator (157-7) never touches
it. Instead, when an unplaced generated NPC is *activated* in a zoned world, its
faction is stamped to the current region's ``controlled_by`` — alongside the
existing ``activated_location``. "A walk-on born in Lilliput is a Lilliputian and
cannot later resurface in Houyhnhnm-land" (no over-suppression; the narrator still
gets walk-ons).

These drive the REAL ``MonsterManual.mark_active`` method directly (no mock) and
assert on the mutated ``ManualNpc.factions`` — the same fuzzy-match entry that
gets ``state``/``activated_location``. RED today: ``mark_active`` has no
``faction`` keyword (``TypeError``).
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.monster_manual import EntryState, MonsterManual
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.server.dispatch import monster_manual_inject

LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"

WORLD = "gulliver"


def _manual() -> MonsterManual:
    return MonsterManual(genre="wry_whimsy", world=WORLD)


def _zoned_pack(regions: dict[str, str | None]) -> SimpleNamespace:
    """Pack stand-in exposing ``worlds[WORLD].cartography`` (the ``cartography_for``
    accessor). ``regions`` maps region id → ``controlled_by`` faction (or None)."""
    carto = CartographyConfig(
        regions={
            rid: Region(name="R", summary="s", description="d", controlled_by=cb)
            for rid, cb in regions.items()
        }
    )
    return SimpleNamespace(worlds={WORLD: SimpleNamespace(cartography=carto)})


def _snapshot(*, region: str | None) -> GameSnapshot:
    """One seated PC ("Gulliver") in ``region`` (None → unresolvable)."""
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug=WORLD,
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
        player_seats={"seat-1": "Gulliver"},
        pc_regions={"Gulliver": region} if region is not None else {},
    )


def _add_walkon(manual: MonsterManual, name: str, *, factions: list[str] | None = None) -> None:
    """Insert a generated (namegen-shaped, untagged) walk-on, optionally
    pre-stamped to simulate a walk-on already born in a region."""
    manual.add_npc({"name": name, "role": "fishwife", "culture": "Lilliputian"}, [])
    if factions is not None:
        npc = manual.find_npc_by_exact_name(name)
        assert npc is not None
        npc.factions = factions


def test_mark_active_origin_stamps_walkon_with_region_faction() -> None:
    """Activating an untagged walk-on in a zoned region stamps its faction to the
    region's ``controlled_by`` — on the SAME entry that goes ACTIVE."""
    manual = _manual()
    _add_walkon(manual, "A Fishwife")

    manual.mark_active("A Fishwife", "the_lilliput_shore", faction=LILLIPUT)

    npc = manual.find_npc_by_exact_name("A Fishwife")
    assert npc is not None
    assert npc.factions == [LILLIPUT], (
        "walk-on was not origin-stamped with the region faction on activation"
    )
    # The stamp must land on the same entry that the existing activation mutates.
    assert npc.state == EntryState.ACTIVE
    assert npc.activated_location == "the_lilliput_shore"


def test_mark_active_does_not_overwrite_existing_faction() -> None:
    """Origin-stamp only fills an EMPTY ``factions`` — a walk-on already born in a
    zone keeps its origin even if later re-activated elsewhere. Otherwise a
    Houyhnhnm-born walk-on wandering onto the Lilliput shore would be silently
    re-homed, defeating the whole point of stamping its origin."""
    manual = _manual()
    _add_walkon(manual, "Old Sailor", factions=[HOUYHNHNM])

    manual.mark_active("Old Sailor", "the_lilliput_shore", faction=LILLIPUT)

    npc = manual.find_npc_by_exact_name("Old Sailor")
    assert npc is not None
    assert npc.factions == [HOUYHNHNM], (
        "origin-stamp overwrote an existing faction — born-Houyhnhnm was re-homed "
        "to Lilliput on a later activation"
    )


def test_mark_active_with_faction_none_leaves_factions_empty() -> None:
    """The unzoned / no-faction path: ``faction=None`` (an unzoned world, where
    ``controlled_by`` resolves to nothing) must NOT stamp — ``factions`` stays
    empty and the legacy activation behavior is untouched. Pins that the new
    keyword is optional and back-compatible."""
    manual = _manual()
    _add_walkon(manual, "A Beggar")

    manual.mark_active("A Beggar", "the_dome", faction=None)

    npc = manual.find_npc_by_exact_name("A Beggar")
    assert npc is not None
    assert npc.factions == [], "no faction provided but factions were stamped anyway"
    assert npc.state == EntryState.ACTIVE
    assert npc.activated_location == "the_dome"


def test_mark_active_stamps_only_the_matched_walkon() -> None:
    """With two distinct walk-ons, the stamp lands ONLY on the activated one — the
    faction write rides the same single-entry match as ``state``, never a
    broadcast across the pool."""
    manual = _manual()
    _add_walkon(manual, "A Fishwife")
    _add_walkon(manual, "A Blacksmith")

    manual.mark_active("A Fishwife", "the_lilliput_shore", faction=LILLIPUT)

    fishwife = manual.find_npc_by_exact_name("A Fishwife")
    blacksmith = manual.find_npc_by_exact_name("A Blacksmith")
    assert fishwife is not None and blacksmith is not None
    assert fishwife.factions == [LILLIPUT]
    assert blacksmith.factions == [], "stamp leaked onto a non-activated walk-on"
    assert blacksmith.state == EntryState.AVAILABLE


# ---------------------------------------------------------------------------
# WIRING — the origin-stamp fires through the PRODUCTION activation path
# (mark_active_from_narration), not just the mark_active unit. Reviewer R1:
# without this, the faction= param has no production caller and sub-part A is
# inert in a real game.
# ---------------------------------------------------------------------------


def test_mark_active_from_narration_origin_stamps_walkon_in_zoned_world() -> None:
    """The production post-narration activation scan stamps a generated walk-on
    with the acting PC's region ``controlled_by`` in a zoned world. Drives the
    REAL ``mark_active_from_narration`` (the call site in
    ``websocket_session_handler``) with a zoned snapshot + pack — proving the
    faction is resolved and threaded through to ``mark_active``, not left None."""
    manual = _manual()
    _add_walkon(manual, "A Fishwife")
    pack = _zoned_pack({"the_lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM})
    snap = _snapshot(region="the_lilliput_shore")

    activated = monster_manual_inject.mark_active_from_narration(
        manual,
        "A Fishwife haggles loudly over a basket of sprats.",
        "The Shore",
        snapshot=snap,
        pack=pack,
        perspective="Gulliver",
    )

    assert "A Fishwife" in activated
    npc = manual.find_npc_by_exact_name("A Fishwife")
    assert npc is not None
    assert npc.state == EntryState.ACTIVE
    assert npc.factions == [LILLIPUT], (
        "walk-on activated via the production narration scan was NOT origin-stamped "
        "with its region faction — the faction never reached mark_active (R1)"
    )


def test_mark_active_from_narration_no_stamp_in_unzoned_world() -> None:
    """In an unzoned world (no ``controlled_by`` on any region) the production
    activation path stamps nothing — protects the 11 single-zone worlds."""
    manual = _manual()
    _add_walkon(manual, "A Fishwife")
    pack = _zoned_pack({"the_dome": None, "the_wastes": None})
    snap = _snapshot(region="the_dome")

    monster_manual_inject.mark_active_from_narration(
        manual,
        "A Fishwife shuffles past.",
        "The Dome",
        snapshot=snap,
        pack=pack,
        perspective="Gulliver",
    )

    npc = manual.find_npc_by_exact_name("A Fishwife")
    assert npc is not None
    assert npc.state == EntryState.ACTIVE
    assert npc.factions == [], "unzoned world stamped a faction anyway"


def test_mark_active_from_narration_backward_compatible_without_snapshot() -> None:
    """The legacy 3-arg call (no snapshot/pack/perspective) still works and
    stamps nothing — so existing non-zoned call paths are unaffected."""
    manual = _manual()
    _add_walkon(manual, "A Fishwife")

    activated = monster_manual_inject.mark_active_from_narration(
        manual, "A Fishwife waves.", "Somewhere"
    )

    assert "A Fishwife" in activated
    npc = manual.find_npc_by_exact_name("A Fishwife")
    assert npc is not None and npc.state == EntryState.ACTIVE
    assert npc.factions == []
