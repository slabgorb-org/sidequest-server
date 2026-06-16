"""Regression tests for the slug-resume LoreStore re-seed gap.

Root cause (post-commit 72750db, which fixed the ToolContext id/store
plumbing and thereby *exposed* this second bug): ``_SessionData.lore_store``
is an in-memory ``field(default_factory=LoreStore)`` — it is NOT persisted
to the SQLite save. The fresh character-creation flow seeds it
(``seed_lore_from_genre_pack`` + ``seed_lore_from_world`` +
``seed_lore_from_char_creation``) inside ``_chargen_confirmation``. But the
slug-resume connect path (``session.slug_resumed``) constructs a brand-new
``_SessionData`` with an empty ``lore_store`` and never re-seeds it.

Symptom: post-fix-72750db live turns logged
``narrator.sdk_path.context_wired ... lore_fragments=0``; Jaeger
``tool.read.query_lore`` showed ``hit_count=0``; the SDK narrator
confabulated world canon instead of recalling it. Every resumed save
was affected.

Fix: the deterministic genre + world lore is re-seeded on the resume
path via the shared ``seed_world_lore`` helper (extracted from the
fresh path so both paths share one implementation), and the resume
path emits the same ``lore_store_loaded`` watcher event the fresh path
emits so the GM panel / Jaeger can prove lore loaded on resume.

Scope: genre + world lore only. Char-creation lore on resume is
deliberately out of scope — ``builder.scenes()`` is not reliably
available on a pure resume; that is a separate concern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.lore_seeding import seed_world_lore
from sidequest.game.lore_store import LoreStore
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.handlers.connect import _seed_world_lore_on_resume

CONTENT_ROOT = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"


@pytest.fixture(scope="module")
def caverns_pack() -> GenrePack:
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _first_world_slug(pack: GenrePack) -> str:
    if not pack.worlds:
        pytest.skip("caverns pack has no worlds — cannot exercise world seed")
    return next(iter(pack.worlds.keys()))


# ---------------------------------------------------------------------------
# (a) The resume connect path results in a non-empty lore_store with the
#     deterministic genre + world fragments.
# ---------------------------------------------------------------------------


def test_resume_helper_populates_empty_lore_store(caverns_pack: GenrePack) -> None:
    """A pure resume starts with a fresh default (empty) LoreStore. After
    the resume seeding helper runs it must hold the genre + world
    fragments — the exact gap that made ``query_lore`` return
    ``hit_count=0`` on every resumed save.
    """
    store = LoreStore()
    assert len(store) == 0

    world_slug = _first_world_slug(caverns_pack)
    genre_added, world_added = _seed_world_lore_on_resume(
        lore_store=store,
        genre_pack=caverns_pack,
        world_slug=world_slug,
        emit=lambda **_: None,
    )

    assert len(store) > 0, (
        "resume seeding must leave the in-memory lore_store non-empty so "
        "query_lore returns grounded hits instead of hit_count=0"
    )
    assert genre_added >= 1
    # Genre-scoped fragments must be present.
    assert "lore_genre_history" in store.fragments
    # World fragments are world-scoped (skip-tolerant: a world with no
    # populated lore fields legitimately adds zero).
    if world_added:
        assert any(fid.startswith(f"lore_world_{world_slug}_") for fid in store.fragments), list(
            store.fragments
        )


# ---------------------------------------------------------------------------
# (b) Wiring test — the resume code path actually invokes the seeding AND
#     emits the lore_store_loaded watcher event (CLAUDE.md "every suite
#     needs a wiring test" + the OTEL lie-detector mandate).
# ---------------------------------------------------------------------------


def test_resume_helper_emits_lore_store_loaded(caverns_pack: GenrePack) -> None:
    """The GM panel / Jaeger can only prove lore loaded on resume if the
    resume path emits the same ``lore_store_loaded`` event the fresh
    path emits. Spy the emit callback and assert it fired with the
    fragment/token counts.
    """
    store = LoreStore()
    world_slug = _first_world_slug(caverns_pack)
    captured: list[dict[str, object]] = []

    def _spy(**kwargs: object) -> None:
        captured.append(kwargs)

    _seed_world_lore_on_resume(
        lore_store=store,
        genre_pack=caverns_pack,
        world_slug=world_slug,
        emit=_spy,
    )

    assert len(captured) == 1, "resume seeding must emit lore_store_loaded exactly once"
    payload = captured[0]
    assert payload["total_fragments"] == len(store)
    assert payload["total_fragments"] > 0
    assert "total_tokens" in payload
    assert "genre_fragments_added" in payload
    assert "world_fragments_added" in payload


def test_connect_module_imports_resume_seeder() -> None:
    """Static-import wiring guard: ``handlers.connect`` must expose the
    resume seeding helper so the ``session.slug_resumed`` branch can
    call it. If a refactor drops the import/definition this fails before
    a resumed session ever hits the SDK narrator with empty lore.
    """
    import sidequest.handlers.connect as connect_mod

    assert hasattr(connect_mod, "_seed_world_lore_on_resume"), (
        "handlers.connect must define _seed_world_lore_on_resume for the "
        "slug-resume branch to re-seed the in-memory LoreStore"
    )
    assert hasattr(connect_mod, "seed_world_lore"), (
        "handlers.connect must import the shared seed_world_lore helper"
    )


def test_resume_seeder_is_called_from_production_connect_handler() -> None:
    """Wiring guard — existence is NOT enough (CLAUDE.md "Verify Wiring,
    Not Just Existence" / "imported, **called**, and reachable").

    ``_seed_world_lore_on_resume`` could stay defined while its call
    site in the production ``ConnectHandler.handle`` slug-resume
    (``has_character``) branch is deleted — re-introducing the exact
    defined-but-not-wired bug class this whole fix exists to repair.
    Assert the call appears in the production handler body via
    ``inspect.getsource`` (same technique as
    ``tests/game/test_lore_seeding.py`` for the seed_world_lore fan-out).
    This test MUST fail if the connect.py call site is removed.
    """
    import inspect

    from sidequest.handlers.connect import ConnectHandler

    handler_src = inspect.getsource(ConnectHandler.handle)
    assert "_seed_world_lore_on_resume(" in handler_src, (
        "ConnectHandler.handle must CALL _seed_world_lore_on_resume in "
        "its has_character slug-resume branch — defining the helper but "
        "not calling it leaves every resumed save with an empty "
        "lore_store and query_lore hit_count=0 (the bug this fix repairs)"
    )


# ---------------------------------------------------------------------------
# Idempotency — re-seeding genre/world lore on every connect (fresh or
# resume, including a reconnect-of-a-reconnect) must not unboundedly
# grow the store. The DuplicateLoreId guard makes the seeders stable by
# id, so a second pass over the same store adds nothing.
# ---------------------------------------------------------------------------


def test_resume_reseed_is_idempotent(caverns_pack: GenrePack) -> None:
    store = LoreStore()
    world_slug = _first_world_slug(caverns_pack)

    g1, w1 = _seed_world_lore_on_resume(
        lore_store=store,
        genre_pack=caverns_pack,
        world_slug=world_slug,
        emit=lambda **_: None,
    )
    size_after_first = len(store)
    assert size_after_first > 0
    assert g1 >= 1

    # Simulate a reconnect that re-runs the resume seeding against the
    # SAME store (e.g. the resume path running twice for one room).
    g2, w2 = _seed_world_lore_on_resume(
        lore_store=store,
        genre_pack=caverns_pack,
        world_slug=world_slug,
        emit=lambda **_: None,
    )
    assert g2 == 0 and w2 == 0, "second seed must add zero (DuplicateLoreId guard)"
    assert len(store) == size_after_first, (
        "re-seeding must not unboundedly grow the store — fragment ids are "
        "stable so duplicates are silently skipped"
    )


# ---------------------------------------------------------------------------
# (c) Behaviour-preserving extraction test — the shared seed_world_lore
#     helper seeds exactly what the old inline genre+world pair seeded
#     (no fresh-path regression).
# ---------------------------------------------------------------------------


def test_shared_helper_matches_inline_genre_plus_world_pair(caverns_pack: GenrePack) -> None:
    """``seed_world_lore`` must be behaviour-identical to the old inline
    ``seed_lore_from_genre_pack`` + ``seed_lore_from_world`` pair the
    fresh path used. Seed two stores — one via the extracted helper, one
    via the original primitives — and assert identical fragment ids.
    """
    from sidequest.game.lore_seeding import (
        seed_lore_from_genre_pack,
        seed_lore_from_world,
    )

    world_slug = _first_world_slug(caverns_pack)

    via_helper = LoreStore()
    helper_genre, helper_world = seed_world_lore(
        via_helper, caverns_pack, world_slug, emit=lambda **_: None
    )

    via_inline = LoreStore()
    inline_genre = seed_lore_from_genre_pack(via_inline, caverns_pack)
    world_obj = caverns_pack.worlds.get(world_slug)
    inline_world = (
        seed_lore_from_world(via_inline, world_obj.lore, world_slug) if world_obj is not None else 0
    )

    assert helper_genre == inline_genre
    assert helper_world == inline_world
    assert set(via_helper.fragments) == set(via_inline.fragments)


def test_shared_helper_no_world_obj_seeds_genre_only(caverns_pack: GenrePack) -> None:
    """When the world slug doesn't resolve to a world object, the helper
    must still seed genre lore and report zero world fragments — never
    silently no-op the whole seed (No Silent Fallbacks).
    """
    store = LoreStore()
    genre_added, world_added = seed_world_lore(
        store, caverns_pack, "nonexistent_world_slug", emit=lambda **_: None
    )
    assert genre_added >= 1
    assert world_added == 0
    assert "lore_genre_history" in store.fragments
