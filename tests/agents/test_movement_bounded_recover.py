"""movement.py bounded branch: cookbook-free routing + loud-but-recoverable
(ADR-157, story 164-10). A materialize failure must NOT propagate out of
run_movement_dispatch — the dispatch contract is recoverable failures only.

Two layers:
  * Reflection tripwires (no DB/session): the bounded path no longer imports the
    cookbook loaders, and the entry point is cookbook-free.
  * Behavioral (the real bug fix): a bounded-site materialization that raises a
    NON-SeamCrossingError is caught by the broad ``except`` — run_movement_dispatch
    returns a recoverable ``movement.unresolved`` (PC does not move) and the
    ``site.enter_unresolved`` span carries the exception identity so the GM panel
    can tell a content gap from an engine bug (never a source-text assertion —
    server CLAUDE.md: prove wiring via behavior + spans).
"""

from __future__ import annotations

import asyncio
import inspect
import types

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region, SiteDecl
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def test_bounded_branch_no_longer_imports_cookbook_loader() -> None:
    """The bounded path is cookbook-free: movement.py must not import
    load_cookbook / load_theme_palette / GenreLoadError anymore."""
    import sidequest.agents.subsystems.movement as mv

    for gone in ("load_cookbook", "load_theme_palette", "GenreLoadError"):
        assert not hasattr(mv, gone), f"{gone} should be removed from movement.py"


def test_ensure_bounded_call_is_cookbook_free() -> None:
    """The dispatch calls ensure_bounded_site_materialized with only
    site/archetype/dungeon_repository — no bundle/palette kwargs."""
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized

    params = set(inspect.signature(ensure_bounded_site_materialized).parameters)
    assert params == {"site", "archetype", "dungeon_repository"}


# ---------------------------------------------------------------------------
# Behavioral: loud-but-recoverable — the store fails DURING materialization.
# ---------------------------------------------------------------------------

_WORLD = "blackthorn_moor"


def _enter_dispatch(site_descriptor: str) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "enter_site", "site_descriptor": site_descriptor},
        idempotency_key="mv-164-10-enter",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _bounded_cartography() -> CartographyConfig:
    """A region-mode world with one BOUNDED site (a tavern) attached to the
    square the PC is standing on."""
    return CartographyConfig(
        starting_region="square",
        navigation_mode=NavigationMode.region,
        regions={
            "square": Region(
                name="Market Square",
                summary="A cobbled square.",
                description="The market square, a tavern on its north side.",
                adjacent=[],
            ),
        },
        sites=[
            SiteDecl(
                site_id="gilded_boar",
                name="The Gilded Boar",
                archetype="tavern",
                attached_to="square",
                extent="bounded",
            )
        ],
    )


def _bounded_pack(world_slug: str = _WORLD):
    from sidequest.genre.models.site_archetype import SiteArchetype

    world = types.SimpleNamespace(cartography=_bounded_cartography())
    arch = SiteArchetype(
        archetype_id="tavern",
        interior_algorithm="roomcorridor",
        room_count_min=3,
        room_count_max=6,
        grid_width=15,
        grid_height=20,
    )
    return types.SimpleNamespace(worlds={world_slug: world}, site_archetypes={"tavern": arch})


class _RaisingStore:
    """A dungeon store whose first materialization call raises a NON-Seam error —
    models a PersistError / malformed-content gap surfacing DURING bounded
    materialization. If run_movement_dispatch re-raised this (the pre-ADR-157
    bug), the whole turn would crash."""

    def load_map(self, *, entrance_id: str = "", site_id: str = ""):
        raise RuntimeError("boom: simulated bounded materialization failure")


def _snapshot_square() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug=_WORLD,
        pc_regions={"Delver": "square"},
        player_seats={"p1": "Delver"},
    )


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_bounded_materialize_failure_is_recoverable_not_reraised(otel_capture) -> None:
    """The headline ADR-157 fix: a materialize failure is loud-but-recoverable —
    run_movement_dispatch returns a movement.unresolved (PC does not move), never
    re-raising out of the dispatch, and the site.enter_unresolved span carries the
    exception identity (error / error_type) for the GM-panel lie-detector."""
    snap = _snapshot_square()

    # Must NOT raise: the whole point is the dispatch swallows-and-surfaces.
    out = asyncio.run(
        run_movement_dispatch(
            _enter_dispatch("the gilded boar"),
            snapshot=snap,
            player_name="Delver",
            dungeon_store=_RaisingStore(),
            pack=_bounded_pack(),
        )
    )

    # Recoverable movement.unresolved — the PC did NOT move, and is told the truth.
    # (error == the unresolved reason IS the proof this took the unresolved path,
    # not site_enter; a resolved_via check would be vacuous — _unresolved's data
    # never carries that key.)
    assert out.data.get("error") == "site_materialize_failed", out.data
    assert snap.pc_regions["Delver"] == "square", "a failed materialize must not move the PC"

    # OTEL lie-detector: the failure span carries the exception identity so a
    # content gap is distinguishable from an engine bug (ADR-157).
    failed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "site.enter_unresolved"
        and (s.attributes or {}).get("reason") == "site_materialize_failed"
    ]
    assert len(failed) == 1, (
        "exactly one site.enter_unresolved(reason=site_materialize_failed) expected; "
        f"saw {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = failed[0].attributes or {}
    assert attrs.get("error_type") == "RuntimeError", attrs
    assert "boom" in (attrs.get("error") or ""), attrs
