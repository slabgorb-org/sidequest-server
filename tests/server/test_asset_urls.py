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


# ---- rewrite_theme_css_asset_urls -----------------------------------------
# Genre client_theme.css ships font (and other asset) url()s as either the
# local-mount form (/genre/...) or the content-relative form (genre_packs/...).
# The server runs the injected theme CSS through this rewriter so those url()s
# resolve through the same asset seam as images/audio: absolute CDN in prod,
# /genre/... in offline-local mode.

CSS_FONT_FACE = (
    "@font-face{font-family:'Orbitron';"
    "src:url('/genre/assets/fonts/Orbitron-Regular.woff2') format('woff2');"
    "font-display:swap;}"
)


def test_rewrite_genre_mount_url_to_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    out = asset_urls.rewrite_theme_css_asset_urls(CSS_FONT_FACE)
    assert (
        "url('https://cdn.slabgorb.com/genre_packs/assets/fonts/Orbitron-Regular.woff2')"
        in out
    )
    assert "/genre/assets/fonts" not in out
    # Surrounding CSS is preserved untouched.
    assert out.startswith("@font-face{font-family:'Orbitron';")
    assert "format('woff2')" in out


def test_rewrite_content_relative_url_to_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    css = "src:url('genre_packs/assets/fonts/Cinzel-Regular.woff2') format('woff2');"
    out = asset_urls.rewrite_theme_css_asset_urls(css)
    assert (
        "url('https://cdn.slabgorb.com/genre_packs/assets/fonts/Cinzel-Regular.woff2')"
        in out
    )


@pytest.mark.parametrize("value", ["", "local"])
def test_rewrite_local_mode_keeps_genre_mount(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", value)
    out = asset_urls.rewrite_theme_css_asset_urls(CSS_FONT_FACE)
    assert "url('/genre/assets/fonts/Orbitron-Regular.woff2')" in out
    assert "cdn.slabgorb.com" not in out


def test_rewrite_local_mode_normalises_content_relative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    css = "src:url(genre_packs/assets/fonts/Cinzel-Regular.woff2);"
    out = asset_urls.rewrite_theme_css_asset_urls(css)
    assert "url(/genre/assets/fonts/Cinzel-Regular.woff2)" in out


def test_rewrite_leaves_foreign_urls_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    css = (
        "a{background:url(data:image/png;base64,AAAA)}"
        "b{background:url('https://example.com/x.png')}"
        "c{background:url(/textures/dice/x.jpg)}"
    )
    out = asset_urls.rewrite_theme_css_asset_urls(css)
    assert out == css


def test_rewrite_handles_quote_styles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    css = (
        'a{src:url("/genre/assets/fonts/A.woff2")}'
        "b{src:url(/genre/assets/fonts/B.woff2)}"
    )
    out = asset_urls.rewrite_theme_css_asset_urls(css)
    assert 'url("https://cdn.slabgorb.com/genre_packs/assets/fonts/A.woff2")' in out
    assert "url(https://cdn.slabgorb.com/genre_packs/assets/fonts/B.woff2)" in out


def test_resolve_asset_url_defaults_scope_pack(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    from tests.server.conftest import span_attrs_by_name

    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = asset_urls.resolve_asset_url("genre_packs/cav/audio/music/combat.ogg")
    assert url == "https://cdn.slabgorb.com/genre_packs/cav/audio/music/combat.ogg"
    attrs = span_attrs_by_name(otel_capture, "server.asset_url.resolved")
    assert len(attrs) == 1
    assert attrs[0]["asset.scope"] == "pack"


def test_resolve_asset_url_accepts_shared_scope(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    # scope is forensic-only; it must not change the URL, only the span.
    from tests.server.conftest import span_attrs_by_name

    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = asset_urls.resolve_asset_url(
        "genre_packs/assets/audio/classical_pd/Satie - Gymnopedie No.1.ogg",
        scope="shared",
    )
    assert url == (
        "https://cdn.slabgorb.com/genre_packs/assets/audio/classical_pd/"
        "Satie - Gymnopedie No.1.ogg"
    )
    attrs = span_attrs_by_name(otel_capture, "server.asset_url.resolved")
    assert len(attrs) == 1
    assert attrs[0]["asset.scope"] == "shared"


def test_player_portrait_url_resolves_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    url = asset_urls.resolve_player_portrait_url(
        "space_opera", "perseus_cloud", "drifter_voidborn_a1"
    )
    assert url == (
        "https://cdn.slabgorb.com/genre_packs/space_opera/worlds/"
        "perseus_cloud/assets/portraits/drifter_voidborn_a1.png"
    )


def test_player_portrait_url_none_for_falsy_ref() -> None:
    assert asset_urls.resolve_player_portrait_url("space_opera", "perseus_cloud", None) is None
    assert asset_urls.resolve_player_portrait_url("space_opera", "perseus_cloud", "") is None
