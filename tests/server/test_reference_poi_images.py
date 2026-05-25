"""RED tests for Story 63-8 — lore-page POI landscape images.

Per project memory feedback_no_content_coupled_tests: fixtures only, no live
pack content. Presenter unit tests construct a synthetic node + PresenterContext;
the wiring test drives the real /reference route against the isolated
``reference_v2_fixture/worlds/poi_fixture`` world.

Expected production contract (drives these tests; not yet implemented):

* ``PresenterContext`` gains ``poi_image_slugs: frozenset[str] = frozenset()``
  — the set of (already slugified) location slugs that have a generated POI
  landscape image. ``assemble_lore_page`` builds it from
  ``history.yaml`` ``chapters[].points_of_interest[].slug`` (slugify-normalised)
  and threads it into the geography/locations presenter context.
* ``present_lore_geography`` emits, inside each ``<article id="location-{slug}">``
  card, an ``<img>`` **iff** ``slug in ctx.poi_image_slugs``. The src is
  ``resolve_asset_url(f"genre_packs/{pack}/worlds/{world}/assets/poi/{slug}.png")``.
  The image renders below the ``ref-card__title`` and above the
  ``ref-card__body`` description, with a border/shadow style derived from
  ``ctx.theme.palette_accent``. When a rendered location has no matching POI
  image, the card renders text-only — no ``<img>``, no placeholder.
* New flat-only OTEL spans observe the decision (project OTEL principle):
  ``sidequest.reference.poi_image_resolved`` (emit) and
  ``sidequest.reference.poi_image_not_found`` (rendered location lacks art —
  INFO, not ERROR; missing landscape art is expected). Helpers
  ``reference_poi_image_resolved_span`` / ``reference_poi_image_not_found_span``
  follow the existing reference-span shape (keyword-only, ``_tracer`` override).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_presenters import (
    PresenterContext,
    present_lore_geography,
)
from sidequest.server.reference_theme import ReferenceTheme

# Span-capture helper lives in the server conftest.
from tests.server.conftest import span_attrs_by_name

SPAN_RESOLVED = "sidequest.reference.poi_image_resolved"
SPAN_NOT_FOUND = "sidequest.reference.poi_image_not_found"


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


def make_ctx_pois(
    theme: ReferenceTheme,
    poi_image_slugs: frozenset[str],
) -> PresenterContext:
    """PresenterContext for the locations file-root, carrying the POI image set.

    Constructing this FAILS until PresenterContext gains ``poi_image_slugs``
    — that is the core plumbing of story 63-8 (RED until implemented)."""
    return PresenterContext(
        pack="space_opera",
        world="coyote_star",
        file_stem="locations",
        key_path=(),
        theme=theme,
        depth=1,
        poi_image_slugs=poi_image_slugs,
    )


def _two_locations() -> list[dict]:
    return [
        {
            "id": "vaskov-centrum",
            "name": "Vaskov Centrum",
            "region": "habitable_world",
            "type": "city",
            "environment": "humid coastal capital",
            "description": "The governor's seat; rain-soaked rooftops.",
        },
        {
            "id": "broken-drift",
            "name": "The Broken Drift",
            "region": "asteroid_belt",
            "type": "void_drift",
            "environment": "low-g shipwreck field",
            "description": "Where the lost ships go.",
        },
    ]


def _card(html: str, slug: str) -> str:
    """Return the HTML slice for a single location card."""
    marker = f'id="location-{slug}"'
    start = html.index(marker)
    end = html.index("</article>", start)
    return html[start:end]


# ---------------------------------------------------------------------------
# Presenter behaviour
# ---------------------------------------------------------------------------


def test_image_emitted_only_for_slug_in_poi_set(fake_theme: ReferenceTheme) -> None:
    """The imaged location gets exactly one <img>; the un-imaged one gets none."""
    html = present_lore_geography(
        _two_locations(), make_ctx_pois(fake_theme, frozenset({"vaskov-centrum"}))
    )
    assert html.count("<img") == 1, "exactly one POI image should render"
    assert "<img" in _card(html, "vaskov-centrum")
    assert "<img" not in _card(html, "broken-drift")


def test_no_image_no_placeholder_when_slug_absent(fake_theme: ReferenceTheme) -> None:
    """A location with no POI image renders text-only — no <img>, no placeholder."""
    html = present_lore_geography(
        _two_locations(), make_ctx_pois(fake_theme, frozenset())
    )
    assert "<img" not in html
    assert "placeholder" not in html.lower()
    # Text content for both cards still renders.
    assert "Vaskov Centrum" in html
    assert "Where the lost ships go." in html


def test_image_src_uses_resolve_asset_url(fake_theme: ReferenceTheme) -> None:
    """src is the R2 URL built from pack/world/slug via resolve_asset_url —
    never a hardcoded CDN string."""
    html = present_lore_geography(
        _two_locations(), make_ctx_pois(fake_theme, frozenset({"vaskov-centrum"}))
    )
    expected = resolve_asset_url(
        "genre_packs/space_opera/worlds/coyote_star/assets/poi/vaskov-centrum.png"
    )
    assert f'src="{expected}"' in html
    # The canonical relative path structure must be present regardless of env base.
    assert "genre_packs/space_opera/worlds/coyote_star/assets/poi/vaskov-centrum.png" in html


def test_image_placed_below_title_above_description(fake_theme: ReferenceTheme) -> None:
    """Image renders below the location name and above the description body."""
    html = present_lore_geography(
        _two_locations(), make_ctx_pois(fake_theme, frozenset({"vaskov-centrum"}))
    )
    card = _card(html, "vaskov-centrum")
    assert "ref-card__title" in card and "<img" in card and "ref-card__body" in card
    assert card.index("ref-card__title") < card.index("<img") < card.index("ref-card__body")


def test_image_border_uses_theme_accent(fake_theme: ReferenceTheme) -> None:
    """Border/shadow tint comes from the (per-pack) theme accent colour."""
    html = present_lore_geography(
        _two_locations(), make_ctx_pois(fake_theme, frozenset({"vaskov-centrum"}))
    )
    card = _card(html, "vaskov-centrum")
    assert fake_theme.palette_accent in card, (
        "POI image card should carry the theme accent colour in its border/shadow style"
    )


# ---------------------------------------------------------------------------
# Presenter fires OTEL spans (drive the flow, assert the span — CLAUDE.md)
# ---------------------------------------------------------------------------


def test_presenter_fires_resolved_and_not_found_spans(
    fake_theme: ReferenceTheme, otel_capture
) -> None:
    present_lore_geography(
        _two_locations(), make_ctx_pois(fake_theme, frozenset({"vaskov-centrum"}))
    )
    resolved = span_attrs_by_name(otel_capture, SPAN_RESOLVED)
    not_found = span_attrs_by_name(otel_capture, SPAN_NOT_FOUND)
    assert any(a.get("reference.slug") == "vaskov-centrum" for a in resolved), (
        f"resolved span missing for imaged location: {resolved}"
    )
    assert any(a.get("reference.slug") == "broken-drift" for a in not_found), (
        f"not_found span missing for un-imaged location: {not_found}"
    )


# ---------------------------------------------------------------------------
# Span helpers — constants, attrs, flat-only registration
# ---------------------------------------------------------------------------


def test_poi_span_name_constants() -> None:
    from sidequest.telemetry.spans.reference import (
        SPAN_REFERENCE_POI_IMAGE_NOT_FOUND,
        SPAN_REFERENCE_POI_IMAGE_RESOLVED,
    )

    assert SPAN_REFERENCE_POI_IMAGE_RESOLVED == SPAN_RESOLVED
    assert SPAN_REFERENCE_POI_IMAGE_NOT_FOUND == SPAN_NOT_FOUND


def test_poi_resolved_span_helper_emits_attrs() -> None:
    from sidequest.telemetry.spans.reference import reference_poi_image_resolved_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_poi_image_resolved_span(
        pack="space_opera",
        world="coyote_star",
        slug="vaskov-centrum",
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == SPAN_RESOLVED
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.pack"] == "space_opera"
    assert attrs["reference.world"] == "coyote_star"
    assert attrs["reference.slug"] == "vaskov-centrum"


def test_poi_not_found_span_helper_emits_attrs() -> None:
    from sidequest.telemetry.spans.reference import reference_poi_image_not_found_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_poi_image_not_found_span(
        pack="space_opera",
        world="coyote_star",
        slug="broken-drift",
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == SPAN_NOT_FOUND
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.slug"] == "broken-drift"


def test_poi_spans_registered_in_flat_only_set() -> None:
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import (
        SPAN_REFERENCE_POI_IMAGE_NOT_FOUND,
        SPAN_REFERENCE_POI_IMAGE_RESOLVED,
    )

    assert SPAN_REFERENCE_POI_IMAGE_RESOLVED in FLAT_ONLY_SPANS
    assert SPAN_REFERENCE_POI_IMAGE_NOT_FOUND in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# Wiring: history.yaml POI slugs reach the rendered lore page (HTTP route)
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"
_WORLD = "poi_fixture"


@pytest.fixture
def poi_client():
    from fastapi.testclient import TestClient

    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


def test_lore_page_renders_poi_image_for_history_listed_location(poi_client) -> None:
    """End-to-end: history.yaml lists vaskov-centrum as a POI; the lore page's
    location-vaskov-centrum card gets the R2 <img>, and location-the-broken-drift
    (not in history) does not. Proves assemble_lore_page threads the history POI
    slug set into the geography presenter."""
    resp = poi_client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    assert resp.status_code == 200, resp.text
    html = resp.text

    vaskov = _card(html, "vaskov-centrum")
    drift = _card(html, "the-broken-drift")

    expected_src = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_WORLD}/assets/poi/vaskov-centrum.png"
    )
    assert f'src="{expected_src}"' in vaskov, "POI image missing from history-listed location card"
    assert "<img" not in drift, "un-listed location must not get a POI image"
