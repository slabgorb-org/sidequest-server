"""RED tests for Story 65-8 — manifest-gated POI images on the lore page.

Story 63-8 (green, ``test_reference_poi_images.py``) already wired
``history.yaml`` POI slugs into the geography presenter and emits an ``<img>``
per **authored** POI. 65-8 tightens that gate to an **existence** check: an
``<img>`` is emitted only when the POI's R2 image key is present in the
committed ``r2_manifest.json`` (Story 65-7). An authored-but-not-on-R2 POI —
e.g. every glenross POI, which is listed in history.yaml but has no rendered
landscape on R2 — must render text-only. No broken ``<img>`` reaches a player.

This file covers ONLY the net-new manifest gate. The authorship→presenter
wiring, image placement, theme accent, HTML-escaping, and the
``poi_image_resolved`` / ``poi_image_not_found`` spans are covered by 63-8 and
are NOT re-tested here (they are already green; duplicating them would be
vacuous per the python-review test-quality rule).

Contract pinned by these tests (drives Dev; NOT yet implemented):

* New ``load_r2_manifest_keys(manifest_path: Path) -> frozenset[str]`` in
  ``sidequest.server.reference_renderer`` returns the set of ``key`` values from
  the r2_manifest.json array. Absent file -> ``FileNotFoundError``; malformed or
  wrong-shape JSON -> ``ValueError`` (loud — No Silent Fallbacks; never a
  silently-empty key set). Loaded once / cached per path.
* ``assemble_lore_page`` discovers the manifest at
  ``pack_dir.parent.parent / "r2_manifest.json"`` (prod:
  ``sidequest-content/r2_manifest.json``; fixture:
  ``tests/fixtures/r2_manifest.json``) and a location card gets its POI
  ``<img>`` only when
  ``f"genre_packs/{pack}/worlds/{world}/assets/poi/{slug}.png"`` is in the
  manifest key set.
* New flat-only span ``sidequest.reference.manifest_loaded`` (helper
  ``reference_manifest_loaded_span``) fires once per lore render, carrying the
  manifest path, total entry count, and the world-prefixed key count.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.server.asset_urls import resolve_asset_url

# Span-capture helper lives in the server conftest.
from tests.server.conftest import span_attrs_by_name

SPAN_MANIFEST_LOADED = "sidequest.reference.manifest_loaded"


def _entry(key: str) -> dict:
    """A well-formed r2_manifest.json entry with the 65-7 schema."""
    return {
        "key": key,
        "md5": "0" * 32,
        "size_bytes": 1,
        "uploaded_at": "2026-05-30T12:00:00Z",
        "source": "r2_bucket_scan",
    }


# ---------------------------------------------------------------------------
# AC1 — manifest loader: key set, loud failure, caching
# ---------------------------------------------------------------------------


def test_load_manifest_keys_returns_key_set(tmp_path: Path) -> None:
    """The loader returns the set of ``key`` strings from the manifest array."""
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    p = tmp_path / "r2_manifest.json"
    p.write_text(
        json.dumps(
            [
                _entry("genre_packs/x/worlds/y/assets/poi/a.png"),
                _entry("genre_packs/x/worlds/y/assets/poi/b.png"),
            ]
        ),
        encoding="utf-8",
    )
    keys = load_r2_manifest_keys(p)
    assert keys == frozenset(
        {
            "genre_packs/x/worlds/y/assets/poi/a.png",
            "genre_packs/x/worlds/y/assets/poi/b.png",
        }
    )


def test_load_manifest_absent_file_raises_loudly(tmp_path: Path) -> None:
    """An absent manifest is a configuration error — raise, never return empty
    (No Silent Fallbacks)."""
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    with pytest.raises(FileNotFoundError):
        load_r2_manifest_keys(tmp_path / "does_not_exist.json")


def test_load_manifest_malformed_json_raises_loudly(tmp_path: Path) -> None:
    """Malformed JSON aborts loudly (``JSONDecodeError`` is a ``ValueError``)."""
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    p = tmp_path / "r2_manifest.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_r2_manifest_keys(p)


def test_load_manifest_wrong_shape_raises_loudly(tmp_path: Path) -> None:
    """Valid JSON of the wrong shape (object, not a list of keyed entries) is a
    malformed manifest -> loud ``ValueError``, not a silently-empty key set."""
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    p = tmp_path / "r2_manifest.json"
    p.write_text(json.dumps({"oops": "not a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_r2_manifest_keys(p)


def test_load_manifest_is_cached_per_path(tmp_path: Path) -> None:
    """Loaded once / cached: overwriting the file after the first load does not
    change the returned set. (A non-caching loader would re-read and return the
    new content; AC1 requires load-once-per-process.) Isolated by unique
    ``tmp_path`` so the per-path cache never bleeds across tests."""
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    p = tmp_path / "r2_manifest.json"
    p.write_text(json.dumps([_entry("genre_packs/k1.png")]), encoding="utf-8")
    first = load_r2_manifest_keys(p)
    p.write_text(json.dumps([_entry("genre_packs/k2.png")]), encoding="utf-8")
    second = load_r2_manifest_keys(p)
    assert first == second == frozenset({"genre_packs/k1.png"})


# ---------------------------------------------------------------------------
# AC6 — manifest_loaded span: constant, helper attrs, flat-only registration
# ---------------------------------------------------------------------------


def test_manifest_loaded_span_name_constant() -> None:
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_MANIFEST_LOADED

    assert SPAN_REFERENCE_MANIFEST_LOADED == SPAN_MANIFEST_LOADED


def test_manifest_loaded_span_helper_emits_attrs() -> None:
    from sidequest.telemetry.spans.reference import reference_manifest_loaded_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_manifest_loaded_span(
        path="tests/fixtures/r2_manifest.json",
        entry_count=1743,
        world_key_count=12,
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == SPAN_MANIFEST_LOADED
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.manifest_path"] == "tests/fixtures/r2_manifest.json"
    assert attrs["reference.manifest_entry_count"] == 1743
    assert attrs["reference.world_key_count"] == 12


def test_manifest_loaded_span_registered_flat_only() -> None:
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_MANIFEST_LOADED

    assert SPAN_REFERENCE_MANIFEST_LOADED in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# AC3 / AC7 — the gate, end-to-end through the real /reference/lore route
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"
_GATED_WORLD = "poi_gated_fixture"


@pytest.fixture
def gated_client():
    from fastapi.testclient import TestClient

    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


def _card(html: str, slug: str) -> str:
    """Return the HTML slice for a single location card."""
    marker = f'id="location-{slug}"'
    start = html.index(marker)
    end = html.index("</article>", start)
    return html[start:end]


def test_manifest_present_poi_gets_image(gated_client) -> None:
    """Regression guard: a POI that IS in r2_manifest.json still renders its
    R2 ``<img>`` (the gate must not over-suppress legitimately-rendered art)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_GATED_WORLD}")
    assert resp.status_code == 200, resp.text
    harbor = _card(resp.text, "harbor-light")
    expected = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_GATED_WORLD}/assets/poi/harbor-light.png"
    )
    assert f'src="{expected}"' in harbor, "in-manifest POI must keep its R2 image"


def test_authored_but_absent_poi_renders_textonly(gated_client) -> None:
    """THE manifest gate. ``sunken-vault`` is authored as a history.yaml POI but
    its image key is NOT in r2_manifest.json -> the card renders text-only, no
    ``<img>``, no broken image. RED today: the 63-8 authorship gate emits an
    ``<img>`` for any authored POI regardless of R2 existence."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_GATED_WORLD}")
    assert resp.status_code == 200, resp.text
    vault = _card(resp.text, "sunken-vault")
    assert "<img" not in vault, "authored-but-not-on-R2 POI must render text-only (no broken image)"
    # The gate suppresses only the image — the card's prose still renders.
    assert "counting-house swallowed by the spring tides" in vault


def test_lore_render_fires_manifest_loaded_span_once(gated_client, otel_capture) -> None:
    """Per-render observability (OTEL principle): one ``manifest_loaded`` span
    per lore render, carrying a non-trivial entry count. RED today: no such span
    exists. The GM/dev panel uses it to confirm the gate consulted the manifest
    rather than the renderer improvising."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_GATED_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = span_attrs_by_name(otel_capture, SPAN_MANIFEST_LOADED)
    assert len(spans) == 1, (
        f"expected exactly one manifest_loaded span per render, got {len(spans)}"
    )
    assert spans[0].get("reference.manifest_entry_count", 0) >= 1
