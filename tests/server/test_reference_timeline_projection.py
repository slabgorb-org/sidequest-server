"""RED-phase contract tests — Story 100-5.

Phase 1, **Timeline section** slice of the reference-pages → React migration
(spec: ``docs/superpowers/specs/2026-06-08-reference-pages-react-migration-design.md``).

Slice A (map), 100-2 (generic-YAML), 100-3 (Cast), and 100-4 (POI) already
project public-only JSON that React renders. This story adds the **Timeline**
section: the JSON projection of the world-historical spine built from the
world's **legends** (Story 65-12), the data-shaping analog of the HTML
``present_lore_timeline`` presenter in ``reference_timeline.py``.

The Timeline differs from POI/Cast: it gates on no R2 art (legends emit no
images), and its one genuinely new behaviour is an **honest conditional sort**.
The temporal value authors write (``era``, falling back to ``period``) is
free-text and bespoke per world. So the spine sorts dated entries ascending
ONLY when every one exposes a uniformly-parseable key (a bare signed-integer
year); otherwise it preserves authored order. The projection records which mode
fired (``sort_mode``) on the SHIPPED Story 65-12 ``reference_timeline_rendered``
span so the page never claims a chronology it could not compute. Undated legends
(no era and no period) always follow the dated spine, in authored order.

Two firewalls govern this section — both load-bearing:

  1. **Section allowlist.** ``build_timeline_section`` projects each ``Legend``
     through a public allowlist (slug/name/summary/temporal). A naive
     ``legend.model_dump()`` splat would carry the keeper-side typed fields
     (``related_tropes`` — dormant-trope spoiler seeds per ADR-135 D1 — plus
     ``faction_grudges``, ``terrain_scars``, ``notable_figures``, …). Those must
     never cross.

  2. **Generic-YAML keeper carve (spec C1).** The SAME flat ``legends.yaml`` is
     ALSO projected as a generic-YAML node-tree (``legends`` is a PUBLIC stem in
     ``reference_visibility.py``, and ``legends.yaml`` is in
     ``LORE_WORLD_FILES`` and NOT in ``EXCLUDED_FILES``). With the stem-default
     PUBLIC, a legend's ``related_tropes`` leaks via the generic path TODAY
     (verified in the RED phase). ``classify()`` must carve the spoiler legend
     field KEEPER so it cannot cross either path.

**One TEA decision pins this RED phase (logged as a deviation in the session
file — read it before changing a test):**

  * **The keeper legend field is ``related_tropes``.** ``Legend`` is a typed
    pydantic model with ``extra="forbid"`` — you cannot author an arbitrary
    ``gm_notes``/``secret`` on a legend (load fails loud), so the firewall is
    about the ACCEPTED-but-keeper typed field. ``related_tropes`` is the
    defensible choice: the ``reference_timeline.py`` docstring names trope seeds
    as the legend spoiler axis (ADR-135 D1). The allowlist test additionally
    pins that no other non-public typed legend field crosses.

Contract this RED phase pins (Dev implements — ``build_timeline_section`` does
not exist yet, so the import fails and every test below is RED):

  - ``build_timeline_section(legends, *, history_prose) -> dict | None``
    Projects the world's legends into the public ``timeline`` section dict, or
    ``None`` when there are no legends. Mirrors ``present_lore_timeline``'s
    signature (legends + optional history-prose preamble; no R2 gate). Section
    shape:
        {"id": "timeline", "label": "Timeline",
         "sort_mode": "sorted" | "authored_order",
         "preamble": str | None,
         "entries": [{"slug": str, "name": str, "summary": str,
                      "temporal": str | None}, ...]}
    ``entries`` lists the dated spine first (sorted ascending by year iff EVERY
    dated entry is a clean signed-integer year, else authored order), then the
    undated legends (``temporal: None``) in authored order.

  - ``build_lore_projection`` (extended) appends a ``timeline`` section built
    from ``load_legends`` + ``load_lore_history``, AFTER the map section.

  - ``reference_visibility.classify()`` (extended) carves the spoiler legend
    field KEEPER for the ``legends`` stem so the generic-YAML path cannot leak it.

The member-dict shape itself is a Dev/Architect call; if Dev lands a different
but equally public-only shape, the SECURITY assertions (no keeper field in the
JSON, the conditional sort honest, sort_mode recorded) are the non-negotiable
ones — do not weaken them to fit a shape change. Shape-only assertions are
flagged ``[shape]``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.genre.models.legends import Legend

# RED: this import fails until Dev adds the Timeline-section projection builder.
from sidequest.server.reference_projection import (
    build_generic_yaml_section,
    build_lore_projection,
    build_timeline_section,
)
from sidequest.server.reference_routes import create_reference_router
from sidequest.server.utils import slugify_player_name
from sidequest.telemetry.spans.reference import SPAN_REFERENCE_TIMELINE_RENDERED
from tests.server.conftest import span_attrs_by_name

# ---------------------------------------------------------------------------
# Fixtures — typed Legend records (the public Timeline source). Each legend
# carries name/summary and a temporal value (era, falling back to period). The
# slug is slugify_player_name(name) — the same rule present_lore_timeline uses.
# ---------------------------------------------------------------------------


def _legend(name: str, summary: str, **extra: object) -> Legend:
    return Legend.model_validate({"name": name, "summary": summary, **extra})


def _member_by_name(section: dict, name: str) -> dict:
    return next(m for m in section["entries"] if m["name"] == name)


# The public allowlist — the maximal set of keys that may cross the boundary.
_PUBLIC_KEYS = {"slug", "name", "summary", "temporal"}

# Non-public typed Legend fields — accepted by the model (extra="forbid" forbids
# only UNKNOWN keys) but keeper-side; none may cross onto a projected member.
_NON_PUBLIC_LEGEND_FIELDS = (
    "related_tropes",
    "notable_figures",
    "faction_grudges",
    "terrain_scars",
    "monuments",
    "lost_arts",
    "cultural_impact",
    "affected_cultures",
    "details",
    "id",
    "culture",
    "period",
)


# ===========================================================================
# Group 1 — Basics: the section envelope, public fields, None on empty.
# ===========================================================================


def test_no_legends_projects_to_none():
    assert build_timeline_section([], history_prose=None) is None


def test_legends_project_into_timeline_section():
    section = build_timeline_section(
        [
            _legend("The Sundering", "The sky cracked and the old empire fell.", era="1612"),
            _legend("The Long Silence", "A century without song.", era="1700"),
        ],
        history_prose=None,
    )
    assert section is not None
    # [shape] section envelope.
    assert section["id"] == "timeline"
    assert section["label"] == "Timeline"
    names = {m["name"] for m in section["entries"]}
    assert names == {"The Sundering", "The Long Silence"}
    sundering = _member_by_name(section, "The Sundering")
    assert sundering["summary"] == "The sky cracked and the old empire fell."
    assert sundering["temporal"] == "1612"
    # The slug follows the shared slugify_player_name rule.
    assert sundering["slug"] == slugify_player_name("The Sundering")


def test_period_used_as_temporal_when_no_era():
    # _temporal_of falls back to ``period`` when ``era`` is empty.
    section = build_timeline_section(
        [_legend("The Founding", "Stones laid by the first hands.", period="First Age")],
        history_prose=None,
    )
    member = section["entries"][0]
    assert member["temporal"] == "First Age"


def test_history_prose_becomes_preamble():
    section = build_timeline_section(
        [_legend("The Sundering", "The sky cracked.", era="1612")],
        history_prose="In the beginning the world was whole.",
    )
    assert section["preamble"] == "In the beginning the world was whole."


def test_no_history_prose_yields_null_preamble():
    section = build_timeline_section(
        [_legend("The Sundering", "The sky cracked.", era="1612")],
        history_prose=None,
    )
    assert section["preamble"] is None


# ===========================================================================
# Group 2 — The honest conditional sort. Dated entries sort ascending ONLY when
#           every one is a clean signed-integer year; else authored order.
#           Undated legends always follow the dated spine, in authored order.
# ===========================================================================


def test_dated_entries_sorted_ascending_when_all_clean_years():
    # Authored out of order; every era is a bare integer → sort ascending.
    section = build_timeline_section(
        [
            _legend("Late Event", "Last.", era="1612"),
            _legend("Ancient Event", "First.", era="-200"),
            _legend("Middle Event", "Between.", era="1450"),
        ],
        history_prose=None,
    )
    assert section["sort_mode"] == "sorted"
    ordered = [m["name"] for m in section["entries"]]
    assert ordered == ["Ancient Event", "Middle Event", "Late Event"], (
        "dated entries with uniformly-parseable years must sort ascending"
    )


def test_authored_order_preserved_when_a_date_is_unparseable():
    # One era is a named age, not an integer → the WHOLE spine falls back to
    # authored order rather than fabricating a cross-dialect chronology.
    section = build_timeline_section(
        [
            _legend("Second", "B.", era="1612"),
            _legend("First", "A.", era="early Second Rising"),
            _legend("Third", "C.", era="1450"),
        ],
        history_prose=None,
    )
    assert section["sort_mode"] == "authored_order"
    ordered = [m["name"] for m in section["entries"]]
    assert ordered == ["Second", "First", "Third"], (
        "a non-integer date in the set forces authored-order for the whole spine"
    )


def test_undated_legends_follow_the_dated_spine():
    # Undated legends (no era and no period) always come AFTER the dated spine,
    # in authored order, regardless of sort mode.
    section = build_timeline_section(
        [
            _legend("Undated A", "No date A."),
            _legend("Dated Late", "Late.", era="1612"),
            _legend("Undated B", "No date B."),
            _legend("Dated Early", "Early.", era="1100"),
        ],
        history_prose=None,
    )
    assert section["sort_mode"] == "sorted"
    ordered = [m["name"] for m in section["entries"]]
    # Dated spine first (sorted ascending), then undated in authored order.
    assert ordered == ["Dated Early", "Dated Late", "Undated A", "Undated B"]
    # Undated members carry temporal: None.
    undated_a = _member_by_name(section, "Undated A")
    assert undated_a["temporal"] is None


def test_all_undated_legends_project_in_authored_order():
    section = build_timeline_section(
        [
            _legend("Gamma", "g."),
            _legend("Alpha", "a."),
            _legend("Beta", "b."),
        ],
        history_prose=None,
    )
    assert section["sort_mode"] == "authored_order", (
        "with no dated entries there is no chronology to compute → authored order"
    )
    assert [m["name"] for m in section["entries"]] == ["Gamma", "Alpha", "Beta"]
    assert all(m["temporal"] is None for m in section["entries"])


# ===========================================================================
# Group 3 — Keeper firewall: no keeper legend field crosses the JSON boundary,
#           through EITHER the section allowlist OR the generic-YAML path.
#           Load-bearing. Do not weaken.
# ===========================================================================


def test_timeline_member_carries_only_allowlisted_keys():
    # An allowlist projection (not a denylist) is the robust firewall: only the
    # known-public keys cross. A naive legend.model_dump() splat would carry the
    # keeper-side typed fields — this pins that it does not.
    section = build_timeline_section(
        [
            _legend(
                "The Sundering",
                "The sky cracked.",
                era="1612",
                related_tropes=["the_duke_betrays_you_in_act_three"],
                notable_figures=["The Hidden Duke"],
                cultural_impact="the keeper's secret weighting",
                details="GM-only staging notes",
            )
        ],
        history_prose=None,
    )
    member = section["entries"][0]
    # [shape] the public key set — reconcilable if Dev's allowlist differs, but
    # it must be a SUBSET that excludes every keeper field.
    assert set(member.keys()) <= _PUBLIC_KEYS
    # The 4 context-named public keys are present.
    assert {"slug", "name", "summary", "temporal"} <= set(member.keys())
    for field in _NON_PUBLIC_LEGEND_FIELDS:
        assert field not in member, f"keeper legend field {field!r} leaked onto the member"


def test_keeper_legend_value_never_crosses_the_section_boundary():
    section = build_timeline_section(
        [
            _legend(
                "The Sundering",
                "The sky cracked.",
                era="1612",
                related_tropes=["the_duke_betrays_you_in_act_three"],
            )
        ],
        history_prose=None,
    )
    blob = _json.dumps(section)
    assert "the_duke_betrays_you_in_act_three" not in blob, (
        "a legend's related_tropes (dormant-trope spoiler seed) must not cross "
        "the JSON boundary via the Timeline section"
    )
    # The public fields DID survive (the section is not just empty).
    assert "The Sundering" in blob
    assert "The sky cracked." in blob


def test_generic_yaml_legends_blocks_related_tropes():
    # Isolate the classify() generic-YAML gate from the section allowlist: call
    # build_generic_yaml_section directly with a flat legends list carrying a
    # spoiler related_tropes, and assert it does NOT cross. legends is a PUBLIC
    # stem, so without a KEEPER carve the field leaks (verified in RED). This
    # proves the carve is wired in the generic path independently of
    # build_timeline_section.
    data = [
        {
            "name": "The Sundering",
            "summary": "The sky cracked.",
            "era": "1612",
            "related_tropes": ["the_duke_betrays_you_in_act_three"],
        }
    ]
    section = build_generic_yaml_section(data, file_stem="legends", pack="p", world="w")
    assert section is not None
    blob = _json.dumps(section)
    assert "the_duke_betrays_you_in_act_three" not in blob, (
        "a legend's related_tropes must NOT cross via the generic-YAML legends "
        "path (spec C1 — classify() must carve it KEEPER)"
    )
    # The public legend fields DO survive the generic projection.
    assert "The Sundering" in blob
    assert "The sky cracked." in blob


def test_generic_yaml_legends_map_form_blocks_related_tropes():
    # The SECOND authoring shape _load_legends_flexible accepts: a {legends: [...]}
    # MAP (genre/loader.py:329,353-356 — historically road_warrior), not the flat
    # Vec. The generic-YAML path reads legends.yaml RAW, so this shape nests the
    # field one level deeper — key path ("legends","*","related_tropes") — needing
    # its OWN classify() carve distinct from the flat-Vec ("*","related_tropes")
    # one. Without this test, deleting the map-form carve regresses 0 tests
    # (vacuous-firewall gap, PR #770 review round 1): a map-form world with
    # related_tropes would leak uncaught.
    data = {
        "legends": [
            {
                "name": "The Sundering",
                "summary": "The sky cracked.",
                "era": "1612",
                "related_tropes": ["the_duke_betrays_you_in_act_three"],
            }
        ]
    }
    section = build_generic_yaml_section(data, file_stem="legends", pack="p", world="w")
    assert section is not None
    blob = _json.dumps(section)
    assert "the_duke_betrays_you_in_act_three" not in blob, (
        "a legend's related_tropes must NOT cross via the generic-YAML legends "
        "path in the {legends: [...]} MAP form either (spec C1 — the map-form "
        "classify() carve is load-bearing and independently tested)"
    )
    # The public legend fields DO survive the generic projection.
    assert "The Sundering" in blob
    assert "The sky cracked." in blob


# ===========================================================================
# Group 4 — OTEL wiring: the projection fires the SHIPPED Story 65-12 timeline
#           span (reuse, not a new span) carrying the entry census + sort_mode.
#           CLAUDE.md OTEL Observability Principle: the GM/dev panel reads
#           sort_mode to know whether a chronology was computed or fell back.
# ===========================================================================


def test_timeline_fires_rendered_span_with_sorted_mode(otel_capture) -> None:
    build_timeline_section(
        [
            _legend("Late", "l.", era="1612"),
            _legend("Early", "e.", era="1100"),
            _legend("Undated", "u."),
        ],
        history_prose=None,
    )
    spans = span_attrs_by_name(otel_capture, SPAN_REFERENCE_TIMELINE_RENDERED)
    assert len(spans) == 1, f"expected one timeline_rendered span, got {len(spans)}"
    attrs = spans[0]
    assert attrs.get("reference.timeline_entry_count") == 3
    assert attrs.get("reference.timeline_undated_count") == 1
    assert attrs.get("reference.timeline_sort_mode") == "sorted"


def test_timeline_span_records_authored_order_fallback(otel_capture) -> None:
    build_timeline_section(
        [
            _legend("A", "a.", era="early Second Rising"),
            _legend("B", "b.", era="1612"),
        ],
        history_prose=None,
    )
    spans = span_attrs_by_name(otel_capture, SPAN_REFERENCE_TIMELINE_RENDERED)
    assert len(spans) == 1
    assert spans[0].get("reference.timeline_sort_mode") == "authored_order", (
        "the span must honestly record the authored-order fallback (lie-detector)"
    )


# ===========================================================================
# Group 5 — Wiring: the Timeline section appears in the full lore document AND
#           is reachable through the production HTTP endpoint, built from a real
#           legends.yaml end-to-end (CLAUDE.md: every test suite needs a wiring
#           test that hits a production code path, not just unit-isolated).
# ===========================================================================


def _world_dir_with_legends(
    tmp_path: Path, *, with_keeper: bool = False, with_map: bool = True
) -> Path:
    """Seed a minimal world with a flat ``legends.yaml`` (top-level Vec<Legend>,
    the form the generic-YAML path reads). When ``with_keeper`` the legend carries
    a spoiler ``related_tropes``. When ``with_map`` (default) a pin-less
    ``cartography.yaml`` is seeded so the assembled document carries a ``map``
    section — this is what makes the map-then-timeline ordering invariant
    testable."""
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    legend = (
        "- name: The Sundering\n"
        "  summary: The sky cracked and the old empire fell.\n"
        '  era: "1612"\n'
    )
    if with_keeper:
        legend += "  related_tropes:\n    - the_duke_betrays_you_in_act_three\n"
    (world_dir / "legends.yaml").write_text(legend, encoding="utf-8")

    if with_map:
        (world_dir / "cartography.yaml").write_text(
            "starting_region: harbor\n"
            "regions:\n"
            "  harbor: {name: The Harbor, summary: Salt docks., "
            "description: Fog and hulls., adjacent: [market]}\n"
            "  market: {name: Night Market, summary: Lit stalls., "
            "description: Spice and smoke., adjacent: [harbor]}\n",
            encoding="utf-8",
        )
    return world_dir


def test_lore_projection_includes_timeline_section(tmp_path: Path):
    world_dir = _world_dir_with_legends(tmp_path, with_map=False)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "timeline" in section_ids, "the assembled lore document must carry a timeline section"
    timeline = next(s for s in doc["sections"] if s["id"] == "timeline")
    assert [m["name"] for m in timeline["entries"]] == ["The Sundering"]


def test_lore_projection_timeline_after_map_section(tmp_path: Path):
    # The Timeline section is appended AFTER the map section. The fixture seeds
    # BOTH cartography (→ map section) and a legend (→ timeline section), so both
    # ids are present and the ordering assertion actually executes.
    world_dir = _world_dir_with_legends(tmp_path, with_map=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "map" in section_ids, "fixture must materialise a map section for this AC to be testable"
    assert "timeline" in section_ids
    assert section_ids.index("timeline") > section_ids.index("map"), (
        "the Timeline section must be appended after the map section"
    )


def test_lore_projection_omits_timeline_when_no_legends(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "timeline" not in section_ids


def test_lore_projection_timeline_keeper_field_never_crosses(tmp_path: Path):
    # Whole-document firewall: the spoiler related_tropes must not survive ANY
    # section — neither the Timeline allowlist nor the generic-YAML legends path.
    world_dir = _world_dir_with_legends(tmp_path, with_keeper=True, with_map=False)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    blob = _json.dumps(doc)
    assert "the_duke_betrays_you_in_act_three" not in blob, (
        "a legend's related_tropes must NOT cross the JSON boundary via the "
        "Timeline section OR the generic-YAML legends projection (spec C1)"
    )
    # The public legend still projects (the section is not just empty).
    timeline = next((s for s in doc["sections"] if s["id"] == "timeline"), None)
    assert timeline is not None
    assert [m["name"] for m in timeline["entries"]] == ["The Sundering"]


def _client(tmp_path: Path) -> TestClient:
    """Build a real FastAPI app with the production reference router, pointed at
    a tmp pack root carrying a legends-bearing world."""
    pack_dir = tmp_path / "pulp_noir"
    world_dir = pack_dir / "worlds" / "annees_folles"
    world_dir.mkdir(parents=True)
    # Story 100-7: the lore endpoint attaches a CSS-var theme token set; a real
    # pack always carries theme.yaml, so the synthetic pack seeds one or it 500s.
    (pack_dir / "theme.yaml").write_text(
        "archetype: parchment\n"
        "primary: '#1A1A1A'\n"
        "secondary: '#3A3A3A'\n"
        "accent: '#B08D57'\n"
        "background: '#0E0E0E'\n"
        "surface: '#161616'\n"
        "text: '#D8D2C4'\n"
        "web_font_family: Lora\n"
        "display_font_family: Cinzel\n"
        "dinkus:\n"
        "  glyph: {light: '·', medium: '· · ·', heavy: '◆ ◆ ◆'}\n",
        encoding="utf-8",
    )
    (world_dir / "legends.yaml").write_text(
        "- name: The Sundering\n"
        "  summary: The sky cracked and the old empire fell.\n"
        '  era: "1612"\n',
        encoding="utf-8",
    )
    app = FastAPI()
    app.state.genre_pack_search_paths = [str(tmp_path)]
    app.include_router(create_reference_router())
    return TestClient(app)


def test_lore_api_endpoint_returns_timeline_section(tmp_path: Path):
    # The production HTTP path: GET /reference/api/lore/{pack}/{world} →
    # lore_api → build_lore_projection. Proves the Timeline section is reachable
    # end-to-end through the real route, not just the unit-isolated builder.
    resp = _client(tmp_path).get("/reference/api/lore/pulp_noir/annees_folles")
    assert resp.status_code == 200
    doc = resp.json()
    section_ids = [s["id"] for s in doc["sections"]]
    assert "timeline" in section_ids, (
        "the Timeline section must be reachable through the production /reference/api/lore endpoint"
    )
    timeline = next(s for s in doc["sections"] if s["id"] == "timeline")
    assert [m["name"] for m in timeline["entries"]] == ["The Sundering"]
