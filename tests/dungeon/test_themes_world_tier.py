"""RED (story 113-1, ADR-140): the dungeon theme palette is WORLD-tier content.

`themes/` is single-world (beneath_sunden) dungeon-palette content that today
sits at the genre-pack ROOT and is resolved as ``world_dir.parent.parent``. Per
ADR-140 (the genre tier is the rulebook only; the world owns cast and catalog)
it relocates to ``worlds/beneath_sunden/themes/`` — a COUPLED content+server
change: the content move (a ``git mv``) and the four server resolution sites
must land together, or ``load_theme_palette`` raises ``ThemePaletteMissingError``
and every beneath_sunden session crashes on dungeon attach.

These tests pin the post-move contract: theme palettes resolve from the WORLD
directory, not the genre-pack root. They fail RED against the current
root-resolution and pass once the call sites repoint AND the content moves.

The loader itself (``load_theme_palette`` reads ``<dir>/themes/*.yaml``) is
UNCHANGED — only *which directory* is passed changes. So the loader unit suite
(``test_themes.py``) and the owned-fixture wiring suite (``test_themes_wiring.py``)
need no edits. The behavioral guard lives here (synthetic fixtures, no DB) plus
the real-attach update in ``test_region_projection_wiring.py``.

If Dev inlines/removes ``session_integration._theme_pack_root`` (the AC permits
"_theme_pack_root *or its callers*"), update the two seam-named tests to assert
the inlined call resolves ``load_theme_palette`` to the world dir.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

# A self-contained, closed-adjacency palette (prefers/avoids empty) so a
# single-file themes/ dir loads cleanly. generator_class↔algorithm family
# (structured↔prim) matches the schema invariant exercised in test_themes.py.
_VALID_THEME = """
id: bone_crypt
display_name: The Bone Crypt
generator_class: structured
interior: {algorithm: prim, braid_ratio: 0.3}
depth_band: {min: 0.0, max: 120.0}
narrator:
  register: grave
  flavor: Dust that remembers names.
  motifs: [ossuary]
adjacency: {prefers: [], avoids: []}
set_pieces:
  - id: false_floor
    name: The False Floor
    telegraph: Newer mortar rings hollow underfoot.
    outcome: The slab drops onto upturned stakes.
"""

# Same palette with an unknown interior algorithm — load_theme_palette wraps
# the pydantic failure as ValueError("<file>: ..."), which the pack validator
# surfaces as a palette-validation error.
_BROKEN_THEME = _VALID_THEME.replace("algorithm: prim", "algorithm: voronoi")


def _write_theme(themes_dir: Path, name: str, body: str) -> None:
    themes_dir.mkdir(parents=True, exist_ok=True)
    (themes_dir / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def _theme_yaml(theme_id: str) -> str:
    return _VALID_THEME.replace("id: bone_crypt", f"id: {theme_id}")


def _world_tier_pack(tmp_path: Path, *, theme_body: str = _VALID_THEME) -> Path:
    """Build a synthetic POST-MOVE pack: themes live ONLY under
    ``worlds/beneath_sunden/themes/``; the old genre-root ``themes/`` is
    deliberately absent. Returns the world dir."""
    world_dir = tmp_path / "genre_packs" / "caverns_and_claudes" / "worlds" / "beneath_sunden"
    _write_theme(world_dir / "themes", "bone_crypt.yaml", theme_body)
    return world_dir


# ---------------------------------------------------------------------------
# AC1 — the resolver points at the world dir, not the genre-pack root.
# ---------------------------------------------------------------------------


def test_theme_pack_root_resolves_to_world_dir(tmp_path: Path) -> None:
    """ADR-140: themes are world-tier, so the resolver must return the world
    dir itself — not ``world_dir.parent.parent`` (the genre-pack root)."""
    from sidequest.dungeon.session_integration import _theme_pack_root

    world_dir = tmp_path / "genre_packs" / "caverns_and_claudes" / "worlds" / "beneath_sunden"
    assert _theme_pack_root(world_dir) == world_dir


def test_world_tier_palette_loads_via_resolver_and_genre_root_is_empty(
    tmp_path: Path,
) -> None:
    """The production resolution must load themes that exist ONLY at the world
    tier, and the old genre-root location must no longer be consulted."""
    from sidequest.dungeon.session_integration import _theme_pack_root
    from sidequest.dungeon.themes import ThemePaletteMissingError, load_theme_palette

    world_dir = _world_tier_pack(tmp_path)

    palette = load_theme_palette(_theme_pack_root(world_dir))
    assert set(palette.themes) == {"bone_crypt"}

    # The old genre-root path is intentionally empty — proving resolution moved
    # off the pack root and onto the world dir (No Silent Fallbacks: a stray
    # root themes/ must not be what satisfies the loader).
    genre_root = world_dir.parent.parent
    assert not (genre_root / "themes").is_dir()
    with pytest.raises(ThemePaletteMissingError):
        load_theme_palette(genre_root)


# ---------------------------------------------------------------------------
# AC2 — the map-emit inline call site (map_emit.py:799) resolves world-tier.
# DIFFERENTIATED fixture: distinct theme ids at the old genre root vs the new
# world tier prove which directory the production code reads, even while a
# stale root copy still exists (so this fails RED for the right reason rather
# than passing on a lingering root palette).
# ---------------------------------------------------------------------------


def test_load_site_map_context_reads_world_tier_palette(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``map_emit._load_site_map_context`` must load the palette from the
    world dir (was ``_load_dungeon_map_context`` before the story 164-4
    scene-context cutover — same content/IO seam, now keyed per site).
    Drives the real production function with a duck-typed ``_SessionData``
    (only ``genre_slug``/``world_slug``/``dungeon_repository`` are read
    before the theme load) — no DB needed."""
    import sidequest.genre.loader as loader_mod
    from sidequest.game.sites.models import SiteDescriptor
    from sidequest.server.websocket_handlers.map_emit import _load_site_map_context

    pack = tmp_path / "content" / "caverns_and_claudes"
    _write_theme(pack / "themes", "old.yaml", _theme_yaml("old_root_theme"))
    _write_theme(
        pack / "worlds" / "beneath_sunden" / "themes",
        "new.yaml",
        _theme_yaml("new_world_theme"),
    )
    monkeypatch.setattr(loader_mod, "DEFAULT_GENRE_PACK_SEARCH_PATHS", [tmp_path / "content"])

    sd = SimpleNamespace(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        dungeon_repository=SimpleNamespace(
            load_map=lambda *, entrance_id, site_id="frontier": SimpleNamespace(
                nodes={"entrance": object()}
            )
        ),
    )
    site = SiteDescriptor(
        site_id="frontier",
        name="The Deep",
        archetype="megadungeon",
        attached_to="the_dropmouth",
        extent="frontier",
    )

    result = _load_site_map_context(sd, site)  # type: ignore[arg-type]
    assert result is not None
    _graph, palette, _entrance = result
    assert "new_world_theme" in palette.themes, "map emit must read world-tier themes/"
    assert "old_root_theme" not in palette.themes, (
        "map emit must NOT read the genre-root themes/ (ADR-140 boundary)"
    )


# ---------------------------------------------------------------------------
# AC4 — content coupling guard against the REAL beneath_sunden pack.
# Fails RED until the `git mv` lands; the loader + GenreLoader are real, so
# this is an end-to-end resolution wiring test (no DB required).
# ---------------------------------------------------------------------------


def test_real_beneath_sunden_palette_lives_at_world_tier() -> None:
    """The shipped beneath_sunden palette must load from the WORLD dir after
    the move — and must NOT remain at the genre-pack root. This is the coupling
    invariant: content move + server repoint land together."""
    from sidequest.dungeon.themes import load_theme_palette
    from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader

    loader = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS)
    world_dir = loader.find("caverns_and_claudes") / "worlds" / "beneath_sunden"

    palette = load_theme_palette(world_dir)
    assert palette.themes, "beneath_sunden world dir must hold the dungeon palette"

    # The genre-pack root must no longer carry themes/ (ADR-140 boundary).
    assert not (world_dir.parent.parent / "themes").is_dir(), (
        "themes/ must move OFF the genre-pack root — a leftover root copy means "
        "the move is half-done and resolution is ambiguous"
    )


# ---------------------------------------------------------------------------
# AC3 — the pack validator validates themes/ at the WORLD tier, not pack tier.
# ---------------------------------------------------------------------------


def test_pack_validator_flags_malformed_world_tier_theme(tmp_path: Path) -> None:
    """A malformed palette under ``worlds/<world>/themes/`` must be caught by
    the validator. Today it only inspects pack-root ``themes/`` and so silently
    validates nothing after the move — a regression this test forbids."""
    from sidequest.cli.validate.pack import _validate_theme_palette

    world_dir = _world_tier_pack(tmp_path, theme_body=_BROKEN_THEME)
    pack_dir = world_dir.parent.parent  # genre-pack root (no themes/ of its own)

    errors = _validate_theme_palette(pack_dir, "caverns_and_claudes")

    assert errors, (
        "validator must inspect world-tier themes/ — a broken palette under "
        "worlds/<world>/themes/ went unreported"
    )
    assert any("palette" in e.lower() or "theme" in e.lower() for e in errors), (
        f"error should name the theme/palette failure; got: {errors}"
    )
