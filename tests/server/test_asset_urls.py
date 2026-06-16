"""Coverage for the resolve_asset_url single-seam URL builder."""

from __future__ import annotations

import pytest

from sidequest.server import asset_urls


def test_default_emits_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = asset_urls.resolve_asset_url("genre_packs/caverns_and_claudes/audio/music/combat.ogg")
    assert url == (
        "https://cdn.slabgorb.com/genre_packs/caverns_and_claudes/audio/music/combat.ogg"
    )


def test_explicit_cdn_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "https://staging.example/")
    url = asset_urls.resolve_asset_url("artifacts/world/sess/portraits/x.png")
    assert url == "https://staging.example/artifacts/world/sess/portraits/x.png"


@pytest.mark.parametrize("value", ["", "local"])
def test_local_serve_mode(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", value)
    url = asset_urls.resolve_asset_url("genre_packs/caverns_and_claudes/audio/music/combat.ogg")
    # Local-serve mirrors the existing /genre/<rest> static mount.
    assert url == "/genre/caverns_and_claudes/audio/music/combat.ogg"


def test_local_serve_for_artifacts_uses_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    url = asset_urls.resolve_asset_url("artifacts/w/s/portraits/abc.png")
    # Local-serve fallback for daemon artifacts goes via /renders/.
    assert url == "/renders/artifacts/w/s/portraits/abc.png"


def test_leading_slash_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = asset_urls.resolve_asset_url("/genre_packs/foo.ogg")
    assert url == "https://cdn.slabgorb.com/genre_packs/foo.ogg"


def test_unknown_top_level_in_local_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    with pytest.raises(ValueError, match="unknown asset prefix"):
        asset_urls.resolve_asset_url("randomthing/foo.ogg")
