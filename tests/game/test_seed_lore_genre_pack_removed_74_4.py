"""Story 74-4 AC3 — remove the dead ``seed_lore_from_genre_pack`` seeder.

After story 74-3 deleted genre-tier ``lore.yaml`` from every live pack and made
lore world-only, ``seed_lore_from_genre_pack`` has ZERO production callers: the
live seeding path is ``seed_world_lore`` → ``seed_lore_from_world`` (the only
``seed_lore_from_genre_pack(`` occurrence in ``sidequest/`` is its own ``def``).
Per "No Stubbing / dead code is worse than no code", it must go — together with
its ``__all__`` export and the now-orphaned genre-seed tests.

These tests:
  * RED — assert the symbol is gone from the module API (reflection on the live
    module + ``__all__``; this is the "tripwire" reflection pattern CLAUDE.md's
    *No Source-Text Wiring Tests* explicitly permits — it interrogates the
    runtime module object, not source strings, so it survives refactor and only
    flips green once the function is actually deleted).
  * GREEN guard — the SURVIVING world-lore seeding path still seeds world lore
    and zero genre lore, proving the deletion did not sever production seeding
    (the epic-74 "world-only lore" invariant).
"""

from __future__ import annotations

from pathlib import Path

from sidequest.game import lore_seeding
from sidequest.game.lore_store import LoreStore
from sidequest.genre.loader import load_genre_pack

# --------------------------------------------------------------------------- #
# RED — the dead seeder is removed from the module API
# --------------------------------------------------------------------------- #


def test_seed_lore_from_genre_pack_symbol_is_removed() -> None:
    """The function must no longer be an attribute of the module."""
    assert not hasattr(lore_seeding, "seed_lore_from_genre_pack"), (
        "seed_lore_from_genre_pack is dead after epic 74 (zero production callers) "
        "and must be deleted, not left as an importable utility"
    )


def test_seed_lore_from_genre_pack_not_in_all() -> None:
    """It must also leave ``__all__`` so it is not re-exported as public API."""
    public = getattr(lore_seeding, "__all__", [])
    assert "seed_lore_from_genre_pack" not in public, (
        "seed_lore_from_genre_pack must be removed from lore_seeding.__all__"
    )


# --------------------------------------------------------------------------- #
# GREEN guard — the surviving world-lore path is intact
# --------------------------------------------------------------------------- #


def test_seed_world_lore_seeds_world_lore_and_zero_genre(
    minimal_pack_factory, tmp_path: Path
) -> None:
    """``seed_world_lore`` (the live path) still seeds the world's lore into the
    store and reports ``genre_added == 0`` — the epic-74 world-only invariant.

    Drives the real ``load_genre_pack`` → ``seed_world_lore`` path so the
    deletion of the dead genre seeder is proven NOT to sever production seeding
    (CLAUDE.md "Verify Wiring, Not Just Existence").
    """
    pack = load_genre_pack(minimal_pack_factory(tmp_path).path)
    world_slug = next(iter(pack.worlds))

    store = LoreStore()
    genre_added, world_added = lore_seeding.seed_world_lore(store, pack, world_slug)

    assert genre_added == 0, "epic 74: genre lore is never seeded (world-only)"
    assert world_added > 0, (
        "the surviving world-lore seeder must still populate the store from the world's lore.yaml"
    )
    assert len(store) == world_added
