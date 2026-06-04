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
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.utils import slugify_player_name

# Span-capture helper lives in the server conftest.
from tests.server.conftest import span_attrs_by_name

SPAN_MANIFEST_LOADED = "sidequest.reference.manifest_loaded"
# Story 65-13 AC8: the reference Cast gate emits its OWN reference-namespaced
# portrait spans (mirroring the 65-11 map-pin spans) instead of reusing the
# scene-time scrapbook portrait family. On the reference page a "not_found"
# means *authored-but-not-on-R2* — a different fact than the scrapbook
# family's "not authored at all" (ad-hoc scene NPC), so the docstrings on
# the scrapbook spans (scrapbook.py:133,152) describe a semantic the
# reference render does not have. These are the new spans Dev must add +
# migrate the presenter onto.
SPAN_REF_PORTRAIT_RESOLVED = "sidequest.reference.portrait_resolved"
SPAN_REF_PORTRAIT_NOT_FOUND = "sidequest.reference.portrait_not_found"
# The scene-time scrapbook spans the reference render must NO LONGER emit
# (they belong to the 65-6 scene-invocation path, not the reference page).
SPAN_SCENE_PORTRAIT_RESOLVED = "scrapbook.npc_portrait_resolved"
SPAN_SCENE_PORTRAIT_NOT_FOUND = "scrapbook.npc_portrait_not_found"

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

    # Story 65-13: the @lru_cache on load_r2_manifest_keys is keyed by Path; a
    # prior test that loaded a same-named tmp manifest could otherwise return a
    # stale cached set. Clear it so this assertion exercises the real read.
    load_r2_manifest_keys.cache_clear()

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
    # Story 65-13: pin the *reason* — a bare ValueError could mask an unrelated
    # failure (e.g. wrong-top-level-shape). The message must name the missing key.
    with pytest.raises(ValueError, match=r"missing 'key'"):
        load_r2_manifest_keys(p)


# ---------------------------------------------------------------------------
# Story 65-13 EDGE — load_cast_entries guards a non-list `characters:` value
# ---------------------------------------------------------------------------


def test_load_cast_entries_non_list_characters_raises_loudly(tmp_path: Path) -> None:
    """Story 65-13 EDGE. A ``portrait_manifest.yaml`` whose top-level
    ``characters:`` is a scalar (e.g. ``characters: 42``) is malformed first-
    party authoring. ``load_cast_entries`` must fail **loud** with a
    ``ValueError`` (No Silent Fallbacks) rather than letting the downstream
    ``[c for c in chars ...]`` blow up with an uncaught ``TypeError`` (an
    unclean — though still loud — 500).

    RED today: ``chars = data.get("characters", [])`` is fed straight into a
    comprehension, so a non-iterable raises ``TypeError`` (not ``ValueError``)
    and a non-dict iterable (e.g. a string) silently mis-parses per character."""
    from sidequest.server.reference_renderer import load_cast_entries

    world = tmp_path / "world"
    world.mkdir()
    (world / "portrait_manifest.yaml").write_text("characters: 42\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_cast_entries(world)


# ---------------------------------------------------------------------------
# Story 65-13 TEST — graceful path: a world with no portrait_manifest.yaml
# ---------------------------------------------------------------------------


def test_load_cast_entries_returns_empty_when_manifest_absent(tmp_path: Path) -> None:
    """Story 65-13. A feature-less world (no ``portrait_manifest.yaml``) yields
    an empty cast list — the graceful no-feature path that lets the caller omit
    the Cast section. This codepath shipped in 65-9 but was never covered."""
    from sidequest.server.reference_renderer import load_cast_entries

    world = tmp_path / "world"
    world.mkdir()
    assert not (world / "portrait_manifest.yaml").exists(), "guard: no manifest authored"

    assert load_cast_entries(world) == []


def test_world_without_cast_renders_no_cast_section(gated_client: TestClient) -> None:
    """Story 65-13 (graceful path, route level). A POI-bearing but cast-less
    world renders 200 with NO Cast section — ``load_cast_entries`` returns ``[]``
    and ``assemble_lore_page`` omits the section entirely. ``poi_gated_fixture``
    authors POIs but no ``portrait_manifest.yaml``."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/poi_gated_fixture")
    assert resp.status_code == 200, resp.text
    assert '<section id="cast">' not in resp.text, "cast-less world must omit the Cast section"
    assert 'id="cast-' not in resp.text, "cast-less world must emit no Cast cards"


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


def test_cast_portrait_decisions_emit_spans(
    gated_client: TestClient, otel_capture: InMemorySpanExporter
) -> None:
    """Story 65-13 AC8 + complement rigor. Per-NPC observability (OTEL
    principle): the present NPC fires a ``sidequest.reference.portrait_resolved``
    span and the absent one fires ``sidequest.reference.portrait_not_found`` —
    both keyed by slug — so the GM/dev panel can distinguish 'authored & on R2'
    from 'authored, not on R2'.

    65-13 migrates these off the scene-time ``scrapbook.npc_portrait_*`` family
    (whose docstrings describe scene-invocation ref attachment, a semantic the
    reference page does not have) onto dedicated reference-namespaced spans, the
    same move 65-11 made for map pins.

    The **complement** assertions are the 65-13 rigor add: without them the span
    test passes even for an always-resolve gate (it would emit a resolved span
    for *both* NPCs). Asserting the absent slug is NOT in the resolved set and
    the present slug is NOT in the not-found set means the span test *alone*
    distinguishes a correct gate from a broken one.

    RED today: the presenter emits the scrapbook spans, and the
    ``sidequest.reference.portrait_*`` spans do not exist yet."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text

    resolved_slugs = {
        a.get("slug") for a in span_attrs_by_name(otel_capture, SPAN_REF_PORTRAIT_RESOLVED)
    }
    not_found_slugs = {
        a.get("slug") for a in span_attrs_by_name(otel_capture, SPAN_REF_PORTRAIT_NOT_FOUND)
    }

    assert _PRESENT_SLUG in resolved_slugs, (
        "present NPC must fire a reference portrait_resolved span"
    )
    assert _ABSENT_SLUG in not_found_slugs, (
        "absent NPC must fire a reference portrait_not_found span"
    )

    # Complement: a correct gate must NOT resolve the absent NPC, nor mark the
    # present NPC not-found. An always-resolve gate would fail exactly here.
    assert _ABSENT_SLUG not in resolved_slugs, (
        "absent NPC must NOT fire a portrait_resolved span (always-resolve gate)"
    )
    assert _PRESENT_SLUG not in not_found_slugs, (
        "present NPC must NOT fire a portrait_not_found span (never-resolve gate)"
    )


def test_cast_render_does_not_emit_scene_scrapbook_spans(
    gated_client: TestClient, otel_capture: InMemorySpanExporter
) -> None:
    """Story 65-13 (DOC span-semantics / AC8 migration proof). The reference
    Cast render must emit NEITHER ``scrapbook.npc_portrait_resolved`` NOR
    ``scrapbook.npc_portrait_not_found`` — those belong to the 65-6 scene-
    invocation path, whose docstrings describe attaching a portrait_url to a
    scrapbook ref (no scrapbook ref exists on the reference page). Reusing them
    here is the misleading-semantics finding 65-13 closes.

    RED today: ``_cast_portrait_img_html`` opens the scrapbook spans."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text

    assert not span_attrs_by_name(otel_capture, SPAN_SCENE_PORTRAIT_RESOLVED), (
        "reference Cast render must not emit the scene-time scrapbook resolved span"
    )
    assert not span_attrs_by_name(otel_capture, SPAN_SCENE_PORTRAIT_NOT_FOUND), (
        "reference Cast render must not emit the scene-time scrapbook not_found span"
    )


def test_reference_portrait_spans_are_registered() -> None:
    """Wiring: the new reference portrait spans must be registered in the span
    routing table (``FLAT_ONLY_SPANS``) so the GM/dev panel's ``agent_span_close``
    fan-out actually surfaces them — defining the contextmanager is not enough.
    Mirrors how the 65-11 map-pin spans are registered.

    RED today: the constants do not exist."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS

    assert SPAN_REF_PORTRAIT_RESOLVED in FLAT_ONLY_SPANS
    assert SPAN_REF_PORTRAIT_NOT_FOUND in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# AC4 — manifest_loaded span fires once per render with the EXACT fixture count
# ---------------------------------------------------------------------------


def test_cast_render_fires_manifest_loaded_span_with_exact_count(
    gated_client: TestClient, otel_capture: InMemorySpanExporter
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
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    # The gate's manifest is @lru_cache'd by Path. This test reads the SAME path
    # twice (absent, then present), so clear the cache up front and between the
    # two phases to defeat cross-phase and cross-test cached state.
    load_r2_manifest_keys.cache_clear()

    search_root = tmp_path / "packs"
    shutil.copytree(FIXTURE_ROOT / _PACK, search_root / _PACK)
    manifest_path = tmp_path / "r2_manifest.json"  # pack_dir.parent.parent / r2_manifest.json
    assert not manifest_path.exists(), "guard: no manifest two levels up"

    app = create_app(genre_pack_search_paths=[search_root])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")

    assert resp.status_code == 500, (
        f"absent manifest must fail loud (500), not a silent image-free page; got "
        f"{resp.status_code}"
    )

    # Manifest-specificity: the SAME fixture render succeeds (200) once the
    # manifest exists two levels up. This proves the 500 above is caused by the
    # absent manifest specifically — not a catch-all 500 from a broken fixture
    # pack that would fail regardless.
    load_r2_manifest_keys.cache_clear()
    shutil.copy(FIXTURE_MANIFEST, manifest_path)
    app_ok = create_app(genre_pack_search_paths=[search_root])
    with TestClient(app_ok, raise_server_exceptions=False) as client_ok:
        resp_ok = client_ok.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")

    assert resp_ok.status_code == 200, (
        f"same fixture + present manifest must render 200 — proving the 500 is "
        f"manifest-specific, not a broken fixture; got {resp_ok.status_code}: {resp_ok.text}"
    )


# ---------------------------------------------------------------------------
# Fix #4 — id-keyed gate end-to-end through assemble_lore_page
# ---------------------------------------------------------------------------


def _seed_id_keyed_cast_content_root(tmp_path: Path) -> tuple[Path, Path]:
    """Content-root fixture (oz shape): a manifest entry with ``id`` (the slug-
    shaped portrait key) distinct from ``name`` (the display heading), and the
    id-derived R2 key present two levels up. Mirrors the chrome-wiring fixture's
    pack-at-<root>/genre_packs layout so the gate's ``pack_dir.parent.parent /
    r2_manifest.json`` discovery lands inside ``tmp_path``."""
    from sidequest.server.reference_presenters import portrait_image_key

    pack = "wry_whimsy"
    world = "oz"
    portrait_id = "witch_of_the_west"
    display_name = "The Wicked Witch of the West"

    pack_dir = tmp_path / "genre_packs" / pack
    world_dir = pack_dir / "worlds" / world
    world_dir.mkdir(parents=True)

    (pack_dir / "theme.yaml").write_text(
        "primary: '#5C7A4F'\n"
        "accent: '#C9A96E'\n"
        "background: '#F4EBDA'\n"
        "archetype: parchment\n"
        "web_font_family: Lora\n"
        "display_font_family: Playfair Display\n"
        "dinkus:\n"
        "  glyph:\n"
        "    light: '—'\n"
        "    medium: '❧'\n"
        "    heavy: '❧❧❧'\n"
    )
    (pack_dir / "rules.yaml").write_text("core: fairytale\n")
    (world_dir / "world.yaml").write_text("name: Oz\n")
    (world_dir / "lore.yaml").write_text("world_name: Oz\nepigraph: Pay no attention.\n")
    (world_dir / "portrait_manifest.yaml").write_text(
        "characters:\n"
        f"  - id: {portrait_id}\n"
        f"    name: {display_name}\n"
        "    role: Tyrant of the Winkie Country\n"
        "    appearance: A withered crone with one telescopic eye.\n"
    )

    manifest = [_entry(portrait_image_key(pack, world, portrait_id))]
    (tmp_path / "r2_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir, world_dir


def test_id_keyed_cast_gate_resolves_portrait_and_heads_with_name(tmp_path: Path) -> None:
    """Fix #4 end-to-end: with an ``id``-bearing entry, the R2-existence gate and
    the per-card portrait key both derive from the ``id``, so the on-R2 portrait
    resolves; the heading shows the display ``name`` (never the snake_case id).

    If the gate still keyed on ``slugify(name)`` (= ``the_wicked_witch_of_the_west``)
    it would NOT match the ``witch_of_the_west`` manifest key and the portrait
    would be wrongly suppressed — so this proves gate/presenter agreement."""
    from sidequest.server.reference_renderer import assemble_lore_page, load_r2_manifest_keys

    load_r2_manifest_keys.cache_clear()
    pack_dir, world_dir = _seed_id_keyed_cast_content_root(tmp_path)
    html = assemble_lore_page("wry_whimsy", "oz", pack_dir, world_dir)

    # Card anchored on the id; heading is the display name; id never the heading.
    assert 'id="cast-witch_of_the_west"' in html
    assert "The Wicked Witch of the West</h3>" in html
    assert ">witch_of_the_west</h3>" not in html
    # The on-R2 portrait resolved (gate agreed with the id-derived key).
    expected = resolve_asset_url(portrait_key("wry_whimsy", "oz", "witch_of_the_west"))
    assert f'src="{expected}"' in html


def portrait_key(pack: str, world: str, slug: str) -> str:
    """Local alias to keep the assertion above readable."""
    from sidequest.server.reference_presenters import portrait_image_key

    return portrait_image_key(pack, world, slug)
