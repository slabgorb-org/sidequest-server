"""Beneath Sünden Plan 5 — persistence layer tests.

Round-trip, freeze, no-floor, overlay, ledger, OTEL, and the Plan-7
wiring contract. Real SQLite only (:memory: + temp-file for WAL).
"""

from __future__ import annotations


def test_persistence_module_importable() -> None:
    import sidequest.dungeon.persistence as persistence

    assert hasattr(persistence, "DungeonStore")


from sidequest.dungeon.region_graph.model import RegionEdge, RegionNode  # noqa: E402


def test_region_node_dict_roundtrip_exact_inverse() -> None:
    n = RegionNode(id="exp001.r0", expansion_id=1, theme="crypt", depth_score=42.5)
    assert RegionNode.from_dict(n.to_dict()) == n

    n_unscored = RegionNode(id="entrance", expansion_id=0, theme="threshold")
    d = n_unscored.to_dict()
    assert d["depth_score"] is None
    assert RegionNode.from_dict(d) == n_unscored


def test_region_edge_dict_roundtrip_exact_inverse() -> None:
    e = RegionEdge(a="entrance", b="exp001.r0", kind="secret", hidden=True, shortcut=True)
    assert RegionEdge.from_dict(e.to_dict()) == e

    plain = RegionEdge(a="x", b="y", kind="corridor")
    d = plain.to_dict()
    assert d["hidden"] is False and d["shortcut"] is False
    assert RegionEdge.from_dict(d) == plain


from sidequest.dungeon.region_graph.model import RegionGraph  # noqa: E402


def test_region_graph_dict_roundtrip_exact_inverse() -> None:
    g = RegionGraph(entrance_id="entrance")
    g.add_node(RegionNode(id="entrance", expansion_id=0, theme="threshold", depth_score=0.0))
    g.add_node(RegionNode(id="exp001.r0", expansion_id=1, theme="crypt", depth_score=10.0))
    g.add_node(RegionNode(id="exp001.r1", expansion_id=1, theme="crypt", depth_score=12.0))
    g.add_edge(RegionEdge(a="entrance", b="exp001.r0", kind="corridor"))
    g.add_edge(RegionEdge(a="exp001.r0", b="exp001.r1", kind="stairs"))
    g.add_edge(RegionEdge(a="entrance", b="exp001.r1", kind="secret", hidden=True))

    restored = RegionGraph.from_dict(g.to_dict())
    assert restored.entrance_id == g.entrance_id
    assert restored.nodes == g.nodes
    assert restored.edges == g.edges


import sqlite3  # noqa: E402

from sidequest.dungeon.persistence import DungeonStore  # noqa: E402

_EXPECTED_TABLES = {
    "dungeon_map",
    "dungeon_edge",
    "dungeon_frontier",
    "dungeon_mutation_overlay",
    "dungeon_complication_ledger",
}


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_ensure_schema_creates_all_five_tables() -> None:
    conn = _mem_conn()
    DungeonStore(conn).ensure_schema()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert _EXPECTED_TABLES.issubset(names)


def test_ensure_schema_is_idempotent() -> None:
    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    store.ensure_schema()  # second call must not raise
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert _EXPECTED_TABLES.issubset({r["name"] for r in rows})


import re  # noqa: E402


def test_no_floor_indexed_keys_anywhere() -> None:
    """Spec §5/§11: nothing in the dungeon schema may be keyed by or
    named for a 'floor'. Introspect every table/column/index name."""
    conn = _mem_conn()
    DungeonStore(conn).ensure_schema()
    floor = re.compile(r"floor", re.IGNORECASE)

    objects = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE name LIKE 'dungeon_%' "
        "   OR name LIKE 'idx_dungeon_%' "
        "   OR name LIKE 'sqlite_autoindex_dungeon_%'"
    ).fetchall()
    assert objects, "schema introspection returned nothing — schema not created"
    for row in objects:
        assert not floor.search(row["name"]), f"floor in object name: {row['name']}"
        assert not floor.search(row["sql"] or ""), f"floor in DDL: {row['sql']}"


import tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from sidequest.dungeon.region_graph.depth import assign_depth_scores  # noqa: E402
from sidequest.dungeon.region_graph.generator import (  # noqa: E402
    attach_expansion,
    generate_expansion,
)
from sidequest.dungeon.region_graph.model import Expansion  # noqa: E402


def _seed_graph() -> RegionGraph:
    g = RegionGraph(entrance_id="entrance")
    g.add_node(RegionNode(id="entrance", expansion_id=0, theme="threshold"))
    return g


def _commit_seed(store: DungeonStore, g: RegionGraph) -> None:
    """Persist the entrance as its own expansion 0 (it belongs to no
    generated expansion's new_nodes). commit_expansion re-reads the
    live depth-scored node from g, so call after depth scores exist."""
    entrance = g.nodes[g.entrance_id]
    store.commit_expansion(Expansion(expansion_id=0, new_nodes=[entrance], new_edges=[]), g)


def _generate_and_attach(
    g: RegionGraph, *, campaign_seed: int, expansion_id: int, attach_ids: list[str]
) -> Expansion:
    exp, _ = generate_expansion(
        graph=g,
        campaign_seed=campaign_seed,
        expansion_id=expansion_id,
        attach_region_ids=attach_ids,
        theme_pool=["crypt", "catacomb", "flooded"],
    )
    attach_expansion(g, exp)
    assign_depth_scores(g, campaign_seed=campaign_seed)
    return exp


def _file_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_commit_then_load_map_roundtrips_graph_in_memory() -> None:
    g = _seed_graph()
    exp = _generate_and_attach(g, campaign_seed=7, expansion_id=1, attach_ids=["entrance"])

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    _commit_seed(store, g)
    store.commit_expansion(exp, g)
    conn.commit()

    reloaded = store.load_map(entrance_id="entrance")
    assert reloaded.nodes == g.nodes
    assert sorted(reloaded.edges, key=repr) == sorted(g.edges, key=repr)


def test_commit_then_load_map_roundtrips_over_wal_file() -> None:
    g = _seed_graph()
    exp = _generate_and_attach(g, campaign_seed=11, expansion_id=1, attach_ids=["entrance"])

    with tempfile.TemporaryDirectory() as d:
        db = str(_Path(d) / "save.db")
        c1 = _file_conn(db)
        s1 = DungeonStore(c1)
        s1.ensure_schema()
        _commit_seed(s1, g)
        s1.commit_expansion(exp, g)
        c1.commit()
        c1.close()

        c2 = _file_conn(db)
        reloaded = DungeonStore(c2).load_map(entrance_id="entrance")
        c2.close()

    assert reloaded.nodes == g.nodes
    assert sorted(reloaded.edges, key=repr) == sorted(g.edges, key=repr)


from sidequest.dungeon.persistence import FrontierEdge  # noqa: E402


def test_frontier_roundtrip() -> None:
    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()

    fe = FrontierEdge(
        frontier_edge_id="f1",
        from_region_id="exp001.r0",
        heading="down-and-east",
        spawn_depth_score=30.0,
    )
    store.put_frontier(fe)
    conn.commit()

    loaded = store.load_frontier()
    assert loaded == [fe]
    assert FrontierEdge.from_dict(fe.to_dict()) == fe


from sidequest.dungeon.persistence import DungeonMutation  # noqa: E402


def test_mutation_overlay_append_only_ordered_and_survives_reload() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = str(_Path(d) / "save.db")
        c1 = _file_conn(db)
        s1 = DungeonStore(c1)
        s1.ensure_schema()
        s1.record_mutation("exp001.r0", "trap_sprung", {"trap": "scything_blade"})
        s1.record_mutation("exp001.r0", "looted", {"item": "ring"})
        s1.record_mutation("exp001.r1", "collapsed", {})
        c1.commit()
        c1.close()

        c2 = _file_conn(db)
        muts = DungeonStore(c2).load_mutations()
        c2.close()

    # append-only + deterministic replay order (mutation_id ascending)
    assert [m.kind for m in muts] == ["trap_sprung", "looted", "collapsed"]
    assert muts[0].region_id == "exp001.r0"
    assert muts[0].payload == {"trap": "scything_blade"}
    assert DungeonMutation.from_dict(muts[1].to_dict()) == muts[1]


from sidequest.dungeon.persistence import (  # noqa: E402
    ComplicationThread,
    NotFoundError,
    PersistError,
)


def test_complication_ledger_open_resolve_and_accumulation() -> None:
    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()

    # expansion 1 lights two threads
    store.open_thread(
        ComplicationThread(
            thread_id="t1",
            origin_region_id="exp001.r0",
            kind="trope",
            status="open",
            started_at_depth_score=10.0,
            payload={"trope": "sacrifice_priest"},
        )
    )
    store.open_thread(
        ComplicationThread(
            thread_id="t2",
            origin_region_id="exp001.r1",
            kind="quest",
            status="open",
            started_at_depth_score=12.0,
            payload={"quest": "drowned_bell"},
        )
    )
    conn.commit()
    assert {t.thread_id for t in store.open_threads()} == {"t1", "t2"}

    # expansion 2 lights a third — accumulation observable (spec §7.1)
    store.open_thread(
        ComplicationThread(
            thread_id="t3",
            origin_region_id="exp002.r0",
            kind="trope",
            status="open",
            started_at_depth_score=30.0,
            payload={},
        )
    )
    conn.commit()
    assert len(store.open_threads()) == 3  # nothing cleared by pushing deeper

    # resolution is the ONLY thing that shrinks the ledger
    store.resolve_thread("t1")
    conn.commit()
    open_ids = {t.thread_id for t in store.open_threads()}
    assert open_ids == {"t2", "t3"}
    assert ComplicationThread.from_dict(store.get_thread("t1").to_dict()).status == "resolved"


def test_resolve_unknown_thread_fails_loud() -> None:
    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    import pytest

    with pytest.raises(NotFoundError):
        store.resolve_thread("does-not-exist")


def test_frozen_region_untouched_after_generator_version_bump() -> None:
    g = _seed_graph()
    exp = _generate_and_attach(g, campaign_seed=3, expansion_id=1, attach_ids=["entrance"])

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    _commit_seed(store, g)
    store.commit_expansion(exp, g, generator_version="plan5.v1")
    conn.commit()

    before = conn.execute(
        "SELECT region_id, payload, generator_version FROM dungeon_map ORDER BY region_id"
    ).fetchall()
    before_snap = [(r["region_id"], r["payload"], r["generator_version"]) for r in before]

    # generator version changes mid-campaign; re-committing the SAME
    # frozen expansion must fail loud, never silently rewrite.
    import pytest

    with pytest.raises(PersistError):
        store.commit_expansion(exp, g, generator_version="plan5.v2")
    conn.rollback()

    after = conn.execute(
        "SELECT region_id, payload, generator_version FROM dungeon_map ORDER BY region_id"
    ).fetchall()
    after_snap = [(r["region_id"], r["payload"], r["generator_version"]) for r in after]

    assert after_snap == before_snap  # bytes + version unchanged (frozen)


def test_dungeon_persist_spans_registered_and_routed() -> None:
    from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES
    from sidequest.telemetry.spans.dungeon_persist import (
        SPAN_DUNGEON_PERSIST_COMMIT,
        SPAN_LEDGER_ADD,
        SPAN_LEDGER_RESOLVE,
    )

    for name in (SPAN_DUNGEON_PERSIST_COMMIT, SPAN_LEDGER_ADD, SPAN_LEDGER_RESOLVE):
        assert name in SPAN_ROUTES or name in FLAT_ONLY_SPANS, (
            f"{name} has no routing decision — routing-completeness gate will fail"
        )


def test_commit_and_ledger_emit_spans() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    captured: list[str] = []

    class _Capture(ConsoleSpanExporter):
        def export(self, spans):  # type: ignore[override]
            captured.extend(s.name for s in spans)
            return super().export(spans)

    from tests.dungeon.conftest import (
        capture_otel_provider_state,
        reset_otel_provider,
        restore_otel_provider_state,
    )

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Capture()))
    # OTEL 1.x gates set_tracer_provider behind a once-only guard. In a
    # full-suite run an earlier conftest's init_tracer() already won that
    # guard, so this set_tracer_provider() would be a silent no-op and the
    # _Capture exporter never installs. HERMETIC isolation (Plan 6 Task 6):
    # capture the prior global provider + guard, reset so our install is
    # effective, then ALWAYS restore in finally so this test's _Capture
    # provider never leaks into later otel_capture-using tests. Test-infra
    # only — production init_tracer() is the single legitimate runtime
    # caller. See tests/dungeon/conftest.py for the full rationale.
    _otel_state = capture_otel_provider_state()
    try:
        reset_otel_provider()
        trace.set_tracer_provider(provider)

        g = _seed_graph()
        exp = _generate_and_attach(g, campaign_seed=5, expansion_id=1, attach_ids=["entrance"])
        conn = _mem_conn()
        store = DungeonStore(conn)
        store.ensure_schema()
        store.commit_expansion(exp, g)
        store.open_thread(
            ComplicationThread(
                thread_id="t1",
                origin_region_id="exp001.r0",
                kind="trope",
                status="open",
                started_at_depth_score=10.0,
                payload={},
            )
        )
        store.resolve_thread("t1")

        assert "dungeon.persist.commit" in captured
        assert "ledger.add" in captured
        assert "ledger.resolve" in captured
    finally:
        restore_otel_provider_state(_otel_state)


import pytest  # noqa: E402


@pytest.mark.parametrize("campaign_seed", [1, 2, 7, 13, 24301, 99991])
def test_serde_exact_inverse_over_seed_sweep(campaign_seed: int) -> None:
    """Generate → attach → depth-score a real 3-expansion chain, persist,
    reload, and assert the rebuilt graph is identical (spec §11)."""
    g = _seed_graph()
    exp1 = _generate_and_attach(
        g, campaign_seed=campaign_seed, expansion_id=1, attach_ids=["entrance"]
    )
    deep_ids = [n.id for n in exp1.new_nodes][:2] or ["entrance"]
    exp2 = _generate_and_attach(
        g,
        campaign_seed=campaign_seed,
        expansion_id=2,
        attach_ids=(deep_ids + ["entrance"])[:2],
    )
    deeper = [n.id for n in exp2.new_nodes][:2] or deep_ids
    exp3 = _generate_and_attach(
        g,
        campaign_seed=campaign_seed,
        expansion_id=3,
        attach_ids=(deeper + deep_ids)[:2],
    )

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    _commit_seed(store, g)
    for exp in (exp1, exp2, exp3):
        store.commit_expansion(exp, g)
    conn.commit()

    reloaded = store.load_map(entrance_id="entrance")
    assert reloaded.nodes == g.nodes
    assert sorted(reloaded.edges, key=repr) == sorted(g.edges, key=repr)
    # every node carries its frozen depth_score through the round-trip
    assert all(reloaded.nodes[rid].depth_score == g.nodes[rid].depth_score for rid in g.nodes)


def test_wiring_contract_runs_on_real_save_db_connection() -> None:
    """Plan-7 deferral contract: DungeonStore must work on a connection
    configured exactly like the real save DB. We reuse the production
    _configure_connection so a PRAGMA drift in game/persistence.py
    breaks THIS test — proving Plan 7 can pass it the live connection.
    """
    from sidequest.game.persistence import _configure_connection

    with tempfile.TemporaryDirectory() as d:
        db = str(_Path(d) / "caverns_beneath_sunden.db")
        conn = sqlite3.connect(db)
        _configure_connection(conn)  # the REAL save-DB PRAGMA contract

        g = _seed_graph()
        exp = _generate_and_attach(g, campaign_seed=42, expansion_id=1, attach_ids=["entrance"])
        store = DungeonStore(conn)
        store.ensure_schema()
        _commit_seed(store, g)
        store.commit_expansion(exp, g)
        conn.commit()

        reloaded = store.load_map(entrance_id="entrance")
        conn.close()

    assert reloaded.nodes == g.nodes
    assert sorted(reloaded.edges, key=repr) == sorted(g.edges, key=repr)


def test_dungeonstore_refuses_to_open_its_own_db() -> None:
    """Spec Decision 3: the store never owns the connection. A Path
    argument must fail loud, not silently open a private DB (that would
    break Plan 7's one-transaction guarantee, spec §7.5)."""
    import pytest

    with pytest.raises(PersistError):
        DungeonStore(_Path("/tmp/should-not-be-opened.db"))
