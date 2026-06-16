"""Story 52-3 — mask persistence wiring contract.

Unit tests in ``test_persistence_mask.py`` prove the persistence API
itself works on hand-built inputs. CLAUDE.md mandates a wiring test:
"Every set of tests must include at least one integration test that
verifies the component is wired into the system — imported, called, and
reachable from production code paths."

This file is that test. It drives the REAL five-stage materializer
coordinator (design → fill → curate → attach → commit) against a real
DungeonStore on a real connection, then asserts that
``dungeon_map.mask`` is populated for the materialised regions — proving
the fill stage's ``RegionMask`` (Story 52-2) is actually threaded into
``commit_expansion(..., masks=...)`` by ``_stage_commit``.

Helpers (``_real_cookbook_bundle``, ``_commit_palette``,
``_seed_graph_themed``, etc.) are re-used from ``test_materializer.py``
via private import — the established cross-test convention for the
Plan 7 fixture stack. If those private helpers are renamed, this test
breaks loudly (it should — the fixture seam is load-bearing).
"""

from __future__ import annotations

import json
import sqlite3


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


async def test_materialize_pipeline_writes_mask_blobs_for_generated_regions() -> None:
    """End-to-end: drive ``materialize()`` against a real DungeonStore;
    after the pipeline completes, every region the FILL stage produced a
    ``RegionMask`` for must have a non-NULL ``dungeon_map.mask`` row.

    This binds three things at once:
      1. ``_stage_fill`` produces ``RegionFill.mask`` (Story 52-2 — already shipped).
      2. ``_stage_commit`` THREADS the fill_result's masks into ``commit_expansion`` (Story 52-3 — this story).
      3. ``commit_expansion`` actually writes the BLOB (Story 52-3 — exercised by the unit suite, here also).

    A NULL mask BLOB for a region that the fill stage produced a mask
    for is the Illusionism the GM panel exists to catch: the mask was
    computed but never persisted, and Plan 1–6 silently used a default
    on reload. That is exactly the materializer.py:57 gap this story
    closes."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from sidequest.dungeon.persistence import DungeonStore

    # Re-use the Plan 7 fixture stack — these are the live helpers that
    # already drive the rest of TestStageCommit's real-coordinator tests
    # (test_materializer.py:3140+). Importing them keeps the seam shape
    # honest — a fixture rename breaks this test the same way it breaks
    # the existing commit-stage tests.
    from tests.dungeon.test_materializer import (
        MaterializationRequest_build,
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _otel_in_memory,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
        _seed_graph_themed,
    )

    theme_id = "mask_wiring_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()

    bundle = _real_cookbook_bundle()
    request = MaterializationRequest_build(campaign_seed=7, expansion_id=1, spawn_depth_score=0.0)

    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        await materialize(
            request,
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

    # Pull the persisted rows + mask columns directly from sqlite. The
    # entrance (expansion_id=0) has no fill grid (Seed=Expansion-0
    # contract) and therefore no mask — it MUST be NULL there. Every
    # row with expansion_id >= 1 came from _stage_fill and MUST have a
    # non-NULL BLOB.
    rows = conn.execute(
        "SELECT region_id, expansion_id, mask FROM dungeon_map ORDER BY region_id"
    ).fetchall()
    assert rows, "no rows persisted — materialize() did not commit anything"

    seed_rows = [r for r in rows if r["expansion_id"] == 0]
    gen_rows = [r for r in rows if r["expansion_id"] >= 1]

    # The entrance: NULL mask is the correct answer (no fill grid).
    for r in seed_rows:
        assert r["mask"] is None, (
            f"seed row {r['region_id']!r} has a non-NULL mask BLOB "
            "(expansion_id=0 has no fill grid; masks must NOT be invented)"
        )

    # Generated regions: every one must carry a mask BLOB. Anything else
    # is the materializer.py:57 wiring gap reopening.
    assert gen_rows, "no generated-expansion rows persisted"
    for r in gen_rows:
        assert r["mask"] is not None, (
            f"generated region {r['region_id']!r} has a NULL mask BLOB — "
            "_stage_commit did NOT thread fill_result masks into "
            "commit_expansion(..., masks=...). This is the exact "
            "materializer.py:57 gap Story 52-3 closes."
        )
        # The BLOB must be valid JSON — the on-disk encoding contract.
        decoded = json.loads(r["mask"].decode("utf-8"))
        assert isinstance(decoded, dict), (
            f"mask BLOB for {r['region_id']!r} is not a JSON object: {decoded!r}"
        )


async def test_materialize_then_reload_returns_masks_for_generated_regions() -> None:
    """End-to-end resume contract (AC3 at the integration layer):
    drive the materialize pipeline → ``conn.commit()`` → call
    ``store.load_masks()`` on the same store; the result must contain
    an entry for every generated region (and only those).

    This is the player-facing "reload on resume" promise — a saved
    procedural dungeon must come back with its masks intact, not as a
    pile of NULL rows that the next render silently treats as empty
    grids."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from sidequest.dungeon.persistence import DungeonStore
    from tests.dungeon.test_materializer import (
        MaterializationRequest_build,
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _otel_in_memory,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
        _seed_graph_themed,
    )

    theme_id = "mask_reload_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()

    request = MaterializationRequest_build(campaign_seed=42, expansion_id=1, spawn_depth_score=0.0)
    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        await materialize(
            request,
            graph=graph,
            bundle=_real_cookbook_bundle(),
            palette=palette,
            persistence=store,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
            claude_client=_reflecting_sdk_client(),
        )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    # Generated region ids — pulled from the persisted rows so the
    # assertion is in terms of the actual save, not a guess at the
    # generator's output.
    gen_region_ids = {
        r["region_id"]
        for r in conn.execute(
            "SELECT region_id FROM dungeon_map WHERE expansion_id >= 1"
        ).fetchall()
    }
    assert gen_region_ids, "no generated regions persisted"

    loaded_masks = store.load_masks()
    assert set(loaded_masks) == gen_region_ids, (
        f"load_masks() key set drift: got {set(loaded_masks)!r}, expected {gen_region_ids!r}"
    )
