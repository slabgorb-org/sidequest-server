"""Story 157-6 — oz faction tagging + no-bleed proof (epic-157).

The CONTENT-side proof that the faction/zone-eligibility fan-out (157-5 gulliver
pattern → oz/wonderland/the_circuit) holds for ``wry_whimsy/oz``. It loads the
REAL pack and drives the real engine predicates
(``sidequest/game/zone_eligibility.py``) against the authored ``factions:`` tags
— verifying wiring END-TO-END, not just that the YAML parses.

oz is the seven-zone Baum fairyland (the four colored countries + the Emerald
City + the open road + the Deadly Desert). The headline no-bleed assertion: a
Winkie witch-servant (the Witch of the West's gray wolf) is NOT eligible on the
Munchkin meadows, and IS eligible in the Winkie country — the oz analog of the
gulliver Yahoo-on-the-Lilliput-shore leak.

Also guards the ``extends`` regression (the gulliver ``the_petty_holy_war``
case): ``incomplete_companion`` extends "Helpful Companion", so its
``[open_country]`` scope only survives inheritance because
``resolve.py::_merge_trope`` propagates ``factions`` (157-5). ``lost_princess``
extends "Impossible Authority" AND carries the ``"*"`` sentinel, proving the merge
preserves world-global tags too. (``green_spectacles`` is a standalone trope — no
``extends:`` in its body — so it gets its own plain no-bleed test, not an
extends-regression guard.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.zone_eligibility import is_eligible, world_is_zoned
from sidequest.genre.loader import load_genre_pack

PACK = Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs/wry_whimsy"

# The seven oz zones (cartography ``controlled_by`` slugs).
MUNCHKINS = "munchkins"
OPEN_COUNTRY = "open_country"
THE_WIZARD = "the_wizard"
WITCH_OF_THE_WEST = "witch_of_the_west"
GLINDA = "glinda"
GILLIKIN = "gillikin_hedge_witches"
DESERT = "no_one"
SENTINEL = "*"


@pytest.fixture(scope="module")
def oz():
    pack = load_genre_pack(PACK)
    assert "oz" in pack.worlds, f"oz not discovered; worlds={sorted(pack.worlds)}"
    return pack.worlds["oz"]


@pytest.fixture(scope="module")
def valid_factions(oz) -> set[str]:
    return {r.controlled_by for r in oz.cartography.regions.values() if r.controlled_by}


# ═══════════════════════════════════════════════════════════
# The world is zoned, and the zones are the seven oz factions
# ═══════════════════════════════════════════════════════════


def test_oz_is_a_zoned_world(oz) -> None:
    # Without this, every zone-eligibility predicate short-circuits to permissive
    # and the whole proof is vacuous.
    assert world_is_zoned(oz.cartography) is True


def test_zones_are_the_seven_oz_factions(valid_factions) -> None:
    assert valid_factions == {
        MUNCHKINS,
        OPEN_COUNTRY,
        THE_WIZARD,
        WITCH_OF_THE_WEST,
        GLINDA,
        GILLIKIN,
        DESERT,
    }


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


def test_every_bestiary_entry_tagged_and_valid(oz, valid_factions) -> None:
    assert oz.bestiary is not None
    assert len(oz.bestiary.entries) == 13
    _assert_all_tagged(oz.bestiary.entries, "bestiary entry", valid_factions)


def test_every_resolved_trope_tagged_and_valid(oz, valid_factions) -> None:
    # ``oz.tropes`` is the RESOLVED list (inheritance applied) — the exact list
    # the Seam-3 trope gate reads. The two ``extends`` tropes (incomplete_companion,
    # lost_princess) only pass here because _merge_trope propagates factions.
    assert len(oz.tropes) == 7
    _assert_all_tagged(oz.tropes, "trope", valid_factions)


def test_every_seed_trope_tagged_and_valid(oz, valid_factions) -> None:
    assert len(oz.seed_tropes) == 11
    _assert_all_tagged(oz.seed_tropes, "seed trope", valid_factions)


# ═══════════════════════════════════════════════════════════
# Behavioral no-bleed — the headline proof
# ═══════════════════════════════════════════════════════════


def _bestiary_entry(oz, entry_id: str):
    return next(e for e in oz.bestiary.entries if e.id == entry_id)


def _trope(oz, trope_id: str):
    return next(t for t in oz.tropes if t.id == trope_id)


def _seed(oz, seed_id: str):
    return next(s for s in oz.seed_tropes if s.id == seed_id)


def test_witch_servant_does_not_bleed_onto_munchkin_meadows(oz) -> None:
    # The headline leak class: a West-witch servant surfacing in the gentle east.
    wolf = _bestiary_entry(oz, "gray_wolf")
    assert wolf.factions == [WITCH_OF_THE_WEST]
    assert is_eligible(wolf.factions, {MUNCHKINS}, zoned=True) is False, (
        "the Witch of the West's wolf must NOT be eligible on the Munchkin meadows"
    )
    assert is_eligible(wolf.factions, {WITCH_OF_THE_WEST}, zoned=True) is True


def test_quadling_obstacle_does_not_bleed_into_emerald_city(oz) -> None:
    hammer = _bestiary_entry(oz, "hammerhead")
    assert hammer.factions == [GLINDA]
    assert is_eligible(hammer.factions, {THE_WIZARD}, zoned=True) is False
    assert is_eligible(hammer.factions, {GLINDA}, zoned=True) is True


def test_road_kalidah_does_not_bleed_into_the_emerald_city(oz) -> None:
    kalidah = _bestiary_entry(oz, "kalidah")
    assert kalidah.factions == [OPEN_COUNTRY]
    assert is_eligible(kalidah.factions, {THE_WIZARD}, zoned=True) is False
    assert is_eligible(kalidah.factions, {OPEN_COUNTRY}, zoned=True) is True


def test_gillikin_captive_does_not_bleed_into_munchkin_country(oz) -> None:
    captive = _bestiary_entry(oz, "ornamented_captive")
    assert captive.factions == [GILLIKIN]
    assert is_eligible(captive.factions, {MUNCHKINS}, zoned=True) is False
    assert is_eligible(captive.factions, {GILLIKIN}, zoned=True) is True


def test_deadly_desert_spine_eligible_in_every_zone(oz, valid_factions) -> None:
    # The go-home spine — the lethal seal at the rim of every quarter — is "*".
    desert = _trope(oz, "deadly_desert")
    assert desert.factions == [SENTINEL]
    for faction in valid_factions:
        assert is_eligible(desert.factions, {faction}, zoned=True) is True


def test_extends_trope_keeps_specific_faction_after_merge(oz) -> None:
    # Regression guard for the _merge_trope factions-propagation fix. The
    # Incomplete Companion trope ``extends`` "Helpful Companion"; without
    # propagation its [open_country] scope is lost to [] (permissive) and the
    # road companions bleed into every region (the oz analog of gulliver's
    # the_petty_holy_war guard).
    companion = _trope(oz, "incomplete_companion")
    assert companion.factions == [OPEN_COUNTRY], (
        "extends-trope lost its faction scope — _merge_trope must propagate factions"
    )
    assert is_eligible(companion.factions, {WITCH_OF_THE_WEST}, zoned=True) is False
    assert is_eligible(companion.factions, {OPEN_COUNTRY}, zoned=True) is True


def test_green_spectacles_scoped_to_the_emerald_city(oz) -> None:
    # green_spectacles is a STANDALONE trope (no ``extends:`` in its body),
    # scoped to the Emerald City — a plain no-bleed check, not a merge guard.
    spectacles = _trope(oz, "green_spectacles")
    assert spectacles.factions == [THE_WIZARD]
    assert is_eligible(spectacles.factions, {MUNCHKINS}, zoned=True) is False
    assert is_eligible(spectacles.factions, {THE_WIZARD}, zoned=True) is True


def test_extends_trope_keeps_world_global_sentinel_after_merge(oz, valid_factions) -> None:
    # lost_princess ``extends`` "Impossible Authority" AND is "*"; the merge must
    # preserve the world-global sentinel, not just specific factions.
    princess = _trope(oz, "lost_princess")
    assert princess.factions == [SENTINEL]
    for faction in valid_factions:
        assert is_eligible(princess.factions, {faction}, zoned=True) is True


def test_cap_seed_does_not_bleed_off_the_winkie_country(oz) -> None:
    seed = _seed(oz, "oz_the_cap_that_commands")
    assert seed.factions == [WITCH_OF_THE_WEST]
    assert is_eligible(seed.factions, {MUNCHKINS}, zoned=True) is False
    assert is_eligible(seed.factions, {WITCH_OF_THE_WEST}, zoned=True) is True
