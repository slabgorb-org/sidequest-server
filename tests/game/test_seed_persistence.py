"""RED-phase tests for Story 22-1 — seed persistence via ADR-023 (AC5).

These are the *wiring* tests required by the server CLAUDE.md: they prove the
new fields round-trip through the real persistence path (``SqliteStore`` ->
``snapshot_json`` -> reload), not merely that the models serialize in isolation.

The load-bearing assertion (AC5): after persisting a snapshot in which some
seeds were drawn, a deck re-instantiated from the reloaded state must NOT redeal
those seeds.

No schema migration is exercised — seeds ride the existing ``snapshot_json``
column.
"""

from __future__ import annotations

from sidequest.game.persistence import SqliteStore
from sidequest.game.seed_deck import SeedDeck
from sidequest.game.session import GameSnapshot, SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope


def _snapshot_with_seeds() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        active_seeds=[
            SeedState(
                id="sealed-letter",
                name="A Sealed Letter",
                activated_at_turn=4,
                flavor_tags=["mystery"],
                lifespan_turns=8,
                delivery_hints=["an innkeeper hands it over"],
            ),
            SeedState(
                id="missing-spoon",
                name="The Missing Spoon",
                activated_at_turn=6,
                flavor_tags=["domestic"],
                lifespan_turns=5,
                delivery_hints=["a maid frets about it"],
            ),
        ],
        seed_ghosts=[
            SeedGhost(
                id="uneasy-innkeeper",
                name="An Uneasy Innkeeper",
                expired_at_turn=3,
                delivery_hints=["he avoids eye contact"],
            ),
        ],
    )


def test_game_snapshot_has_seed_fields_defaulting_empty():
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    assert snap.active_seeds == []
    assert snap.seed_ghosts == []


def test_snapshot_seed_fields_round_trip_as_json():
    snap = _snapshot_with_seeds()
    reloaded = GameSnapshot.model_validate_json(snap.model_dump_json())
    assert [s.id for s in reloaded.active_seeds] == ["sealed-letter", "missing-spoon"]
    assert [g.id for g in reloaded.seed_ghosts] == ["uneasy-innkeeper"]
    assert reloaded.active_seeds == snap.active_seeds
    assert reloaded.seed_ghosts == snap.seed_ghosts


def test_snapshot_persists_seeds_through_sqlite_store():
    # WIRING: real persistence path, not in-memory model copy.
    store = SqliteStore.open_in_memory()
    store.save(_snapshot_with_seeds())
    loaded = store.load()
    assert loaded is not None
    snap = loaded.snapshot
    assert {s.id for s in snap.active_seeds} == {"sealed-letter", "missing-spoon"}
    assert {g.id for g in snap.seed_ghosts} == {"uneasy-innkeeper"}


def test_drawn_seeds_do_not_return_after_reload():
    # The AC5 load-bearing assertion. Drawn seeds are exactly those that are
    # active or have become ghosts; a deck rebuilt from reloaded state must
    # skip them.
    store = SqliteStore.open_in_memory()
    store.save(_snapshot_with_seeds())
    snap = store.load().snapshot

    drawn = {s.id for s in snap.active_seeds} | {g.id for g in snap.seed_ghosts}
    full_pack = [
        SeedTrope(id=sid, name=sid, lifespan_turns=5)
        for sid in ["sealed-letter", "missing-spoon", "uneasy-innkeeper", "fresh-1", "fresh-2"]
    ]
    deck = SeedDeck(
        genre_id="tea_and_murder",
        world_id="glenross",
        session_id="resumed-session",
        seeds=full_pack,
        drawn_ids=drawn,
    )

    remaining = []
    while (s := deck.draw()) is not None:
        remaining.append(s.id)

    assert set(remaining) == {"fresh-1", "fresh-2"}
    assert "sealed-letter" not in remaining
    assert "uneasy-innkeeper" not in remaining
