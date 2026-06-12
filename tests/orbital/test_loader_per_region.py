"""RED tests — Story 98-2 (ADR-141): per-region system-file resolution.

After Story 98-1 split perseus_cloud's monolithic ``orbits.yaml`` into
``systems/<region_id>.yaml`` (and deleted the fake root), the loader must
resolve the *right* per-system file for the party's current region — replacing
the single hard-coded ``world_dir / "orbits.yaml"`` (loader.py:42).

Contract pinned for Dev (GREEN):

    load_orbital_content(world_dir: Path, region_id: str | None = None) -> OrbitalContent

  Two-scale (multi-system) world — a ``systems/`` directory is present:
    - ``region_id`` selects ``world_dir/systems/<region_id>.yaml``.
    - A missing ``systems/<region_id>.yaml`` raises ``OrbitalContentMissingError``
      naming the missing path (No Silent Fallbacks) — it must NOT fall back to a
      cluster-wide chart or a stray top-level ``orbits.yaml`` retirement stub.
    - Each per-system file has exactly one parent-less primary, so
      ``Scope.system_root()`` (render.py:132) resolves it VERBATIM — unchanged.

  Single-system world — NO ``systems/`` directory, only ``orbits.yaml``:
    - Loads ``orbits.yaml`` and collapses the two scales cleanly; the region key
      is not required (``coyote_star`` regression, AC5).

  OTEL (AC4): every resolution emits ``orbital.system_resolve`` carrying
  ``region_id``, ``system_file``, and a ``hit`` boolean — the GM-panel
  lie-detector for whether per-region resolution actually fired.

The exact span name/attr keys and the discriminator between single- and
multi-system layouts are the contract this suite enforces; if Dev chooses a
different seam they must update these tests with a logged Design Deviation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.orbital.loader import (
    OrbitalContentMissingError,
    load_orbital_content,
)
from sidequest.orbital.render import Scope, _resolve_scope_center

FIXTURES = Path(__file__).parent / "fixtures"
TWO_SCALE = FIXTURES / "world_two_scale"  # systems/{yula,vorn}.yaml, no orbits.yaml
TWO_SCALE_STUB = FIXTURES / "world_two_scale_stub"  # systems/yula.yaml + stray orbits.yaml
SINGLE_SYSTEM = FIXTURES / "world_minimal"  # orbits.yaml only, single 'coyote' star


def _spans_named(exporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ===========================================================================
# AC1 — per-region resolution replaces the hard-coded orbits.yaml path
# ===========================================================================


def test_resolves_systems_file_for_current_region() -> None:
    """Party in region ``yula`` → loader opens ``systems/yula.yaml``.

    Proven by body membership: ``yula`` / ``yula_anchorage`` live only in
    yula.yaml; ``vorn`` lives only in vorn.yaml.
    """
    content = load_orbital_content(TWO_SCALE, region_id="yula")

    assert "yula" in content.orbits.bodies
    assert "yula_anchorage" in content.orbits.bodies
    assert "vorn" not in content.orbits.bodies, "loaded the wrong per-system file"


def test_resolves_distinct_file_for_a_different_region() -> None:
    """Region key actually selects the file — ``vorn`` loads vorn's system,
    not yula's. Without this a hard-coded "always yula" would pass the test
    above."""
    content = load_orbital_content(TWO_SCALE, region_id="vorn")

    assert "vorn" in content.orbits.bodies
    assert "vorn_depot" in content.orbits.bodies
    assert "yula" not in content.orbits.bodies


def test_does_not_read_top_level_orbits_monolith() -> None:
    """Per AC1 the loader no longer reads ``world_dir/orbits.yaml``. Even when a
    stray retirement-stub monolith sits beside the systems/ dir, resolving
    region ``yula`` returns the per-system file — the monolith's marker body is
    absent."""
    content = load_orbital_content(TWO_SCALE_STUB, region_id="yula")

    assert "yula" in content.orbits.bodies
    assert "legacy_monolith_marker" not in content.orbits.bodies, (
        "loader fell back to the top-level orbits.yaml stub"
    )


# ===========================================================================
# AC2 — system_root() used UNCHANGED (one parent-less primary per file)
# ===========================================================================


def test_system_root_resolves_verbatim_against_resolved_file() -> None:
    """The drilled-in scope for ``yula`` returns ``yula`` as root, using
    render.py's existing ``_resolve_scope_center`` / ``Scope.system_root()``
    contract VERBATIM. A per-system file has exactly one parent-less primary,
    so the unmodified contract holds."""
    content = load_orbital_content(TWO_SCALE, region_id="yula")

    root = _resolve_scope_center(content.orbits, Scope.system_root())

    assert root == "yula"


def test_resolved_file_has_exactly_one_parentless_primary() -> None:
    """The invariant that lets ``system_root()`` stay unchanged: each
    per-system file is single-rooted. If a file had two parent-less bodies the
    verbatim contract (render.py:831) would raise — assert the fixture upholds
    it so the reuse argument is real, not assumed."""
    content = load_orbital_content(TWO_SCALE, region_id="vorn")

    roots = [bid for bid, b in content.orbits.bodies.items() if b.parent is None]
    assert roots == ["vorn"]


def test_loader_reached_from_production_bind_path_for_yula() -> None:
    """WIRING (CLAUDE.md mandate): the per-region loader is reached from a
    PRODUCTION code path — ``SessionRoom.bind_world`` — not just a unit harness.
    With the party's ``current_region == "yula"``, binding a two-scale world
    exposes ``session.orbital_content`` resolved from ``systems/yula.yaml`` and
    its system root is ``yula``.

    RED today: ``bind_world`` calls ``load_orbital_content(world_dir)`` with no
    region → ``world_two_scale`` has no top-level orbits.yaml → the missing-file
    error is swallowed → ``orbital_content is None``.
    """
    from sidequest.game.persistence import GameMode
    from sidequest.game.repository import SaveRepository
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_room import SessionRoom

    snap = GameSnapshot()
    snap.current_region = "yula"

    room = SessionRoom(slug="two_scale_world", mode=GameMode.SOLO)
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=TWO_SCALE,
    )

    content = room.session.orbital_content
    assert content is not None, "production bind did not resolve a per-region system file"
    assert "yula" in content.orbits.bodies
    assert "vorn" not in content.orbits.bodies
    assert _resolve_scope_center(content.orbits, Scope.system_root()) == "yula"


# ===========================================================================
# AC3 — fail loud on a missing system file (No Silent Fallbacks)
# ===========================================================================


def test_missing_system_file_fails_loud_naming_path() -> None:
    """Drilling into a region with no authored ``systems/<id>.yaml`` raises a
    clear error that NAMES the missing path — not a silent empty/monolith
    chart. ``amanta`` has no file in the two-scale fixture."""
    with pytest.raises(OrbitalContentMissingError, match=r"systems[/\\]amanta\.yaml"):
        load_orbital_content(TWO_SCALE, region_id="amanta")


def test_missing_system_file_does_not_fall_back_to_stub_monolith() -> None:
    """AC3 edge case: a missing ``systems/<id>.yaml`` must fail loud even when a
    stray top-level ``orbits.yaml`` retirement stub exists — the stub must NOT
    satisfy resolution. Region ``amanta`` has no per-system file in the
    stub-bearing world."""
    with pytest.raises(OrbitalContentMissingError, match=r"systems[/\\]amanta\.yaml"):
        load_orbital_content(TWO_SCALE_STUB, region_id="amanta")


# ===========================================================================
# AC4 — OTEL span on resolution (region -> file -> hit/miss)
# ===========================================================================


def test_resolution_hit_emits_system_resolve_span(otel_capture) -> None:
    """A successful per-region resolution emits ``orbital.system_resolve``
    carrying the region, the resolved file, and ``hit=True``."""
    load_orbital_content(TWO_SCALE, region_id="yula")

    spans = _spans_named(otel_capture, "orbital.system_resolve")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs.get("region_id") == "yula"
    assert "yula.yaml" in str(attrs.get("system_file")), (
        "span must record which file was resolved"
    )
    assert attrs.get("hit") is True


def test_resolution_miss_emits_system_resolve_span_with_hit_false(otel_capture) -> None:
    """A miss (no ``systems/<id>.yaml``) still emits the span with ``hit=False``
    BEFORE failing loud — the GM panel must see the miss, not just an exception.
    """
    with pytest.raises(OrbitalContentMissingError):
        load_orbital_content(TWO_SCALE, region_id="amanta")

    spans = _spans_named(otel_capture, "orbital.system_resolve")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs.get("region_id") == "amanta"
    assert "amanta.yaml" in str(attrs.get("system_file")), (
        "miss span must record the attempted file, not just hit=False"
    )
    assert attrs.get("hit") is False


# ===========================================================================
# AC5 — single-system world regression (collapse the two scales)
# ===========================================================================


def test_single_system_world_loads_without_region_key() -> None:
    """A single-system world (``orbits.yaml``, NO ``systems/`` dir — the
    coyote_star shape) still loads its lone orrery with NO region key, exactly
    as before. The two scales collapse to one. This is what keeps the existing
    ``load_orbital_content(path)`` callers (test_render_coyote_star.py) working.
    """
    content = load_orbital_content(SINGLE_SYSTEM)

    assert "coyote" in content.orbits.bodies
    assert _resolve_scope_center(content.orbits, Scope.system_root()) == "coyote"


def test_single_system_world_ignores_a_supplied_region_key() -> None:
    """Passing a region key to a single-system world is harmless — the lone
    orrery still resolves (the collapse path ignores the key rather than
    demanding a ``systems/<region>.yaml`` that single-system worlds never
    have). Distinguishes single-system collapse from the multi-system
    fail-loud-on-miss path."""
    content = load_orbital_content(SINGLE_SYSTEM, region_id="coyote")

    assert "coyote" in content.orbits.bodies


def test_single_system_world_emits_no_resolve_span(otel_capture) -> None:
    """The single-system collapse path must NOT emit ``orbital.system_resolve``.
    That span is the multi-system lie-detector; firing it on the lone-orrery
    path would pollute the GM panel's miss-rate metrics. (Review round-trip 1,
    test-analyzer #5.)"""
    load_orbital_content(SINGLE_SYSTEM, region_id="coyote")

    assert _spans_named(otel_capture, "orbital.system_resolve") == []


# ===========================================================================
# Round-trip 1 — Reviewer findings (path traversal + uncovered guards)
# ===========================================================================
#
# Reviewer (REJECTED) found a NEW path-traversal surface and two untested
# guards. These tests drive the fix:
#   - [HIGH/BLOCKING] region_id is interpolated raw into a filesystem path
#     (loader.py `_resolve_system_file`). Its runtime source is
#     ``snapshot.current_region`` — a narrator-patchable field — so a path-like
#     region must be rejected (No Silent Fallbacks: a path-like region is
#     INVALID input), never used to escape ``systems/``.
#   - [MED] the explicit ``region_id=`` kwarg branch of bind_world (the
#     fresh-connect production path) and the blank/None fail-loud guard had no
#     coverage.


def test_path_traversal_does_not_load_sibling_orbits_stub() -> None:
    """AIRTIGHT traversal proof: in ``world_two_scale_stub`` the sibling
    ``orbits.yaml`` stub (body ``legacy_monolith_marker``) EXISTS, so
    ``region_id="../orbits"`` resolves ``systems/../orbits.yaml`` to a real file
    OUTSIDE ``systems/``. Because the target exists, a raise here can ONLY come
    from a path-segment guard — not from a missing file. The loader must reject
    the path-like region, not silently load the escaped file.

    RED until the guard lands: today the loader loads the stub and returns it
    (traversal succeeds).
    """
    with pytest.raises((ValueError, OrbitalContentMissingError)):
        load_orbital_content(TWO_SCALE_STUB, region_id="../orbits")


def test_parent_ref_region_id_rejected_before_filesystem_probe(otel_capture) -> None:
    """A ``..`` region must be rejected BEFORE the loader builds and probes a
    path — so no ``orbital.system_resolve`` span fires for it. RED until the
    guard lands: today ``region_id="../orbits"`` against ``world_two_scale``
    (no sibling orbits.yaml) emits a miss span for ``systems/../orbits.yaml``
    then raises a generic miss; the guard must short-circuit before that span.
    """
    with pytest.raises((ValueError, OrbitalContentMissingError)):
        load_orbital_content(TWO_SCALE, region_id="../orbits")

    assert _spans_named(otel_capture, "orbital.system_resolve") == [], (
        "a path-like region must be rejected before any path is built/probed"
    )


def test_separator_region_id_rejected_before_filesystem_probe(otel_capture) -> None:
    """A bare path separator (no ``..``) must also be rejected before probing —
    the guard rejects the whole class of path-like region ids, not just parent
    refs. RED until the guard lands: ``"sub/yula"`` currently emits a miss span
    for ``systems/sub/yula.yaml`` then raises a generic miss.
    """
    with pytest.raises((ValueError, OrbitalContentMissingError)):
        load_orbital_content(TWO_SCALE, region_id="sub/yula")

    assert _spans_named(otel_capture, "orbital.system_resolve") == [], (
        "a region id containing a path separator must be rejected before probing"
    )


def test_blank_region_none_fails_loud_for_multi_system() -> None:
    """The blank-region fail-loud guard (``region_id=None`` on a multi-system
    world) had no coverage — a refactor turning it into a silent fallback would
    have passed the suite. Pin it. (Review round-trip 1, test-analyzer #2.)"""
    with pytest.raises(OrbitalContentMissingError, match="blank region"):
        load_orbital_content(TWO_SCALE, region_id=None)


def test_blank_region_empty_string_fails_loud_for_multi_system() -> None:
    """Empty-string region is as invalid as ``None`` — both must fail loud, not
    collapse to a single-system read. (Review round-trip 1, test-analyzer #2.)"""
    with pytest.raises(OrbitalContentMissingError, match="blank region"):
        load_orbital_content(TWO_SCALE, region_id="")


def test_explicit_region_id_kwarg_resolves_via_bind_world() -> None:
    """WIRING gap (test-analyzer #1): the existing wiring test only exercises
    the ``snapshot.current_region`` fallback branch of ``bind_world``. The
    fresh-connect production path passes ``region_id=`` EXPLICITLY (snapshot has
    a blank ``current_region``). Prove that branch resolves the per-system file
    — a regression in ``_starting_region_for``/the kwarg thread would otherwise
    leave ``orbital_content=None`` on a fresh multi-system connect with no test
    failing.
    """
    from sidequest.game.persistence import GameMode
    from sidequest.game.repository import SaveRepository
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_room import SessionRoom

    snap = GameSnapshot()  # fresh: current_region == "" (no fallback available)

    room = SessionRoom(slug="two_scale_explicit_region", mode=GameMode.SOLO)
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=TWO_SCALE,
        region_id="vorn",
    )

    content = room.session.orbital_content
    assert content is not None, "explicit region_id kwarg did not resolve a per-system file"
    assert "vorn" in content.orbits.bodies
    assert "yula" not in content.orbits.bodies
    assert _resolve_scope_center(content.orbits, Scope.system_root()) == "vorn"
