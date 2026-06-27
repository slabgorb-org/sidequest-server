"""Story 158-42 — region-anchor quest premature completion + quest mint/complete OTEL.

sq-playtest 2026-06-27 (beneath_sunden, Harpo): the Quests tab showed ONE quest,
"Below the Tended Rows — exp001.r0", already marked ``status=completed`` even though
its objective ("find the seed-gallery … and the way down past it") was plainly unmet —
Harpo had just *entered* exp001.r0 and killed one tender, never found a gallery or a
way down. Stored ground truth: ``quest_log['dungeon:exp1'].status == 'completed'`` with
the objective unmet.

Two coupled defects:

  1. **Premature completion.** A ``reach_deep`` expansion quest whose anchor region
     IS the region the PC first enters (a single-region expansion, or one where the
     entry region scored deepest) flips to ``completed`` on the SAME region transition
     that mints it. ``make_expansion_quest_observer`` mints via
     ``reconcile_dungeon_quests_into_log`` and then immediately resolves via
     ``resolve_expansion_quests(reached_region_ids={to_region})`` — and
     ``_beat_fired`` fires ``reach_deep`` the instant ``anchor_region in {to_region}``.
     "The way down past it" is unmet the moment you arrive.

  2. **Silent mint.** The projection that makes a quest player-visible
     (``reconcile_dungeon_quests_into_log``) emits NO watcher span. Seed
     (``dungeon.quest.bound``) and resolve (``dungeon.quest.resolved``) emit spans;
     the mint does not, so timeline rounds 1–5 carried no quest event and the GM panel
     could not see the quest appear (OTEL Observability Principle).

These tests drive the REAL production seam — ``seed_expansion_quest`` +
``make_expansion_quest_observer`` wired through ``notify_region_transition`` (the same
path the frontier hook fires in ``session_integration``) — so they survive refactors
and fail on real wiring breakage, not source shape.

RED until the quest engine (a) gates ``reach_deep`` so entering the anchor on the mint
transition does not complete it and (b) emits a mint watcher span.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import sidequest.telemetry.spans as _spans_module

# Contract this story introduces: the projection of an expansion quest into
# quest_log emits a dungeon.quest.minted span (mirrors dungeon.quest.bound /
# dungeon.quest.resolved so it routes to the GM panel the same way).
SPAN_QUEST_MINTED = "dungeon.quest.minted"


def _store() -> tuple[Any, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from sidequest.dungeon.persistence import DungeonStore

    s = DungeonStore(conn)
    s.ensure_schema()
    return conn, s


def _fresh_snapshot() -> Any:
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def _reach_deep_template() -> Any:
    """A reach_deep quest template carrying the real beneath_sunden objective."""
    from sidequest.dungeon.themes import ExpansionQuestTemplate

    return ExpansionQuestTemplate(
        signature="reach_deep",
        title="Below the Tended Rows",
        objective=(
            "find the seed-gallery where the first bloom was farmed, below the rows "
            "still worth working, and the way down past it."
        ),
    )


def _single_region_expansion(*, exp_id: int, depth: float) -> Any:
    """One region (r0) — necessarily the deepest, so the quest anchors to the
    very region the PC enters first. This is the minimal reproduction of the
    beneath_sunden bug (anchor == entry region)."""
    from sidequest.dungeon.region_graph import Expansion, RegionNode

    r0 = f"exp{exp_id:03d}.r0"
    return Expansion(
        expansion_id=exp_id,
        new_nodes=[RegionNode(id=r0, expansion_id=exp_id, theme="spore_dark", depth_score=depth)],
        new_edges=[],
    )


def _two_region_expansion(*, exp_id: int, shallow: float, deep: float) -> Any:
    """Entry region r0 (shallow) and a genuinely deeper anchor r1 — the quest
    anchors to r1 (the deepest), so entering r0 mints but must NOT complete, and
    descending to r1 is the legitimate completion."""
    from sidequest.dungeon.region_graph import Expansion, RegionEdge, RegionNode

    r0 = f"exp{exp_id:03d}.r0"
    r1 = f"exp{exp_id:03d}.r1"
    return Expansion(
        expansion_id=exp_id,
        new_nodes=[
            RegionNode(id=r0, expansion_id=exp_id, theme="spore_dark", depth_score=shallow),
            RegionNode(id=r1, expansion_id=exp_id, theme="spore_dark", depth_score=deep),
        ],
        new_edges=[RegionEdge(a=r0, b=r1, kind="corridor")],
    )


def _otel_in_memory() -> tuple[Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test-158-42")


# ---------------------------------------------------------------------------
# AC1 — entering the anchor region must NOT complete a reach_deep quest on entry
# ---------------------------------------------------------------------------


def test_entering_anchor_region_does_not_complete_reach_deep_quest_on_entry() -> None:
    """AC1 — the exact beneath_sunden repro.

    Seed a reach_deep quest whose anchor IS the region the PC first enters
    (single-region expansion). Driving the real observer through
    ``notify_region_transition`` for that single entry must leave the quest
    minted-and-active, NOT completed — its objective ('the way down past it')
    is unmet the instant the PC arrives.

    RED: current code resolves ``reach_deep`` when ``anchor_region in {to_region}``,
    so the mint transition immediately flips it to 'completed'.
    """
    from sidequest.dungeon.expansion_quest import (
        make_expansion_quest_observer,
        seed_expansion_quest,
    )
    from sidequest.dungeon.frontier_hook import (
        notify_region_transition,
        register_frontier_observer,
        unregister_frontier_observer,
    )

    conn, store = _store()
    exp = _single_region_expansion(exp_id=1, depth=10.0)
    seed_expansion_quest(
        campaign_seed=99,
        expansion=exp,
        manifests_by_region={},
        template=_reach_deep_template(),
        store=store,
        started_at_depth_score=10.0,
    )
    conn.commit()

    snap = _fresh_snapshot()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    try:
        notify_region_transition(snap, pc_name="Harpo", from_region=None, to_region="exp001.r0")
        conn.commit()
    finally:
        unregister_frontier_observer(observer)

    qid = "dungeon:exp1"
    assert qid in snap.quest_log, (
        f"expansion quest {qid!r} should be minted (player-visible) on entry, but it is "
        f"absent; quest_log keys: {list(snap.quest_log.keys())}"
    )
    assert snap.quest_log[qid].status != "completed", (
        "BUG (158-42): the reach_deep quest flipped to 'completed' the instant the PC "
        "entered its anchor region exp001.r0 — objective 'the way down past it' is "
        f"unmet. Got status={snap.quest_log[qid].status!r}."
    )
    assert snap.quest_log[qid].status == "active", (
        f"quest should remain 'active' after mere entry, got {snap.quest_log[qid].status!r}"
    )


# ---------------------------------------------------------------------------
# AC2 (mint) — the projection of a quest into quest_log emits a watcher span
# ---------------------------------------------------------------------------


def test_quest_mint_emits_watcher_span() -> None:
    """AC2 — minting an expansion quest into quest_log must emit a watcher span
    so the GM panel sees the quest appear (it currently emits nothing).

    Two-region expansion: the anchor is the deeper r1, so entering the entry
    region r0 mints the quest WITHOUT prematurely resolving it. We assert a
    ``dungeon.quest.minted`` span fired for the projection.

    RED: ``reconcile_dungeon_quests_into_log`` writes the QuestEntry silently —
    no span — so no ``dungeon.quest.minted`` span exists.
    """
    from sidequest.dungeon.expansion_quest import (
        make_expansion_quest_observer,
        seed_expansion_quest,
    )
    from sidequest.dungeon.frontier_hook import (
        notify_region_transition,
        register_frontier_observer,
        unregister_frontier_observer,
    )

    conn, store = _store()
    exp = _two_region_expansion(exp_id=2, shallow=10.0, deep=50.0)
    seed_expansion_quest(
        campaign_seed=7,
        expansion=exp,
        manifests_by_region={},
        template=_reach_deep_template(),
        store=store,
        started_at_depth_score=50.0,
    )
    conn.commit()

    exporter, real_tracer = _otel_in_memory()
    original_tracer = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[assignment]

    snap = _fresh_snapshot()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    try:
        notify_region_transition(snap, pc_name="Rux", from_region=None, to_region="exp002.r0")
        conn.commit()

        qid = "dungeon:exp2"
        assert qid in snap.quest_log and snap.quest_log[qid].status == "active", (
            f"quest should be minted 'active' on entering the entry region; "
            f"got {snap.quest_log.get(qid)!r}"
        )

        minted = [s for s in exporter.get_finished_spans() if s.name == SPAN_QUEST_MINTED]
        assert minted, (
            "BUG (158-42): minting the expansion quest into quest_log emitted NO "
            f"{SPAN_QUEST_MINTED!r} span. Quest mint is a silent state write — the GM "
            "panel cannot see the quest appear (OTEL Observability Principle). "
            f"spans seen: {sorted({s.name for s in exporter.get_finished_spans()})}"
        )
        assert minted[0].attributes is not None
        assert minted[0].attributes.get("expansion_id") == 2, (
            "mint span must carry the expansion_id so the GM panel can correlate it "
            f"to the quest; got {dict(minted[0].attributes)!r}"
        )
    finally:
        unregister_frontier_observer(observer)
        _spans_module.tracer = original_tracer  # type: ignore[assignment]


def test_quest_mint_span_fires_once_not_on_idempotent_reprojection() -> None:
    """AC2 — the mint span marks the moment the quest becomes visible, so it must
    fire on the FIRST projection only, not on every idempotent re-run of the
    observer over an already-active quest.

    RED: no mint span exists at all (so zero, not one).
    """
    from sidequest.dungeon.expansion_quest import (
        make_expansion_quest_observer,
        seed_expansion_quest,
    )
    from sidequest.dungeon.frontier_hook import (
        notify_region_transition,
        register_frontier_observer,
        unregister_frontier_observer,
    )

    conn, store = _store()
    exp = _two_region_expansion(exp_id=3, shallow=10.0, deep=50.0)
    seed_expansion_quest(
        campaign_seed=11,
        expansion=exp,
        manifests_by_region={},
        template=_reach_deep_template(),
        store=store,
        started_at_depth_score=50.0,
    )
    conn.commit()

    exporter, real_tracer = _otel_in_memory()
    original_tracer = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[assignment]

    snap = _fresh_snapshot()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    try:
        # First entry mints the quest; a within-expansion shuffle back to r0 must
        # not re-mint (the QuestEntry already exists and is active → projected 0).
        notify_region_transition(snap, pc_name="Rux", from_region=None, to_region="exp003.r0")
        conn.commit()
        notify_region_transition(
            snap, pc_name="Rux", from_region="exp003.r0", to_region="exp003.r0"
        )
        conn.commit()

        minted = [s for s in exporter.get_finished_spans() if s.name == SPAN_QUEST_MINTED]
        assert len(minted) == 1, (
            "expected exactly one dungeon.quest.minted span (fired on the first "
            f"projection only), got {len(minted)}"
        )
    finally:
        unregister_frontier_observer(observer)
        _spans_module.tracer = original_tracer  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# AC3 + AC2(completion) — legitimate descent still completes AND emits a span
# (wiring test: drives the full frontier-hook path end to end)
# ---------------------------------------------------------------------------


def test_reach_deep_completes_on_genuine_descent_and_emits_resolved_span() -> None:
    """AC3 regression + AC2 (completion) — a reach_deep quest still completes when
    the PC descends to the genuinely deeper anchor region, and completion emits a
    ``dungeon.quest.resolved`` span.

    Entering the entry region r0 mints the quest (active, not completed);
    descending to the deeper anchor r1 completes it. This is the legitimate path
    the fix must preserve, and it doubles as the wiring test exercising the real
    ``notify_region_transition`` → registered observer dispatch.
    """
    from sidequest.dungeon.expansion_quest import (
        make_expansion_quest_observer,
        seed_expansion_quest,
    )
    from sidequest.dungeon.frontier_hook import (
        notify_region_transition,
        register_frontier_observer,
        unregister_frontier_observer,
    )
    from sidequest.telemetry.spans.dungeon_quest import SPAN_QUEST_RESOLVED

    conn, store = _store()
    exp = _two_region_expansion(exp_id=4, shallow=10.0, deep=50.0)
    seed_expansion_quest(
        campaign_seed=7,
        expansion=exp,
        manifests_by_region={},
        template=_reach_deep_template(),
        store=store,
        started_at_depth_score=50.0,
    )
    conn.commit()

    exporter, real_tracer = _otel_in_memory()
    original_tracer = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[assignment]

    snap = _fresh_snapshot()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    try:
        qid = "dungeon:exp4"

        # Mint on entering the entry region — active, NOT completed.
        notify_region_transition(snap, pc_name="Rux", from_region=None, to_region="exp004.r0")
        conn.commit()
        assert snap.quest_log[qid].status == "active", (
            f"entering the entry region must not complete the quest; "
            f"got {snap.quest_log[qid].status!r}"
        )

        # Descend to the deeper anchor — legitimate completion.
        notify_region_transition(
            snap, pc_name="Rux", from_region="exp004.r0", to_region="exp004.r1"
        )
        conn.commit()
        assert snap.quest_log[qid].status == "completed", (
            "reach_deep quest should complete when the PC reaches the genuinely deeper "
            f"anchor region exp004.r1; got {snap.quest_log[qid].status!r}"
        )

        # Ledger thread closed.
        open_expansion_quests = [
            t
            for t in store.open_threads()
            if t.kind == "quest" and t.payload.get("scope") == "expansion"
        ]
        assert open_expansion_quests == [], (
            f"ledger thread still open after completion: "
            f"{[(t.thread_id, t.status) for t in open_expansion_quests]}"
        )

        # Completion emits a watcher span (AC2, completion side).
        resolved = [s for s in exporter.get_finished_spans() if s.name == SPAN_QUEST_RESOLVED]
        assert resolved, (
            "completion emitted no dungeon.quest.resolved span — the GM panel cannot "
            f"see the quest resolve. spans seen: "
            f"{sorted({s.name for s in exporter.get_finished_spans()})}"
        )
        assert resolved[0].attributes is not None
        assert resolved[0].attributes.get("expansion_id") == 4
    finally:
        unregister_frontier_observer(observer)
        _spans_module.tracer = original_tracer  # type: ignore[assignment]
