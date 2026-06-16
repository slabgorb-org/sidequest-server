"""Tests for ``resolve_char_creation_scenes`` and the connect-handler wiring.

Story 45-NN: ``World.char_creation`` was loaded by the genre loader and
present on the model but never read — the two ``CharacterBuilder``
construction sites in ``handlers/connect.py`` consulted only
``GenrePack.char_creation``. Per-world chargen overrides were dead
wiring. These tests cover the resolver semantics (replace, not merge)
and prove the connect handler reaches the resolver end-to-end.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import cast

import sidequest.genre.loader as _genre_loader_mod
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.pack import GenrePack, World
from sidequest.protocol.messages import SessionEventMessage, SessionEventPayload
from sidequest.server.dispatch.char_creation_resolve import (
    resolve_char_creation_scenes,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import mock_claude_client_factory

# ---------------------------------------------------------------------------
# Helpers — minimal scene/world/pack builders
# ---------------------------------------------------------------------------


def _scene(scene_id: str, title: str = "Scene") -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=title,
        narration="...",
        choices=[
            CharCreationChoice(
                label="continue",
                description="press on",
                mechanical_effects=MechanicalEffects(),
            )
        ],
    )


def _make_pack(
    *,
    genre_scenes: list[CharCreationScene],
    worlds: dict[str, list[CharCreationScene]],
) -> GenrePack:
    """Build a minimal-but-valid GenrePack stub for resolver testing.

    The resolver only reads ``pack.char_creation`` and
    ``pack.worlds[slug].char_creation`` — every other field is ignored.
    Pydantic still requires required fields (rules, lore, theme, etc.)
    for full validation, so we construct via ``model_construct`` to
    skip validation; tests that need a real loaded pack should use
    fixture content instead.
    """
    world_objs: dict[str, World] = {}
    for slug, scenes in worlds.items():
        world_objs[slug] = cast(
            World,
            World.model_construct(char_creation=scenes),
        )
    return cast(
        GenrePack,
        GenrePack.model_construct(
            char_creation=genre_scenes,
            worlds=world_objs,
        ),
    )


# ---------------------------------------------------------------------------
# Unit tests for resolve_char_creation_scenes
# ---------------------------------------------------------------------------


class TestResolveCharCreationScenes:
    def test_world_override_replaces_genre_scenes(self) -> None:
        genre = [_scene("g1"), _scene("g2")]
        world = [_scene("w1")]
        pack = _make_pack(genre_scenes=genre, worlds={"shire": world})

        result = resolve_char_creation_scenes(pack, "shire")

        assert [s.id for s in result] == ["w1"], (
            "world override must replace, not merge with, the genre scenes"
        )

    def test_world_without_override_falls_back_to_genre(self) -> None:
        genre = [_scene("g1"), _scene("g2")]
        # World exists in pack but has empty char_creation.
        pack = _make_pack(genre_scenes=genre, worlds={"shire": []})

        result = resolve_char_creation_scenes(pack, "shire")

        assert [s.id for s in result] == ["g1", "g2"]

    def test_missing_world_slug_returns_genre_scenes(self) -> None:
        genre = [_scene("g1")]
        pack = _make_pack(
            genre_scenes=genre,
            worlds={"shire": [_scene("w1")]},  # exists but unused
        )

        # None — no world selected (e.g. not yet bound).
        assert [s.id for s in resolve_char_creation_scenes(pack, None)] == ["g1"]
        # Empty string — same fall-through.
        assert [s.id for s in resolve_char_creation_scenes(pack, "")] == ["g1"]

    def test_unknown_world_returns_genre_scenes(self) -> None:
        genre = [_scene("g1")]
        pack = _make_pack(
            genre_scenes=genre,
            worlds={"shire": [_scene("w1")]},
        )

        result = resolve_char_creation_scenes(pack, "mordor")

        assert [s.id for s in result] == ["g1"], (
            "unknown world must fall through to genre, not raise"
        )

    def test_returns_fresh_list_callers_can_mutate(self) -> None:
        genre = [_scene("g1")]
        pack = _make_pack(genre_scenes=genre, worlds={})

        result = resolve_char_creation_scenes(pack, None)
        result.append(_scene("dirty"))

        # Pack's underlying list must not be mutated.
        assert [s.id for s in pack.char_creation] == ["g1"]

    def test_returns_fresh_list_from_world_override(self) -> None:
        world_scenes = [_scene("w1")]
        pack = _make_pack(genre_scenes=[], worlds={"shire": world_scenes})

        result = resolve_char_creation_scenes(pack, "shire")
        result.clear()

        # World's underlying list must not be mutated.
        assert [s.id for s in pack.worlds["shire"].char_creation] == ["w1"]

    def test_both_tiers_empty_returns_empty_list(self) -> None:
        pack = _make_pack(genre_scenes=[], worlds={"shire": []})

        assert resolve_char_creation_scenes(pack, "shire") == []
        assert resolve_char_creation_scenes(pack, None) == []


# ---------------------------------------------------------------------------
# Wiring test — connect handler must read the resolver, not pack directly
# ---------------------------------------------------------------------------


_FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


class TestConnectHandlerWiresWorldOverride:
    """Prove the connect path picks up world chargen overrides.

    Copies the ``space_opera`` fixture pack into ``tmp_path`` and writes
    a ``char_creation.yaml`` into the ``flickering_reach`` world (which
    has none in the fixture). After driving a connect through the real
    handler, the builder seated on ``_session_data.builder`` must
    expose the world override scenes — not the genre scenes.

    If this test starts failing because someone added a real
    ``char_creation.yaml`` to the world fixture, update the world slug
    to one without an authored override, or add a fresh world
    sub-directory inside the copy. Do **not** weaken the assertion.
    """

    # The shared test-fixture pack ("test_genre") is what the
    # space_opera/heavy_metal/etc. fixture symlinks all resolve to.
    # Using the underlying name keeps lethality_policy's genre_key check
    # happy after we copy the pack into tmp_path (the copy is materialised,
    # so the symlink target is no longer reachable).
    GENRE_SLUG = "test_genre"
    WORLD_SLUG = "flickering_reach"

    def _copy_fixture_pack(self, dst_root: Path) -> Path:
        src = _FIXTURE_PACKS_DIR / self.GENRE_SLUG
        if not src.is_dir():
            raise RuntimeError(f"fixture pack missing at {src} — test fixture drift")
        dst = dst_root / self.GENRE_SLUG
        shutil.copytree(src, dst)
        return dst

    def _add_world_override(self, pack_dir: Path) -> None:
        world_dir = pack_dir / "worlds" / self.WORLD_SLUG
        if not world_dir.is_dir():
            raise RuntimeError(f"fixture world missing at {world_dir} — test fixture drift")
        target = world_dir / "char_creation.yaml"
        if target.exists():
            raise RuntimeError(
                f"world fixture {target} already has char_creation.yaml; "
                "update this test to use a world without a real override"
            )
        target.write_text(
            "- id: WORLD_OVERRIDE_SCENE\n"
            "  title: World Override\n"
            "  narration: world-tier scene authored by the wiring test\n"
            "  choices:\n"
            "    - label: continue\n"
            "      description: press on\n"
            "      mechanical_effects: {}\n"
        )

    def test_connect_picks_up_world_override(self, tmp_path: Path, monkeypatch) -> None:
        packs_root = tmp_path / "packs"
        packs_root.mkdir()
        pack_dir = self._copy_fixture_pack(packs_root)

        # Capture the genre-tier scene ids BEFORE we drive connect so
        # the assertion can't accidentally tautologise (the override id
        # must not coincidentally appear in the genre-tier list).
        original_pack = _genre_loader_mod.load_genre_pack(pack_dir)
        genre_scene_ids = [s.id for s in original_pack.char_creation]
        assert genre_scene_ids, (
            "fixture drift: space_opera genre-tier char_creation is empty; "
            "test cannot distinguish genre vs world tier"
        )
        assert "WORLD_OVERRIDE_SCENE" not in genre_scene_ids, (
            "fixture drift: genre tier already has WORLD_OVERRIDE_SCENE id"
        )

        # Now write the override and bust the conftest pack cache so
        # the connect handler re-reads from disk.
        self._add_world_override(pack_dir)
        # Conftest caches GenreLoader.load() per (code, search_paths).
        # We use a unique tmp path so the cache key is fresh and we
        # avoid clobbering any other test's entry.

        handler = WebSocketSessionHandler(
            claude_client_factory=mock_claude_client_factory(),
            genre_pack_search_paths=[packs_root],
            save_dir=tmp_path / "saves",
        )

        from tests.server.conftest import attach_default_room_context, seed_slug_for_test

        slug = seed_slug_for_test(tmp_path / "saves", genre=self.GENRE_SLUG, world=self.WORLD_SLUG)
        attach_default_room_context(handler)

        async def body() -> None:
            await handler.handle_message(
                SessionEventMessage(
                    payload=SessionEventPayload(
                        event="connect",
                        player_name="WiringProbe",
                        game_slug=slug,
                    ),
                    player_id="",
                )
            )

        asyncio.run(body())

        sd = handler._session_data  # type: ignore[attr-defined]
        assert sd is not None, "connect did not initialise session data"
        assert sd.builder is not None, (
            "chargen builder missing — connect did not initialise builder "
            "despite world having char_creation scenes"
        )

        scene_ids = [s.id for s in sd.builder.scenes()]
        assert scene_ids == ["WORLD_OVERRIDE_SCENE"], (
            f"connect did not read world override; got {scene_ids}. "
            "The genre-tier scenes leaked through, which means "
            "resolve_char_creation_scenes is not wired into the connect "
            "handler."
        )
        # Belt-and-suspenders: none of the genre-tier ids may appear.
        for gid in genre_scene_ids:
            assert gid not in scene_ids, (
                f"genre-tier scene {gid!r} leaked into builder despite world override"
            )
