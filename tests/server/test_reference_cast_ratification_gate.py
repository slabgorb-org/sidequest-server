"""RED tests for Story 75-13 — the ADR-138 §D4 ratification gate on the ADR-135
reference Cast projection.

Background. ADR-138 makes *ratification* the single projection-eligibility gate
shared by every projection surface (ADR-138 §D1/§D3, the ``is_projectable()``
predicate landed in 75-11). Story 75-12 wired it into the ADR-118 retrieval index:
an unratified, ``observation_pending`` pool member is withheld from the semantic
index and the skip is reported to the GM panel (``entity_sync.npc_unratified_skipped``).
Story 75-13 is the **reference-page leg** — the public Cast section of the lore page
(``GET /reference/lore/{pack}/{world}``, ADR-135) must withhold the same unratified
phantom for the same reason: the world has not committed to it.

Safe-by-design (the §D4 invariant). Authored ``portrait_manifest.yaml`` content is
never auto-minted, so ``observation_pending`` is ``False`` by construction — the gate
should withhold *nothing* in practice. The gate exists defensively so the two surfaces
(retrieval index + reference page) enforce ONE rule via the SAME predicate, and a
future change that lets an unratified member reach the manifest cannot leak a phantom
onto a player-facing page. "Handled loudly" here means the skip is **observable** (an
OTEL span carrying the count), mirroring 75-12 — not a 500.

Contract pinned by these tests (drives Dev; NOT yet implemented — RED). Every
assertion targets behavior through the REAL ``/reference/lore`` route + OTEL spans,
never the source text (server CLAUDE.md: "No Source-Text Wiring Tests"):

* ``assemble_lore_page`` filters the authored Cast entries through the ratification
  gate (``sidequest.game.npc_pool.is_projectable`` — the 75-11 single source of
  truth) before rendering. A ratified entry renders its card; an
  ``observation_pending: true`` entry is withheld (no ``id="cast-{slug}"`` card,
  name absent from the page).
* A ``sidequest.reference.npc_unratified_skipped`` span fires once per render that
  has authored Cast entries, carrying ``reference.npc_unratified_skipped_count`` —
  the number of withheld unratified members. It fires even when the count is 0 (a
  ratified-only world): 0 is a valid, observable count, the lie-detector that
  distinguishes "clean cast, gate ran" from "gate never ran".
* The count is computed from the RAW authored entries (the gate input), so an
  all-unratified world fires the span with count == N even though it renders no
  Cast section at all.
* The span constant is registered in ``FLAT_ONLY_SPANS`` so the GM/dev panel's
  ``agent_span_close`` fan-out surfaces it (defining the contextmanager is not
  enough — mirrors the 65-13 portrait-span registration).

The portrait gate, manifest loading, HTML escaping, and Cast-section omission for
cast-less worlds are covered by 65-9/65-13 and are NOT re-tested here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.server.utils import slugify_player_name

# Span-capture helper lives in the server conftest (mirrors 65-9/65-10/65-13).
from tests.server.conftest import span_attrs_by_name

# --- Contract: the span Dev must add + register ------------------------------
SPAN_NPC_UNRATIFIED_SKIPPED = "sidequest.reference.npc_unratified_skipped"
ATTR_SKIPPED_COUNT = "reference.npc_unratified_skipped_count"

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"

# Selective-withhold world: one ratified + one observation_pending member.
_RATIFY_WORLD = "cast_ratification_fixture"
_RATIFIED_NAME = "Marrow Quill"  # ratified — renders in the Cast section
_PENDING_NAME = "Hollow Bessom"  # observation_pending — withheld
_RATIFIED_SLUG = slugify_player_name(_RATIFIED_NAME)  # -> marrow_quill
_PENDING_SLUG = slugify_player_name(_PENDING_NAME)  # -> hollow_bessom

# All-unratified edge world: the ONLY member is a phantom -> no Cast section,
# but the skip span still fires with count == 1.
_ALL_UNRATIFIED_WORLD = "cast_all_unratified_fixture"
_LONE_PHANTOM_NAME = "Threnody Vane"
_LONE_PHANTOM_SLUG = slugify_player_name(_LONE_PHANTOM_NAME)  # -> threnody_vane

# None-withheld baseline: cast_gated_fixture authors TWO ratified NPCs (no
# observation_pending flag). Reused so the gate's count==0 path is pinned on a
# fixture the 65-9/65-13 suite already trusts — doubling as an over-suppression
# regression guard (the gate must NOT drop ratified members).
_RATIFIED_ONLY_WORLD = "cast_gated_fixture"


@pytest.fixture
def gated_client() -> Iterator[TestClient]:
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


def _skip_spans(otel_capture) -> list[dict]:
    return span_attrs_by_name(otel_capture, SPAN_NPC_UNRATIFIED_SKIPPED)


# ---------------------------------------------------------------------------
# §D4 behavior — the gate withholds the phantom, keeps the ratified member
# ---------------------------------------------------------------------------


def test_ratified_cast_member_renders(gated_client: TestClient) -> None:
    """The ratified member renders her card end-to-end through the real route —
    the gate must not over-suppress committed content."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_RATIFY_WORLD}")
    assert resp.status_code == 200, resp.text
    assert f'id="cast-{_RATIFIED_SLUG}"' in resp.text, (
        "ratified Cast member must render her card"
    )
    assert _RATIFIED_NAME in resp.text, "ratified Cast member's name must appear on the page"


def test_unratified_cast_member_withheld(gated_client: TestClient) -> None:
    """THE §D4 gate. The ``observation_pending`` phantom is withheld from the
    public reference page: no card, and her name never reaches the player. RED
    today — the Cast projection renders every authored entry unconditionally."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_RATIFY_WORLD}")
    assert resp.status_code == 200, resp.text
    assert f'id="cast-{_PENDING_SLUG}"' not in resp.text, (
        "unratified phantom must NOT render a Cast card"
    )
    assert _PENDING_NAME not in resp.text, (
        "unratified phantom's name must not leak onto the public reference page"
    )


def test_gate_is_selective_not_blanket(gated_client: TestClient) -> None:
    """Complement rigor in one assertion pair: the SAME render keeps the ratified
    member AND drops the phantom. A suppress-all gate fails the first half; a
    suppress-none (no-gate) gate fails the second. Only a correct, predicate-driven
    gate passes both."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_RATIFY_WORLD}")
    assert resp.status_code == 200, resp.text
    assert f'id="cast-{_RATIFIED_SLUG}"' in resp.text
    assert f'id="cast-{_PENDING_SLUG}"' not in resp.text


# ---------------------------------------------------------------------------
# §D6 observability — the skip is loud (count on a per-render span)
# ---------------------------------------------------------------------------


def test_skip_span_reports_count_one_when_one_withheld(
    gated_client: TestClient, otel_capture: InMemorySpanExporter
) -> None:
    """The lie-detector: withholding one phantom fires exactly one
    ``npc_unratified_skipped`` span carrying count == 1, and the span names the
    pack/world so the GM panel can attribute it."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_RATIFY_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = _skip_spans(otel_capture)
    assert len(spans) == 1, f"expected exactly one skip span, got {len(spans)}"
    assert spans[0].get(ATTR_SKIPPED_COUNT) == 1
    assert spans[0].get("reference.pack") == _PACK
    assert spans[0].get("reference.world") == _RATIFY_WORLD


def test_skip_span_fires_with_zero_count_when_nothing_withheld(
    gated_client: TestClient, otel_capture: InMemorySpanExporter
) -> None:
    """0 is a valid, observable count. A ratified-only world (cast_gated_fixture,
    both NPCs authored without the flag) still fires exactly one skip span with
    count == 0 — the per-render record that the gate RAN and found nothing to
    withhold. Without this, a silently-broken gate is indistinguishable from a
    clean cast. Doubles as an over-suppression regression guard."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_RATIFIED_ONLY_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = _skip_spans(otel_capture)
    assert len(spans) == 1, f"expected exactly one skip span, got {len(spans)}"
    assert spans[0].get(ATTR_SKIPPED_COUNT) == 0


def test_skip_count_is_computed_from_raw_entries_not_survivors(
    gated_client: TestClient, otel_capture: InMemorySpanExporter
) -> None:
    """Edge: a world whose ONLY Cast member is unratified renders NO Cast section,
    yet the skip span still fires with count == 1. This pins that the count comes
    from the raw authored entries (the gate's input), not the post-filter survivor
    set (which is empty here) — the failure mode where a dev computes the count
    after dropping the entries and always reports 0."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ALL_UNRATIFIED_WORLD}")
    assert resp.status_code == 200, resp.text
    # The lone phantom is withheld: no Cast section, name never on the page.
    assert '<section id="cast">' not in resp.text, (
        "an all-unratified world must render no Cast section"
    )
    assert _LONE_PHANTOM_NAME not in resp.text
    # ...but the skip is still observable.
    spans = _skip_spans(otel_capture)
    assert len(spans) == 1, f"expected exactly one skip span, got {len(spans)}"
    assert spans[0].get(ATTR_SKIPPED_COUNT) == 1


# ---------------------------------------------------------------------------
# Wiring — the span is registered so the GM panel actually surfaces it
# ---------------------------------------------------------------------------


def test_skip_span_is_registered(otel_capture: InMemorySpanExporter) -> None:
    """The span must be registered in ``FLAT_ONLY_SPANS`` so the GM/dev panel's
    ``agent_span_close`` fan-out surfaces it — defining the contextmanager is not
    enough (mirrors the 65-13 portrait-span registration). RED today: the constant
    does not exist."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS

    assert SPAN_NPC_UNRATIFIED_SKIPPED in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# Predicate reuse — the gate consults the 75-11 single source of truth
# ---------------------------------------------------------------------------


def test_is_projectable_gates_on_observation_pending() -> None:
    """The gate's contract anchor: ``is_projectable`` (75-11) is the ONE predicate
    both projection surfaces consult. A ratified pool member is projectable; an
    ``observation_pending`` member is not. Pins the semantics 75-13 wires into the
    reference Cast projection so a dev does not silently re-implement the rule.
    (Already GREEN — this is the shared-contract anchor, not the story's new code.)"""
    from sidequest.game.npc_pool import NpcPoolMember, is_projectable

    ratified = NpcPoolMember(name="Marrow Quill", drawn_from="world_authored")
    phantom = NpcPoolMember(
        name="Hollow Bessom", drawn_from="dialogue_extraction", observation_pending=True
    )

    assert is_projectable(ratified) is True
    assert is_projectable(phantom) is False
