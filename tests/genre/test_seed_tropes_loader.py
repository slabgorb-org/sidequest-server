"""Loader wires seed_tropes.yaml at the WORLD tier (epic 94, was story 22-3).

Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
the seed-trope deck is a world-tier CAST/CATALOG surface — the seeds a world
plants — NOT a genre mechanic. The genre tier is the rulebook only. Epic 94
moved ``tea_and_murder/seed_tropes.yaml`` down to the ``glenross`` world.

Contract under test:

* The loader reads ``seed_tropes.yaml`` from a WORLD's directory and populates
  ``World.seed_tropes`` as a list of
  :class:`~sidequest.genre.models.tropes.SeedTrope`.
* Worlds without a ``seed_tropes.yaml`` get an empty list (no silent fallback
  to a default deck — per CLAUDE.md "No Silent Fallbacks"; missing file is
  fine, but the field must reflect reality).
* The migrated ``glenross`` deck is loadable end-to-end with no validation
  errors — every authored seed round-trips through :class:`SeedTrope`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.tropes import SeedTrope

# Anchor the tea_and_murder pack the same way the world-load test anchors
# its pack (see tests/genre/test_beneath_sunden_world_load.py:19).
_TEA_AND_MURDER = (
    Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs/tea_and_murder"
)


@pytest.fixture(scope="module")
def tea_pack():
    return load_genre_pack(_TEA_AND_MURDER)


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


def test_genre_tier_seed_tropes_is_empty(tea_pack):
    """Epic 94: the genre tier no longer owns the deck — it migrated to the
    world. GenrePack.seed_tropes is [] for the migrated tea_and_murder pack."""
    assert tea_pack.seed_tropes == []


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


def test_pack_without_seed_tropes_yaml_loads_with_empty_list(tmp_path: Path):
    """No silent fallback: a pack that ships no seed_tropes.yaml must
    load successfully (the file is optional) AND surface an explicitly
    empty list — not omit the attribute, not raise, not auto-seed from
    a default deck.

    This is the negative side of the wiring test: prevents a future
    refactor where the loader hard-requires seed_tropes.yaml and breaks
    every non-tea_and_murder pack at load time.
    """
    # Skeleton pack that load_genre_pack will accept without a seed file.
    # We don't build a full pack here — instead we use the canonical
    # caverns_and_claudes pack which (today) has no seed_tropes.yaml.
    caverns = Path(__file__).resolve().parents[3] / (
        "sidequest-content/genre_packs/caverns_and_claudes"
    )
    if not caverns.exists():
        pytest.skip(
            "caverns_and_claudes pack not found — cannot verify no-seed-file fallback contract."
        )
    pack = load_genre_pack(caverns)
    assert hasattr(pack, "seed_tropes"), (
        "Packs without seed_tropes.yaml must still expose the attribute — "
        "the loader sets it to [] explicitly. Missing attribute breaks "
        "every consumer that reads pack.seed_tropes."
    )
    assert pack.seed_tropes == [], (
        f"Expected empty seed_tropes list when no seed_tropes.yaml is "
        f"present; got {len(pack.seed_tropes)} entries. Either the "
        "loader is auto-populating from another pack (silent fallback) "
        "or caverns_and_claudes secretly grew a seed file."
    )
    # Epic 94: the world tier must mirror this — worlds without a
    # seed_tropes.yaml expose an explicit empty list, no silent fallback.
    for world in pack.worlds.values():
        assert hasattr(world, "seed_tropes")
        assert world.seed_tropes == []
