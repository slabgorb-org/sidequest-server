"""Task 7: end-to-end region-population wiring test (generate → freeze → inject → seat).

CLAUDE.md "Every Test Suite Needs a Wiring Test": unit tests prove a component
works in isolation; that is not enough. This file proves the FULL chain:

  materialize() (Task 3 freezes rosters to dungeon store)
    → load_map() yields generated region nodes
    → snap.seed_pc_regions(region_id) places the party
    → monster_manual_inject.ensure_loaded(sd) binds the Manual
    → monster_manual_inject.inject(sd, snap, …, room_id=region_id) reads
      frozen rows via load_region_population and stamps region on patches
    → snap.npcs contains at least one Npc with .region == region_id
      and .threat_level is not None

Non-circular: the test drives REAL materialize() + REAL inject(); the ONLY
mocked seam is the claude -p curation subprocess (``_reflecting_sdk_client``),
the established wiring-test rule for this materializer harness.  ``sd`` carries
the same ``dungeon_repository`` that materialize() wrote to, so
``load_region_population`` reads the rows Task 3 actually committed.

Pre-existing failures (not caused by this feature, do not touch):
- tests/agents/test_59_30_witnesses.py::test_witnesses_count_is_nine_and_docstring_not_stale
- tests/agents/subsystems/test_movement_dispatch.py::test_move_toward_uncommitted_edge_sync_materializes
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Wiring test
# ---------------------------------------------------------------------------


async def test_region_population_end_to_end_inject(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    """Drive the REAL materialize() coordinator, pick a generated region from
    the committed dungeon map, inject the region's frozen creature roster via
    the REAL monster_manual_inject.inject() path, and assert that at least one
    Npc in snap.npcs carries .region == region_id and .threat_level is not None.

    Teeth: if the materializer does not write region_population rows (Task 3
    broken), or the loader returns empty (Task 4 broken), or the inject seam
    does not stamp region (Task 5 broken), or NpcPatch.region is not propagated
    to snap.npcs (Task 1 broken) — the assertion fails. This test is NOT
    satisfied by unit fakes: it calls the same code paths production uses.
    """
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from sidequest.game.session import GameSnapshot
    from sidequest.server.dispatch import monster_manual_inject
    from tests.dungeon.conftest import build_pg_dungeon_repo
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _commit_palette,
        _make_request_task3,
        _otel_in_memory,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
        _seed_graph_themed,
    )

    # --- Phase 1: materialize a real expansion into a real PgDungeonRepository.
    #
    # This is the same harness used by Task 3's wiring test
    # (test_region_population.py::test_region_population_rows_land_in_dungeon_store)
    # and the materializer wiring test.  It writes real region_population rows.
    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    bundle = _real_cookbook_bundle()
    theme_id = "wire_crypt_pop"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        req = _make_request_task3()
        await materialize(
            req,
            graph=graph,
            bundle=bundle,
            palette=palette,
            dungeon_repository=repo,
            snapshot=GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden"),
            pack_tropes=_attach_pack("cave_in"),
            claude_client=_reflecting_sdk_client(),
        )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    # --- Phase 2: pick a generated region from the committed map.
    #
    # load_map() re-reads from Postgres so this is NOT using the in-memory
    # expansion object — it exercises the real serialise/deserialise round-trip.
    dungeon_map = repo.load_map(entrance_id="entrance")
    generated_region_ids = [
        node_id for node_id, node in dungeon_map.nodes.items() if node_id != "entrance"
    ]
    assert generated_region_ids, (
        "materialize() committed no generated nodes beyond the entrance — "
        "cannot probe the inject path (wiring test is invalid)"
    )
    region_id = generated_region_ids[0]

    # Verify the region has a frozen population (Task 3/4 pre-check so the
    # assertion below is clearly about Task 5/6, not a missing earlier task).
    from sidequest.server.dispatch.region_population import load_region_population

    roster, big_bad = load_region_population(repo, region_id)
    assert roster or big_bad is not None, (
        f"region {region_id!r} has no frozen region_population rows — "
        "Task 3 (_stage_commit) did not write the roster (or Task 4 loader "
        "failed to parse it). Fix the earlier tasks before re-running Task 7."
    )

    # --- Phase 3: build a minimal sd carrying the REAL repo + a seeded Manual.
    #
    # ensure_loaded needs: .monster_manual (None → triggers load), .genre_slug,
    #   .world_slug, .genre_pack (for stale-cache coherence + authored backfill).
    # inject needs: .monster_manual (set by ensure_loaded), .genre_pack,
    #   .world_slug, .dungeon_repository.
    #
    # We use a real MonsterManual (empty, loaded fresh from disk for the test
    # genre key), and a pack shaped to allow combat_encounters=True so the
    # inject does not suppress creature patches.  genre_pack=None skips the
    # stale-cache purge and authored-NPC backfill paths — both are safe to skip
    # for this wiring test whose goal is the region_population path only.

    sd: Any = SimpleNamespace(
        monster_manual=None,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        # genre_pack=None causes ensure_loaded to skip seed_manual (needs_seeding
        # check) and authored backfill, but NOT to skip loading.  The Manual load
        # succeeds (returns an empty Manual); combat_encounters defaults to True in
        # inject's `getattr(rules, "combat_encounters", True)` fallback.
        genre_pack=None,
        dungeon_repository=repo,
    )

    # --- Phase 4: ensure_loaded + seed a manual NPC so the snapshot has content
    #   then inject with room_id=region_id to fire the region_population path.
    #
    # ensure_loaded will load a fresh MonsterManual (genre_slug seeded).
    manual = monster_manual_inject.ensure_loaded(sd)
    assert manual is not None, (
        "ensure_loaded returned None — genre_slug is set so this should "
        "have loaded (or created fresh) a MonsterManual"
    )

    # --- Phase 5: build a snapshot with a seated PC so seed_pc_regions works.
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    # Provide a character name so seed_pc_regions has something to seed.
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore

    core = CreatureCore(name="TestPC", description="test", personality="brave")
    snap.characters = [Character(core=core, backstory="test", char_class="Fighter", race="human")]

    seeded = snap.seed_pc_regions(region_id)
    assert seeded > 0, (
        f"seed_pc_regions({region_id!r}) seeded 0 PCs — the snapshot has no "
        "seated PCs or characters (check snap.characters setup)"
    )

    # --- Phase 6: inject with room_id=region_id — the region_population path.
    injected_count = monster_manual_inject.inject(
        sd,
        snap,
        current_location="probe_room",
        in_combat=True,
        room_id=region_id,
    )

    # The inject may return 0 if Manual + region_pop are both empty; we check
    # snap.npcs directly for region-stamped entries.
    region_npcs = [n for n in snap.npcs if n.region == region_id]
    assert region_npcs, (
        f"inject(room_id={region_id!r}) did not produce any Npc entries with "
        f".region == {region_id!r} in snap.npcs.  "
        f"inject() returned {injected_count}; snap.npcs = {snap.npcs!r}.  "
        "Possible causes: _npc_patches_for_region_population returned [] "
        "(check that load_region_population sees the committed rows), or "
        "NpcPatch.region is not propagated through apply_world_patch/_merge_npc_patch "
        "(check Task 1/5 wiring)."
    )

    # All injected region-stamped NPCs must carry threat_level (Task 2 + 5).
    assert all(n.threat_level is not None for n in region_npcs), (
        f"some region-stamped NPCs lack .threat_level: "
        f"{[n for n in region_npcs if n.threat_level is None]!r}"
    )
