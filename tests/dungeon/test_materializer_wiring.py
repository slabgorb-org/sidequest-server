"""Beneath Sünden Plan 7 Task 6 — MANDATORY WIRING TEST.

CLAUDE.md "Every Test Suite Needs a Wiring Test": unit tests prove a
component works in isolation; that is not enough. This file proves the
materializer's look-ahead frontier seam is **invoked from the real
production region-transition path** — not called directly by a test.

The real production region-transition point that mutates
``snap.current_region`` mid-session is the ADR-011 ``WorldStatePatch``
apply in ``GameSnapshot.apply_world_patch`` (session.py) — the narrator's
escape-hatch / monster-manual-inject path both flow through it
(``sidequest/agents/tools/apply_world_patch.py``,
``sidequest/server/dispatch/monster_manual_inject.py``). ADR-055's
``room_movement`` runtime surface (``validate_room_transition`` /
``apply_validated_move``) is explicitly deferred to a later story
(``room_movement.py`` docstring) and is ROOM-level, orthogonal to the
REGION-transition the materializer's frontier needs. So the
frontier-approach/crossing hook lands on the real ``apply_world_patch``
region-transition point, extending it + ADR-055 ``region_init``
semantics — NOT a parallel navigation path, NOT a stub.

Task 6 owns the producer side (the real dispatch seam wired into the real
path + promote-to-active recognition). Task 7 owns the async worker that
consumes the enqueue — this test spies the seam, it does not implement
the worker.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Any:
    """Belt-and-suspenders: unconditionally restore
    ``frontier_hook._OBSERVERS`` after every test in this module,
    regardless of how each test wrapped registration.

    ``_OBSERVERS`` is a process-global module list. The per-test
    register/unregister ``try/finally`` blocks are the intentional,
    in-test contract and stay as-is — but a future edit (a registered
    callable that raises before its ``finally``, or a new test that
    forgets the ``finally``) would leak an observer into the global
    registry and pollute the rest of the ~6500-test suite for the whole
    process. This fixture closes that latent cross-test-pollution hole by
    snapshotting the module list before the test and restoring its exact
    contents in teardown — independent of any per-test cleanup. It reuses
    the existing module list directly (test-only snapshot/restore; no new
    production API is added just for the test).
    """
    from sidequest.dungeon import frontier_hook

    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        # Restore exact pre-test contents in place (other code may hold a
        # reference to the same list object — mutate, don't rebind).
        frontier_hook._OBSERVERS[:] = before


def test_apply_world_patch_region_transition_fires_frontier_hook() -> None:
    """Drive the REAL production region-transition (a WorldStatePatch with
    ``current_region`` applied through ``GameSnapshot.apply_world_patch``,
    the exact code path the narrator/monster-manual-inject use) and assert
    the materializer's frontier look-ahead seam was invoked FROM that path
    — observed via a real spy registered on the enqueue seam, NOT by the
    test calling the hook directly.

    If this fails because ``apply_world_patch`` does not reach the hook,
    the wiring is broken (the seam exists but production never crosses it
    — CLAUDE.md "Verify Wiring, Not Just Existence")."""
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )
    from sidequest.game.session import GameSnapshot, WorldStatePatch

    # A real GameSnapshot at a real region (the surface entrance).
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.current_region = "entrance"

    seen: list[dict[str, Any]] = []

    def _spy(*, snapshot: Any, pc_name: str, from_region: str | None, to_region: str) -> None:
        # The real Task-7-shaped observer signature (now per-PC: pc_name is
        # which PC moved — Movement subsystem §Q2); here it just records that
        # the production transition reached the seam.
        seen.append(
            {
                "snapshot_is": snapshot is snap,
                "pc_name": pc_name,
                "from_region": from_region,
                "to_region": to_region,
            }
        )

    register_frontier_observer(_spy)
    try:
        # THE PRODUCTION PATH: a narrator-shaped WorldStatePatch moving the
        # party to a new region, applied through the real public
        # apply_world_patch entry point (NOT a direct call to the hook).
        snap.apply_world_patch(WorldStatePatch(current_region="exp001.r0"))
    finally:
        unregister_frontier_observer(_spy)

    # The seam fired BECAUSE the production region-transition fired.
    assert seen, (
        "the frontier look-ahead seam was NOT invoked from the real "
        "apply_world_patch region-transition path — the hook is not wired "
        "into production (CLAUDE.md: half-wired features are forbidden)"
    )
    assert seen[0]["snapshot_is"], "hook did not receive the live snapshot"
    assert seen[0]["from_region"] == "entrance"
    assert seen[0]["to_region"] == "exp001.r0"
    # The transition actually happened (real apply, not intercepted away).
    assert snap.current_region == "exp001.r0"


def test_region_transition_without_change_does_not_fire_hook() -> None:
    """A WorldStatePatch that does NOT set ``current_region`` (or sets it
    to the same region) must not fire the frontier seam — the hook is a
    REGION-TRANSITION hook, not a fire-on-every-patch hook (No Silent
    Fallbacks: no spurious look-ahead enqueue)."""
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )
    from sidequest.game.session import GameSnapshot, WorldStatePatch

    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.current_region = "entrance"

    fired: list[Any] = []
    obs = lambda **kw: fired.append(kw)  # noqa: E731

    register_frontier_observer(obs)
    try:
        # No current_region in the patch → no region transition.
        snap.apply_world_patch(WorldStatePatch(atmosphere="cold"))
        assert fired == [], "hook fired on a non-region-transition patch"

        # current_region set to the SAME region → not a transition.
        snap.apply_world_patch(WorldStatePatch(current_region="entrance"))
        assert fired == [], (
            "hook fired when current_region was set to the unchanged "
            "value — that is not a region transition"
        )
    finally:
        unregister_frontier_observer(obs)


def test_frontier_crossing_promotes_region_to_active() -> None:
    """The frontier-crossing transition: when the party crosses INTO a
    region, the minimal real promote-to-active state transition runs —
    the crossed-into region is recognized in ``snap.discovered_regions``
    (extending ADR-055 ``region_init`` dedup-append semantics, NOT a
    parallel path). The committed expansion is already live from Task 6's
    commit txn; "promote to active" = the session/region state now
    recognizes it."""
    from sidequest.game.session import GameSnapshot, WorldStatePatch

    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.current_region = "entrance"
    snap.discovered_regions = ["entrance"]

    snap.apply_world_patch(WorldStatePatch(current_region="exp001.r0"))

    assert snap.current_region == "exp001.r0"
    assert "exp001.r0" in snap.discovered_regions, (
        "crossing into a look-ahead-materialized region did not promote "
        "it to active (it must be recognized in discovered_regions — the "
        "ADR-055 region_init dedup-append semantics, extended)"
    )
    # Dedup-append: the existing entry order is preserved (save compat,
    # the region_init contract).
    assert snap.discovered_regions[0] == "entrance"
    assert snap.discovered_regions.count("exp001.r0") == 1


# ---------------------------------------------------------------------------
# Plan 7 Task 7 — async look-ahead WORKER wiring test.
#
# CLAUDE.md "Every Test Suite Needs a Wiring Test": this proves Task 7's
# async look-ahead worker is invoked FROM Task 6's REAL production
# region-transition producer (apply_world_patch → notify_region_transition),
# NOT called directly by the test. Real DungeonStore on a real connection,
# real materialize() pipeline through Tasks 1–6, real frontier_hook
# producer. The ONLY mock is the claude -p curation subprocess.
# ---------------------------------------------------------------------------


def _mem_conn() -> Any:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


async def _seed_expansion_one(store: Any) -> Any:
    """Run the REAL five-stage coordinator for expansion 1 against a real
    DungeonStore so the store carries a committed seed (entrance, exp 0) +
    expansion 1's regions + REAL unexpanded frontier edges rooted at
    exp001.r* (Task 6's commit derived them). Returns the theme_id used so
    the caller can resolve the same palette for the look-ahead expansion."""
    from tests.dungeon.test_materializer import (
        _commit_palette,
        _materialize_full,
        _seed_graph_themed,
    )

    theme_id = "lookahead_wire_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    await _materialize_full(graph=graph, palette=palette, store=store)
    return theme_id, palette


async def test_lookahead_worker_materializes_from_real_region_transition() -> None:
    """Drive the REAL production region-transition (a WorldStatePatch with
    ``current_region`` applied through ``GameSnapshot.apply_world_patch``,
    the exact code path the narrator/monster-manual-inject use) and assert
    Task 7's async look-ahead worker materialized the next expansion FROM
    that path — observed via real committed store state, NOT by the test
    calling the worker/materialize directly.

    Teeth: with the worker NOT registered, crossing the frontier commits
    NO new expansion (the seam is wired-but-unconsumed). Not circular: the
    test never calls the worker or materialize() — it drives
    apply_world_patch and inspects the real DungeonStore."""
    from sidequest.dungeon.lookahead_worker import register_lookahead_worker
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import GameSnapshot, WorldStatePatch
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
    )

    # --- Teeth half: worker NOT registered → no look-ahead expansion. ---
    conn_a = _mem_conn()
    store_a = DungeonStore(conn_a)
    store_a.ensure_schema()
    _theme_a, _palette_a = await _seed_expansion_one(store_a)

    frontier_a = store_a.load_frontier()
    assert frontier_a, "expansion 1 commit did not derive any frontier edges"
    target_region = frontier_a[0].from_region_id  # a real exp001.r* region

    snap_a = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap_a.current_region = "entrance"
    snap_a.apply_world_patch(WorldStatePatch(current_region=target_region))
    # No worker registered: only the original expansion 1 exists.
    exp_ids_a = {n.expansion_id for n in store_a.load_map(entrance_id="entrance").nodes.values()}
    assert 2 not in exp_ids_a, (
        "a look-ahead expansion 2 was committed with NO worker registered "
        "— the wiring test is circular / the seam fires without a consumer"
    )

    # --- Live half: worker registered → crossing the frontier
    #     materializes the next expansion FROM the real producer path. ---
    conn_b = _mem_conn()
    store_b = DungeonStore(conn_b)
    store_b.ensure_schema()
    _theme_b, palette_b = await _seed_expansion_one(store_b)

    frontier_b = store_b.load_frontier()
    target_b = frontier_b[0].from_region_id

    obs = register_lookahead_worker(
        persistence=store_b,
        bundle=_real_cookbook_bundle(),
        palette=palette_b,
        pack_tropes=_attach_pack("cave_in"),
        claude_client=_reflecting_sdk_client(),
        campaign_seed=7,
        lookahead_breadth=1,
    )
    try:
        snap_b = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
        snap_b.current_region = "entrance"
        # THE PRODUCTION PATH: a narrator-shaped WorldStatePatch moving the
        # party into a region with rooted unexpanded frontier edges, applied
        # through the real public apply_world_patch entry point (NOT a
        # direct call to the worker).
        snap_b.apply_world_patch(WorldStatePatch(current_region=target_b))

        # The observer scheduled the worker fire-and-forget; the region
        # transition itself returned synchronously (central constraint).
        assert snap_b.current_region == target_b
        # Await the in-flight look-ahead task(s) the observer scheduled.
        await obs.drain()
    finally:
        obs.unregister()

    exp_ids_b = {n.expansion_id for n in store_b.load_map(entrance_id="entrance").nodes.values()}
    assert 2 in exp_ids_b, (
        "the look-ahead worker did NOT materialize the next expansion from "
        "the real apply_world_patch region-transition path — Task 7's "
        "worker is not wired into Task 6's producer (CLAUDE.md: half-wired "
        "features are forbidden)"
    )


# ---------------------------------------------------------------------------
# Story 52-2 — ADR-096 mask emit wiring test.
#
# CLAUDE.md "Every Test Suite Needs a Wiring Test": this proves the mask
# emit step is reached FROM the REAL production materialize() pipeline (not
# called directly by a unit test). The mask span fires inside the real
# coordinator when the look-ahead worker drives materialize() through the
# real region-transition producer. If 52-2 is properly wired, the GM panel
# (via SPAN_ROUTES) sees one ``dungeon.materialize.mask`` span per region
# in every real expansion.
# ---------------------------------------------------------------------------


async def test_mask_emit_fires_from_real_materialize_pipeline() -> None:
    """Drive the REAL materialize() coordinator (the same path the look-ahead
    worker uses) and assert that ``dungeon.materialize.mask`` spans were
    emitted during the run — proving the mask-emit step is wired INSIDE the
    fill stage and reached on every production-shape pipeline run.

    Non-circular: the test never calls ``_emit_mask`` or builds a
    ``RegionMask`` itself — it only inspects spans the real pipeline emitted
    via the routed OTEL surface (the GM panel's consumer).
    """
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_DUNGEON_MATERIALIZE_MASK,
    )
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _make_request_task3,
        _otel_in_memory,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
        _seed_graph_themed,
    )

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()

    bundle = _real_cookbook_bundle()
    theme_id = "mask_wired_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        req = _make_request_task3()
        await materialize(
            req,
            graph=graph,
            bundle=bundle,
            palette=palette,
            persistence=store,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
            claude_client=_reflecting_sdk_client(),
        )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    finished = exporter.get_finished_spans()
    mask_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_MASK]
    assert mask_spans, (
        "dungeon.materialize.mask span was NOT emitted by the real "
        "materialize() pipeline — the emit step is not wired into the fill "
        "stage (CLAUDE.md: half-wired features are forbidden)"
    )
    # The routed extractor surfaces the lie-detector attributes
    route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_MASK]
    for span_obj in mask_spans:
        attrs = route.extract(span_obj)  # type: ignore[arg-type]
        assert attrs.get("cell_width") == 28
        assert attrs.get("mask_sha") is not None
        assert isinstance(attrs.get("grid_width"), int)
        assert isinstance(attrs.get("grid_height"), int)
