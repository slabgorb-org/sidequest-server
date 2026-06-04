"""Tests for ``sidequest.game.lore_seeding`` — Story 2.3 Slice F.

Covers ``seed_lore_from_char_creation`` fragment shape + id format
(Rust parity) and ``seed_lore_from_world`` against a real loaded pack.
The genre-pack seeder was removed in story 74-4 (lore is world-only,
epic 74), so there is no genre-tier seed left to exercise here.
"""

from __future__ import annotations

from sidequest.game.lore_seeding import (
    seed_lore_from_char_creation,
    seed_lore_from_world,
)
from sidequest.game.lore_store import (
    LoreCategory,
    LoreSource,
    LoreStore,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)


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
# seed_lore_from_world — pingpong 2026-04-30 (lore RAG returns empty)
#
# These exercise the world-scoped seeder against SYNTHETIC ``WorldLore``
# (story 74-5: no real-pack coupling). A world's ``lore.yaml`` is flavor and
# lives at the world tier (epic 74); the seeding LOGIC is genre-agnostic, so
# an in-test ``WorldLore`` is the correct fixture — the same pattern the two
# tests below (``..._slug_does_not_break_id`` / ``_idempotent_...``) already
# use. Synthetic lore makes every assertion deterministic — no skip-guards on
# "did the real pack happen to populate this field?".
# ---------------------------------------------------------------------------


class TestSeedFromWorld:
    """The world's ``lore.yaml`` carries history/geography/cosmology/factions
    distinct from any other world in the genre. These tests exercise the
    world-scoped variant of the seeder added by pingpong 2026-04-30 against a
    synthetic world so the fragment-id scoping and metadata contracts are
    pinned independent of shipping content."""

    @staticmethod
    def _world_lore():
        # All four seedable fields populated (history + geography + cosmology +
        # one faction) so the seeder yields exactly four world-scoped fragments
        # and every one of its field branches is exercised at the unit level.
        from sidequest.genre.models.lore import Faction, WorldLore

        return WorldLore(
            world_name="The Flickering Reach",
            history="Three wounds define the Reach; the black glass plain still hums.",
            geography="A continental interior scarred by a black glass plain and bone-wind canyons.",
            cosmology="The Drifters hear the Long Signal in the static of pre-war machines.",
            factions=[
                Faction(name="The Dome Syndicate", summary="water cartel", description="x"),
            ],
        )

    def test_world_lore_seeded_with_world_scoped_ids(self) -> None:
        world_slug = "flickering_reach"
        store = LoreStore()
        added = seed_lore_from_world(store, self._world_lore(), world_slug)
        # history + geography + cosmology + one faction → four fragments.
        assert added == 4
        # EVERY id must be world-scoped so a future world swap doesn't leak the
        # prior world's lore into the new world's RAG queries. `all`, not `any`:
        # the assertion message is universal, so one correctly-scoped id is not
        # enough — a single mis-scoped fragment must fail this test.
        assert all(fid.startswith(f"lore_world_{world_slug}_") for fid in store.fragments), (
            f"World seeder must scope fragment ids by world_slug "
            f"({world_slug!r}); got: {list(store.fragments)}"
        )

    def test_world_lore_carries_world_slug_metadata(self) -> None:
        world_slug = "flickering_reach"
        store = LoreStore()
        added = seed_lore_from_world(store, self._world_lore(), world_slug)
        assert added == 4
        for frag in store.fragments.values():
            assert frag.metadata.get("world_slug") == world_slug, (
                "Every world-seeded fragment must carry world_slug metadata "
                "so future cross-world queries can filter by world without "
                "re-parsing the fragment id."
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


def test_websocket_session_handler_imports_world_seeder() -> None:
    """Production wiring guard: the chargen-confirmation hook in
    ``chargen_mixin.py`` must reach the world-lore seeder. This is a
    reflection-based import-contract test (the legitimate exception to the
    no-source-text-wiring rule) — if a future refactor removes the wiring,
    this fails before runtime ever encounters a session with empty lore
    (the pingpong 2026-04-30 symptom).

    Epic 74: lore is world-only. The fresh chargen path AND the slug-resume
    connect path both go through the single shared ``seed_world_lore`` helper
    (DRY), which seeds WORLD lore only — genre lore is no longer seeded. The
    behavioural guarantee (``genre_added == 0``, world lore present) is pinned
    by ``test_genre_flavor_world_tier.py::test_ac3_genre_lore_no_longer_seeded``
    and the resume-reseed tests; this test guards only the import wiring.
    """
    import sidequest.server.websocket_handlers.chargen_mixin as wsh

    assert hasattr(wsh, "seed_world_lore"), (
        "chargen_mixin must import seed_world_lore — the shared world-lore "
        "seeding helper the chargen-confirm path uses to seed the lore corpus "
        "into the per-session lore store. The helper backs BOTH the fresh and "
        "the slug-resume paths so a resumed save's query_lore no longer "
        "returns hit_count=0."
    )
