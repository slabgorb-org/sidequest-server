"""Tests for ``sidequest.game.lore_seeding`` — Story 2.3 Slice F.

Covers ``seed_lore_from_char_creation`` fragment shape + id format
(Rust parity) and ``seed_lore_from_genre_pack`` against a real loaded
pack — no synthetic GenrePack fixtures, since the aggregate root is
wide and the seeding helpers only read ``pack.lore``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.lore_seeding import (
    seed_lore_from_char_creation,
    seed_lore_from_genre_pack,
    seed_lore_from_world,
)
from sidequest.game.lore_store import (
    LoreCategory,
    LoreSource,
    LoreStore,
)
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.pack import GenrePack

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _choice(label: str, description: str) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(),
    )


def _scene(scene_id: str, choices: list[CharCreationChoice]) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=scene_id.replace("_", " ").title(),
        narration="prompt",
        choices=choices,
    )


# ---------------------------------------------------------------------------
# seed_lore_from_char_creation
# ---------------------------------------------------------------------------


class TestSeedFromCharCreation:
    def test_one_fragment_per_choice(self) -> None:
        store = LoreStore()
        scenes = [
            _scene(
                "origin",
                [
                    _choice("Exile", "Driven from home by famine."),
                    _choice("Hunter", "Raised among the marsh folk."),
                ],
            ),
            _scene(
                "vow",
                [_choice("Never again", "A promise made to the dead.")],
            ),
        ]
        added = seed_lore_from_char_creation(store, scenes)
        assert added == 3
        assert len(store) == 3

    def test_fragment_id_matches_rust_format(self) -> None:
        store = LoreStore()
        scenes = [
            _scene(
                "origin",
                [
                    _choice("Exile", "a"),
                    _choice("Hunter", "b"),
                ],
            ),
        ]
        seed_lore_from_char_creation(store, scenes)
        assert "lore_char_creation_origin_0" in store.fragments
        assert "lore_char_creation_origin_1" in store.fragments

    def test_fragment_shape(self) -> None:
        store = LoreStore()
        scenes = [_scene("origin", [_choice("Exile", "Driven from home.")])]
        seed_lore_from_char_creation(store, scenes)
        frag = store.fragments["lore_char_creation_origin_0"]
        assert frag.category == LoreCategory.Character
        assert frag.source == LoreSource.CharacterCreation
        assert frag.content == "Exile: Driven from home."
        assert frag.metadata == {
            "scene_id": "origin",
            "choice_index": "0",
            "choice_label": "Exile",
        }

    def test_scene_with_no_choices_produces_nothing(self) -> None:
        store = LoreStore()
        scenes = [_scene("display_only", [])]
        added = seed_lore_from_char_creation(store, scenes)
        assert added == 0
        assert store.is_empty()

    def test_empty_scene_list_is_noop(self) -> None:
        store = LoreStore()
        added = seed_lore_from_char_creation(store, [])
        assert added == 0

    def test_duplicate_ids_are_skipped_not_raised(self) -> None:
        """Re-seeding after a reconnect must not hard-fail. Rust uses
        ``if store.add(frag).is_ok()`` so duplicates silently drop.
        The Python port matches that contract via DuplicateLoreId catch."""
        store = LoreStore()
        scenes = [_scene("origin", [_choice("Exile", "a")])]
        first = seed_lore_from_char_creation(store, scenes)
        second = seed_lore_from_char_creation(store, scenes)
        assert first == 1
        assert second == 0
        assert len(store) == 1


# ---------------------------------------------------------------------------
# seed_lore_from_genre_pack — real caverns pack
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def caverns_pack() -> GenrePack:
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


class TestSeedFromGenrePack:
    def test_adds_history_geography_cosmology_and_factions(self, caverns_pack: GenrePack) -> None:
        store = LoreStore()
        added = seed_lore_from_genre_pack(store, caverns_pack)
        # caverns has non-empty history/geography/cosmology + multiple factions.
        assert added >= 3
        assert "lore_genre_history" in store.fragments
        assert "lore_genre_geography" in store.fragments
        assert "lore_genre_cosmology" in store.fragments

    def test_cosmology_bucketed_as_history(self, caverns_pack: GenrePack) -> None:
        store = LoreStore()
        seed_lore_from_genre_pack(store, caverns_pack)
        cosmology = store.fragments["lore_genre_cosmology"]
        # Rust bucket cosmology → History (seeding.rs line 47).
        assert cosmology.category == LoreCategory.History

    def test_faction_fragment_carries_name_metadata(self, caverns_pack: GenrePack) -> None:
        store = LoreStore()
        seed_lore_from_genre_pack(store, caverns_pack)
        faction_frags = store.query_by_category(LoreCategory.Faction)
        if not faction_frags:
            pytest.skip("pack has no factions")
        frag = faction_frags[0]
        assert "faction_name" in frag.metadata
        assert frag.metadata["faction_name"]

    def test_idempotent_second_call_adds_nothing(self, caverns_pack: GenrePack) -> None:
        store = LoreStore()
        first = seed_lore_from_genre_pack(store, caverns_pack)
        second = seed_lore_from_genre_pack(store, caverns_pack)
        assert first > 0
        assert second == 0


# ---------------------------------------------------------------------------
# seed_lore_from_world — pingpong 2026-04-30 (lore RAG returns empty)
# ---------------------------------------------------------------------------


class TestSeedFromWorld:
    """The world's ``lore.yaml`` overrides genre defaults for a specific
    world (e.g. ``coyote_star`` has its own history/geography distinct
    from ``space_opera``'s genre-level lore). These tests exercise the
    world-scoped variant of the seeder added by pingpong 2026-04-30."""

    def test_world_lore_seeded_with_world_scoped_ids(self, caverns_pack: GenrePack) -> None:
        worlds = caverns_pack.worlds
        if not worlds:
            pytest.skip("caverns pack has no worlds — cannot exercise world seed")
        world_slug, world = next(iter(worlds.items()))
        store = LoreStore()
        added = seed_lore_from_world(store, world.lore, world_slug)
        # Worlds have at minimum a history string in shipping content.
        if added == 0:
            pytest.skip(f"world {world_slug!r} has no populated lore fields")
        # Ids must be world-scoped so a future world swap doesn't leak
        # the prior world's lore into the new world's RAG queries.
        assert any(fid.startswith(f"lore_world_{world_slug}_") for fid in store.fragments), (
            f"World seeder must scope fragment ids by world_slug "
            f"({world_slug!r}); got: {list(store.fragments)}"
        )

    def test_world_lore_carries_world_slug_metadata(self, caverns_pack: GenrePack) -> None:
        worlds = caverns_pack.worlds
        if not worlds:
            pytest.skip("caverns pack has no worlds")
        world_slug, world = next(iter(worlds.items()))
        store = LoreStore()
        added = seed_lore_from_world(store, world.lore, world_slug)
        if added == 0:
            pytest.skip(f"world {world_slug!r} has no populated lore fields")
        for frag in store.fragments.values():
            assert frag.metadata.get("world_slug") == world_slug, (
                "Every world-seeded fragment must carry world_slug metadata "
                "so future cross-world queries can filter by world without "
                "re-parsing the fragment id."
            )

    def test_world_seed_does_not_collide_with_genre_seed(self, caverns_pack: GenrePack) -> None:
        """Wiring contract: in production both seeders run against the
        same store. The genre seeder uses ``lore_genre_*`` ids; the
        world seeder uses ``lore_world_<slug>_*``. They must NOT
        collide on shared topics like 'history' — pre-fix the bug
        report would suggest both were silent, but a future regression
        could collide ids and silently drop world lore as a duplicate.
        """
        worlds = caverns_pack.worlds
        if not worlds:
            pytest.skip("caverns pack has no worlds")
        world_slug, world = next(iter(worlds.items()))
        store = LoreStore()
        genre_added = seed_lore_from_genre_pack(store, caverns_pack)
        world_added = seed_lore_from_world(store, world.lore, world_slug)
        # If world has ANY populated field, total must equal sum (no
        # silent dedup against the genre layer).
        assert genre_added >= 1
        assert len(store) == genre_added + world_added, (
            "Genre and world seed ids must not collide — len(store) must "
            "equal the sum of fragments_added across both seeders."
        )

    def test_unicode_or_uppercase_world_slug_does_not_break_id(self) -> None:
        from sidequest.genre.models.lore import Faction, WorldLore

        lore = WorldLore(
            world_name="Test",
            history="A single line.",
            factions=[Faction(name="The Corp", summary="x", description="y")],
        )
        store = LoreStore()
        added = seed_lore_from_world(store, lore, "Coyote Star")
        # Two fragments expected (history + faction).
        assert added == 2
        # Slug-normalized: "Coyote Star" → "coyote_star".
        assert "lore_world_coyote_star_history" in store.fragments
        assert "lore_world_coyote_star_faction_the_corp" in store.fragments

    def test_idempotent_second_call_adds_nothing(self) -> None:
        from sidequest.genre.models.lore import WorldLore

        lore = WorldLore(world_name="X", history="story")
        store = LoreStore()
        first = seed_lore_from_world(store, lore, "x_world")
        second = seed_lore_from_world(store, lore, "x_world")
        assert first == 1
        assert second == 0


# ---------------------------------------------------------------------------
# Wiring contract — pingpong 2026-04-30
# CLAUDE.md "Every Test Suite Needs a Wiring Test": prove the seeders
# are reachable from the production chargen-confirmation path. Pre-fix
# the genre/world seeders existed and were unit-tested but had ZERO
# production callers — exactly the half-wired-feature gap CLAUDE.md
# warns about.
# ---------------------------------------------------------------------------


def test_websocket_session_handler_imports_genre_and_world_seeders() -> None:
    """Production wiring guard: the chargen-confirmation hook in
    ``websocket_session_handler.py`` must reach the genre+world seeders.
    This is a static-import contract test — if a future refactor removes
    the wiring, this fails before runtime ever encounters a session with
    empty lore (the pingpong 2026-04-30 symptom).

    Post slug-resume re-seed fix: the fresh chargen path no longer calls
    ``seed_lore_from_genre_pack``/``seed_lore_from_world`` inline — both
    the fresh path AND the slug-resume connect path now go through the
    single shared ``seed_world_lore`` helper (DRY). The wiring guarantee
    is therefore: the handler imports ``seed_world_lore`` (which itself
    fans out to the genre + world seeders).
    """
    import sidequest.server.websocket_session_handler as wsh

    assert hasattr(wsh, "seed_world_lore"), (
        "websocket_session_handler must import seed_world_lore — the "
        "shared genre+world seeding helper the chargen-confirm path uses "
        "to seed the lore corpus into the per-session lore store. Pre-fix "
        "the genre/world seeding was inline; the helper now backs BOTH "
        "the fresh and the slug-resume paths so a resumed save's "
        "query_lore no longer returns hit_count=0."
    )
    # The shared helper must itself fan out to the genre + world
    # seeders — guards against a refactor that hollows seed_world_lore.
    import inspect

    from sidequest.game import lore_seeding

    src = inspect.getsource(lore_seeding.seed_world_lore)
    assert "seed_lore_from_genre_pack" in src, (
        "seed_world_lore must call seed_lore_from_genre_pack so the genre "
        "lore corpus reaches the per-session store"
    )
    assert "seed_lore_from_world" in src, (
        "seed_world_lore must call seed_lore_from_world so world-level "
        "history/geography overrides reach the narrator's RAG retrieval"
    )
