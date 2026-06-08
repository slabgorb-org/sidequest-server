"""Loader wires seed_tropes.yaml at BOTH the genre and world tiers.

The seed-trope deck loads from a pack's own ``seed_tropes.yaml`` (genre tier →
``GenrePack.seed_tropes``) AND from each ``worlds/<slug>/seed_tropes.yaml``
(world tier → ``World.seed_tropes``). A world that authors its own deck plants
those seeds; a pack-level deck is the genre default. Both are optional and both
fail loud on a malformed entry (``SeedTrope`` is ``extra='forbid'``).

History: epic 94 (was story 22-3) first moved ``tea_and_murder``'s deck down to
the ``glenross`` world while the genre tier carried none — the "genre tier is
the rulebook only" reading of ADR-120. That was later relaxed: seed decks were
authored at the genre tier for every pack (content #398/#399, the 77-7 lull-
escalation ammunition), and the loader reads them into ``GenrePack.seed_tropes``.
So the live assertion is no longer "genre tier must be empty" — it is the
no-silent-default contract: present → loaded, absent → ``[]``. The absence
cases below anchor on the drift-proof ``test_genre`` FIXTURE pack (which ships
no seed file at either tier) rather than a live pack whose authored decks move.

Contract under test:

* The loader reads ``seed_tropes.yaml`` from a WORLD's directory and populates
  ``World.seed_tropes`` as a list of
  :class:`~sidequest.genre.models.tropes.SeedTrope` (live ``glenross`` deck).
* A pack/world without a ``seed_tropes.yaml`` gets an explicit empty list (no
  silent fallback to a default deck — per CLAUDE.md "No Silent Fallbacks").
* The ``glenross`` deck is loadable end-to-end with no validation errors —
  every authored seed round-trips through :class:`SeedTrope`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.tropes import SeedTrope

# Anchor the tea_and_murder pack the same way the world-load test anchors
# its pack (see tests/genre/test_beneath_sunden_world_load.py:19). Used only
# for the live ``glenross`` world-tier deck assertions below.
_TEA_AND_MURDER = (
    Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs/tea_and_murder"
)

# Drift-proof fixture pack: ships NO seed_tropes.yaml at the genre tier and
# its one world (flickering_reach) ships none either — the controlled anchor
# for the "absent → empty list, no silent default" contract.
_FIXTURE_GENRE = Path(__file__).resolve().parents[1] / "fixtures/packs/test_genre"


@pytest.fixture(scope="module")
def tea_pack():
    return load_genre_pack(_TEA_AND_MURDER)


@pytest.fixture(scope="module")
def fixture_pack():
    """A controlled pack with no seed_tropes.yaml at either tier."""
    return load_genre_pack(_FIXTURE_GENRE)


@pytest.fixture(scope="module")
def glenross_world(tea_pack):
    """The world that owns the migrated seed-trope deck (epic 94)."""
    world = tea_pack.worlds.get("glenross")
    if world is None:
        pytest.skip("glenross world not present in tea_and_murder pack")
    return world


def test_world_exposes_seed_tropes_attribute(glenross_world):
    """The loader must populate a ``seed_tropes`` collection on the
    World — parallel to the existing ``tropes`` attribute that carries
    :class:`TropeDefinition`. Seeds are a sibling type per 22-1, not a
    field extension on TropeDefinition, so they want their own slot.
    """
    assert hasattr(glenross_world, "seed_tropes"), (
        "World must expose a 'seed_tropes' attribute populated by the "
        "loader from worlds/<slug>/seed_tropes.yaml. Without it the "
        "authored content cannot reach the narrator."
    )


def test_genre_tier_seed_tropes_absent_loads_empty(fixture_pack):
    """No-silent-default contract at the genre tier: a pack that ships no
    genre-tier ``seed_tropes.yaml`` exposes ``GenrePack.seed_tropes == []`` —
    never auto-seeded from a default deck. (Live packs now author genre-tier
    decks per content #398/#399, so this anchors on the drift-proof fixture
    pack rather than a live pack whose deck moves.)"""
    assert fixture_pack.seed_tropes == []


def test_glenross_seed_tropes_load_non_empty(glenross_world):
    """The migrated deck has 12+ seeds. Loader must surface them on the
    world — not silently default to []."""
    assert isinstance(glenross_world.seed_tropes, list)
    assert len(glenross_world.seed_tropes) > 0, (
        "glenross/seed_tropes.yaml is authored but the loaded world reports "
        "zero seeds — the loader is not reading the world-tier file."
    )


def test_glenross_seeds_round_trip_as_seedtrope_instances(glenross_world):
    """Every entry must validate through the SeedTrope pydantic model
    (``extra='forbid'`` — typos in authored YAML must fail loudly). If
    the loader stores raw dicts instead, downstream rendering breaks at
    the attribute level."""
    for seed in glenross_world.seed_tropes:
        assert isinstance(seed, SeedTrope), (
            f"Loaded seed is {type(seed).__name__}, expected SeedTrope. "
            "Loader must call SeedTrope.model_validate on each YAML item."
        )


def test_glenross_seed_known_id_present(glenross_world):
    """glenross/seed_tropes.yaml ships with 'a_sealed_letter_unopened'
    as its first seed. Pinning a specific id catches the case where the
    loader reads *some* file but not the right one (e.g. a stale glob)."""
    ids = {s.id for s in glenross_world.seed_tropes}
    assert "a_sealed_letter_unopened" in ids, (
        "Expected 'a_sealed_letter_unopened' to be among the loaded "
        f"seeds for glenross; got ids={sorted(ids)}."
    )


def test_glenross_seeds_carry_authored_prose(glenross_world):
    """Each loaded seed must surface the authored prose fields the
    narrator-context renderer will consume. Pinning these guards against
    a future schema change that quietly drops a field on load — the
    YAML→pydantic round-trip is the lie-detector."""
    sealed = next(
        (s for s in glenross_world.seed_tropes if s.id == "a_sealed_letter_unopened"),
        None,
    )
    assert sealed is not None
    assert sealed.name, "Authored 'name' must round-trip"
    assert sealed.description, "Authored 'description' must round-trip"
    assert sealed.flavor_tags, "Authored 'flavor_tags' must round-trip"
    assert sealed.lifespan_turns > 0, (
        "lifespan_turns must round-trip as a positive int — 0 would "
        "make is_expired() fire immediately on the activation turn."
    )
    assert sealed.delivery_hints, "Authored 'delivery_hints' must round-trip"
    assert sealed.narrative_hint, "Authored 'narrative_hint' must round-trip"


def test_pack_without_seed_tropes_yaml_loads_with_empty_list(fixture_pack):
    """No silent fallback: a pack that ships no seed_tropes.yaml must
    load successfully (the file is optional) AND surface an explicitly
    empty list — not omit the attribute, not raise, not auto-seed from
    a default deck.

    This is the negative side of the wiring test: prevents a future
    refactor where the loader hard-requires seed_tropes.yaml and breaks
    every pack that ships none at load time. Anchored on the ``test_genre``
    fixture pack — a live pack can grow a genre/world deck at any time
    (every live pack now ships one), so a live anchor is the prod-rows-in-
    tests anti-pattern; the fixture is the controlled negative case.
    """
    assert hasattr(fixture_pack, "seed_tropes"), (
        "Packs without seed_tropes.yaml must still expose the attribute — "
        "the loader sets it to [] explicitly. Missing attribute breaks "
        "every consumer that reads pack.seed_tropes."
    )
    assert fixture_pack.seed_tropes == [], (
        f"Expected empty seed_tropes list when no seed_tropes.yaml is "
        f"present; got {len(fixture_pack.seed_tropes)} entries — the loader "
        "is auto-populating from a default (silent fallback)."
    )
    # The world tier must mirror this — worlds without a seed_tropes.yaml
    # expose an explicit empty list, no silent fallback.
    assert fixture_pack.worlds, "fixture pack must ship at least one world"
    for world in fixture_pack.worlds.values():
        assert hasattr(world, "seed_tropes")
        assert world.seed_tropes == []
