"""RED tests for Story 65-10 — lore-page TOC/section-mapping repair.

Background (scope reconciliation, recorded in the session + context doc): 65-10
entered the sprint as a title-only stub ("Reference TOC/section-mapping repair +
register POI and Cast sections"). Its sibling slices shipped FIRST and made the
literal title stale — 65-8 lit POI imagery (inline in the geography section, not
a registered section), 65-9 registered the Cast section, 65-11 the Map, 65-12 the
Timeline. So "register POI and Cast sections" is already done; the genuinely
remaining work is the **TOC/section-mapping repair** half (Operator decision
2026-06-03: "Build the repair").

Today the three dynamic sections register by THREE near-identical ad-hoc blocks in
``assemble_lore_page`` (reference_renderer.py ~1349-1424): each does
``body += html`` then ``kept_toc = [*kept_toc, {num, id, label}]``. There is:
  * NO server-side observability for the assembly decision — every *sub-feature*
    has a span (poi_image_*, map_rendered, portrait_*, timeline_rendered) but the
    TOC/section composition that stitches them emits NOTHING; and
  * NO server-side TOC<->section parity guard — the only dangling-link check is
    the CLIENT-side ``ref-bad-anchor`` banner (a JS island), invisible to OTEL.

This story adds the missing observability + guard (CLAUDE.md OTEL principle: every
subsystem decision emits a span; the GM panel is the lie detector) and DRYs the
three append blocks behind one helper.

Observable contract pinned by these tests (drives Dev; NOT yet implemented — RED).
Assertions target the contract through the REAL route + OTEL spans, never the
source text (server CLAUDE.md: no source-text wiring tests):

* A single ``sidequest.reference.lore_assembled`` span fires once per lore render,
  carrying the composed TOC: ``reference.lore_section_ids`` ("/"-joined, in
  composed order), ``reference.lore_section_count`` (int), and
  ``reference.lore_dynamic_sections`` ("/"-joined subset of cast/map/timeline that
  registered this render, "" when none).
* ``reference.lore_parity_ok`` (bool) is True iff EVERY composed TOC id has a
  matching ``id="..."`` anchor in the rendered body — the dangling-TOC-link
  guarantee the client banner only checks browser-side.
* The dynamic-section list is HONEST (complement across worlds): a Cast-only world
  reports "cast" and NOT map/timeline; a Map-only world reports "map" and NOT
  cast/timeline; a world with no dynamic sections reports "". This complement is
  what distinguishes a real registration record from an always-list constant.
* The parity GUARD is real: ``emit_lore_assembled_span`` given a TOC entry whose id
  is NOT among the body anchors returns ``parity_ok=False`` AND fires one
  ``sidequest.reference.lore_section_orphaned`` WARN span naming the ghost id
  (fail-visible, not silent — SOUL "No Silent Fallbacks").
* Regression: the combined world still renders all of reckoning/cast/map/timeline
  after the three ad-hoc blocks are unified, and ADR-135's single fixed public
  projection holds (``?audience`` does not change the composed section ids).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Span-capture helper lives in the server conftest (mirrors 65-9/65-11/65-12).
from tests.server.conftest import span_attrs_by_name

# --- Contract: span names (Dev registers these in FLAT_ONLY_SPANS) -----------
SPAN_LORE_ASSEMBLED = "sidequest.reference.lore_assembled"
SPAN_LORE_SECTION_ORPHANED = "sidequest.reference.lore_section_orphaned"

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"

# Combined world authoring ALL THREE dynamic sections (cast + map + timeline) plus
# the base reckoning section (lore.yaml). Composed TOC: reckoning/cast/map/timeline.
_ALL_WORLD = "assembled_fixture"
# Single-dynamic-section worlds — the honest-complement corpus.
_CAST_WORLD = "cast_gated_fixture"  # cast only
_MAP_WORLD = "map_fixture"  # map only
_TIMELINE_WORLD = "timeline_fixture"  # timeline only
_BARE_WORLD = "poi_fixture"  # no cast/map/timeline -> zero dynamic sections

_DYNAMIC = ("cast", "map", "timeline")


@pytest.fixture
def gated_client() -> Iterator[TestClient]:
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


def _assembled_attrs(otel_capture) -> list[dict]:
    return span_attrs_by_name(otel_capture, SPAN_LORE_ASSEMBLED)


def _split(joined: object) -> list[str]:
    """Split a "/"-joined span attribute into a list ("" -> [])."""
    s = "" if joined is None else str(joined)
    return [part for part in s.split("/") if part]


# ---------------------------------------------------------------------------
# AC1 — the assembly span fires exactly once per lore render
# ---------------------------------------------------------------------------


def test_lore_assembled_span_fires_once_per_render(gated_client: TestClient, otel_capture) -> None:
    """Every lore render emits exactly one ``lore_assembled`` span. RED today:
    assemble_lore_page records no assembly decision at all — only its
    sub-features (map_rendered, timeline_rendered, ...) emit spans."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = _assembled_attrs(otel_capture)
    assert len(spans) == 1, f"expected exactly one lore_assembled span, got {len(spans)}"
    attrs = spans[0]
    assert attrs.get("reference.pack") == _PACK
    assert attrs.get("reference.world") == _ALL_WORLD


def test_bare_world_still_emits_one_assembled_span(gated_client: TestClient, otel_capture) -> None:
    """A world with NO dynamic sections still emits exactly one assembly span
    (observability is per-render, not per-feature). Its dynamic-section list is
    empty — the span is the per-render record, not a dynamic-feature side effect."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_BARE_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = _assembled_attrs(otel_capture)
    assert len(spans) == 1, f"expected exactly one lore_assembled span, got {len(spans)}"
    assert _split(spans[0].get("reference.lore_dynamic_sections")) == [], (
        "a world with no cast/map/timeline must report an empty dynamic-section list"
    )


# ---------------------------------------------------------------------------
# AC2 — the span records the composed TOC (ordered ids + count)
# ---------------------------------------------------------------------------


def test_assembled_span_records_composed_section_ids(
    gated_client: TestClient, otel_capture
) -> None:
    """For the combined world the span carries the composed TOC in order —
    base reckoning first, then the dynamic cast/map/timeline appended in
    registration order — and a matching section count."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}")
    assert resp.status_code == 200, resp.text
    attrs = _assembled_attrs(otel_capture)[0]
    assert _split(attrs.get("reference.lore_section_ids")) == [
        "reckoning",
        "cast",
        "map",
        "timeline",
    ], f"composed section ids/order wrong: {attrs.get('reference.lore_section_ids')!r}"
    assert attrs.get("reference.lore_section_count") == 4


# ---------------------------------------------------------------------------
# AC3 — the dynamic-section list is honest (complement across worlds)
# ---------------------------------------------------------------------------


def test_combined_world_reports_all_three_dynamic_sections(
    gated_client: TestClient, otel_capture
) -> None:
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}")
    assert resp.status_code == 200, resp.text
    attrs = _assembled_attrs(otel_capture)[0]
    assert _split(attrs.get("reference.lore_dynamic_sections")) == ["cast", "map", "timeline"]


def test_cast_only_world_reports_cast_not_map_or_timeline(
    gated_client: TestClient, otel_capture
) -> None:
    """COMPLEMENT half 1: a Cast-only world reports exactly {cast}. The negative
    half (NOT map, NOT timeline) is what proves the list reflects real
    registration rather than an always-emitted constant."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text
    dyn = _split(_assembled_attrs(otel_capture)[0].get("reference.lore_dynamic_sections"))
    assert "cast" in dyn, f"cast-only world must register cast: {dyn}"
    assert "map" not in dyn and "timeline" not in dyn, (
        f"cast-only world must NOT register map/timeline: {dyn}"
    )


def test_map_only_world_reports_map_not_cast_or_timeline(
    gated_client: TestClient, otel_capture
) -> None:
    """COMPLEMENT half 2: a Map-only world reports exactly {map}."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    dyn = _split(_assembled_attrs(otel_capture)[0].get("reference.lore_dynamic_sections"))
    assert "map" in dyn, f"map-only world must register map: {dyn}"
    assert "cast" not in dyn and "timeline" not in dyn, (
        f"map-only world must NOT register cast/timeline: {dyn}"
    )


def test_timeline_only_world_reports_timeline_not_cast_or_map(
    gated_client: TestClient, otel_capture
) -> None:
    """COMPLEMENT half 3: a Timeline-only world reports exactly {timeline}."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_TIMELINE_WORLD}")
    assert resp.status_code == 200, resp.text
    dyn = _split(_assembled_attrs(otel_capture)[0].get("reference.lore_dynamic_sections"))
    assert "timeline" in dyn, f"timeline-only world must register timeline: {dyn}"
    assert "cast" not in dyn and "map" not in dyn, (
        f"timeline-only world must NOT register cast/map: {dyn}"
    )


# ---------------------------------------------------------------------------
# AC4 — parity is real and observable (positive: well-formed world)
# ---------------------------------------------------------------------------


def test_well_formed_world_reports_parity_ok_and_no_orphan_span(
    gated_client: TestClient, otel_capture
) -> None:
    """For a well-formed world ``parity_ok`` is True, EVERY composed TOC id has a
    real ``id="..."`` anchor in the body (no dangling nav link), and NO
    ``lore_section_orphaned`` span fires."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}")
    assert resp.status_code == 200, resp.text
    attrs = _assembled_attrs(otel_capture)[0]
    assert attrs.get("reference.lore_parity_ok") is True, "well-formed world must report parity_ok"
    for sid in _split(attrs.get("reference.lore_section_ids")):
        assert f'id="{sid}"' in resp.text, (
            f"composed TOC id {sid!r} has no matching anchor in the body (dangling link)"
        )
    assert span_attrs_by_name(otel_capture, SPAN_LORE_SECTION_ORPHANED) == [], (
        "a well-formed world must fire no orphan-section WARN span"
    )


# ---------------------------------------------------------------------------
# AC5 — parity GUARD negative branch (unit): drift is fail-visible
# ---------------------------------------------------------------------------


def test_emit_lore_assembled_span_flags_orphan_toc_entry(otel_capture) -> None:
    """The parity guard's negative branch. Given a composed TOC entry whose id is
    NOT among the body anchors, ``emit_lore_assembled_span`` must return
    ``parity_ok=False`` AND fire exactly one ``lore_section_orphaned`` WARN span
    naming the ghost id. This is the server-side analog of the client bad-anchor
    banner — drift surfaces in OTEL instead of only in the browser.

    RED today: ``emit_lore_assembled_span`` does not exist."""
    from sidequest.server.reference_renderer import emit_lore_assembled_span

    toc_entries = [
        {"num": "I", "id": "reckoning", "label": "The World"},
        {"num": "II", "id": "ghost_section", "label": "Ghost"},  # no matching anchor
    ]
    anchor_ids = {"reckoning", "hero", "ref-anchors"}  # 'ghost_section' deliberately absent

    parity_ok = emit_lore_assembled_span(
        pack=_PACK,
        world="unit_drift",
        toc_entries=toc_entries,
        anchor_ids=anchor_ids,
    )

    assert parity_ok is False, "an orphan TOC entry must make parity_ok False"
    orphans = span_attrs_by_name(otel_capture, SPAN_LORE_SECTION_ORPHANED)
    assert len(orphans) == 1, f"expected exactly one orphan WARN span, got {len(orphans)}"
    assert orphans[0].get("reference.section_id") == "ghost_section"
    assert orphans[0].get("reference.level") == "WARN"
    # The assembly span still fires once, recording the unhealthy parity.
    assembled = span_attrs_by_name(otel_capture, SPAN_LORE_ASSEMBLED)
    assert len(assembled) == 1, f"assembly span must still fire once, got {len(assembled)}"
    assert assembled[0].get("reference.lore_parity_ok") is False


def test_emit_lore_assembled_span_clean_input_fires_no_orphan(otel_capture) -> None:
    """COMPLEMENT to the negative branch: when every TOC id is anchored, the
    emitter returns ``parity_ok=True`` and fires NO orphan span — so the orphan
    span distinguishes a real drift from a healthy render, not an always-warn."""
    from sidequest.server.reference_renderer import emit_lore_assembled_span

    toc_entries = [
        {"num": "I", "id": "reckoning", "label": "The World"},
        {"num": "II", "id": "cast", "label": "Cast"},
    ]
    anchor_ids = {"reckoning", "cast", "hero"}

    parity_ok = emit_lore_assembled_span(
        pack=_PACK,
        world="unit_clean",
        toc_entries=toc_entries,
        anchor_ids=anchor_ids,
    )
    assert parity_ok is True
    assert span_attrs_by_name(otel_capture, SPAN_LORE_SECTION_ORPHANED) == []


# ---------------------------------------------------------------------------
# AC6 — regression + ADR-135 single fixed public projection
# ---------------------------------------------------------------------------


def test_all_dynamic_sections_still_render_after_repair(gated_client: TestClient) -> None:
    """Regression: unifying the three ad-hoc append blocks must not drop any
    section. The combined world still renders reckoning/cast/map/timeline as real
    anchors end-to-end."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}")
    assert resp.status_code == 200, resp.text
    for sid in ("reckoning", "cast", "map", "timeline"):
        assert f'id="{sid}"' in resp.text, f"section {sid!r} must still render after the repair"


def test_audience_param_does_not_change_composed_sections(
    gated_client: TestClient, otel_capture
) -> None:
    """ADR-135 D1: one fixed public projection. An ``?audience`` query param must
    not change the composed section ids the assembly span records."""
    gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}")
    gated_client.get(f"/reference/lore/{_PACK}/{_ALL_WORLD}?audience=gm")
    id_sets = {a.get("reference.lore_section_ids") for a in _assembled_attrs(otel_capture)}
    assert len(id_sets) == 1, (
        f"the audience param must not change the composed section ids: {id_sets}"
    )
