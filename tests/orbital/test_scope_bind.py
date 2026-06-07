"""RED tests — Story 95-1: region -> orbital-scope binding mechanism.

The per-location orrery follows the party's system: when the party's
cartography region changes, the chart re-centers on the matching star body.
The join is an *identity* join — a star body whose id equals the region id
(region ``yula`` -> star body ``yula``), guaranteed by how perseus_cloud's
``orbits.yaml`` was authored (content#383).

These are unit tests over the binding MECHANISM, driven through a synthetic
sector fixture (``fixtures/world_sector_join`` — hub -> stars yula/vorn ->
planet), never shipped content (per the "no content in unit tests" rule;
the perseus_cloud-specific 34/34 join is a content-validator concern). The
real Site-B relocation wiring is exercised end-to-end in
``tests/server/test_region_orbital_scope_wiring.py``.

Contract under test (defined here for Dev's GREEN):

    Session.bind_region_scope(region_id: str, *, trigger: str) -> bool

  - ``trigger`` is ``"init"`` (bind-on-connect from cartography.starting_region)
    or ``"relocation"`` (bind on a pc_region change).
  - On a MATCH (``region_id`` is a body id in the bound orbital content) the
    method sets ``snapshot.party_body_id`` and ``session.orbital_scope`` to that
    body, emits an ``orbital.scope_bind`` span, and returns ``True``.
  - On NO MATCH with ``trigger="init"`` it raises ``RegionScopeBindError`` — a
    blank/foreign starting_region must fail loud, never silently fall back to
    the system root (No Silent Fallbacks).
  - On NO MATCH with ``trigger="relocation"`` it leaves scope/party_body_id
    unchanged, emits an ``orbital.scope_bind_skipped`` span, and returns
    ``False`` (a loud skip, e.g. region ``ceron`` which has no star body).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.orbital.loader import load_orbital_content
from sidequest.orbital.render import Scope
from sidequest.server.session import Session

FIXTURES = Path(__file__).parent / "fixtures"
SECTOR = FIXTURES / "world_sector_join"


def _bind_error_cls() -> Any:
    """RegionScopeBindError — home left to Dev (prefer the orbital surface,
    fall back to the session module). Importing it forces RED until GREEN."""
    try:
        from sidequest.orbital.scope_bind import RegionScopeBindError

        return RegionScopeBindError
    except ImportError:
        from sidequest.server.session import RegionScopeBindError

        return RegionScopeBindError


@pytest.fixture
def sector_session() -> Session:
    """Session over the synthetic hub->stars sector, party initially nowhere."""
    snapshot = GameSnapshot()
    content = load_orbital_content(SECTOR)
    return Session(snapshot, orbital_content=content)


def _spans_named(exporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ===========================================================================
# Match — bind centers scope + sets party_body_id (init and relocation)
# ===========================================================================


def test_bind_init_match_centers_scope_and_sets_party_body_id(sector_session) -> None:
    """Binding the starting region to its matching star centers the chart and
    records the party's body. ``yula`` (a region) joins star body ``yula``."""
    bound = sector_session.bind_region_scope("yula", trigger="init")

    assert bound is True
    assert sector_session.party_body_id == "yula"
    assert sector_session.orbital_scope.center_body_id == "yula"


def test_bind_relocation_match_recenters_from_prior_scope(sector_session) -> None:
    """A relocation to a new system re-centers the chart on that system's star,
    overriding whatever the player had drilled to before."""
    # Player had drilled elsewhere (e.g. into yula's anchorage).
    sector_session.orbital_scope = Scope(center_body_id="yula_anchorage")

    bound = sector_session.bind_region_scope("vorn", trigger="relocation")

    assert bound is True
    assert sector_session.party_body_id == "vorn"
    assert sector_session.orbital_scope.center_body_id == "vorn"


# ===========================================================================
# No match — init fails loud, relocation skips loud (No Silent Fallbacks)
# ===========================================================================


def test_bind_init_no_matching_star_fails_loud(sector_session) -> None:
    """A starting_region with no matching star body must raise, NOT silently
    fall back to the system root. ``ceron`` has no star in the sector tree."""
    err_cls = _bind_error_cls()

    with pytest.raises(err_cls):
        sector_session.bind_region_scope("ceron", trigger="init")

    # No partial mutation on the fail-loud path.
    assert sector_session.party_body_id is None
    assert sector_session.orbital_scope.is_system_root


def test_bind_relocation_no_matching_star_leaves_scope_unchanged(sector_session) -> None:
    """A relocation into a region with no star body (``ceron``) leaves the chart
    where it was — a loud skip, never a silent miss or a system-root reset."""
    _bind_error_cls()  # force RED on the missing model even on the skip path
    sector_session.bind_region_scope("yula", trigger="init")  # park on yula

    bound = sector_session.bind_region_scope("ceron", trigger="relocation")

    assert bound is False
    assert sector_session.party_body_id == "yula"
    assert sector_session.orbital_scope.center_body_id == "yula"


# ===========================================================================
# OTEL — every bind / skip emits a span the GM panel can verify
# ===========================================================================


def test_bind_emits_scope_bind_span(sector_session, otel_capture) -> None:
    """A successful bind emits ``orbital.scope_bind`` carrying region_id,
    body_id, and trigger — the lie-detector record that the chart re-centered."""
    sector_session.bind_region_scope("yula", trigger="relocation")

    spans = _spans_named(otel_capture, "orbital.scope_bind")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["region_id"] == "yula"
    assert attrs["body_id"] == "yula"
    assert attrs["trigger"] == "relocation"


def test_bind_skip_emits_scope_bind_skipped_span(sector_session, otel_capture) -> None:
    """A no-match relocation emits ``orbital.scope_bind_skipped`` with the region
    and a reason — the loud skip is observable, not swallowed."""
    sector_session.bind_region_scope("ceron", trigger="relocation")

    spans = _spans_named(otel_capture, "orbital.scope_bind_skipped")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["region_id"] == "ceron"
    assert attrs.get("reason"), "skip span must carry a non-empty reason"


# ===========================================================================
# Guard — a world without orbital content binds cleanly (no-op, no crash)
# ===========================================================================


def test_bind_relocation_without_orbital_content_is_noop(sector_session) -> None:
    """A non-orbital world (orbital_content=None) must not crash on a region
    change — the bind is a no-op skip, party_body_id stays None.

    Regression guard: caverns_and_claudes / tea_and_murder have no orbital
    tier and must keep relocating cleanly (existing behavior unchanged)."""
    no_orbital = Session(GameSnapshot(), orbital_content=None)

    bound = no_orbital.bind_region_scope("yula", trigger="relocation")

    assert bound is False
    assert no_orbital.party_body_id is None
