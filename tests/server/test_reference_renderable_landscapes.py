"""Renderable Landscapes section — the lore-page POI gallery.

DRIVER 2026-06-04: the lore page rendered 0 POI ``<img>`` for every world, even
oz (12 POIs on R2). Root cause (cross-world, not wry_whimsy-specific): the only
POI-image emitter, ``present_lore_geography``, is bound to ``geography.yaml`` /
``locations.yaml`` — files NO live world authors. POIs actually live in
``history.yaml points_of_interest[]`` (where they fed only the R2-gate slug set,
never any card). This section renders them where they live.

Contract pinned here:
* ``load_points_of_interest(world_dir)`` returns every POI dict (chapters[] +
  top-level), in authored order.
* ``present_renderable_landscapes`` renders a ``landscape-{slug}`` card with an
  ``img.ref-card__poi`` for each POI whose slug is in the (R2-gated) slug set;
  omits POIs with no gated image; returns "" when none render.
* ``assemble_lore_page`` appends a "Renderable Landscapes" dynamic section (with
  its own TOC entry) when a world has gated POI landscapes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_presenters import present_renderable_landscapes
from sidequest.server.reference_renderer import load_points_of_interest
from sidequest.server.reference_theme import ReferenceTheme


@pytest.fixture
def fake_theme() -> ReferenceTheme:
    return ReferenceTheme(
        archetype="terminal",
        palette_primary="#4A90D9",
        palette_accent="#E8A838",
        palette_background="#0D1117",
        web_font_family="Rajdhani",
        display_font_family="Orbitron",
        dinkus_light="·",
        dinkus_medium="✦",
        dinkus_heavy="✦✦",
    )


# ---------------------------------------------------------------------------
# load_points_of_interest — the shared POI source
# ---------------------------------------------------------------------------


def test_load_points_of_interest_extracts_nested_and_top_level(tmp_path: Path) -> None:
    """POIs under chapters[] AND a top-level points_of_interest are both
    collected, in authored order (chapters first, then top-level)."""
    (tmp_path / "history.yaml").write_text(
        "history: A place.\n"
        "chapters:\n"
        "  - label: Ch1\n"
        "    points_of_interest:\n"
        "      - name: The Munchkin Country\n"
        "        slug: munchkin_country\n"
        "        description: Blue East.\n"
        "points_of_interest:\n"
        "  - name: Top Level POI\n"
        "    slug: top_level_poi\n"
        "    description: Authored at file root.\n",
        encoding="utf-8",
    )
    pois = load_points_of_interest(tmp_path)
    slugs = [p.get("slug") for p in pois]
    assert slugs == ["munchkin_country", "top_level_poi"]


def test_load_points_of_interest_absent_file_is_empty(tmp_path: Path) -> None:
    assert load_points_of_interest(tmp_path) == []


# ---------------------------------------------------------------------------
# present_renderable_landscapes — the gallery presenter
# ---------------------------------------------------------------------------


def _poi(name: str, slug: str) -> dict:
    return {
        "name": name,
        "slug": slug,
        "type": "country",
        "region": slug,
        "description": f"{name} description.",
    }


def test_renders_gated_poi_as_landscape_card_with_image(fake_theme: ReferenceTheme) -> None:
    # The gate set is the slugify-normalised form (as load_poi_image_slugs
    # produces), so a "harbor-light"-style hyphen slug round-trips cleanly. (The
    # underscore-vs-hyphen mismatch against the actual R2 keys is a separate,
    # upstream normalization bug — see test below — that empties the gate set for
    # real underscore-slug worlds; this test pins the section's own behavior.)
    html = present_renderable_landscapes(
        [_poi("Harbor Light", "harbor-light")],
        pack="wry_whimsy",
        world="oz",
        theme=fake_theme,
        poi_image_slugs=frozenset({"harbor-light"}),
    )
    assert 'id="landscape-harbor-light"' in html
    assert 'class="ref-card__poi"' in html  # the lightbox-targeted image class
    assert "Harbor Light" in html
    expected_src = resolve_asset_url("genre_packs/wry_whimsy/worlds/oz/assets/poi/harbor-light.png")
    assert f'src="{expected_src}"' in html


def test_presenter_keys_cards_on_the_slugify_anchor_form(
    fake_theme: ReferenceTheme,
) -> None:
    """Presenter-level invariant (revisited for Story 71-38). The presenter keys
    each card on ``slugify(authored_slug)`` (the hyphen anchor form), so a caller
    that hands it a *raw underscore* gate value gets no card — the slugified poi
    slug (munchkin-country) is not in the underscore set (munchkin_country).

    Pre-71-38 this exact mismatch reached production: ``load_poi_image_slugs``
    slugified the gate set to a hyphen while the R2 key stayed underscore, so the
    section was empty for every underscore-slug world (oz: 12 POIs suppressed).
    Story 71-38 decouples those two forms UPSTREAM (the gate now compares the
    verbatim underscore key against R2, see the end-to-end tests below), so the
    presenter no longer receives a form-mismatched set in production. This test
    now pins only the presenter's own keying rule — it stays GREEN through the
    fix because the presenter contract (anchor = slugify) is intentionally
    UNTOUCHED by 71-38."""
    html = present_renderable_landscapes(
        [_poi("The Munchkin Country", "munchkin_country")],
        pack="wry_whimsy",
        world="oz",
        theme=fake_theme,
        # A raw underscore gate value: the presenter slugifies the poi slug to a
        # hyphen for the membership check, so this underscore value does not match.
        poi_image_slugs=frozenset({"munchkin_country"}),
    )
    assert html == "", (
        "presenter keys cards on slugify(munchkin_country) = munchkin-country; "
        "a raw underscore gate value does not match — the anchor form is slugify"
    )


def test_omits_pois_with_no_gated_image(fake_theme: ReferenceTheme) -> None:
    """A POI whose slug is NOT in the R2-gated set is not a renderable landscape
    — no card, no broken <img>."""
    html = present_renderable_landscapes(
        [_poi("The Sunken Vault", "sunken_vault")],
        pack="wry_whimsy",
        world="oz",
        theme=fake_theme,
        poi_image_slugs=frozenset(),  # nothing rendered on R2
    )
    assert html == ""


def test_empty_when_no_pois(fake_theme: ReferenceTheme) -> None:
    assert (
        present_renderable_landscapes(
            [], pack="p", world="w", theme=fake_theme, poi_image_slugs=frozenset({"x"})
        )
        == ""
    )


# ---------------------------------------------------------------------------
# End-to-end through the real /reference/lore route (wiring test)
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"
_WORLD = "poi_gated_fixture"  # history POIs: harbor-light (on R2), sunken-vault (not)


@pytest.fixture
def client() -> Iterator[TestClient]:
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


def test_lore_page_has_renderable_landscapes_section(client) -> None:
    """The page gains a 'Renderable Landscapes' section + TOC entry, with a
    landscape card carrying the R2-gated POI image — the wiring that was missing
    for every world (DRIVER 2026-06-04)."""
    resp = client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Renderable Landscapes" in html, "missing the dedicated section + TOC label"
    assert 'id="landscape-harbor-light"' in html, "gated POI must render a landscape card"
    gated_src = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_WORLD}/assets/poi/harbor-light.png"
    )
    assert f'src="{gated_src}"' in html, "the on-R2 POI must surface its landscape image"


def test_landscape_section_omits_ungated_poi(client) -> None:
    """sunken-vault is authored but not on R2 → no landscape card for it (the
    section shows only renderable landscapes)."""
    resp = client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    assert resp.status_code == 200, resp.text
    assert 'id="landscape-sunken-vault"' not in resp.text


# ---------------------------------------------------------------------------
# Story 71-38 — decouple the R2-object-key slug (verbatim, underscore) from the
# HTML-anchor slug (slugify, hyphen). The load-bearing repro: an UNDERSCORE-slug
# world whose r2_manifest.json holds the verbatim underscore key. Pre-71-38 the
# gate slugifies the authored slug to a hyphen, builds a hyphen R2 key, never
# matches the underscore manifest key → 0 POI <img> (the oz / 12-munchkin bug).
# ---------------------------------------------------------------------------

# This fixture world mirrors oz: history.yaml authors `munchkin_country`
# (underscore); its landscape lives on R2 under the verbatim underscore key
# `.../poi/munchkin_country.png` (added to tests/fixtures/r2_manifest.json).
_UNDERSCORE_WORLD = "poi_underscore_fixture"
_UNDERSCORE_WORLD_DIR = FIXTURE_ROOT / _PACK / "worlds" / _UNDERSCORE_WORLD


def test_underscore_slug_world_renders_poi_with_verbatim_r2_src(client) -> None:
    """LOAD-BEARING REPRO (AC2 + AC3 both-forms pin): an underscore-slug POI
    whose verbatim R2 key is on R2 renders a landscape card. The two slug forms
    are decoupled and pinned together so the fix can never silently re-couple:

      * R2 object key / <img src> = the VERBATIM authored slug (underscore):
        .../poi/munchkin_country.png — the key that is actually on R2.
      * HTML card id / deep-link anchor = slugify(authored slug) (hyphen):
        id="landscape-munchkin-country".

    Pre-71-38 the gate slugifies to a hyphen, the hyphen R2 key never matches
    the underscore manifest key, gated_poi_slugs is empty, and NONE of these
    three assertions hold (no section, no card, no img) — RED."""
    resp = client.get(f"/reference/lore/{_PACK}/{_UNDERSCORE_WORLD}")
    assert resp.status_code == 200, resp.text
    html = resp.text

    assert "Renderable Landscapes" in html, "underscore-slug world must gain the section"
    # Anchor / card id keeps the hyphen (slugify) form — the conventional,
    # blast-radius-bearing deep-link id (Story 63-6).
    assert 'id="landscape-munchkin-country"' in html, "card id must use the slugify (hyphen) anchor"
    # The <img src> must address the VERBATIM underscore R2 key — the asset that
    # actually exists. A hyphen src here (munchkin-country.png) is a 404 and the
    # gate-only half-fix; assert the underscore key to force the full decouple.
    verbatim_src = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_UNDERSCORE_WORLD}/assets/poi/munchkin_country.png"
    )
    assert f'src="{verbatim_src}"' in html, (
        "the on-R2 underscore POI must surface its verbatim-key img"
    )
    # Defense against silent re-couple: the hyphen R2 key must NOT be the src.
    hyphen_src = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_UNDERSCORE_WORLD}/assets/poi/munchkin-country.png"
    )
    assert f'src="{hyphen_src}"' not in html, (
        "the hyphen R2 key is the bug — it must never be the src"
    )


def test_underscore_world_omits_poi_not_on_r2(client) -> None:
    """The fix is a DECOUPLE, not a 'render every authored POI'. emerald_void is
    authored (underscore slug) but absent from r2_manifest.json → no landscape
    card. Guards against a fix that drops the R2-existence gate entirely."""
    resp = client.get(f"/reference/lore/{_PACK}/{_UNDERSCORE_WORLD}")
    assert resp.status_code == 200, resp.text
    assert 'id="landscape-emerald-void"' not in resp.text, (
        "ungated underscore POI must stay omitted"
    )


def test_underscore_region_deep_link_still_resolves_via_hyphen_anchor() -> None:
    """DEEP-LINK REGRESSION GUARD (AC3): the production consumer in map_emit.py
    feeds ``load_poi_image_slugs(world_dir)`` straight into
    ``reference_url_for_region``, which matches ``slugify(region_id)`` against
    that set. The decouple MUST keep the hyphen (slugify) form reachable on this
    path — a naive 'make load_poi_image_slugs return verbatim underscore slugs'
    fix would make ``slugify('munchkin_country') == 'munchkin-country'`` miss the
    underscore set and silently break every region-header deep-link.

    This guard is GREEN today and must STAY green through the fix."""
    from sidequest.server.reference_anchors import reference_url_for_region
    from sidequest.server.reference_renderer import load_poi_image_slugs

    known_location_slugs = load_poi_image_slugs(_UNDERSCORE_WORLD_DIR)
    url = reference_url_for_region(
        pack=_PACK,
        world=_UNDERSCORE_WORLD,
        region_id="munchkin_country",
        known_location_slugs=known_location_slugs,
    )
    assert url is not None, "an authored underscore region must keep its lore-page deep-link"
    assert url.endswith("#location-munchkin-country"), (
        "deep-link anchor stays the slugify (hyphen) form"
    )
