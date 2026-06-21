"""Tests for the faction/zone-scoped content eligibility core (story 157-2).

Pins the pure-logic contract of ``sidequest.game.zone_eligibility`` plus the
additive ``factions`` content tag, per the design spec
``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``
(ADR-059 amendment).

These tests are fixture/behavior-driven and import the spec'd public names
directly. The Seam-1 wiring (the ``inject()`` filter + ``zone_eligibility.filtered``
OTEL span) lives in ``tests/server/dispatch/test_zone_eligibility_seam.py``.

Contract summary (from the spec):

- ``world_is_zoned(cartography) -> bool`` — True iff ANY region declares
  ``controlled_by``. The 11 single-zone worlds → False → every predicate
  short-circuits to eligible (zero behavior change).
- ``active_factions(snapshot, pack, *, perspective=None) -> set[str]`` —
  split-party-safe resolver; NEVER raises on an unresolvable region, returns
  ``∅`` instead. Never puts ``None`` in the set.
- ``is_eligible(content_factions, active, *, zoned) -> bool`` — the one
  predicate. Runtime is PERMISSIVE; the load validator (story 157-7) owns
  strictness. The only exclusion is *tagged-but-wrong-zone*.
"""

from __future__ import annotations

import pytest

from sidequest.game.monster_manual import ManualEncounter, ManualNpc
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.zone_eligibility import active_factions, is_eligible, world_is_zoned
from sidequest.genre.models.bestiary import BestiaryEntry
from sidequest.genre.models.world import CartographyConfig, Region

# Two worlds' worth of faction slugs from the gulliver proof case.
LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"
BROBDINGNAG = "the_brobdingnag_crown"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _region(controlled_by: str | None) -> Region:
    return Region(
        name="R",
        summary="s",
        description="d",
        controlled_by=controlled_by,
    )


def _cartography(regions: dict[str, str | None]) -> CartographyConfig:
    """A CartographyConfig whose regions carry the given ``controlled_by`` map."""
    return CartographyConfig(regions={rid: _region(cb) for rid, cb in regions.items()})


class _Pack:
    """Minimal pack stand-in exposing only ``worlds`` — matches the established
    ``pregen._seed_authored_npcs`` accessor convention (``pack.worlds[slug]``).
    """

    def __init__(self, world_slug: str, cartography: CartographyConfig) -> None:
        from types import SimpleNamespace

        self.worlds = {world_slug: SimpleNamespace(cartography=cartography)}


def _snapshot(
    *,
    world_slug: str = "gulliver",
    player_seats: dict[str, str] | None = None,
    pc_regions: dict[str, str] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug=world_slug,
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
        player_seats=player_seats or {},
        pc_regions=pc_regions or {},
    )


# ---------------------------------------------------------------------------
# world_is_zoned
# ---------------------------------------------------------------------------


def test_world_is_zoned_true_when_any_region_has_controlled_by() -> None:
    carto = _cartography({"lilliput_shore": LILLIPUT, "open_sea": None})
    assert world_is_zoned(carto) is True


def test_world_is_zoned_false_when_no_region_has_controlled_by() -> None:
    """The 11 single-zone worlds declare no ``controlled_by`` → unzoned → every
    predicate short-circuits to eligible (zero behavior change)."""
    carto = _cartography({"the_dome": None, "the_wastes": None})
    assert world_is_zoned(carto) is False


def test_world_is_zoned_false_for_empty_cartography() -> None:
    assert world_is_zoned(CartographyConfig()) is False


def test_world_is_zoned_false_for_none_cartography() -> None:
    """``cartography_for`` returns None for a pre-bind/stub session; the production
    inject() path passes that straight into ``world_is_zoned`` → must be False."""
    assert world_is_zoned(None) is False


# ---------------------------------------------------------------------------
# is_eligible — the truth table (runtime is permissive)
# ---------------------------------------------------------------------------


def test_is_eligible_unzoned_world_always_true_even_for_tagged_mismatch() -> None:
    """Unzoned world → no scoping at all, even if content happens to be tagged."""
    assert is_eligible([HOUYHNHNM], {LILLIPUT}, zoned=False) is True


def test_is_eligible_unresolvable_region_does_not_suppress() -> None:
    """Empty active set (region unresolvable: pre-bind / split with no consensus)
    must NOT suppress — fail toward SHOWING content, never a silent empty scene."""
    assert is_eligible([HOUYHNHNM], set(), zoned=True) is True


def test_is_eligible_star_is_world_global() -> None:
    """The reserved ``"*"`` sentinel means 'all zones in this world'."""
    assert is_eligible(["*"], {LILLIPUT}, zoned=True) is True


def test_is_eligible_untagged_content_is_permissive() -> None:
    """Untagged content stays eligible at runtime — the load validator (157-7),
    not the runtime predicate, guarantees zoned worlds carry no untagged content.
    If the runtime excluded untagged content, every still-untagged creature would
    vanish the moment the engine ships, breaking the world mid-epic."""
    assert is_eligible([], {LILLIPUT}, zoned=True) is True


def test_is_eligible_tagged_match_is_eligible() -> None:
    assert is_eligible([LILLIPUT], {LILLIPUT}, zoned=True) is True


def test_is_eligible_tagged_mismatch_is_excluded() -> None:
    """THE bug fix: a Houyhnhnm-tagged Yahoo on the Lilliput shore is excluded."""
    assert is_eligible([HOUYHNHNM], {LILLIPUT}, zoned=True) is False


def test_is_eligible_intersection_with_split_party_union() -> None:
    """Split party → active is the UNION of seated PCs' zones; content eligible if
    it matches ANY seated PC's zone."""
    assert is_eligible([HOUYHNHNM], {LILLIPUT, HOUYHNHNM}, zoned=True) is True


def test_is_eligible_no_intersection_is_excluded() -> None:
    assert is_eligible([HOUYHNHNM, BROBDINGNAG], {LILLIPUT}, zoned=True) is False


# ---------------------------------------------------------------------------
# active_factions — split-party-safe resolver
# ---------------------------------------------------------------------------


def test_active_factions_perspective_resolves_that_pcs_zone() -> None:
    pack = _Pack("gulliver", _cartography({"lilliput_shore": LILLIPUT}))
    snap = _snapshot(
        player_seats={"seat-1": "Gulliver"},
        pc_regions={"Gulliver": "lilliput_shore"},
    )
    assert active_factions(snap, pack, perspective="Gulliver") == {LILLIPUT}


def test_active_factions_perspective_missing_region_is_empty() -> None:
    """A PC with no ``pc_regions`` entry resolves to ∅ (region_for → None), never
    raises — the predicate then treats ∅ as 'do not suppress'."""
    pack = _Pack("gulliver", _cartography({"lilliput_shore": LILLIPUT}))
    snap = _snapshot(player_seats={"seat-1": "Gulliver"}, pc_regions={})
    assert active_factions(snap, pack, perspective="Gulliver") == set()


def test_active_factions_consensus_region_when_no_perspective() -> None:
    pack = _Pack("gulliver", _cartography({"lilliput_shore": LILLIPUT}))
    snap = _snapshot(
        player_seats={"seat-1": "Gulliver"},
        pc_regions={"Gulliver": "lilliput_shore"},
    )
    assert active_factions(snap, pack) == {LILLIPUT}


def test_active_factions_split_party_is_union() -> None:
    """region_for(None) returns None on a split party — the resolver must still
    produce the UNION of every seated PC's zone, not collapse to ∅."""
    pack = _Pack(
        "gulliver",
        _cartography({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}),
    )
    snap = _snapshot(
        player_seats={"seat-1": "Gulliver", "seat-2": "Glumdalclitch"},
        pc_regions={"Gulliver": "lilliput_shore", "Glumdalclitch": "houyhnhnm_land"},
    )
    assert active_factions(snap, pack) == {LILLIPUT, HOUYHNHNM}


def test_active_factions_no_seated_pcs_is_empty() -> None:
    """Pre-bind / malformed turn: no seated PC resolves a region → ∅."""
    pack = _Pack("gulliver", _cartography({"lilliput_shore": LILLIPUT}))
    snap = _snapshot(player_seats={}, pc_regions={})
    assert active_factions(snap, pack) == set()


def test_active_factions_hub_region_controlled_by_none_contributes_nothing() -> None:
    """A region with ``controlled_by=None`` (an unowned hub) must NOT inject
    ``None`` into the active set — the set stays clean so ``is_eligible`` keeps
    its permissive 'no active faction → do not suppress' semantics."""
    pack = _Pack("gulliver", _cartography({"open_sea": None}))
    snap = _snapshot(
        player_seats={"seat-1": "Gulliver"},
        pc_regions={"Gulliver": "open_sea"},
    )
    active = active_factions(snap, pack)
    assert None not in active
    assert active == set()


# ---------------------------------------------------------------------------
# The `factions` content tag — additive, default-empty, isolated per instance
# ---------------------------------------------------------------------------


def _bestiary_entry(**overrides: object) -> BestiaryEntry:
    base: dict[str, object] = {
        "id": "yahoo_brute",
        "name": "Yahoo Brute",
        "level": 2,
        "hp": 9,
        "armor_class": 12,
        "attack_bonus": 2,
    }
    base.update(overrides)
    return BestiaryEntry(**base)  # type: ignore[arg-type]


def test_bestiary_entry_factions_defaults_empty() -> None:
    """Additive field: existing content (no ``factions:``) keeps parsing — default
    is an empty list, NOT None."""
    assert _bestiary_entry().factions == []


def test_bestiary_entry_factions_round_trips() -> None:
    assert _bestiary_entry(factions=[HOUYHNHNM]).factions == [HOUYHNHNM]


def test_bestiary_entry_factions_not_shared_between_instances() -> None:
    """lang-review #2 (mutable default): the default-empty list must be per-instance
    (``Field(default_factory=list)``), never a shared mutable singleton."""
    a = _bestiary_entry()
    b = _bestiary_entry()
    a.factions.append(LILLIPUT)
    assert b.factions == [], "factions default leaked across BestiaryEntry instances"


def test_manual_encounter_factions_defaults_empty_and_round_trips() -> None:
    enc = ManualEncounter(data={"enemies": []}, label="x", tier=1)
    assert enc.factions == []
    tagged = ManualEncounter(data={"enemies": []}, label="x", tier=1, factions=[HOUYHNHNM])
    assert tagged.factions == [HOUYHNHNM]


def test_manual_encounter_factions_not_shared_between_instances() -> None:
    a = ManualEncounter(data={"enemies": []}, label="a", tier=1)
    b = ManualEncounter(data={"enemies": []}, label="b", tier=1)
    a.factions.append(HOUYHNHNM)
    assert b.factions == [], "factions default leaked across ManualEncounter instances"


def test_manual_npc_factions_defaults_empty_and_round_trips() -> None:
    """Spec adds ``factions`` to ManualNpc for symmetry (origin-stamp lands in
    157-3); the field must exist and default empty now."""
    npc = ManualNpc(data={"name": "n"}, name="n", role="r", culture="c")
    assert npc.factions == []
    tagged = ManualNpc(
        data={"name": "n"}, name="n", role="r", culture="c", factions=[LILLIPUT]
    )
    assert tagged.factions == [LILLIPUT]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
