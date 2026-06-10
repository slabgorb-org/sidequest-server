"""RED — Story 101-8 AC2: a diacritic-named NPC and POI RESOLVE their R2 asset
under the unified NFKD-fold rule (no silent 404).

Wiring proof per server CLAUDE.md — OTEL span assertions + fixture-driven
behavior through the REAL projection builders (``build_cast_section`` /
``build_poi_section``), never a source-text grep.

The setup mirrors production: the render/daemon side writes the asset under the
UNIFIED fold slug, so the on-R2 slug set passed to the projection is the folded
form. Today the consumer derives a DIFFERENT slug (rule 2 drops the diacritic;
rule 3 splits on it), so the folded asset is missed → ``*_not_found`` span +
withheld member. After Dev re-points the slug helpers to the fold, the consumer
derives the same folded slug → ``*_resolved`` span + projected member.

Golden vector ``"Srárný Fyzioloniązka"`` matches the unit suite
(``test_101_8_slug_unification.py``).
"""

from __future__ import annotations

from sidequest.server.reference_projection import (
    build_cast_section,
    build_poi_section,
)
from sidequest.telemetry.spans.reference import (
    SPAN_REFERENCE_POI_IMAGE_NOT_FOUND,
    SPAN_REFERENCE_POI_IMAGE_RESOLVED,
    SPAN_REFERENCE_PORTRAIT_NOT_FOUND,
    SPAN_REFERENCE_PORTRAIT_RESOLVED,
)
from tests.server.conftest import span_attrs_by_name

_DIACRITIC = "Srárný Fyzioloniązka"
# Folded forms the render/daemon side writes to R2 (per session 101-8 decision).
_PORTRAIT_FOLDED = "srarny_fyzioloniazka"  # portrait surface keeps '_'
_POI_ANCHOR_FOLDED = "srarny-fyzioloniazka"  # reference/POI anchor keeps '-'


# ---------------------------------------------------------------------------
# NPC portrait — Cast projection.
# ---------------------------------------------------------------------------


def test_diacritic_npc_portrait_resolves_under_unified_fold(otel_capture) -> None:
    # The portrait IS on R2 under the folded slug. The Cast member's slug
    # (cast_portrait_slug → slugify_player_name) must derive the SAME folded
    # slug so the gate hits and the portrait resolves.
    section = build_cast_section(
        [{"name": _DIACRITIC, "role": "Physiologist", "appearance": "Pale, exact."}],
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset({_PORTRAIT_FOLDED}),
    )
    assert section is not None, "the diacritic NPC must project (its portrait is on R2)"
    member = section["members"][0]
    assert member["slug"] == _PORTRAIT_FOLDED, (
        "the Cast slug must be the unified NFKD-fold form, matching the R2 key"
    )
    assert member["portrait_url"] is not None, (
        "diacritic NPC portrait must RESOLVE under the unified fold — not a silent 404"
    )

    resolved = span_attrs_by_name(otel_capture, SPAN_REFERENCE_PORTRAIT_RESOLVED)
    assert any(a.get("slug") == _PORTRAIT_FOLDED for a in resolved), (
        "a resolved diacritic portrait must fire portrait_resolved with the folded slug"
    )
    not_found = span_attrs_by_name(otel_capture, SPAN_REFERENCE_PORTRAIT_NOT_FOUND)
    assert not any(a.get("slug") == _PORTRAIT_FOLDED for a in not_found), (
        "the folded portrait is on R2 — it must not fire portrait_not_found"
    )


# ---------------------------------------------------------------------------
# POI landscape — POI projection.
# ---------------------------------------------------------------------------


def test_diacritic_poi_resolves_under_unified_fold(otel_capture) -> None:
    # The landscape IS on R2 under the folded anchor. build_poi_section derives
    # the membership anchor via reference_slug.slugify(verbatim); it must equal
    # the folded anchor so the gate hits and the POI projects.
    section = build_poi_section(
        [{"name": _DIACRITIC, "region": "North Reach", "description": "A cold lab."}],
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_POI_ANCHOR_FOLDED}),
    )
    assert section is not None, "the diacritic POI must project (its landscape is on R2)"
    member = section["entries"][0]
    assert member["slug"] == _POI_ANCHOR_FOLDED, (
        "the POI anchor must be the unified NFKD-fold form, matching the R2 gate"
    )

    # The POI span keys the slug under "reference.slug" (via _poi_attrs), unlike
    # the portrait span which uses bare "slug".
    resolved = span_attrs_by_name(otel_capture, SPAN_REFERENCE_POI_IMAGE_RESOLVED)
    assert any(a.get("reference.slug") == _POI_ANCHOR_FOLDED for a in resolved), (
        "a resolved diacritic POI must fire poi_image_resolved with the folded anchor"
    )
    not_found = span_attrs_by_name(otel_capture, SPAN_REFERENCE_POI_IMAGE_NOT_FOUND)
    assert not any(a.get("reference.slug") == _POI_ANCHOR_FOLDED for a in not_found), (
        "the folded landscape is on R2 — it must not fire poi_image_not_found"
    )
