"""char_creation.yaml must load loud, not silently degrade to ``[]``.

Regression for the playtest finding that ``the_real_mccoy`` showed
desert-western *genre* chargen scenes and never its authored Pittsburgh
``origins`` scene. Root cause: its ``char_creation.yaml`` was a MAPPING
(``party_size`` / ``inherits_scenes_from_genre_pack`` / ``scenes``), but
the loader only parsed a bare LIST and silently coerced anything else to
``[]`` (``isinstance(raw, list) else []``). An empty world char_creation
then fell through to the genre's scenes — a No Silent Fallbacks violation.

Contract under test (``char_creation_resolve.py``): a world's
char_creation.yaml is a BARE LIST of scenes that REPLACES the genre's
wholesale — there is no per-scene merge, and ``inherits_scenes_from_genre_pack``
is not a supported key. ``None`` (absent/empty file) means "inherit the
genre's scenes" → ``[]``. Any other shape is an authoring error → fail loud.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import _load_single_world, _parse_char_creation_scenes

# ---------------------------------------------------------------------------
# Unit: the shared parse/validate helper.
# ---------------------------------------------------------------------------

_VALID_SCENE = {
    "id": "origins",
    "title": "Where Did You Come From?",
    "narration": "Gaslight warm on mahogany.",
}


def test_parse_none_inherits_genre_returns_empty() -> None:
    """Absent/empty char_creation.yaml → [] (world inherits genre scenes)."""
    assert _parse_char_creation_scenes(None, path=Path("char_creation.yaml")) == []


def test_parse_bare_list_returns_scenes() -> None:
    """A bare list is the supported shape and parses into scenes."""
    scenes = _parse_char_creation_scenes([_VALID_SCENE], path=Path("char_creation.yaml"))
    assert [s.id for s in scenes] == ["origins"]


def test_parse_mapping_fails_loud() -> None:
    """A mapping (the unsupported inherits_scenes_from_genre_pack wrapper) must
    raise — never silently degrade to []."""
    payload = {
        "party_size": {"min": 1, "max": 4},
        "inherits_scenes_from_genre_pack": ["crucible", "the_ride"],
        "scenes": [_VALID_SCENE],
    }
    with pytest.raises(GenreLoadError) as exc:
        _parse_char_creation_scenes(payload, path=Path("the_real_mccoy/char_creation.yaml"))
    msg = str(exc.value)
    assert "bare list" in msg
    assert "inherits_scenes_from_genre_pack" in msg


def test_parse_scalar_fails_loud() -> None:
    """A scalar payload is also an authoring error → loud."""
    with pytest.raises(GenreLoadError):
        _parse_char_creation_scenes("nope", path=Path("char_creation.yaml"))


# ---------------------------------------------------------------------------
# Wiring: the world loader uses the helper end-to-end.
# ---------------------------------------------------------------------------

_WORLD_YAML = textwrap.dedent(
    """\
    name: Testworld
    description: Synthetic test world for char_creation loud-load.
    starting_location: testtown
    """
)
_LORE_YAML = textwrap.dedent(
    """\
    world_name: Testworld
    history: A brief history of testing.
    geography: Flat. Featureless. Test-shaped.
    cosmology: Two suns, no moons, deterministic stars.
    """
)
_CARTOGRAPHY_YAML = textwrap.dedent(
    """\
    world_name: Testworld
    starting_region: testtown
    navigation_mode: region
    regions:
      testtown:
        name: Testtown
        summary: A region for tests.
        description: A flat plain with one inn and a notional river.
        terrain: settlement
        adjacent: []
    """
)
_OPENINGS_YAML = textwrap.dedent(
    """\
    version: "1.0.0"
    world: testworld
    genre: testgenre
    openings:
      - id: solo_default
        triggers:
          mode: either
          min_players: 1
          max_players: 6
          backgrounds: []
        setting:
          location_label: testtown
          situation: Standing in the square at noon.
        establishing_narration: |
          The square is empty. The sun is high. You stand alone.
    """
)


def _make_world_tree(tmp_path: Path) -> tuple[Path, Path]:
    genre_root = tmp_path / "genre"
    world_path = genre_root / "worlds" / "testworld"
    world_path.mkdir(parents=True)
    (world_path / "world.yaml").write_text(_WORLD_YAML, encoding="utf-8")
    (world_path / "lore.yaml").write_text(_LORE_YAML, encoding="utf-8")
    (world_path / "cartography.yaml").write_text(_CARTOGRAPHY_YAML, encoding="utf-8")
    (world_path / "openings.yaml").write_text(_OPENINGS_YAML, encoding="utf-8")
    return genre_root, world_path


def test_world_without_char_creation_inherits_genre(tmp_path: Path) -> None:
    """No char_creation.yaml → World.char_creation == [] (inherit genre)."""
    genre_root, world_path = _make_world_tree(tmp_path)

    world = _load_single_world(world_path, [], genre_root)

    assert world.char_creation == []


def test_world_char_creation_list_parses(tmp_path: Path) -> None:
    """A bare-list char_creation.yaml parses onto the World model."""
    genre_root, world_path = _make_world_tree(tmp_path)
    (world_path / "char_creation.yaml").write_text(
        textwrap.dedent(
            """\
            - id: origins
              title: Where Did You Come From?
              narration: Gaslight warm on mahogany.
            """
        ),
        encoding="utf-8",
    )

    world = _load_single_world(world_path, [], genre_root)

    assert [s.id for s in world.char_creation] == ["origins"]


def test_world_char_creation_mapping_fails_loud(tmp_path: Path) -> None:
    """The real mccoy repro: a mapping-shaped char_creation.yaml fails loud
    instead of silently degrading to genre scenes."""
    genre_root, world_path = _make_world_tree(tmp_path)
    (world_path / "char_creation.yaml").write_text(
        textwrap.dedent(
            """\
            party_size:
              min: 1
              max: 4
            inherits_scenes_from_genre_pack:
              - crucible
              - the_ride
            scenes:
              - id: origins
                title: Where Did You Come From?
                narration: Gaslight warm on mahogany.
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(GenreLoadError):
        _load_single_world(world_path, [], genre_root)
