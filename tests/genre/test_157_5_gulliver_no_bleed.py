"""Story 157-5 — gulliver faction tagging + no-bleed proof (epic-157).

The CONTENT-side proof for the faction/zone-scoped content eligibility epic. It
loads the REAL ``wry_whimsy/gulliver`` pack and drives the real engine
predicates (``sidequest/game/zone_eligibility.py``) against the authored
``factions:`` tags — verifying wiring END-TO-END, not just that the YAML fields
parse.

Reproduces the original leak (playtest session ``2026-06-20-gulliver-e721409c``:
a 4th-voyage **Yahoo** surfaced on the 1st-voyage **Lilliput shore**) as a
falsifiable assertion — the Yahoo bestiary entries are NOT eligible in the
Lilliput zone and ARE in the Houyhnhnm zone.

Also guards the ``the_petty_holy_war`` regression: that world trope ``extends``
a genre-tier parent, so its ``[the_lilliput_court]`` scope only survives
inheritance because ``resolve.py::_merge_trope`` propagates ``factions`` (the
engine fix landed alongside this content in 157-5). Without it the Lilliput
egg-war would bleed into every voyage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.zone_eligibility import is_eligible, world_is_zoned
from sidequest.genre.loader import load_genre_pack

PACK = Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs/wry_whimsy"

# The five gulliver voyage-zones (cartography ``controlled_by`` slugs).
LILLIPUT = "the_lilliput_court"
BROBDINGNAG = "the_brobdingnag_crown"
LAGADO = "the_lagado_academy"
HOUYHNHNM = "the_houyhnhnm_assembly"
HUB = "no_one"
SENTINEL = "*"


@pytest.fixture(scope="module")
def gulliver():
    pack = load_genre_pack(PACK)
    assert "gulliver" in pack.worlds, f"gulliver not discovered; worlds={sorted(pack.worlds)}"
    return pack.worlds["gulliver"]


@pytest.fixture(scope="module")
def valid_factions(gulliver) -> set[str]:
    return {r.controlled_by for r in gulliver.cartography.regions.values() if r.controlled_by}


# ═══════════════════════════════════════════════════════════
# The world is zoned, and the zones are the five voyage-factions
# ═══════════════════════════════════════════════════════════


def test_gulliver_is_a_zoned_world(gulliver) -> None:
    # Without this, every zone-eligibility predicate short-circuits to permissive
    # and the whole proof is vacuous.
    assert world_is_zoned(gulliver.cartography) is True


def test_zones_are_the_five_voyage_factions(valid_factions) -> None:
    assert valid_factions == {LILLIPUT, BROBDINGNAG, LAGADO, HOUYHNHNM, HUB}


# ═══════════════════════════════════════════════════════════
# Completeness — every pooled item tagged + referentially valid.
# Pre-flights the strict load validator (story 157-7): in a zoned world every
# pooled, home-less item must carry a non-empty `factions`, and every value must
# be "*" or a real cartography `controlled_by`.
# ═══════════════════════════════════════════════════════════


def _assert_all_tagged(items, label: str, valid_factions: set[str]) -> None:
    for item in items:
        ident = getattr(item, "id", None) or getattr(item, "name", "?")
        assert item.factions, f"{label} {ident!r} has empty factions in a zoned world"
        for faction in item.factions:
            assert faction == SENTINEL or faction in valid_factions, (
                f"{label} {ident!r} references unknown faction {faction!r}"
            )


def test_every_bestiary_entry_tagged_and_valid(gulliver, valid_factions) -> None:
    assert gulliver.bestiary is not None
    assert len(gulliver.bestiary.entries) == 11
    _assert_all_tagged(gulliver.bestiary.entries, "bestiary entry", valid_factions)


def test_every_resolved_trope_tagged_and_valid(gulliver, valid_factions) -> None:
    # ``gulliver.tropes`` is the RESOLVED list (inheritance applied) — the exact
    # list the Seam-3 trope gate reads. The three ``extends`` tropes only pass
    # here because _merge_trope now propagates factions.
    assert len(gulliver.tropes) == 9
    _assert_all_tagged(gulliver.tropes, "trope", valid_factions)


def test_every_seed_trope_tagged_and_valid(gulliver, valid_factions) -> None:
    assert len(gulliver.seed_tropes) == 11
    _assert_all_tagged(gulliver.seed_tropes, "seed trope", valid_factions)


# ═══════════════════════════════════════════════════════════
# Behavioral no-bleed — the headline proof
# ═══════════════════════════════════════════════════════════


def _bestiary_entry(gulliver, entry_id: str):
    return next(e for e in gulliver.bestiary.entries if e.id == entry_id)


def _trope(gulliver, trope_id: str):
    return next(t for t in gulliver.tropes if t.id == trope_id)


@pytest.mark.parametrize("yahoo_id", ["yahoo_brute", "yahoo_pack"])
def test_yahoo_does_not_bleed_onto_lilliput_shore(gulliver, yahoo_id: str) -> None:
    # The exact bug from session 2026-06-20-gulliver-e721409c.
    yahoo = _bestiary_entry(gulliver, yahoo_id)
    assert yahoo.factions == [HOUYHNHNM]
    assert is_eligible(yahoo.factions, {LILLIPUT}, zoned=True) is False, (
        "4th-voyage Yahoo must NOT be eligible on the 1st-voyage Lilliput shore"
    )
    # ...and still appears where it belongs.
    assert is_eligible(yahoo.factions, {HOUYHNHNM}, zoned=True) is True


def test_lilliput_creature_does_not_bleed_into_houyhnhnm_land(gulliver) -> None:
    archers = _bestiary_entry(gulliver, "lilliput_archer_volley")
    assert archers.factions == [LILLIPUT]
    assert is_eligible(archers.factions, {HOUYHNHNM}, zoned=True) is False
    assert is_eligible(archers.factions, {LILLIPUT}, zoned=True) is True


def test_brobdingnag_creature_does_not_bleed_into_lilliput(gulliver) -> None:
    cat = _bestiary_entry(gulliver, "brobdingnag_cat")
    assert cat.factions == [BROBDINGNAG]
    assert is_eligible(cat.factions, {LILLIPUT}, zoned=True) is False
    assert is_eligible(cat.factions, {BROBDINGNAG}, zoned=True) is True


def test_petty_holy_war_trope_scoped_to_lilliput_after_merge(gulliver) -> None:
    # Regression guard for the _merge_trope factions-propagation fix. This trope
    # ``extends`` "Impossible Authority"; without propagation its scope is lost
    # to [] (permissive) and the Lilliput egg-war bleeds into every voyage.
    war = _trope(gulliver, "the_petty_holy_war")
    assert war.factions == [LILLIPUT], (
        "extends-trope lost its faction scope — _merge_trope must propagate factions"
    )
    assert is_eligible(war.factions, {HOUYHNHNM}, zoned=True) is False
    assert is_eligible(war.factions, {LILLIPUT}, zoned=True) is True


def test_world_global_spine_eligible_in_every_zone(gulliver, valid_factions) -> None:
    # the_compulsion_to_reembark is "*" — the go-home spine fires in every
    # voyage and on the open sea. (Also an ``extends`` trope, so it likewise
    # proves the merge fix preserves the "*" sentinel.)
    spine = _trope(gulliver, "the_compulsion_to_reembark")
    assert spine.factions == [SENTINEL]
    for faction in valid_factions:
        assert is_eligible(spine.factions, {faction}, zoned=True) is True


def test_houyhnhnm_seed_does_not_bleed_onto_lilliput_shore(gulliver) -> None:
    seed = next(s for s in gulliver.seed_tropes if s.id == "gulliver_taken_for_the_yahoo")
    assert seed.factions == [HOUYHNHNM]
    assert is_eligible(seed.factions, {LILLIPUT}, zoned=True) is False
    assert is_eligible(seed.factions, {HOUYHNHNM}, zoned=True) is True
