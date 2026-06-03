"""Unit tests for :func:`sidequest.genre.audio_paths.resolve_audio_relpath`.

Verifies the single shared-bucket convention:
* ``assets/`` prefix  → ``genre_packs/assets/`` (no pack slug, scope="shared")
* other relative path → ``genre_packs/<slug>/`` (scope="pack")
* absolute URL / server-absolute path / empty string → pass-through
"""

from __future__ import annotations

import pytest

from sidequest.genre.audio_paths import resolve_audio_relpath


def test_pack_relative_path_gets_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = resolve_audio_relpath("audio/music/combat.ogg", genre_slug="cav")
    assert url == "https://cdn.slabgorb.com/genre_packs/cav/audio/music/combat.ogg"


def test_assets_prefix_resolves_to_shared_bucket_no_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = resolve_audio_relpath(
        "assets/audio/classical_pd/Satie - Gymnopedie No.1.ogg", genre_slug="cav"
    )
    assert url == (
        "https://cdn.slabgorb.com/genre_packs/assets/audio/classical_pd/"
        "Satie - Gymnopedie No.1.ogg"
    )


def test_already_absolute_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    assert resolve_audio_relpath("https://x/y.ogg", genre_slug="cav") == "https://x/y.ogg"
    assert resolve_audio_relpath("http://x/y.ogg", genre_slug="cav") == "http://x/y.ogg"
    assert resolve_audio_relpath("/renders/x.ogg", genre_slug="cav") == "/renders/x.ogg"


def test_empty_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    assert resolve_audio_relpath("", genre_slug="cav") == ""
