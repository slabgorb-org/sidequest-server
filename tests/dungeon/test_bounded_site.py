"""Bounded-site materialization — Track B, Task 11 (story 164-6).

RED: ``sidequest.dungeon.bounded_site`` does not exist — the whole file fails
with ModuleNotFoundError until Task 11 lands.

Contract under test (plan 2026-07-08-mapping-track-b-site-system.md §Task 11,
consumed by Task 6/12):

    async def ensure_bounded_site_materialized(
        *, site, archetype, dungeon_repository, snapshot, pack, bundle, palette
    ) -> None

A ``bounded`` site materializes its ENTIRE graph + grids in ONE committed
transaction at first entry, deterministic from ``blake2b(campaign_seed,
site_id)`` and the archetype's dims. No frontier worker, no lookahead:

  - **Whole**: the site's entrance node plus its rooms all land at once
    (``load_map`` shows entrance + [room_count_min..room_count_max] nodes).
  - **Bounded**: no open frontier edges survive (``load_frontier(site_id) == []``).
  - **One transaction**: a partial site is never visible (asserted via the
    idempotency/whole invariants — a half-committed site would leave a
    node-count mismatch or a stray entrance).
  - **Idempotent**: re-entry skips (no new nodes, ``site.materialize.skip`` span).
  - **Deterministic**: same base seed + site_id → identical node-id set.
  - **Fail loud**: a missing store raises ``SeamCrossingError`` (No Silent
    Fallbacks) — never a quiet no-op.
  - **OTEL**: ``site.materialize.commit`` fires with ``node_count`` so the GM
    panel can verify the site actually materialized (not narrator prose).

The behavioral tests use a real ``PgDungeonRepository`` (the transaction
boundary is the whole point — an in-memory double could not prove it) built via
``tests.dungeon.conftest.build_pg_dungeon_repo``; they skip loudly without a
test DB. Bundle/palette/snapshot/pack are the SAME real shapes the materializer
suite drives — the archetype (not the theme flavor) governs the structural
invariants these tests assert.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from sidequest.game.sites.models import SiteDescriptor
from sidequest.game.sites.namespacing import site_entrance_id

_SITE_ID = "gilded_boar"


def _tavern_descriptor() -> SiteDescriptor:
    return SiteDescriptor(
        site_id=_SITE_ID,
        name="The Gilded Boar",
        archetype="tavern",
        attached_to="square",
        extent="bounded",
    )


def _tavern_archetype() -> Any:
    from sidequest.genre.models.site_archetype import SiteArchetype

    return SiteArchetype(
        archetype_id="tavern",
        interior_algorithm="roomcorridor",
        room_count_min=3,
        room_count_max=6,
        grid_width=15,
        grid_height=20,
        cell_scale_feet=5,
    )


def _real_bundle_palette_snapshot_pack() -> tuple[Any, Any, Any, Any]:
    """Real materialize dependencies, reusing the materializer suite's honest
    helpers (real cookbook load, real ThemePalette, real GameSnapshot, real
    trope pack) — no mocking of the dungeon layer."""
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _real_cookbook_bundle,
    )

    bundle = _real_cookbook_bundle()
    palette = _commit_palette("tavern_interior")
    snapshot = _fresh_snapshot()
    pack = _attach_pack("cave_in")
    return bundle, palette, snapshot, pack


async def _materialize_site(repo: Any, *, base_seed: int = 12345) -> None:
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized

    repo.set_campaign_seed(base_seed)
    bundle, palette, snapshot, pack = _real_bundle_palette_snapshot_pack()
    await ensure_bounded_site_materialized(
        site=_tavern_descriptor(),
        archetype=_tavern_archetype(),
        dungeon_repository=repo,
        snapshot=snapshot,
        pack=pack,
        bundle=bundle,
        palette=palette,
    )


# ---------------------------------------------------------------------------
# Contract — module + async signature (no DB)
# ---------------------------------------------------------------------------


def test_module_exposes_ensure_bounded_site_materialized() -> None:
    """The public entry point is an async function with the keyword-only
    signature the movement dispatch (Task 6/12) calls."""
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized

    assert inspect.iscoroutinefunction(ensure_bounded_site_materialized)
    params = inspect.signature(ensure_bounded_site_materialized).parameters
    for name in (
        "site",
        "archetype",
        "dungeon_repository",
        "snapshot",
        "pack",
        "bundle",
        "palette",
    ):
        assert name in params, f"missing keyword param {name!r}"
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_missing_store_fails_loud() -> None:
    """No Silent Fallbacks: a bounded site with no dungeon store raises
    SeamCrossingError — never a quiet no-op that leaves the player stuck."""
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized
    from sidequest.game.seams.base import SeamCrossingError

    bundle, palette, snapshot, pack = _real_bundle_palette_snapshot_pack()
    with pytest.raises(SeamCrossingError):
        await ensure_bounded_site_materialized(
            site=_tavern_descriptor(),
            archetype=_tavern_archetype(),
            dungeon_repository=None,
            snapshot=snapshot,
            pack=pack,
            bundle=bundle,
            palette=palette,
        )


# ---------------------------------------------------------------------------
# Behavioral invariants — real PgDungeonRepository (the transaction boundary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_site_materializes_whole_with_no_open_frontier(
    monkeypatch: Any, migrated_db: str
) -> None:
    """A bounded site materializes its entrance + rooms whole, and leaves NO
    open frontier edges (bounded ≠ frontier — no lookahead worker)."""
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    await _materialize_site(repo)

    entrance = site_entrance_id(_SITE_ID)
    graph = repo.load_map(entrance_id=entrance, site_id=_SITE_ID)
    assert entrance in graph.nodes
    # entrance + [room_count_min .. room_count_max] rooms, all at once.
    arch = _tavern_archetype()
    assert arch.room_count_min <= len(graph.nodes) <= arch.room_count_max + 1
    # Bounded: no frontier edges left open for a lookahead worker to expand.
    assert repo.load_frontier(site_id=_SITE_ID) == []


@pytest.mark.asyncio
async def test_idempotent_second_entry_skips(monkeypatch: Any, migrated_db: str) -> None:
    """Re-entering an already-materialized bounded site is a no-op — the second
    call adds no nodes (idempotency guard on the entrance node)."""
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    entrance = site_entrance_id(_SITE_ID)

    await _materialize_site(repo)
    count1 = len(repo.load_map(entrance_id=entrance, site_id=_SITE_ID).nodes)

    # Second entry — must NOT re-materialize. set_campaign_seed is write-once,
    # so re-run the call directly (the base seed is already committed).
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized

    bundle, palette, snapshot, pack = _real_bundle_palette_snapshot_pack()
    await ensure_bounded_site_materialized(
        site=_tavern_descriptor(),
        archetype=_tavern_archetype(),
        dungeon_repository=repo,
        snapshot=snapshot,
        pack=pack,
        bundle=bundle,
        palette=palette,
    )
    count2 = len(repo.load_map(entrance_id=entrance, site_id=_SITE_ID).nodes)
    assert count1 == count2


@pytest.mark.asyncio
async def test_bounded_site_is_deterministic(monkeypatch: Any, migrated_db: str) -> None:
    """Same base seed + same site_id → identical node-id set across two fresh
    sessions (deterministic from blake2b(campaign_seed, site_id), no RNG draw)."""
    from tests.dungeon.conftest import build_pg_dungeon_repo

    entrance = site_entrance_id(_SITE_ID)

    _p1, repo_a, _s1 = build_pg_dungeon_repo(monkeypatch, migrated_db)
    await _materialize_site(repo_a, base_seed=999)
    nodes_a = set(repo_a.load_map(entrance_id=entrance, site_id=_SITE_ID).nodes.keys())

    _p2, repo_b, _s2 = build_pg_dungeon_repo(monkeypatch, migrated_db)
    await _materialize_site(repo_b, base_seed=999)
    nodes_b = set(repo_b.load_map(entrance_id=entrance, site_id=_SITE_ID).nodes.keys())

    assert nodes_a == nodes_b
    assert entrance in nodes_a


@pytest.mark.asyncio
async def test_materialize_emits_commit_span(monkeypatch: Any, migrated_db: str) -> None:
    """OTEL lie-detector: materializing a bounded site fires
    ``site.materialize.commit`` with the node count — the GM panel's proof the
    site engine engaged (not narrator improvisation)."""
    import sidequest.telemetry.spans as _spans_module
    from tests.dungeon.conftest import build_pg_dungeon_repo
    from tests.dungeon.test_materializer import _otel_in_memory

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        await _materialize_site(repo)
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    names = [s.name for s in exporter.get_finished_spans()]
    assert any("site.materialize.commit" in n for n in names), (
        f"site.materialize.commit span not emitted; saw {names}"
    )


@pytest.mark.asyncio
async def test_missing_base_seed_is_minted_not_defaulted_to_zero(
    monkeypatch: Any, migrated_db: str
) -> None:
    """No Silent Fallbacks: a world with no bootstrapped frontier base seed
    (every non-beneath_sunden world) MINTS + persists a fresh base seed on first
    bounded entry — never silently coalesces a missing seed to 0."""
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    # Deliberately do NOT set a base campaign seed (unlike _materialize_site).
    assert repo.get_campaign_seed() is None
    bundle, palette, snapshot, pack = _real_bundle_palette_snapshot_pack()
    await ensure_bounded_site_materialized(
        site=_tavern_descriptor(),
        archetype=_tavern_archetype(),
        dungeon_repository=repo,
        snapshot=snapshot,
        pack=pack,
        bundle=bundle,
        palette=palette,
    )
    # A real base seed was established — not left None, not a silent 0.
    assert repo.get_campaign_seed() is not None
    entrance = site_entrance_id(_SITE_ID)
    assert entrance in repo.load_map(entrance_id=entrance, site_id=_SITE_ID).nodes


def test_non_default_site_id_requires_zero_lookahead() -> None:
    """Track B invariant: a non-frontier site_id with lookahead_breadth > 0 is
    incoherent (a bounded site has no frontier worker) and fails loud, so
    put_frontier can never cross-contaminate the frontier store."""
    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

    fe = FrontierEdge(
        frontier_edge_id="fe1", from_region_id="e", heading="in", spawn_depth_score=0.0
    )
    with pytest.raises(ValueError, match="lookahead_breadth"):
        MaterializationRequest.build(
            campaign_seed=1,
            expansion_id=1,
            frontier_edge=fe,
            frontier=[fe],
            attach_region_ids=["e"],
            heading="in",
            burst_magnitude=3,
            lookahead_breadth=1,
            site_id="gilded_boar",
        )


# ---------------------------------------------------------------------------
# ADR-157 — cookbook-free coordinator (materialize_bounded, story 164-10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_bounded_commits_whole_graph_with_masks(
    monkeypatch: Any, migrated_db: str
) -> None:
    """materialize_bounded() commits the entrance + rooms whole, WITH per-room
    masks, using a synthetic archetype palette and NO cookbook."""
    from sidequest.dungeon.bounded_site import _derive_site_seed
    from sidequest.dungeon.materializer import (
        MaterializationRequest,
        build_bounded_palette,
        materialize_bounded,
    )
    from sidequest.dungeon.persistence import FrontierEdge
    from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
    from sidequest.dungeon.seed_bootstrap import select_entrance_theme_id
    from sidequest.game.sites.namespacing import site_entrance_id
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    repo.set_campaign_seed(4242)
    seed = _derive_site_seed(base_seed=4242, site_id=_SITE_ID)
    repo.set_campaign_seed(seed, site_id=_SITE_ID)

    archetype = _tavern_archetype()
    palette = build_bounded_palette(archetype)
    entrance = site_entrance_id(_SITE_ID)
    entrance_theme = select_entrance_theme_id(palette)
    graph = RegionGraph(entrance_id=entrance)
    graph.add_node(RegionNode(id=entrance, expansion_id=0, theme=entrance_theme))
    fe = FrontierEdge(
        frontier_edge_id=f"{_SITE_ID}:seed_fe1",
        from_region_id=entrance,
        heading="in",
        spawn_depth_score=0.0,
    )
    request = MaterializationRequest.build(
        campaign_seed=seed,
        expansion_id=1,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=[entrance],
        heading="in",
        burst_magnitude=archetype.room_count_max,
        lookahead_breadth=0,
        site_id=_SITE_ID,
    )
    await materialize_bounded(
        request,
        graph=graph,
        palette=palette,
        dungeon_repository=repo,
        archetype=archetype,
    )

    committed = repo.load_map(entrance_id=entrance, site_id=_SITE_ID)
    assert entrance in committed.nodes
    assert archetype.room_count_min <= len(committed.nodes) <= archetype.room_count_max + 1
    # Bounded → no frontier edges left for a lookahead worker.
    assert repo.load_frontier(site_id=_SITE_ID) == []
    # Per-room masks were persisted (the TACTICAL_GRID source).
    masks = repo.load_masks(site_id=_SITE_ID)
    room_ids = [n for n in committed.nodes if n != entrance]
    assert room_ids, "expected at least one procedural room"
    assert all(rid in masks for rid in room_ids)
    # Cookbook-free: NO monster population mutations written.
    kinds = {m.kind for m in repo.load_mutations(site_id=_SITE_ID)}
    assert "region_population" not in kinds
    assert "setpiece_state" not in kinds
