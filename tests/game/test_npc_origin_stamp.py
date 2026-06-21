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

from sidequest.game.monster_manual import EntryState, MonsterManual

LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"


def _manual() -> MonsterManual:
    return MonsterManual(genre="wry_whimsy", world="gulliver")


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
