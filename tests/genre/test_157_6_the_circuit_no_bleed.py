"""Story 157-6 — the_circuit faction tagging + no-bleed proof (epic-157).

The CONTENT-side proof for ``road_warrior/the_circuit``. It loads the REAL pack
and drives the real engine predicates (``sidequest/game/zone_eligibility.py``)
against the authored ``factions:`` tags — verifying wiring END-TO-END.

the_circuit is a shared port city carved into ten subculture turfs. Unlike the
fairytale worlds, most of its bestiary (port-city ecology) and tropes
(circuit-wide event formats) are world-global ``"*"``; the per-faction scoping
lives mostly in the seed deck, which reference-stacks on named faction mechanisms
(the Mod mirror network, the Rocker parts economy, the Lowrider Sagrado Pact, …).

The headline no-bleed assertion: the Lowrider Sagrado-Pact seed is NOT eligible
on a rival's turf, and IS eligible in lowrider-controlled Doracanto. A two-faction
tag (the Mods/Rockers blood feud) and the world-global port fauna round out the
proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.zone_eligibility import is_eligible, world_is_zoned
from sidequest.genre.loader import load_genre_pack

PACK = Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs/road_warrior"

# A few of the ten the_circuit subculture zones (cartography ``controlled_by``).
BOSOZOKU = "bosozoku"
CAFE_RACERS = "cafe_racers"
TUK_TUK = "tuk_tuk"
LOWRIDERS = "lowriders"
DEKOTORA = "dekotora"
ONE_PERCENTERS = "one_percenters"
MODS = "mods"
ROCKERS = "rockers"
MATATU = "matatu"
RAGGARE = "raggare"
SENTINEL = "*"

ALL_TURFS = {
    BOSOZOKU,
    CAFE_RACERS,
    TUK_TUK,
    LOWRIDERS,
    DEKOTORA,
    ONE_PERCENTERS,
    MODS,
    ROCKERS,
    MATATU,
    RAGGARE,
}


@pytest.fixture(scope="module")
def the_circuit():
    pack = load_genre_pack(PACK)
    assert "the_circuit" in pack.worlds, f"the_circuit not discovered; worlds={sorted(pack.worlds)}"
    return pack.worlds["the_circuit"]


@pytest.fixture(scope="module")
def valid_factions(the_circuit) -> set[str]:
    return {r.controlled_by for r in the_circuit.cartography.regions.values() if r.controlled_by}


# ═══════════════════════════════════════════════════════════
# The world is zoned, and the zones are the ten subculture turfs
# ═══════════════════════════════════════════════════════════


def test_the_circuit_is_a_zoned_world(the_circuit) -> None:
    assert world_is_zoned(the_circuit.cartography) is True


def test_zones_are_the_ten_subculture_turfs(valid_factions) -> None:
    assert valid_factions == ALL_TURFS


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


def test_every_bestiary_entry_tagged_and_valid(the_circuit, valid_factions) -> None:
    assert the_circuit.bestiary is not None
    assert len(the_circuit.bestiary.entries) == 9
    _assert_all_tagged(the_circuit.bestiary.entries, "bestiary entry", valid_factions)


def test_every_resolved_trope_tagged_and_valid(the_circuit, valid_factions) -> None:
    assert len(the_circuit.tropes) == 6
    _assert_all_tagged(the_circuit.tropes, "trope", valid_factions)


def test_every_seed_trope_tagged_and_valid(the_circuit, valid_factions) -> None:
    assert len(the_circuit.seed_tropes) == 12
    _assert_all_tagged(the_circuit.seed_tropes, "seed trope", valid_factions)


# ═══════════════════════════════════════════════════════════
# Behavioral no-bleed — the headline proof
# ═══════════════════════════════════════════════════════════


def _bestiary_entry(the_circuit, entry_id: str):
    return next(e for e in the_circuit.bestiary.entries if e.id == entry_id)


def _trope(the_circuit, trope_id: str):
    return next(t for t in the_circuit.tropes if t.id == trope_id)


def _seed(the_circuit, seed_id: str):
    return next(s for s in the_circuit.seed_tropes if s.id == seed_id)


def test_lowrider_seed_does_not_bleed_onto_a_rival_turf(the_circuit) -> None:
    # The headline leak class: a Lowrider Cruise-Night/Sagrado-Pact seed
    # surfacing on Mod or Rocker ground.
    seed = _seed(the_circuit, "the_circuit_sagrado_pact_tested")
    assert seed.factions == [LOWRIDERS]
    assert is_eligible(seed.factions, {MODS}, zoned=True) is False, (
        "a Lowrider Sagrado-Pact seed must NOT be eligible on Mod turf"
    )
    assert is_eligible(seed.factions, {ROCKERS}, zoned=True) is False
    assert is_eligible(seed.factions, {LOWRIDERS}, zoned=True) is True


def test_rocker_guard_dog_does_not_bleed_onto_lowrider_turf(the_circuit) -> None:
    dog = _bestiary_entry(the_circuit, "junkyard_guard_dog")
    assert dog.factions == [ROCKERS]
    assert is_eligible(dog.factions, {LOWRIDERS}, zoned=True) is False
    assert is_eligible(dog.factions, {ROCKERS}, zoned=True) is True


def test_two_faction_feud_seed_is_eligible_for_both_and_no_others(the_circuit) -> None:
    # The Mods/Rockers blood feud is tagged for exactly its two combatants — it
    # fires on either's turf and bleeds onto neither lowriders nor anyone else.
    feud = _seed(the_circuit, "the_circuit_blood_feud_spark")
    assert feud.factions == [MODS, ROCKERS]
    assert is_eligible(feud.factions, {MODS}, zoned=True) is True
    assert is_eligible(feud.factions, {ROCKERS}, zoned=True) is True
    assert is_eligible(feud.factions, {LOWRIDERS}, zoned=True) is False
    assert is_eligible(feud.factions, {TUK_TUK}, zoned=True) is False


def test_world_global_port_fauna_eligible_on_every_turf(the_circuit, valid_factions) -> None:
    # Port-city ecology is "*": a harbor rat belongs to the whole shared city.
    rat = _bestiary_entry(the_circuit, "harbor_rat")
    assert rat.factions == [SENTINEL]
    for faction in valid_factions:
        assert is_eligible(rat.factions, {faction}, zoned=True) is True


def test_world_global_event_trope_eligible_on_every_turf(the_circuit, valid_factions) -> None:
    # A turf race is a circuit-wide conflict format: "*" — eligible everywhere.
    race = _trope(the_circuit, "turf_race")
    assert race.factions == [SENTINEL]
    for faction in valid_factions:
        assert is_eligible(race.factions, {faction}, zoned=True) is True
