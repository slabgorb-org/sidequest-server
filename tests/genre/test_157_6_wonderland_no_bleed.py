"""Story 157-6 — wonderland faction tagging + no-bleed proof (epic-157).

The CONTENT-side proof for ``wry_whimsy/wonderland``. It loads the REAL pack and
drives the real engine predicates (``sidequest/game/zone_eligibility.py``)
against the authored ``factions:`` tags — verifying wiring END-TO-END.

wonderland has a COARSE cartography: many regions share a few faction banners —
``the_queens_terror`` (Card Country + the Tulgey Wood, the Alice's-Adventures
half), ``the_rigged_game`` (Looking-Glass Land), and ``no_one`` (the
looking-glass seam). The headline no-bleed assertion: a Card-Country creature is
NOT eligible on the Looking-Glass side, and IS eligible in the Card Country.

Also guards the ``extends`` regression: ``nonsense_as_authority`` extends a
genre-tier parent AND carries the ``"*"`` sentinel (it is the central
nonsense-as-authority engine, present in both halves), so it proves
``resolve.py::_merge_trope`` propagates a world-global tag through inheritance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.zone_eligibility import is_eligible, world_is_zoned
from sidequest.genre.loader import load_genre_pack

PACK = Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs/wry_whimsy"

# The three wonderland zones (cartography ``controlled_by`` slugs).
QUEENS_TERROR = "the_queens_terror"  # Card Country + the Tulgey Wood
RIGGED_GAME = "the_rigged_game"  # Looking-Glass Land
SEAM = "no_one"  # the looking-glass threshold
SENTINEL = "*"


@pytest.fixture(scope="module")
def wonderland():
    pack = load_genre_pack(PACK)
    assert "wonderland" in pack.worlds, f"wonderland not discovered; worlds={sorted(pack.worlds)}"
    return pack.worlds["wonderland"]


@pytest.fixture(scope="module")
def valid_factions(wonderland) -> set[str]:
    return {r.controlled_by for r in wonderland.cartography.regions.values() if r.controlled_by}


# ═══════════════════════════════════════════════════════════
# The world is zoned, and the zones are the two halves + the seam
# ═══════════════════════════════════════════════════════════


def test_wonderland_is_a_zoned_world(wonderland) -> None:
    assert world_is_zoned(wonderland.cartography) is True


def test_zones_are_the_two_halves_and_the_seam(valid_factions) -> None:
    assert valid_factions == {QUEENS_TERROR, RIGGED_GAME, SEAM}


# ═══════════════════════════════════════════════════════════
# Completeness — every pooled item tagged + referentially valid.
# Pre-flights the strict load validator (story 157-7).
# ═══════════════════════════════════════════════════════════


def _assert_all_tagged(items, label: str, valid_factions: set[str]) -> None:
    for item in items:
        ident = getattr(item, "id", None) or getattr(item, "name", "?")
        assert item.factions, f"{label} {ident!r} has empty factions in a zoned world"
        for faction in item.factions:
            assert faction == SENTINEL or faction in valid_factions, (
                f"{label} {ident!r} references unknown faction {faction!r}"
            )


def test_every_bestiary_entry_tagged_and_valid(wonderland, valid_factions) -> None:
    assert wonderland.bestiary is not None
    assert len(wonderland.bestiary.entries) == 12
    _assert_all_tagged(wonderland.bestiary.entries, "bestiary entry", valid_factions)


def test_every_resolved_trope_tagged_and_valid(wonderland, valid_factions) -> None:
    assert len(wonderland.tropes) == 9
    _assert_all_tagged(wonderland.tropes, "trope", valid_factions)


def test_every_seed_trope_tagged_and_valid(wonderland, valid_factions) -> None:
    assert len(wonderland.seed_tropes) == 11
    _assert_all_tagged(wonderland.seed_tropes, "seed trope", valid_factions)


# ═══════════════════════════════════════════════════════════
# Behavioral no-bleed — the headline proof
# ═══════════════════════════════════════════════════════════


def _bestiary_entry(wonderland, entry_id: str):
    return next(e for e in wonderland.bestiary.entries if e.id == entry_id)


def _trope(wonderland, trope_id: str):
    return next(t for t in wonderland.tropes if t.id == trope_id)


def _seed(wonderland, seed_id: str):
    return next(s for s in wonderland.seed_tropes if s.id == seed_id)


def test_card_creature_does_not_bleed_onto_the_looking_glass_side(wonderland) -> None:
    # The headline leak class: a Card-Country pasteboard soldier surfacing in
    # Looking-Glass Land.
    soldier = _bestiary_entry(wonderland, "card_soldier")
    assert soldier.factions == [QUEENS_TERROR]
    assert is_eligible(soldier.factions, {RIGGED_GAME}, zoned=True) is False, (
        "a Card-Country soldier must NOT be eligible on the Looking-Glass side"
    )
    assert is_eligible(soldier.factions, {QUEENS_TERROR}, zoned=True) is True


def test_looking_glass_trope_does_not_bleed_into_card_country(wonderland) -> None:
    race = _trope(wonderland, "run_to_stay_in_place")
    assert race.factions == [RIGGED_GAME]
    assert is_eligible(race.factions, {QUEENS_TERROR}, zoned=True) is False
    assert is_eligible(race.factions, {RIGGED_GAME}, zoned=True) is True


def test_red_king_seed_does_not_bleed_into_card_country(wonderland) -> None:
    seed = _seed(wonderland, "wonderland_only_a_dream")
    assert seed.factions == [RIGGED_GAME]
    assert is_eligible(seed.factions, {QUEENS_TERROR}, zoned=True) is False
    assert is_eligible(seed.factions, {RIGGED_GAME}, zoned=True) is True


def test_card_seed_does_not_bleed_onto_the_looking_glass_side(wonderland) -> None:
    seed = _seed(wonderland, "wonderland_sentence_first")
    assert seed.factions == [QUEENS_TERROR]
    assert is_eligible(seed.factions, {RIGGED_GAME}, zoned=True) is False
    assert is_eligible(seed.factions, {QUEENS_TERROR}, zoned=True) is True


def test_way_home_spine_eligible_in_every_zone(wonderland, valid_factions) -> None:
    # the_pack_of_cards is "*" — the refusal that wakes the dreamer fires at both
    # the Card trial and the Looking-Glass feast (and at the seam home).
    spine = _trope(wonderland, "the_pack_of_cards")
    assert spine.factions == [SENTINEL]
    for faction in valid_factions:
        assert is_eligible(spine.factions, {faction}, zoned=True) is True


def test_extends_trope_keeps_world_global_sentinel_after_merge(wonderland, valid_factions) -> None:
    # nonsense_as_authority ``extends`` "Impossible Authority" AND is "*"; the
    # merge must preserve the world-global sentinel through inheritance.
    engine = _trope(wonderland, "nonsense_as_authority")
    assert engine.factions == [SENTINEL], (
        "extends-trope lost its '*' sentinel — _merge_trope must propagate factions"
    )
    for faction in valid_factions:
        assert is_eligible(engine.factions, {faction}, zoned=True) is True
