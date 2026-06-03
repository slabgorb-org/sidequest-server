"""Story 74-4 AC1 — isinstance shape-guard on raw world theme/audio.

Epic 74 makes the world tier authoritative for flavor. ``_load_single_world``
loads ``worlds/<slug>/theme.yaml`` and ``audio.yaml`` RAW via
``_load_yaml_raw_optional`` (return type ``Any``) — see
``sidequest/genre/loader.py`` around the ``world_theme`` / ``world_audio`` seam.

Today a world that authors a *non-mapping* theme/audio file (a YAML list or a
bare scalar) flows that bad value straight into ``World(...)`` and surfaces an
OPAQUE ``pydantic.ValidationError`` deep in model construction — it does not name
the offending file or the world, so the operator gets the "why isn't this quite
right" debugging that the **No Silent Fallbacks** principle exists to prevent.

RED contract (this story):
  * a non-dict ``theme.yaml`` / ``audio.yaml`` fails LOUD with ``GenreLoadError``
    naming the offending file AND the world (mirroring the existing fail-loud
    shape guard in ``_parse_char_creation_scenes``);
  * an ABSENT or EMPTY file (``None``) stays valid — the genre tier is the
    transitional fallback — and must still load (the guard must not over-reject);
  * a well-formed mapping must still load unchanged.

Wiring: every case drives the real ``load_genre_pack`` path, not an isolated
helper, per CLAUDE.md "Every Test Suite Needs a Wiring Test". If the dev's guard
were removed, the non-dict cases would revert to the opaque pydantic error and
these tests would fail on the wrong exception type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import load_genre_pack


def _first_world_dir(pack_path: Path) -> Path:
    worlds = [p.parent for p in (pack_path / "worlds").glob("*/world.yaml")]
    assert worlds, "fixture pack must ship at least one world"
    return worlds[0]


# --------------------------------------------------------------------------- #
# RED — non-mapping theme/audio must fail loud and world-scoped
# --------------------------------------------------------------------------- #


def test_non_dict_world_theme_raises_genre_load_error(minimal_pack_factory, tmp_path: Path) -> None:
    """A YAML *list* is a valid document but the wrong SHAPE for a theme file."""
    pack = minimal_pack_factory(tmp_path)
    world = _first_world_dir(pack.path)
    (world / "theme.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack.path)

    msg = str(exc_info.value)
    assert "theme.yaml" in msg, f"loud-fail must name the offending file; got: {msg!r}"
    assert world.name in msg, f"loud-fail must be world-scoped; got: {msg!r}"


def test_scalar_world_theme_raises_genre_load_error(minimal_pack_factory, tmp_path: Path) -> None:
    """A bare scalar (string) theme.yaml is also the wrong shape."""
    pack = minimal_pack_factory(tmp_path)
    world = _first_world_dir(pack.path)
    (world / "theme.yaml").write_text("just-a-string\n", encoding="utf-8")

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack.path)
    assert "theme.yaml" in str(exc_info.value)


def test_non_dict_world_audio_raises_genre_load_error(minimal_pack_factory, tmp_path: Path) -> None:
    """The same shape guard applies to ``audio.yaml`` (also loaded raw)."""
    pack = minimal_pack_factory(tmp_path)
    world = _first_world_dir(pack.path)
    (world / "audio.yaml").write_text("- track_one\n- track_two\n", encoding="utf-8")

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack.path)

    msg = str(exc_info.value)
    assert "audio.yaml" in msg, f"loud-fail must name the offending file; got: {msg!r}"
    assert world.name in msg, f"loud-fail must be world-scoped; got: {msg!r}"


# --------------------------------------------------------------------------- #
# Guard must not over-reject — valid mapping and absent file still load
# --------------------------------------------------------------------------- #


def test_dict_world_theme_loads(minimal_pack_factory, tmp_path: Path) -> None:
    """A well-formed mapping theme.yaml loads and reaches ``World.theme``.

    Regression guard: proves the shape guard rejects *shape*, not *presence* —
    a valid world theme must still flow through to the consumer.
    """
    pack = minimal_pack_factory(tmp_path)
    world = _first_world_dir(pack.path)
    (world / "theme.yaml").write_text("palette: midnight\nfont: serif\n", encoding="utf-8")

    loaded = load_genre_pack(pack.path)
    world_obj = loaded.worlds[world.name]
    assert isinstance(world_obj.theme, dict), (
        "world-authored theme.yaml must reach World.theme as a raw dict (epic 74)"
    )
    assert world_obj.theme["palette"] == "midnight"


def test_absent_world_theme_audio_still_loads(minimal_pack_factory, tmp_path: Path) -> None:
    """No theme.yaml / audio.yaml authored → valid (genre fallback for theme,
    ``None`` for audio). The guard must NOT turn 'absent' into an error — an
    over-eager guard would itself violate the transitional fallback contract.
    """
    pack = minimal_pack_factory(tmp_path)
    world = _first_world_dir(pack.path)
    assert not (world / "theme.yaml").exists()
    assert not (world / "audio.yaml").exists()

    loaded = load_genre_pack(pack.path)  # must not raise
    assert world.name in loaded.worlds
