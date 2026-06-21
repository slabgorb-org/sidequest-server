"""Tests — Seam 2 sub-part B: authored-cast staging on region entry (story 157-3).

Design: ``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``
(§ "Seam 2 — NPCs", second bullet) + implementation plan Task 3.

The "right cast appears" half: register the first real consumer of the empty
``frontier_hook`` observer registry. On region entry, push-stage that region's
cartography ``entities`` where ``binding.kind == "npc"`` into the snapshot's
``npc_pool`` (so e.g. entering Mildendo surfaces the Emperor / Reldresal without
the narrator electing ``resolve_location_entity``, curbing invented cross-voyage
extras), and emit ``zone_eligibility.cast_staged`` (the GM-panel lie-detector).

These drive the REAL module + the REAL ``frontier_hook.notify_region_transition``
dispatch (CLAUDE.md: fixture-driven behavior + OTEL spans — never grep production
source for wiring). RED today: ``sidequest.game.region_cast_staging`` does not
exist, and ``SPAN_ZONE_ELIGIBILITY_CAST_STAGED`` is not defined.

TEST ASSUMPTION (logged as a deviation): ``stage_region_cast`` resolves the pack
from the snapshot's slugs via ``load_genre_pack_cached`` (the plan's stated path,
matching ``zone_eligibility.cartography_for``'s ``pack.worlds[slug].cartography``
accessor). The loader is patched at both the source module and the used site so
either import style is covered; the cross-fire test proves resolution is per-call
from the handed snapshot, not a captured closure.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import sidequest.game.region_cast_staging as region_cast_staging
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sidequest.game.region_cast_staging import (
    register_cast_staging_observer,
    stage_region_cast,
)

import sidequest.genre.loader as loader_mod
from sidequest.dungeon import frontier_hook
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.protocol.models import LocationEntity, LocationEntityBinding
from sidequest.telemetry.spans import FLAT_ONLY_SPANS
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_CAST_STAGED

LILLIPUT = "the_lilliput_court"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """Capture spans on the live OTEL provider singleton (the project's
    ``tracer()`` helper closes over the global provider, so a SimpleSpanProcessor
    on the singleton is the reliable way to observe production spans)."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

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


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Iterator[None]:
    """Prevent observer registration leaking into the rest of the suite (the
    Task-6/7 frontier wiring-test fixture pattern)."""
    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _npc_entity(name: str) -> LocationEntity:
    """An authored cartography NPC manifest entry (``binding.kind == "npc"``).
    ``label == ref == name`` so the staged pool name is unambiguous regardless of
    which field the implementation reads for the display name."""
    return LocationEntity(
        id=name.lower().replace(",", "").replace(" ", "_"),
        label=name,
        tier="real_object",
        binding=LocationEntityBinding(kind="npc", ref=name),
    )


def _feature_entity(name: str) -> LocationEntity:
    """A non-NPC manifest entry — must NOT be staged as cast."""
    return LocationEntity(
        id=name.lower().replace(" ", "_"),
        label=name,
        tier="real_object",
        binding=LocationEntityBinding(kind="location_feature", ref=name),
    )


def _region(*, controlled_by: str | None, entities: list[LocationEntity]) -> Region:
    return Region(
        name="R", summary="s", description="d", controlled_by=controlled_by, entities=entities
    )


def _pack(world: str, regions: dict[str, Region]) -> SimpleNamespace:
    """Pack stand-in exposing ``worlds[world].cartography`` — the established
    ``pregen._seed_authored_npcs`` / ``zone_eligibility.cartography_for`` accessor."""
    return SimpleNamespace(
        worlds={world: SimpleNamespace(cartography=CartographyConfig(regions=regions))}
    )


def _snapshot(*, genre: str, world: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug=genre,
        world_slug=world,
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
        player_seats={"seat-1": "Gulliver"},
        pc_regions={},
    )


def _patch_loader(monkeypatch: pytest.MonkeyPatch, packs_by_genre: dict[str, Any]) -> None:
    """Make ``load_genre_pack_cached(genre)`` return the synthetic pack — patched
    at BOTH the source module and the used site so either import style is covered.
    Keyed by genre so resolution is genuinely per-call from the passed slug."""

    def fake(genre_code: Any, search_paths: Any = None) -> Any:
        return packs_by_genre[str(genre_code)]

    monkeypatch.setattr(loader_mod, "load_genre_pack_cached", fake)
    monkeypatch.setattr(region_cast_staging, "load_genre_pack_cached", fake, raising=False)


def _pool_names(snap: GameSnapshot) -> list[str]:
    return [m.name for m in snap.npc_pool]


# ---------------------------------------------------------------------------
# Behavior — the cast appears on entry
# ---------------------------------------------------------------------------


def test_entering_region_stages_authored_npc_cast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entering a region push-stages that region's authored NPC cast into the
    snapshot's ``npc_pool`` — the Mildendo case (Emperor + Reldresal surface)."""
    mildendo = _region(
        controlled_by=LILLIPUT,
        entities=[
            _npc_entity("the Emperor of Lilliput"),
            _npc_entity("Reldresal, the Principal Secretary"),
            _feature_entity("the Grand Treasury"),
        ],
    )
    pack = _pack(
        "gulliver",
        {"mildendo": mildendo, "the_lilliput_shore": _region(controlled_by=LILLIPUT, entities=[])},
    )
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    stage_region_cast(
        snapshot=snap, pc_name="Gulliver", from_region="the_lilliput_shore", to_region="mildendo"
    )

    names = _pool_names(snap)
    assert "the Emperor of Lilliput" in names
    assert "Reldresal, the Principal Secretary" in names


def test_staging_skips_non_npc_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``binding.kind == "npc"`` entities stage — items, clues, and location
    features are NOT minted as cast (they belong to other seams / no seam)."""
    treasury = _region(
        controlled_by=LILLIPUT,
        entities=[
            _feature_entity("the Grand Treasury"),
            LocationEntity(
                id="royal_seal",
                label="the Royal Seal",
                tier="real_object",
                binding=LocationEntityBinding(kind="item", ref="the Royal Seal"),
            ),
        ],
    )
    pack = _pack("gulliver", {"mildendo": treasury})
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    stage_region_cast(
        snapshot=snap, pc_name="Gulliver", from_region="the_lilliput_shore", to_region="mildendo"
    )

    assert _pool_names(snap) == [], "a non-NPC manifest entry was staged as cast"


def test_staging_is_idempotent_on_reentry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-entering the same region stages no duplicates — each authored NPC
    appears in the pool exactly once across repeated transitions."""
    mildendo = _region(controlled_by=LILLIPUT, entities=[_npc_entity("the Emperor of Lilliput")])
    pack = _pack("gulliver", {"mildendo": mildendo})
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    stage_region_cast(snapshot=snap, pc_name="Gulliver", from_region="x", to_region="mildendo")
    stage_region_cast(snapshot=snap, pc_name="Gulliver", from_region="x", to_region="mildendo")

    assert _pool_names(snap).count("the Emperor of Lilliput") == 1, (
        "re-entry double-staged the authored cast — staging is not idempotent"
    )


# ---------------------------------------------------------------------------
# OTEL — the lie-detector (zone_eligibility.cast_staged)
# ---------------------------------------------------------------------------


def test_staging_emits_cast_staged_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Per the OTEL Observability Principle: staging is a subsystem decision the GM
    panel must see (proof the engine surfaced the cast, not that the narrator named
    them by luck). Pins region + the staged names on the span."""
    mildendo = _region(
        controlled_by=LILLIPUT,
        entities=[
            _npc_entity("the Emperor of Lilliput"),
            _npc_entity("Reldresal, the Principal Secretary"),
        ],
    )
    pack = _pack("gulliver", {"mildendo": mildendo})
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    stage_region_cast(snapshot=snap, pc_name="Gulliver", from_region="x", to_region="mildendo")

    staged = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ZONE_ELIGIBILITY_CAST_STAGED
    ]
    assert len(staged) == 1, f"expected exactly one {SPAN_ZONE_ELIGIBILITY_CAST_STAGED!r} span"
    attrs = dict(staged[0].attributes or {})
    assert attrs.get("region") == "mildendo"
    # OTEL serializes a list attribute as a tuple — compare by membership.
    names = list(attrs.get("npc_names") or [])
    assert "the Emperor of Lilliput" in names
    assert "Reldresal, the Principal Secretary" in names


def test_no_cast_staged_span_when_region_has_no_authored_cast(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Entering a region with no NPC-bound entities stages nothing and fires no
    ``cast_staged`` span — no false positive on the lie-detector."""
    empty = _region(controlled_by=LILLIPUT, entities=[_feature_entity("a bare field")])
    pack = _pack("gulliver", {"the_plain": empty})
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    stage_region_cast(snapshot=snap, pc_name="Gulliver", from_region="x", to_region="the_plain")

    assert [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ZONE_ELIGIBILITY_CAST_STAGED
    ] == []


def test_cast_staged_span_is_flat_only_registered() -> None:
    """Persistence-routing contract: ``cast_staged`` must be a flat, persisted
    game-engine event (forensics reconstructs staging from a stored session), like
    its ``zone_eligibility.filtered`` sibling — not a live-only pipeline span."""
    assert SPAN_ZONE_ELIGIBILITY_CAST_STAGED in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# Concurrency — resolve the pack from the handed snapshot, never a closure
# ---------------------------------------------------------------------------


def test_staging_resolves_pack_from_snapshot_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """``frontier_hook._OBSERVERS`` is a module-global registry shared across
    sessions. A transition in session A must stage A's cast and a transition in
    session B must stage B's — the pack/cartography is resolved from the snapshot
    each call, never from a per-session captured closure (the plan's hard
    concurrency constraint). Same region id, different worlds, one observer."""
    pack_a = _pack(
        "alpha_world",
        {"capital": _region(controlled_by="alpha", entities=[_npc_entity("Alpha Lord")])},
    )
    pack_b = _pack(
        "beta_world",
        {"capital": _region(controlled_by="beta", entities=[_npc_entity("Beta Lord")])},
    )
    _patch_loader(monkeypatch, {"alpha": pack_a, "beta": pack_b})

    snap_a = _snapshot(genre="alpha", world="alpha_world")
    snap_b = _snapshot(genre="beta", world="beta_world")

    stage_region_cast(snapshot=snap_a, pc_name="A", from_region="x", to_region="capital")
    stage_region_cast(snapshot=snap_b, pc_name="B", from_region="x", to_region="capital")

    assert _pool_names(snap_a) == ["Alpha Lord"], "session A staged the wrong world's cast"
    assert _pool_names(snap_b) == ["Beta Lord"], (
        "session B staged the wrong world's cast (closure capture leak)"
    )


# ---------------------------------------------------------------------------
# Wiring — the observer fires through the REAL frontier dispatch path
# ---------------------------------------------------------------------------


def test_register_cast_staging_observer_is_idempotent() -> None:
    """``register_cast_staging_observer`` registers ``stage_region_cast`` exactly
    once — a second call (uvicorn --reload re-runs startup) does NOT double-register
    (which would double-stage every transition)."""
    register_cast_staging_observer()
    assert stage_region_cast in frontier_hook._OBSERVERS
    register_cast_staging_observer()
    assert frontier_hook._OBSERVERS.count(stage_region_cast) == 1, (
        "cast-staging observer double-registered on a repeated startup call"
    )


def test_real_region_transition_stages_cast_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keystone wiring test (CLAUDE.md: Every Test Suite Needs a Wiring Test):
    after the production registration, a REAL ``frontier_hook.notify_region_transition``
    — the same producer ``GameSnapshot._apply_world_patch_inner`` fires in
    production — invokes the staging observer and the cast surfaces. Proves the
    observer is reachable from the live dispatch, not just callable in isolation."""
    mildendo = _region(controlled_by=LILLIPUT, entities=[_npc_entity("the Emperor of Lilliput")])
    pack = _pack("gulliver", {"mildendo": mildendo})
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    register_cast_staging_observer()
    frontier_hook.notify_region_transition(
        snap, pc_name="Gulliver", from_region="the_lilliput_shore", to_region="mildendo"
    )

    assert "the Emperor of Lilliput" in _pool_names(snap), (
        "the cast-staging observer is not wired into the real frontier dispatch "
        "(notify_region_transition fired but no cast was staged)"
    )
