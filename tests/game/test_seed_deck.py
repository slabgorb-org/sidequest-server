"""RED-phase tests for Story 22-1 — SeedDeck draw engine (AC2).

Contract under test:
- ``SeedDeck`` draws seeds without replacement, keyed per (genre, world,
  session_id), reproducibly seeded by ``session_id``.
- ``draw()`` returns a ``SeedTrope`` and removes it; returns ``None`` when
  exhausted.
- The deck exposes ``drawn_ids`` so persistence (AC5) can re-instantiate a deck
  that won't redeal already-drawn seeds.

Test-data discipline (feedback_no_content_coupled_tests): seeds are injected
fixtures, never loaded from a live ``genre_packs/*`` directory. The "loads seeds
from YAML via genre loader" wording in AC2 is the *caller's* responsibility; the
deck itself takes an explicit seed list so it stays pure and testable. (Logged as
a test-design deviation.)
"""

from __future__ import annotations

import pytest

from sidequest.genre.models.tropes import SeedTrope
from sidequest.game.seed_deck import SeedDeck


def _seeds(n: int) -> list[SeedTrope]:
    return [SeedTrope(id=f"seed-{i}", name=f"Seed {i}", lifespan_turns=5) for i in range(n)]


def _make_deck(seeds, session_id="session-alpha", drawn_ids=None) -> SeedDeck:
    return SeedDeck(
        genre_id="tea_and_murder",
        world_id="glenross",
        session_id=session_id,
        seeds=seeds,
        drawn_ids=drawn_ids,
    )


def test_draw_returns_a_seed_and_removes_it():
    deck = _make_deck(_seeds(3))
    first = deck.draw()
    assert isinstance(first, SeedTrope)
    assert first.id in {"seed-0", "seed-1", "seed-2"}
    assert first.id in deck.drawn_ids


def test_draw_exhausts_to_none_after_all_seeds_dealt():
    deck = _make_deck(_seeds(3))
    dealt = [deck.draw() for _ in range(3)]
    assert all(s is not None for s in dealt)
    assert deck.draw() is None
    assert deck.draw() is None  # stays None, no wrap-around


def test_empty_deck_draws_none_immediately():
    deck = _make_deck([])
    assert deck.draw() is None


def test_single_seed_deck_deals_once_then_none():
    deck = _make_deck(_seeds(1))
    only = deck.draw()
    assert only is not None and only.id == "seed-0"
    assert deck.draw() is None


def test_draw_is_without_replacement_no_id_repeats():
    deck = _make_deck(_seeds(10))
    drawn = [deck.draw().id for _ in range(10)]
    assert len(set(drawn)) == 10  # no id appears twice
    assert set(drawn) == {f"seed-{i}" for i in range(10)}  # all seeds dealt


def test_same_session_id_produces_same_draw_order():
    order_a = [s.id for s in iter(lambda: _make_deck(_seeds(10), "fixed-session").draw(), None)]
    order_b = [s.id for s in iter(lambda: _make_deck(_seeds(10), "fixed-session").draw(), None)]
    assert order_a == order_b
    assert len(order_a) == 10


def test_different_session_id_produces_different_draw_order():
    order_a = [s.id for s in iter(lambda: _make_deck(_seeds(10), "session-alpha").draw(), None)]
    order_b = [s.id for s in iter(lambda: _make_deck(_seeds(10), "session-beta").draw(), None)]
    # With 10! orderings, collision is ~1/3.6M — a real shuffle differs.
    assert order_a != order_b


def test_deck_reinstantiated_with_drawn_ids_does_not_redraw_them():
    # AC2 persistence contract / AC5 hook: a deck rebuilt from persisted
    # drawn_ids must skip seeds already dealt in a prior session segment.
    seeds = _seeds(5)
    already = {"seed-0", "seed-2", "seed-4"}
    deck = _make_deck(seeds, drawn_ids=already)
    remaining = [deck.draw().id for _ in range(2)]
    assert set(remaining) == {"seed-1", "seed-3"}
    assert deck.draw() is None
    # drawn_ids reflects both the seeded-in set and the freshly drawn pair.
    assert already.issubset(deck.drawn_ids)
    assert deck.drawn_ids == {f"seed-{i}" for i in range(5)}


def test_deck_is_keyed_per_genre_world_session():
    deck = _make_deck(_seeds(2))
    assert deck.genre_id == "tea_and_murder"
    assert deck.world_id == "glenross"
    assert deck.session_id == "session-alpha"


def test_constructor_drawn_ids_defaults_to_empty_not_shared():
    # Rule #2 (mutable defaults): two decks must not share a drawn_ids set.
    d1 = _make_deck(_seeds(2))
    d2 = _make_deck(_seeds(2))
    d1.draw()
    assert d2.drawn_ids == set()
