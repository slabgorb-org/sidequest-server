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


def test_underscore_slug_is_hyphenated_by_the_gate_membership_check(
    fake_theme: ReferenceTheme,
) -> None:
    """Documents the SEPARATE upstream bug: the presenter slugifies the authored
    POI slug (munchkin_country -> munchkin-country) for the gate-membership check
    and the R2 key, but the real R2 asset key is underscore (munchkin_country.png).
    So an underscore gate value does NOT match and the landscape is suppressed —
    the load-bearing reason oz renders 0 POI images even with this section. When
    the slug-normalization is fixed upstream this test should be revisited."""
    html = present_renderable_landscapes(
        [_poi("The Munchkin Country", "munchkin_country")],
        pack="wry_whimsy",
        world="oz",
        theme=fake_theme,
        # The actual R2/manifest form (underscore) — what _gate_poi_slugs would
        # need to retain. Today load_poi_image_slugs slugifies to a hyphen, so
        # this never matches and the section is empty for oz.
        poi_image_slugs=frozenset({"munchkin_country"}),
    )
    assert html == "", (
        "underscore gate value does not match slugify(munchkin_country) "
        "= munchkin-country — the upstream normalization mismatch"
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
