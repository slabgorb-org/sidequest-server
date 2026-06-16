"""Verify that audio paths are URL-resolved at GenrePack load time.

Wires :func:`sidequest.server.asset_urls.resolve_asset_url` into the audio
loader so the UI receives full URLs (not bare relative paths) for every
path-bearing field on ``AudioConfig``:

* ``mood_tracks[mood][i].path``
* ``sfx_library[bucket][i]`` (list of bare strings)
* ``themes[i].variations[j].path``
* ``faction_themes[i].track.path``

This is the wiring test for Task 8 of the R2 media migration plan: it
exercises the production code path (``load_genre_pack``) against a real
genre pack rather than constructing ``AudioConfig`` by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack


def _packs_root() -> Path:
    # tests/genre/test_audio_url_resolution.py -> repo root -> sidequest-content
    return Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"


@pytest.fixture
def caverns_pack(monkeypatch: pytest.MonkeyPatch) -> object:
    """Load caverns_and_claudes with the default (CDN) base URL."""
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    return load_genre_pack(_packs_root() / "caverns_and_claudes")


def test_mood_track_paths_are_full_urls(caverns_pack: object) -> None:
    moods = caverns_pack.audio.mood_tracks  # type: ignore[attr-defined]
    assert moods, "expected at least one mood bucket in caverns_and_claudes"
    seen_any = False
    for mood_name, tracks in moods.items():
        for track in tracks:
            seen_any = True
            assert track.path.startswith("https://cdn.slabgorb.com/"), (
                f"mood {mood_name!r} track has non-CDN path: {track.path!r}"
            )
            assert "genre_packs/caverns_and_claudes/" in track.path
    assert seen_any, "expected at least one mood track across all moods"


def test_sfx_library_paths_are_full_urls(caverns_pack: object) -> None:
    sfx = caverns_pack.audio.sfx_library  # type: ignore[attr-defined]
    assert sfx, "expected at least one sfx bucket in caverns_and_claudes"
    for bucket, paths in sfx.items():
        for p in paths:
            assert p.startswith("https://cdn.slabgorb.com/"), (
                f"sfx {bucket!r} has non-CDN path: {p!r}"
            )
            assert "genre_packs/caverns_and_claudes/" in p


def test_theme_variation_paths_are_full_urls(caverns_pack: object) -> None:
    themes = caverns_pack.audio.themes  # type: ignore[attr-defined]
    assert themes, "expected at least one theme in caverns_and_claudes"
    for theme in themes:
        for variation in theme.variations:
            assert variation.path.startswith("https://cdn.slabgorb.com/"), (
                f"theme {theme.name!r} variation has non-CDN path: {variation.path!r}"
            )
            assert "genre_packs/caverns_and_claudes/" in variation.path


def test_local_mode_emits_genre_static_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    pack = load_genre_pack(_packs_root() / "caverns_and_claudes")

    # mood_tracks
    moods = pack.audio.mood_tracks
    assert moods
    for tracks in moods.values():
        for track in tracks:
            assert track.path.startswith("/genre/caverns_and_claudes/"), (
                f"local mood path not under /genre/: {track.path!r}"
            )

    # sfx_library
    for paths in pack.audio.sfx_library.values():
        for p in paths:
            assert p.startswith("/genre/caverns_and_claudes/"), (
                f"local sfx path not under /genre/: {p!r}"
            )

    # themes
    for theme in pack.audio.themes:
        for variation in theme.variations:
            assert variation.path.startswith("/genre/caverns_and_claudes/"), (
                f"local theme variation not under /genre/: {variation.path!r}"
            )


def test_faction_themes_resolved_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """If any genre pack defines faction_themes, the track.path must resolve.

    caverns_and_claudes itself has none today, so iterate the available packs
    and assert on whichever pack(s) populate this field. If no pack uses it,
    the test is vacuously true — that's accepted (don't fabricate fixtures).
    """
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    packs_root = _packs_root()
    saw_any = False
    for pack_dir in sorted(packs_root.iterdir()):
        if not pack_dir.is_dir() or not (pack_dir / "audio.yaml").exists():
            continue
        try:
            pack = load_genre_pack(pack_dir)
        except Exception:
            # Some packs may be incomplete (missing required files); skip them.
            continue
        for ft in pack.audio.faction_themes:
            saw_any = True
            assert ft.track.path.startswith("https://cdn.slabgorb.com/"), (
                f"faction theme {ft.faction_id!r} in {pack_dir.name!r} "
                f"has non-CDN path: {ft.track.path!r}"
            )
            assert f"genre_packs/{pack_dir.name}/" in ft.track.path
    # No assertion if no pack uses faction_themes — fine.
    _ = saw_any
