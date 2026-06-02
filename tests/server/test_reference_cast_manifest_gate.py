"""RED tests for Story 65-9 — public Cast section with manifest-gated portraits.

The lore reference page (``GET /reference/lore/{pack}/{world}``, ADR-135) is a
public table tool. 65-8 lit up its **Points of Interest** with manifest-gated
landscape images. 65-9 is the **portrait analog**: a public **Cast** section
that lists a world's named NPCs (the ``portrait_manifest.yaml`` projection — the
public, non-spoiler cast; ``npcs.yaml`` is keeper-only and excluded) and emits a
portrait ``<img>`` **iff** that NPC's portrait is actually on R2 per the Story
65-7 ``r2_manifest.json`` existence oracle. Authored-but-not-on-R2 NPCs render
text-only — no broken ``<img>`` reaches a player.

This is the *existence* tightening of Story 65-6's *manifest-membership* portrait
resolution. The canonical world-scoped portrait key and slug rule come straight
from 65-6 (``emitters.py:_resolve_npc_portrait_url`` builds
``genre_packs/<g>/worlds/<w>/assets/portraits/<slug>.png`` and slugs names with
``slugify_player_name``). 65-9 reuses 65-8's loaded-once manifest machinery
(``load_r2_manifest_keys`` + ``pack_dir.parent.parent / "r2_manifest.json"``
discovery + the ``sidequest.reference.manifest_loaded`` span) and the existing
portrait span family (``scrapbook.npc_portrait_{resolved,not_found}``).

Contract pinned by these tests (drives Dev; NOT yet implemented — RED):

* New ``portrait_image_key(pack, world, slug) -> str`` in
  ``sidequest.server.reference_presenters``, the portrait analog of
  ``poi_image_key`` — returns the **world-scoped** R2 key
  ``genre_packs/{pack}/worlds/{world}/assets/portraits/{slug}.png`` (matches the
  65-6 URL construction so URL == manifest key by construction).
* ``assemble_lore_page`` builds a **Cast** section from the world's
  ``portrait_manifest.yaml`` characters. Each NPC card carries ``id="cast-{slug}"``
  where ``slug = slugify_player_name(name)``. A portrait ``<img>`` is emitted
  for a card **iff** ``portrait_image_key(pack, world, slug)`` is in the loaded
  manifest key set; otherwise the card renders text-only (name/role/appearance),
  never a broken ``<img>``.
* Each portrait decision reuses the 65-6 span family: a present portrait fires
  ``scrapbook.npc_portrait_resolved``; an absent one fires
  ``scrapbook.npc_portrait_not_found`` — both carrying ``slug`` — so the GM/dev
  panel can tell "authored & on R2" from "authored, not on R2".
* The ``manifest_loaded`` span fires once per render carrying the **exact**
  fixture entry count (proves the gate read the fixture oracle, not prod's 1743).
* ``load_r2_manifest_keys`` fails **loud** on a list entry that is a dict missing
  the ``key`` field (``ValueError``) — the second loud-fail branch, uncovered by
  the POI suite.
* A Cast-bearing world whose ``r2_manifest.json`` is **absent** returns HTTP 500
  (loud), never a silently image-free 200 (No Silent Fallbacks) — the route-level
  gap the 65-8 reviewer flagged for the shared gate.

The POI authorship→presenter wiring, image placement, HTML escaping, and the
POI-specific spans are covered by 63-8/65-8 and are NOT re-tested here.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.utils import slugify_player_name

# Span-capture helper lives in the server conftest.
from tests.server.conftest import span_attrs_by_name

SPAN_MANIFEST_LOADED = "sidequest.reference.manifest_loaded"
SPAN_PORTRAIT_RESOLVED = "scrapbook.npc_portrait_resolved"
SPAN_PORTRAIT_NOT_FOUND = "scrapbook.npc_portrait_not_found"

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
FIXTURE_MANIFEST = Path(__file__).parent.parent / "fixtures" / "r2_manifest.json"
_PACK = "reference_v2_fixture"
_CAST_WORLD = "cast_gated_fixture"

# The two authored NPCs in cast_gated_fixture/portrait_manifest.yaml.
_PRESENT_NAME = "Vivian Harbormaster"  # portrait key IS in the fixture manifest
_ABSENT_NAME = "Thessaly Dunmore"  # authored, but NOT on R2
_PRESENT_SLUG = slugify_player_name(_PRESENT_NAME)  # -> "vivian_harbormaster"
_ABSENT_SLUG = slugify_player_name(_ABSENT_NAME)  # -> "thessaly_dunmore"


def _entry(key: str) -> dict[str, object]:
    """A well-formed r2_manifest.json entry with the 65-7 schema."""
    return {
        "key": key,
        "md5": "0" * 32,
        "size_bytes": 1,
        "uploaded_at": "2026-05-30T12:00:00Z",
        "source": "r2_bucket_scan",
    }


def _card(html: str, slug: str) -> str:
    """Return the HTML slice for a single Cast card (contract: ``id=cast-{slug}``)."""
    marker = f'id="cast-{slug}"'
    start = html.index(marker)
    end = html.index("</article>", start)
    return html[start:end]


@pytest.fixture
def gated_client() -> Iterator[TestClient]:
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Contract: portrait_image_key (the poi_image_key analog) — world-scoped
# ---------------------------------------------------------------------------


def test_portrait_image_key_is_world_scoped() -> None:
    """``portrait_image_key`` returns the world-scoped R2 key 65-6 already writes
    — NOT the legacy pack-level ``images/portraits`` path. URL == manifest key by
    construction, so the gate and the presenter's ``<img src>`` agree."""
    from sidequest.server.reference_presenters import portrait_image_key

    assert (
        portrait_image_key(_PACK, _CAST_WORLD, _PRESENT_SLUG)
        == f"genre_packs/{_PACK}/worlds/{_CAST_WORLD}/assets/portraits/{_PRESENT_SLUG}.png"
    )


# ---------------------------------------------------------------------------
# AC3 — manifest loader: second loud-fail branch (entry missing 'key')
# ---------------------------------------------------------------------------


def test_load_manifest_entry_missing_key_raises_loudly(tmp_path: Path) -> None:
    """A manifest whose list entry is a dict MISSING the ``key`` field is
    malformed -> loud ``ValueError`` (No Silent Fallbacks), never a key set that
    silently drops the bad entry. Second loud-fail branch of
    ``load_r2_manifest_keys``; the POI suite only covers absent-file,
    malformed-JSON, and wrong-top-level-shape."""
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    p = tmp_path / "r2_manifest.json"
    p.write_text(
        json.dumps(
            [
                _entry("genre_packs/x/worlds/y/assets/portraits/ok.png"),
                {"md5": "0" * 32, "size_bytes": 1},  # <- dict, but no "key"
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_r2_manifest_keys(p)


# ---------------------------------------------------------------------------
# AC1 — the Cast gate, end-to-end through the real /reference/lore route
# ---------------------------------------------------------------------------


def test_cast_present_npc_gets_portrait_image(gated_client: TestClient) -> None:
    """An NPC whose world-scoped portrait key IS in r2_manifest.json renders her
    R2 ``<img>`` in the Cast section (the gate must not over-suppress real art)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text
    card = _card(resp.text, _PRESENT_SLUG)
    expected = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_CAST_WORLD}/assets/portraits/{_PRESENT_SLUG}.png"
    )
    assert f'src="{expected}"' in card, "on-R2 NPC must render her portrait <img>"


def test_cast_authored_but_absent_npc_renders_textonly(gated_client: TestClient) -> None:
    """THE Cast manifest gate. Thessaly Dunmore is authored in
    portrait_manifest.yaml but her portrait key is NOT in r2_manifest.json -> her
    card renders text-only (no ``<img>``, no broken image) while keeping her
    name/role/appearance prose. RED today: there is no Cast section at all."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text
    card = _card(resp.text, _ABSENT_SLUG)
    assert "<img" not in card, "authored-but-not-on-R2 NPC must render text-only"
    # The gate suppresses only the image — the card's identity prose still renders.
    assert "sunken counting-house" in card


def test_cast_section_renders_both_npcs(gated_client: TestClient) -> None:
    """Both authored NPCs appear in the Cast section regardless of R2 state — the
    gate decides the *image*, never whether the NPC is listed. Pins that the
    public projection source is portrait_manifest.yaml (both cards present)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text
    assert f'id="cast-{_PRESENT_SLUG}"' in resp.text
    assert f'id="cast-{_ABSENT_SLUG}"' in resp.text


# ---------------------------------------------------------------------------
# AC1 (observability) — per-NPC portrait decision reuses the 65-6 span family
# ---------------------------------------------------------------------------


def test_cast_portrait_decisions_emit_spans(gated_client, otel_capture) -> None:
    """Per-NPC observability (OTEL principle): the present NPC fires a
    ``scrapbook.npc_portrait_resolved`` span and the absent one fires
    ``scrapbook.npc_portrait_not_found`` — both keyed by slug — so the GM/dev
    panel can distinguish 'authored & on R2' from 'authored, not on R2' rather
    than trusting the renderer. RED today: no Cast render, no spans."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text

    resolved = span_attrs_by_name(otel_capture, SPAN_PORTRAIT_RESOLVED)
    not_found = span_attrs_by_name(otel_capture, SPAN_PORTRAIT_NOT_FOUND)

    assert _PRESENT_SLUG in {a.get("slug") for a in resolved}, (
        "present NPC must fire a portrait_resolved span"
    )
    assert _ABSENT_SLUG in {a.get("slug") for a in not_found}, (
        "absent NPC must fire a portrait_not_found span"
    )


# ---------------------------------------------------------------------------
# AC4 — manifest_loaded span fires once per render with the EXACT fixture count
# ---------------------------------------------------------------------------


def test_cast_render_fires_manifest_loaded_span_with_exact_count(
    gated_client, otel_capture
) -> None:
    """The ``manifest_loaded`` span carries the EXACT number of entries in the
    fixture manifest (== N, not >= 1). If the gate had read prod's manifest the
    count would be in the thousands — this pins that ``pack_dir.parent.parent``
    resolved to the fixture oracle. Updates if the fixture manifest grows."""
    expected_n = len(json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8")))

    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text

    spans = span_attrs_by_name(otel_capture, SPAN_MANIFEST_LOADED)
    assert len(spans) == 1, f"expected exactly one manifest_loaded span, got {len(spans)}"
    assert spans[0].get("reference.manifest_entry_count") == expected_n


# ---------------------------------------------------------------------------
# AC2 — No Silent Fallbacks: absent manifest on a Cast world -> loud HTTP 500
# ---------------------------------------------------------------------------


def test_absent_manifest_on_cast_world_returns_500(tmp_path: Path) -> None:
    """A Cast-bearing world whose ``r2_manifest.json`` is ABSENT must return a
    loud 500 — never a silently image-free 200. Closes the route-level gap the
    65-8 reviewer flagged.

    The fixture pack is copied under ``tmp_path`` so the gate's
    ``pack_dir.parent.parent / "r2_manifest.json"`` discovery lands on a path
    with no manifest. ``raise_server_exceptions=False`` so an uncaught loud
    failure surfaces as a 500 *response* (the assertion) rather than re-raising
    into the test — and pins that Dev converts the absent-manifest
    ``FileNotFoundError`` into a clean 500 (the route handler today only catches
    ``ValueError``/``MissingThemeFieldError``)."""
    from sidequest.server.app import create_app

    search_root = tmp_path / "packs"
    shutil.copytree(FIXTURE_ROOT / _PACK, search_root / _PACK)
    assert not (tmp_path / "r2_manifest.json").exists(), "guard: no manifest two levels up"

    app = create_app(genre_pack_search_paths=[search_root])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")

    assert resp.status_code == 500, (
        f"absent manifest must fail loud (500), not a silent image-free page; got "
        f"{resp.status_code}"
    )
