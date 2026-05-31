"""RED tests for story 74-1 — genre-tier flavor becomes world-tier/optional.

Epic 74 ("Genre tier = mechanics only", Keith 2026-05-30): genre packs hold
MECHANICS ONLY; all flavor (lore, cultures, archetypes, theme, visual_style,
audio, weather) lives in the WORLD, not the genre. Worlds diverge too hard for
shared genre flavor to be correct (spaghetti_western Mexican-border tropes are
wrong for 1878 Pittsburgh).

Today the genre-pack loader HARD-REQUIRES the genre-tier flavor files via
``_load_yaml(path / "X.yaml", ...)`` (loader.py:1125-1147) — deleting any raises
``GenreLoadError``. That mandatory load is the single blocker to the mechanics-
only end state. Story 74-1 makes genre flavor optional and the world tier
authoritative, with OTEL spans + wiring tests proving the world-tier loads fire.

These tests are written RED-first against the six acceptance criteria in
``sprint/context/context-story-74-1.md``. Authoritative spec:
``docs/genre-pack-content-audit.md``.

Fixture strategy — *relocate, don't fabricate*: the AC1/AC2/AC5 fixtures clone a
real live pack, copy its genre-tier ``theme``/``audio``/``visual_style`` DOWN into
the world (mirroring the real epic-74 migration), then delete the genre-tier
flavor files. This keeps the tests robust against the spec's open decision on
whether a moved world surface is *required* or merely *optional* ("move to world
tier (or genre-optional + world-required)", audit §"Server changes required") —
the world always supplies the flavor, so the only thing under test at the genre
tier is *absence tolerance*.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sidequest.game.lore_seeding import seed_world_lore
from sidequest.game.lore_store import LoreStore
from sidequest.game.world_grounding_bootstrap import load_world_grounding
from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content"
PACKS_ROOT = CONTENT_ROOT / "genre_packs"

# neon_dystopia is the lightest live pack with a single self-sufficient world
# (franchise_nations authors its own lore/cultures/archetypes/visual_style).
NEON_PACK = PACKS_ROOT / "neon_dystopia"
NEON_WORLD = "franchise_nations"

# The one shipped, schema-valid pack-tier weather.yaml — reused as a world-tier
# fixture for the weather-relocation ACs.
WEATHER_FIXTURE = PACKS_ROOT / "tea_and_murder" / "weather.yaml"

# The genre-tier flavor files that must become optional (AC1). visual_style is
# already optional in the loader today; the other five are mandatory loads.
GENRE_FLAVOR_FILES = (
    "lore.yaml",
    "cultures.yaml",
    "archetypes.yaml",
    "theme.yaml",
    "visual_style.yaml",
    "audio.yaml",
)

# Flavor that has no world-tier loader yet and which the neon world does not
# already author — these get relocated genre→world by the fixture.
RELOCATED_TO_WORLD = ("theme.yaml", "audio.yaml", "visual_style.yaml")

LIVE_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "heavy_metal",
    "mutant_wasteland",
    "neon_dystopia",
    "pulp_noir",
    "road_warrior",
    "space_opera",
    "spaghetti_western",
    "tea_and_murder",
]


# --------------------------------------------------------------------------- #
# Watcher-event capture (mirror tests/genre/test_world_items_loader.py)
# --------------------------------------------------------------------------- #


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.telemetry import watcher_hub as hub_mod

    monkeypatch.setattr(hub_mod, "publish_event", _capture)
    yield captured


def _loaded_fields(events: list[dict[str, Any]]) -> set[str | None]:
    """Set of ``field`` values across all ``state_transition`` loaded events."""
    return {
        e["fields"].get("field")
        for e in events
        if e["event_type"] == "state_transition" and e["fields"].get("op") == "loaded"
    }


# --------------------------------------------------------------------------- #
# Fixture builder — clone a live pack as "mechanics only" (flavor in the world)
# --------------------------------------------------------------------------- #


def _clone_pack_mechanics_only(src_pack: Path, dst_root: Path, world_slug: str) -> Path:
    """Clone ``src_pack`` into ``dst_root`` as a mechanics-only pack.

    1. copytree the pack (skipping heavy binary assets — the loader reads YAML).
    2. Relocate genre-tier ``theme``/``audio``/``visual_style`` DOWN into the
       named world (only when the world does not already author its own).
    3. Delete every genre-tier flavor file.

    The result is exactly the epic-74 target shape: genre tier carries no
    flavor, the world carries it. Returns the cloned pack directory.
    """
    pack_dst = dst_root / src_pack.name
    shutil.copytree(
        src_pack,
        pack_dst,
        ignore=shutil.ignore_patterns(
            "*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.ogg", "*.mp3", "*.wav", "__pycache__"
        ),
    )

    world_dst = pack_dst / "worlds" / world_slug
    if not world_dst.is_dir():
        raise AssertionError(f"fixture world {world_slug!r} missing in cloned pack {pack_dst}")

    for fname in RELOCATED_TO_WORLD:
        genre_file = pack_dst / fname
        world_file = world_dst / fname
        if genre_file.exists() and not world_file.exists():
            shutil.copy2(genre_file, world_file)

    for fname in GENRE_FLAVOR_FILES:
        f = pack_dst / fname
        if f.exists():
            f.unlink()

    return pack_dst


# --------------------------------------------------------------------------- #
# AC1 — genre-tier flavor loads are OPTIONAL
# --------------------------------------------------------------------------- #


def test_ac1_genre_pack_loads_without_any_genre_flavor(tmp_path: Path) -> None:
    """A pack whose root lacks lore/cultures/archetypes/theme/visual_style/audio
    loads without error and assembles a GenrePack with its world intact.

    RED today: ``load_genre_pack`` calls mandatory ``_load_yaml(path/'lore.yaml')``
    (loader.py:1125) and siblings, raising ``GenreLoadError`` on the deleted
    genre-tier flavor files.
    """
    pack_dir = _clone_pack_mechanics_only(NEON_PACK, tmp_path, NEON_WORLD)

    # Precondition: the genre tier really is flavor-free.
    for fname in GENRE_FLAVOR_FILES:
        assert not (pack_dir / fname).exists(), f"fixture left genre {fname} in place"

    pack = load_genre_pack(pack_dir)
    assert isinstance(pack, GenrePack)
    assert NEON_WORLD in pack.worlds, "world dropped when genre flavor went optional"


# --------------------------------------------------------------------------- #
# AC2 — world tier is AUTHORITATIVE and LOUD
# --------------------------------------------------------------------------- #


def test_ac2_world_is_authoritative_for_theme_and_audio(tmp_path: Path) -> None:
    """With genre flavor absent, the loaded World exposes theme + audio from the
    world tier. Genre supplies neither, so a non-None value can ONLY be
    world-sourced — that *is* the authoritative-world assertion.

    RED today: ``_load_single_world`` has no theme/audio loader, so the World
    object carries no ``theme``/``audio`` at all.
    """
    pack_dir = _clone_pack_mechanics_only(NEON_PACK, tmp_path, NEON_WORLD)
    pack = load_genre_pack(pack_dir)
    world = pack.worlds[NEON_WORLD]

    assert getattr(world, "theme", None) is not None, (
        "world-tier theme not loaded — World.theme must be populated from "
        "worlds/<slug>/theme.yaml"
    )
    assert getattr(world, "audio", None) is not None, (
        "world-tier audio not loaded — World.audio must be populated from "
        "worlds/<slug>/audio.yaml"
    )
    # visual_style already loads at the world tier today; assert it survives the
    # refactor (regression guard, not RED).
    assert getattr(world, "visual_style", None) is not None


def test_ac2_world_missing_required_surface_fails_loud(tmp_path: Path) -> None:
    """A world that supplies none of a now-world-authoritative surface fails
    loud at load, naming the world — not silently degrading (No Silent
    Fallbacks; mirror visibility_baseline/lethality_policy).

    Representative surface: ``theme`` (connect-time + reference-chrome need it;
    a themeless client is broken). See the Design Deviation logged in the
    session file — the spec leaves the exact required-surface set to Dev
    ("move to world tier (or genre-optional + world-required)"), so this test
    pins theme as the representative required surface and Dev confirms or
    adjusts during GREEN.

    RED today: deleting genre theme raises at the GENRE tier (loader.py:1126),
    so the error names the pack root, NOT the world. The ``world-scoped`` half
    of this assertion is what fails now and must pass once the loud-fail moves
    to the world tier.
    """
    pack_dir = _clone_pack_mechanics_only(NEON_PACK, tmp_path, NEON_WORLD)
    # Now neither tier supplies theme.
    (pack_dir / "worlds" / NEON_WORLD / "theme.yaml").unlink()

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack_dir)

    msg = str(exc_info.value)
    assert "theme" in msg.lower(), "loud-fail must name the missing surface"
    assert NEON_WORLD in msg, (
        "loud-fail must be world-scoped (name the world) — a genre-tier raise "
        "means the genre flavor load is still mandatory, not world-authoritative"
    )


# --------------------------------------------------------------------------- #
# AC3 — lore is WORLD-ONLY (genre lore no longer seeded)
# --------------------------------------------------------------------------- #


def test_ac3_genre_lore_no_longer_seeded() -> None:
    """The narrator's LoreStore for a world holds only world lore. Genre lore is
    no longer merged in.

    RED today: ``seed_world_lore`` calls ``seed_lore_from_genre_pack`` first
    (lore_seeding.py:220), so ``genre_added`` is > 0 for any pack with genre
    lore. neon_dystopia ships both genre lore and franchise_nations world lore.
    """
    pack = load_genre_pack(NEON_PACK)
    store = LoreStore()

    genre_added, world_added = seed_world_lore(store, pack, NEON_WORLD)

    assert genre_added == 0, (
        "genre lore must no longer be seeded — lore is world-only after epic 74"
    )
    assert world_added > 0, "world lore must still seed (franchise_nations ships lore)"


# --------------------------------------------------------------------------- #
# AC4 — weather reads the WORLD tier
# --------------------------------------------------------------------------- #


def _make_grounding_dirs(tmp_path: Path) -> tuple[Path, Path]:
    pack_dir = tmp_path / "pack"
    world_dir = pack_dir / "worlds" / "w"
    world_dir.mkdir(parents=True)
    return pack_dir, world_dir


def test_ac4_weather_loads_from_world_dir(tmp_path: Path) -> None:
    """``load_world_grounding`` reads ``world_dir/weather.yaml``. With weather
    present ONLY at the world tier, bootstrap produces a WeatherState.

    RED today: weather is read from ``pack_dir`` (world_grounding_bootstrap.py:111
    ``load_pack_weather(pack_dir)`` + WeatherGenerator on ``pack_dir/weather.yaml``),
    so a world-only weather file yields ``weather_state is None``.
    """
    _pack_dir, world_dir = _make_grounding_dirs(tmp_path)
    shutil.copy2(WEATHER_FIXTURE, world_dir / "weather.yaml")

    result = load_world_grounding(
        world_dir=world_dir,
        genre_slug="test_genre",
        seed_source="seed-74-1",
    )

    assert result.weather_state is not None, (
        "weather must load from world_dir/weather.yaml after the world-tier switch"
    )


def test_ac4_pack_root_weather_is_ignored(tmp_path: Path) -> None:
    """Pack-root weather is no longer consulted once weather is world-tier.

    RED today: weather IS read from pack root, so this returns a WeatherState
    instead of None.
    """
    pack_dir, world_dir = _make_grounding_dirs(tmp_path)
    shutil.copy2(WEATHER_FIXTURE, pack_dir / "weather.yaml")  # only at the pack tier

    result = load_world_grounding(
        world_dir=world_dir,
        genre_slug="test_genre",
        seed_source="seed-74-1",
    )

    assert result.weather_state is None, (
        "pack-root weather.yaml must be ignored — weather is world-tier after epic 74"
    )


# --------------------------------------------------------------------------- #
# AC5 — OTEL proves the world-tier flavor loads fired
# --------------------------------------------------------------------------- #


def test_ac5_world_flavor_loads_emit_otel_spans(
    tmp_path: Path, captured_events: list[dict[str, Any]]
) -> None:
    """Each world-tier flavor load emits a ``state_transition`` watcher event so
    the GM panel can prove world-tier loading actually fired (mirrors the
    existing ``world_items`` span). Expected field names follow the world_items
    convention: ``world_theme``, ``world_visual_style``, ``world_audio``.

    RED today: no world-tier flavor loaders exist, so no such spans are emitted.
    """
    pack_dir = _clone_pack_mechanics_only(NEON_PACK, tmp_path, NEON_WORLD)

    load_genre_pack(pack_dir)

    loaded = _loaded_fields(captured_events)
    for field in ("world_theme", "world_visual_style", "world_audio"):
        assert field in loaded, (
            f"missing world-tier OTEL span {field!r} — GM panel cannot prove the "
            f"world-tier load fired (spans seen: {sorted(f for f in loaded if f)})"
        )


# --------------------------------------------------------------------------- #
# AC6 — all 10 live packs still load (backward-compat regression guard)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pack_name", LIVE_PACKS)
def test_ac6_live_pack_still_loads(pack_name: str) -> None:
    """The refactor is backward-compatible until content actually moves: every
    live pack keeps its genre-tier flavor files for now and must keep loading.

    GREEN guard (not RED): passes today and must keep passing through GREEN —
    it catches the refactor breaking the still-flavored live packs.
    """
    pack = load_genre_pack(PACKS_ROOT / pack_name)
    assert isinstance(pack, GenrePack)
    assert len(pack.worlds) > 0, f"{pack_name} assembled zero worlds"
