"""RED tests for Story 65-12 — lore-page world Timeline section.

The lore reference page (``GET /reference/lore/{pack}/{world}``, ADR-135) is a
public table tool. 65-8 lit up Points of Interest, 65-9 added Cast, 65-11 added
the Map. 65-12 adds a **Timeline**: a world-historical spine built from the
world's legends. Per the 65-12 design (Operator-decided 2026-06-03) the spine is
**legends only** — campaign ``history.yaml:chapters`` are a different (play-time)
axis and carry dormant-trope spoiler seeds (ADR-135 D1), and POI founding has no
authored field (deferred to 74-3). The one genuinely new behaviour is an
**honest conditional sort**: the temporal values authors write are free-text and
bespoke per world (absolute years, relative phrases, fantasy calendars, prose),
so the spine sorts numerically ONLY when every dated entry exposes a uniformly
parseable key, and otherwise preserves authored order — recording which mode
fired in OTEL so the page never claims a chronology it could not compute.

Observable contract pinned by these tests (drives Dev; NOT yet implemented — RED).
Assertions target this contract through the real route, never the source text
(server CLAUDE.md: no source-text wiring tests):

* ``assemble_lore_page`` appends a ``<section id="timeline">`` (heading
  "Timeline", semantic class ``ref-timeline``) and a TOC entry
  ``{"id": "timeline", "label": "Timeline"}`` for any world that has legends.
  A world with no legends renders unchanged (no Timeline section, HTTP 200). A
  legends.yaml that fails the typed loader (``Legend`` is ``extra="forbid"``)
  fails loud (HTTP 500) — never a silently timeline-less page.
* Each legend renders exactly one entry carrying
  ``data-timeline-entry="<slugify_player_name(name)>"`` (class
  ``ref-timeline__entry``). Entry count == legend count; nothing dropped.
* A *dated* entry (``era`` non-empty, else ``period`` non-empty) carries
  ``data-temporal="<verbatim era-or-period>"`` and renders that verbatim value
  in a chip (class ``ref-timeline__era``). An *undated* entry (neither) carries
  no ``data-temporal`` and is placed inside ``data-timeline-group="undated"``,
  after all dated entries.
* Order: the sequence of ``data-timeline-entry`` values is AUTHORED order,
  UNLESS every dated entry's temporal value is uniformly parseable — then dated
  entries are sorted ascending and undated entries follow (authored order).
* Span ``sidequest.reference.timeline_rendered`` fires once per render carrying
  ``reference.timeline_entry_count`` / ``reference.timeline_undated_count`` /
  ``reference.timeline_sort_mode`` (``"sorted"`` | ``"authored_order"``).
* If ``lore.yaml:history`` prose is present, the section carries it in a
  ``ref-timeline__preamble`` container (framing only, not decomposed into
  entries); absent history -> no preamble.
* Public projection only (ADR-135 D1): no ``history.yaml`` chapter label,
  ``session_range``, or trope-seed token appears in the section, and no
  ``?audience`` query param changes it.
* The verbatim temporal value is HTML-escaped in the chip (Python rule #11 /
  CWE-79): a raw ``<swords>`` must never appear unescaped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.utils import slugify_player_name

# Span-capture helper lives in the server conftest (mirrors the 65-9/65-11 tests).
from tests.server.conftest import span_attrs_by_name

# --- Contract: span name (Dev registers this in FLAT_ONLY_SPANS) -------------
SPAN_TIMELINE_RENDERED = "sidequest.reference.timeline_rendered"

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"

_MIXED_WORLD = "timeline_fixture"  # 4 dialects -> authored_order; 5 legends, 1 undated
_SORTED_WORLD = "timeline_sorted_fixture"  # uniform years -> sorted; 4 legends, 1 undated
_EMPTY_WORLD = "timeline_empty_fixture"  # no legends.yaml -> no Timeline section
_MALFORMED_WORLD = "timeline_malformed_fixture"  # extra-key legend -> typed loader 500
_ESCAPE_WORLD = "timeline_escape_fixture"  # era with HTML-special chars
_DIR_WORLD = "timeline_dir_fixture"  # per-file legends/ DIRECTORY form (the dominant real form)
_CAST_WORLD = "cast_gated_fixture"  # regression: has a Cast section, no legends

# Authored order of the mixed world (must be preserved — dialects not uniformly
# parseable). The Nameless Age is the lone undated entry (tail).
_MIXED_AUTHORED = [
    slugify_player_name(n)
    for n in (
        "The Cataclysm",
        "The Mesh Launch",
        "The Second Rising",
        "The Bowery Wake",
        "The Nameless Age",
    )
]
_MIXED_UNDATED = slugify_player_name("The Nameless Age")

# The mixed world's four dialect strings — each must render VERBATIM in a chip.
_DIALECTS = (
    "-11540",
    "15 years ago",
    "early Second Rising",
    "shot February 25, 1855, buried March 11",
)

# Sorted world: authored OUT of order (1804, undated, 1612, 1666). A correct
# ascending sort of the dated entries + undated-to-tail yields this order.
_SORTED_AUTHORED = [
    slugify_player_name(n)
    for n in ("The Second Charter", "The Hollow Year", "The Founding", "The Great Fire")
]
_SORTED_EXPECTED = [
    slugify_player_name(n)
    for n in ("The Founding", "The Great Fire", "The Second Charter", "The Hollow Year")
]
_SORTED_UNDATED = slugify_player_name("The Hollow Year")


@pytest.fixture
def gated_client() -> Iterator[TestClient]:
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


# --- helpers: scope every assertion to the Timeline section ------------------


def _timeline_section(html: str) -> str:
    """Return the ``<section id="timeline"> ... </section>`` slice, or fail loud.

    Scopes timeline assertions to the section so unrelated page chrome (the
    generic legends walk, other sections) can't satisfy an assertion by
    accident. RED today: there is no Timeline section, so this fails clearly.
    """
    marker = 'id="timeline"'
    idx = html.find(marker)
    assert idx != -1, 'no Timeline section (id="timeline") in the rendered lore page'
    start = html.rfind("<section", 0, idx)
    assert start != -1, "Timeline section marker not inside a <section> element"
    end = html.index("</section>", idx)
    return html[start:end]


def _entry_slugs(section: str) -> list[str]:
    """Ordered list of ``data-timeline-entry`` values as emitted (DOM order)."""
    return re.findall(r'data-timeline-entry="([^"]+)"', section)


def _temporal_values(section: str) -> list[str]:
    """All ``data-temporal`` values emitted in the section (dated entries only)."""
    return re.findall(r'data-temporal="([^"]*)"', section)


# ---------------------------------------------------------------------------
# AC1 — Timeline section registered + rendered (present / absent / loud)
# ---------------------------------------------------------------------------


def test_timeline_section_present_for_world_with_legends(gated_client: TestClient) -> None:
    """A world WITH legends gains a Timeline section and a 'Timeline' TOC entry.
    RED today: assemble_lore_page never builds a timeline from legends."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    assert "ref-timeline" in section, "Timeline section must carry the ref-timeline chrome class"
    assert ">Timeline<" in resp.text or 'id="timeline"' in resp.text


def test_no_legends_world_renders_without_timeline_section(gated_client: TestClient) -> None:
    """A world with NO legends.yaml renders unchanged: HTTP 200, no Timeline
    section, no error (the section is purely additive)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_EMPTY_WORLD}")
    assert resp.status_code == 200, resp.text
    assert 'id="timeline"' not in resp.text, "world without legends must have no Timeline section"


def test_malformed_legends_fails_loud_500() -> None:
    """legends.yaml carrying a schema-forbidden field must fail LOUD (HTTP 500)
    through the typed timeline loader, never a silently timeline-less 200
    (No Silent Fallbacks). RED today: the generic lore walk does not validate via
    ``Legend``, so the page currently renders 200 — the typed loader is what must
    make it 500."""
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/reference/lore/{_PACK}/{_MALFORMED_WORLD}")
    assert resp.status_code == 500, (
        f"a schema-invalid legends.yaml must fail loud (500), not a silent "
        f"timeline-less page; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# AC2 — entries from legends; nothing dropped; undated grouped
# ---------------------------------------------------------------------------


def test_every_legend_is_an_entry(gated_client: TestClient) -> None:
    """Every legend renders exactly one entry; entry count == legend count
    (the mixed world authors five legends). Pins the 'silently dropped a legend'
    bug."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    slugs = _entry_slugs(_timeline_section(resp.text))
    assert len(slugs) == 5, f"expected 5 timeline entries, got {len(slugs)}: {slugs}"
    assert set(slugs) == set(_MIXED_AUTHORED), (
        f"entry slugs must match the legends exactly: {slugs}"
    )


def test_undated_entry_is_grouped_and_dated_entries_are_not(gated_client: TestClient) -> None:
    """The neither-era-nor-period legend (The Nameless Age) lands in the
    ``data-timeline-group="undated"`` group; the four dated legends carry a
    ``data-temporal`` value and are not in that group."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    group_idx = section.find('data-timeline-group="undated"')
    assert group_idx != -1, (
        'an undated legend must be placed in a data-timeline-group="undated" group'
    )
    # The undated entry appears after the undated-group marker.
    undated_pos = section.find(f'data-timeline-entry="{_MIXED_UNDATED}"')
    assert undated_pos > group_idx, "the undated legend must sit inside the undated group"
    # Exactly four dated entries expose a temporal value.
    assert len(_temporal_values(section)) == 4, (
        f"exactly the four dated legends carry data-temporal: {_temporal_values(section)}"
    )


def test_directory_form_legends_render_a_timeline(gated_client: TestClient) -> None:
    """AC1/AC2 for the per-file ``legends/`` DIRECTORY authoring form — the
    dominant real form (five_points, evropi, franchise_nations, … author legends
    as a directory with NO flat legends.yaml). The Timeline must render for this
    form exactly as for the flat form: both legends become entries, and the dated
    one's era renders verbatim. Pins the spec-check Major: ``load_legends`` must
    read the directory form, not only the flat file."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_DIR_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    slugs = set(_entry_slugs(section))
    assert slugs == {slugify_player_name("The Charter"), slugify_player_name("The Drift")}, (
        f"both directory-form legends must render as entries: {slugs}"
    )
    assert "1500" in section, "the dated directory-form legend's era must render verbatim"


# ---------------------------------------------------------------------------
# AC3 — verbatim temporal chip, never normalized
# ---------------------------------------------------------------------------


def test_temporal_values_render_verbatim(gated_client: TestClient) -> None:
    """Each dialect string (absolute fantasy year, relative phrase, named-age
    period, prose date) renders VERBATIM in the section — never reformatted,
    parsed into a date, or truncated."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    for raw in _DIALECTS:
        assert raw in section, f"temporal value {raw!r} must render verbatim in the timeline"


# ---------------------------------------------------------------------------
# AC4 — honest conditional sort (sorted iff uniformly parseable, else authored)
# ---------------------------------------------------------------------------


def test_uniform_year_world_sorts_ascending(gated_client: TestClient) -> None:
    """When every dated entry is a clean year (uniformly parseable), dated
    entries sort ASCENDING — visibly reordering the out-of-order fixture — and
    the undated entry follows at the tail."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_SORTED_WORLD}")
    assert resp.status_code == 200, resp.text
    slugs = _entry_slugs(_timeline_section(resp.text))
    assert slugs == _SORTED_EXPECTED, f"dated entries must sort ascending by year: {slugs}"
    assert slugs != _SORTED_AUTHORED, "a real sort must reorder the out-of-order authored fixture"


def test_undated_entry_sorts_to_tail_in_sorted_mode(gated_client: TestClient) -> None:
    """The undated legend (authored in the MIDDLE) must move to the tail when the
    dated entries sort — undated entries always follow the dated spine."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_SORTED_WORLD}")
    assert resp.status_code == 200, resp.text
    slugs = _entry_slugs(_timeline_section(resp.text))
    assert slugs[-1] == _SORTED_UNDATED, f"undated entry must be last in sorted mode: {slugs}"


def test_mixed_dialect_world_preserves_authored_order(gated_client: TestClient) -> None:
    """When the dialects are NOT uniformly parseable, the spine must preserve
    AUTHORED order unchanged — no fabricated cross-dialect chronology."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    slugs = _entry_slugs(_timeline_section(resp.text))
    assert slugs == _MIXED_AUTHORED, f"mixed-dialect spine must keep authored order: {slugs}"


# ---------------------------------------------------------------------------
# AC5 — optional history preamble, no decomposition
# ---------------------------------------------------------------------------


def test_history_preamble_present_when_lore_history_authored(gated_client: TestClient) -> None:
    """When lore.yaml carries `history` prose, the Timeline section frames it in a
    ``ref-timeline__preamble`` container (not decomposed into entries)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    assert "ref-timeline__preamble" in section, "history prose must render as a timeline preamble"
    assert "TIMELINE_PREAMBLE_TOKEN" in section, (
        "the preamble must carry the authored history prose"
    )


def test_no_preamble_when_lore_history_absent(gated_client: TestClient) -> None:
    """A world whose lore.yaml has no `history` field renders the Timeline section
    with NO preamble container (entries only, no empty preamble node)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_SORTED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    assert "ref-timeline__preamble" not in section, "no history -> no preamble container"


# ---------------------------------------------------------------------------
# AC6 — OTEL on the render decision, with a sort-mode complement
# ---------------------------------------------------------------------------


def test_timeline_span_sorted_mode_carries_counts(gated_client: TestClient, otel_capture) -> None:
    """The sorted world fires exactly one ``timeline_rendered`` span recording
    ``sort_mode == "sorted"`` and the exact entry/undated counts — so the GM/dev
    panel can confirm the conditional sort engaged rather than the renderer
    improvising an order."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_SORTED_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = span_attrs_by_name(otel_capture, SPAN_TIMELINE_RENDERED)
    assert len(spans) == 1, f"expected exactly one timeline_rendered span, got {len(spans)}"
    attrs = spans[0]
    assert attrs.get("reference.timeline_sort_mode") == "sorted"
    assert attrs.get("reference.timeline_entry_count") == 4
    assert attrs.get("reference.timeline_undated_count") == 1


def test_timeline_span_sort_mode_complement(gated_client: TestClient, otel_capture) -> None:
    """COMPLEMENT assertion across two worlds: the uniform-year world reports
    ``sorted`` and the mixed-dialect world reports ``authored_order``. The
    complement is what distinguishes an honest conditional sort from an
    always-sort (which would mis-order the incommensurable dialects) or an
    always-authored gate (which would never sort the clean years)."""
    gated_client.get(f"/reference/lore/{_PACK}/{_SORTED_WORLD}")
    gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    modes = {
        a.get("reference.timeline_sort_mode")
        for a in span_attrs_by_name(otel_capture, SPAN_TIMELINE_RENDERED)
    }
    assert modes == {"sorted", "authored_order"}, (
        f"the two worlds must report complementary sort modes, got: {modes}"
    )


# ---------------------------------------------------------------------------
# AC7 — public projection only (ADR-135 D1): no campaign-chapter spoilers
# ---------------------------------------------------------------------------


def test_timeline_excludes_campaign_chapter_spoilers(gated_client: TestClient) -> None:
    """The mixed world's history.yaml carries a dormant-trope campaign chapter.
    None of its spoiler tokens (chapter label, trope id, trope note, session
    range) may appear in the public Timeline section — the spine is legends only
    (ADR-135 D1; the chapters axis is excluded by design)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    assert "SPOILER_" not in section, "no campaign-chapter spoiler token may leak into the timeline"
    assert "session_range" not in section, (
        "campaign session ranges must not appear in the world timeline"
    )


def test_no_query_param_changes_timeline_section(gated_client: TestClient) -> None:
    """ADR-135 D1: the page renders one fixed public projection. An ``?audience``
    query param must not change the Timeline section at all."""
    plain = _timeline_section(gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}").text)
    gm = _timeline_section(
        gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}?audience=gm").text
    )
    assert plain == gm, "no query param may change the timeline projection"


# ---------------------------------------------------------------------------
# AC8 — wiring + regression + chrome classes
# ---------------------------------------------------------------------------


def test_existing_sections_unchanged_by_timeline_feature(gated_client: TestClient) -> None:
    """Regression: a world with a Cast section (and no legends) still renders its
    Cast section and 200 — the additive Timeline feature does not disturb
    existing sections."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_CAST_WORLD}")
    assert resp.status_code == 200, resp.text
    assert 'id="cast-vivian_harbormaster"' in resp.text, "existing Cast section must still render"
    assert 'id="timeline"' not in resp.text, "a world with no legends gets no Timeline section"


def test_timeline_emits_semantic_chrome_classes(gated_client: TestClient) -> None:
    """The Timeline section emits the semantic CSS classes the chrome contract
    expects so the chrome-wiring suite can validate them. Dev must add matching
    CSS rules + extend the chrome-wiring seed world."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MIXED_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    for css_class in ("ref-timeline", "ref-timeline__entry", "ref-timeline__era"):
        assert css_class in section, f"timeline section must emit the semantic class {css_class!r}"


# ---------------------------------------------------------------------------
# Python rule #11 (CWE-79) — the verbatim chip must be HTML-escaped
# ---------------------------------------------------------------------------


def test_era_chip_is_html_escaped(gated_client: TestClient) -> None:
    """A legend whose era carries HTML-special characters must be escaped when
    interpolated into the chip — the raw ``<swords>`` must never appear
    unescaped, even though the chip is rendered 'verbatim' (semantic value
    preserved, safely escaped)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ESCAPE_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _timeline_section(resp.text)
    assert "<swords>" not in section, "raw HTML-special era must not appear unescaped"
    assert "&lt;swords&gt;" in section, "era must be HTML-escaped in the chip"
