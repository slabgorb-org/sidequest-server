"""PgDungeonRepository — real-Postgres round-trip tests (ADR-115 B1).

Every test uses a uuid-namespaced session slug to avoid cross-test bleed
under xdist -n auto.  The migrated_db fixture is session-scoped and the
pool COMMITS (no rollback), so fixed slugs would collide across workers.
"""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions

# ---------------------------------------------------------------------------
# Helpers — reused fixture-construction patterns from
# tests/dungeon/test_persistence.py
# ---------------------------------------------------------------------------


def _make_slug() -> str:
    return f"dungeon_{uuid.uuid4().hex[:12]}"


def _seed_pool_and_session(monkeypatch, migrated_db: str) -> tuple:
    """Return (pool, session_id) for an isolated test session."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = _make_slug()
    sid = sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="caverns", world_slug="beneath_sunden"
    )
    return pool, sid


def _build_graph():
    """Minimal 3-region graph: entrance + expansion-1 nodes + edges."""
    from sidequest.dungeon.region_graph.depth import assign_depth_scores
    from sidequest.dungeon.region_graph.generator import attach_expansion, generate_expansion
    from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode

    g = RegionGraph(entrance_id="entrance")
    g.add_node(RegionNode(id="entrance", expansion_id=0, theme="threshold"))
    exp, _ = generate_expansion(
        graph=g,
        campaign_seed=42,
        expansion_id=1,
        attach_region_ids=["entrance"],
        theme_pool=["crypt", "catacomb"],
    )
    attach_expansion(g, exp)
    assign_depth_scores(g, campaign_seed=42)
    return g, exp


# ---------------------------------------------------------------------------
# campaign_seed: get/set + write-once
# ---------------------------------------------------------------------------


def test_get_campaign_seed_returns_none_on_fresh_session(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        assert repo.get_campaign_seed() is None
    finally:
        db_pool.close_pool()


def test_set_and_get_campaign_seed_round_trips(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        repo.set_campaign_seed(98765)
        assert repo.get_campaign_seed() == 98765
    finally:
        db_pool.close_pool()


def test_set_campaign_seed_is_write_once(monkeypatch, migrated_db):
    from sidequest.game.persistence import PersistError

    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        repo.set_campaign_seed(1)
        with pytest.raises(PersistError):
            repo.set_campaign_seed(2)
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# commit_expansion / load_map / load_masks
# ---------------------------------------------------------------------------


def test_commit_expansion_then_load_map_round_trips_graph(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.region_graph.model import Expansion
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        g, exp = _build_graph()

        entrance = g.nodes[g.entrance_id]
        seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
        repo.commit_expansion(seed_exp, g)
        repo.commit_expansion(exp, g)

        reloaded = repo.load_map(entrance_id="entrance")
        assert reloaded.nodes == g.nodes
        assert sorted(reloaded.edges, key=repr) == sorted(g.edges, key=repr)
    finally:
        db_pool.close_pool()


def test_commit_expansion_freeze_violation_raises_persist_error(monkeypatch, migrated_db):
    from sidequest.game.persistence import PersistError

    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.region_graph.model import Expansion
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        g, exp = _build_graph()
        entrance = g.nodes[g.entrance_id]
        seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
        repo.commit_expansion(seed_exp, g)
        repo.commit_expansion(exp, g)

        # Second commit of the same expansion must raise
        with pytest.raises(PersistError):
            repo.commit_expansion(exp, g)
    finally:
        db_pool.close_pool()


def test_load_masks_returns_empty_on_no_masks(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.region_graph.model import Expansion
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        g, exp = _build_graph()
        entrance = g.nodes[g.entrance_id]
        seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
        repo.commit_expansion(seed_exp, g)
        repo.commit_expansion(exp, g)

        assert repo.load_masks() == {}
    finally:
        db_pool.close_pool()


def test_load_masks_round_trips_mask_blobs(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.region_graph.model import Expansion
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        g, exp = _build_graph()
        entrance = g.nodes[g.entrance_id]
        seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
        repo.commit_expansion(seed_exp, g)

        region_id = exp.new_nodes[0].id
        masks = {region_id: {"cells": [[0, 1], [1, 0]]}}
        repo.commit_expansion(exp, g, masks=masks)

        loaded = repo.load_masks()
        assert region_id in loaded
        assert loaded[region_id] == {"cells": [[0, 1], [1, 0]]}
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# put_frontier / load_frontier
# ---------------------------------------------------------------------------


def test_put_and_load_frontier_round_trips(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import FrontierEdge
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        fe = FrontierEdge(
            frontier_edge_id=f"fe_{uuid.uuid4().hex[:8]}",
            from_region_id="entrance",
            heading="down",
            spawn_depth_score=10.0,
        )
        repo.put_frontier(fe)
        loaded = repo.load_frontier()
        assert loaded == [fe]
    finally:
        db_pool.close_pool()


def test_put_frontier_is_upsert(monkeypatch, migrated_db):
    """put_frontier on same frontier_edge_id must replace, not duplicate."""
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import FrontierEdge
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        fid = f"fe_{uuid.uuid4().hex[:8]}"
        fe1 = FrontierEdge(
            frontier_edge_id=fid,
            from_region_id="entrance",
            heading="down",
            spawn_depth_score=10.0,
        )
        fe2 = FrontierEdge(
            frontier_edge_id=fid,
            from_region_id="entrance",
            heading="down",
            spawn_depth_score=20.0,
        )
        repo.put_frontier(fe1)
        repo.put_frontier(fe2)
        loaded = repo.load_frontier()
        # Must have exactly one row with the updated depth
        assert len(loaded) == 1
        assert loaded[0].spawn_depth_score == 20.0
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# record_mutation / load_mutations
# ---------------------------------------------------------------------------


def test_record_mutation_and_load_mutations_round_trips(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        repo.record_mutation("region_1", "trap_sprung", {"trap": "pit"})
        repo.record_mutation("region_1", "looted", {"item": "sword"})
        repo.record_mutation("region_2", "collapsed", {})

        muts = repo.load_mutations()
        assert len(muts) == 3
        assert [m.kind for m in muts] == ["trap_sprung", "looted", "collapsed"]
        assert muts[0].region_id == "region_1"
        assert muts[0].payload == {"trap": "pit"}
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# open_thread / get_thread / open_threads / resolve_thread
# ---------------------------------------------------------------------------


def test_open_thread_and_get_thread_round_trips(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import ComplicationThread
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        tid = f"t_{uuid.uuid4().hex[:8]}"
        thread = ComplicationThread(
            thread_id=tid,
            origin_region_id="entrance",
            kind="trope",
            status="open",
            started_at_depth_score=5.0,
            payload={"trope": "doomed_priest"},
        )
        repo.open_thread(thread)
        got = repo.get_thread(tid)
        assert got.thread_id == tid
        assert got.kind == "trope"
        assert got.status == "open"
        assert got.payload == {"trope": "doomed_priest"}
    finally:
        db_pool.close_pool()


def test_open_threads_lists_only_open(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import ComplicationThread
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        t1 = f"t_{uuid.uuid4().hex[:8]}"
        t2 = f"t_{uuid.uuid4().hex[:8]}"
        repo.open_thread(
            ComplicationThread(
                thread_id=t1,
                origin_region_id="entrance",
                kind="trope",
                status="open",
                started_at_depth_score=5.0,
                payload={},
            )
        )
        repo.open_thread(
            ComplicationThread(
                thread_id=t2,
                origin_region_id="entrance",
                kind="quest",
                status="open",
                started_at_depth_score=10.0,
                payload={},
            )
        )
        repo.resolve_thread(t1)
        open_ids = {t.thread_id for t in repo.open_threads()}
        assert open_ids == {t2}
    finally:
        db_pool.close_pool()


def test_resolve_thread_sets_status_and_resolved_at(monkeypatch, migrated_db):
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import ComplicationThread
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        tid = f"t_{uuid.uuid4().hex[:8]}"
        repo.open_thread(
            ComplicationThread(
                thread_id=tid,
                origin_region_id="entrance",
                kind="trope",
                status="open",
                started_at_depth_score=5.0,
                payload={},
            )
        )
        repo.resolve_thread(tid)
        got = repo.get_thread(tid)
        assert got.status == "resolved"

        # resolved_at must be a non-null ISO string
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT resolved_at FROM dungeon_complication_ledger "
                "WHERE session_id = %s AND thread_id = %s",
                (sid, tid),
            ).fetchone()
        assert row is not None
        assert row[0] is not None
    finally:
        db_pool.close_pool()


def test_resolve_unknown_thread_raises_not_found(monkeypatch, migrated_db):
    from sidequest.game.persistence import NotFoundError

    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        with pytest.raises(NotFoundError):
            repo.resolve_thread("does-not-exist")
    finally:
        db_pool.close_pool()


def test_get_thread_raises_not_found_for_unknown(monkeypatch, migrated_db):
    from sidequest.game.persistence import NotFoundError

    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        with pytest.raises(NotFoundError):
            repo.get_thread("no-such-thread")
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# transaction() — caller-owned boundary (D6 seam)
# ---------------------------------------------------------------------------


def test_transaction_commits_atomically(monkeypatch, migrated_db):
    """commit_expansion + put_frontier inside one transaction() commit together."""
    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import FrontierEdge
        from sidequest.dungeon.region_graph.model import Expansion
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        g, exp = _build_graph()
        entrance = g.nodes[g.entrance_id]
        fid = f"fe_{uuid.uuid4().hex[:8]}"
        fe = FrontierEdge(
            frontier_edge_id=fid,
            from_region_id="entrance",
            heading="down",
            spawn_depth_score=5.0,
        )

        with repo.transaction() as tx:
            seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
            tx.commit_expansion(seed_exp, g)
            tx.commit_expansion(exp, g)
            tx.put_frontier(fe)

        # Both reads after the transaction closed must see committed data
        reloaded = repo.load_map(entrance_id="entrance")
        frontier = repo.load_frontier()
        assert reloaded.nodes == g.nodes
        assert any(f.frontier_edge_id == fid for f in frontier)
    finally:
        db_pool.close_pool()


def test_transaction_rolls_back_on_persist_error(monkeypatch, migrated_db):
    """A PersistError inside transaction() must roll back all writes."""
    from sidequest.game.persistence import PersistError

    pool, sid = _seed_pool_and_session(monkeypatch, migrated_db)
    try:
        from sidequest.dungeon.persistence import FrontierEdge
        from sidequest.dungeon.region_graph.model import Expansion
        from sidequest.game.pg.dungeon import PgDungeonRepository

        repo = PgDungeonRepository(pool, session_id=sid)
        g, exp = _build_graph()
        entrance = g.nodes[g.entrance_id]
        fid = f"fe_{uuid.uuid4().hex[:8]}"
        fe = FrontierEdge(
            frontier_edge_id=fid,
            from_region_id="entrance",
            heading="down",
            spawn_depth_score=5.0,
        )

        # First commit so we can trigger a freeze violation
        with repo.transaction() as tx:
            seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
            tx.commit_expansion(seed_exp, g)

        # Now try a transaction that hits a freeze violation mid-way
        with pytest.raises(PersistError), repo.transaction() as tx:
            tx.put_frontier(fe)
            tx.commit_expansion(exp, g)
            tx.commit_expansion(exp, g)  # freeze violation — same expansion twice

        # The frontier write in the aborted transaction must NOT be visible
        frontier_after = repo.load_frontier()
        assert not any(f.frontier_edge_id == fid for f in frontier_after)
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# Cross-session isolation
# ---------------------------------------------------------------------------


def test_cross_session_isolation(monkeypatch, migrated_db):
    """Data from session A must not appear in session B's reads."""
    from sidequest.dungeon.persistence import ComplicationThread, FrontierEdge
    from sidequest.dungeon.region_graph.model import Expansion
    from sidequest.game.pg.dungeon import PgDungeonRepository

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    try:
        slug_a = _make_slug()
        slug_b = _make_slug()
        sid_a = sessions.ensure_session(
            pool,
            slug=slug_a,
            mode="solo",
            genre_slug="caverns",
            world_slug="beneath_sunden",
        )
        sid_b = sessions.ensure_session(
            pool,
            slug=slug_b,
            mode="solo",
            genre_slug="caverns",
            world_slug="beneath_sunden",
        )

        repo_a = PgDungeonRepository(pool, session_id=sid_a)
        repo_b = PgDungeonRepository(pool, session_id=sid_b)

        g, exp = _build_graph()
        entrance = g.nodes[g.entrance_id]
        seed_exp = Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[])
        fid = f"fe_{uuid.uuid4().hex[:8]}"
        tid = f"t_{uuid.uuid4().hex[:8]}"

        # Write everything into session A
        repo_a.set_campaign_seed(12345)
        repo_a.commit_expansion(seed_exp, g)
        repo_a.commit_expansion(exp, g)
        repo_a.put_frontier(
            FrontierEdge(
                frontier_edge_id=fid,
                from_region_id="entrance",
                heading="down",
                spawn_depth_score=5.0,
            )
        )
        repo_a.record_mutation("entrance", "trap_sprung", {})
        repo_a.open_thread(
            ComplicationThread(
                thread_id=tid,
                origin_region_id="entrance",
                kind="trope",
                status="open",
                started_at_depth_score=0.0,
                payload={},
            )
        )

        # Session B must see nothing from A
        assert repo_b.get_campaign_seed() is None
        assert not repo_b.load_map(entrance_id="entrance").nodes
        assert repo_b.load_frontier() == []
        assert repo_b.load_mutations() == []
        assert repo_b.open_threads() == []
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# DungeonRepository Protocol
# ---------------------------------------------------------------------------


def test_pg_dungeon_repository_satisfies_dungeon_repository_protocol():
    """PgDungeonRepository is an isinstance match for DungeonRepository."""
    from unittest.mock import MagicMock

    from psycopg_pool import ConnectionPool

    from sidequest.game.pg.dungeon import PgDungeonRepository
    from sidequest.game.repository import DungeonRepository

    pool = MagicMock(spec=ConnectionPool)
    repo = PgDungeonRepository(pool, session_id=1)
    assert isinstance(repo, DungeonRepository)
