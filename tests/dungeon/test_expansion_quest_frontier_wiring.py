"""TDD Task 8: wire expansion-quest projection+resolution into the frontier seam.

The observer returned by make_expansion_quest_observer(store) must be registered
via register_frontier_observer so that notify_region_transition:
  (1) projects open expansion-quest threads for the reached expansion into
      snapshot.quest_log (status="active"), and
  (2) resolves any whose reach_deep beat fires on the subsequent transition.

Imports are at the top; no mid-file noqa: E402.
"""

import sqlite3

import pytest

from sidequest.dungeon import frontier_hook
from sidequest.dungeon.expansion_quest import make_expansion_quest_observer
from sidequest.dungeon.frontier_hook import (
    notify_region_transition,
    register_frontier_observer,
    unregister_frontier_observer,
)
from sidequest.dungeon.persistence import ComplicationThread, DungeonStore
from sidequest.game.session import GameSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store() -> tuple[sqlite3.Connection, DungeonStore]:
    """In-memory SQLite DungeonStore."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    s = DungeonStore(conn)
    s.ensure_schema()
    return conn, s


def _snap() -> GameSnapshot:
    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def _reach_deep_thread(exp_id: int, anchor_region: str) -> ComplicationThread:
    """An open expansion-quest thread with signature_kind=reach_deep."""
    return ComplicationThread(
        thread_id=f"q.exp{exp_id}.wiring",
        origin_region_id=anchor_region,
        kind="quest",
        status="open",
        started_at_depth_score=10.0,
        payload={
            "scope": "expansion",
            "expansion_id": exp_id,
            "signature_kind": "reach_deep",
            "ref_id": anchor_region,
            "anchor_region": anchor_region,
            "title": "Go deep",
            "objective": "Reach the anchor.",
        },
    )


# ---------------------------------------------------------------------------
# Main wiring test
# ---------------------------------------------------------------------------


def test_transition_projects_then_resolves() -> None:
    """Enter exp001.r0 → quest projected active; enter exp001.r1 → quest resolved.

    RED: observer is not yet registered; quest never appears.
    GREEN: make_expansion_quest_observer + register_frontier_observer wires it.
    """
    conn, store = _store()
    # Seed an expansion-quest thread whose anchor (reach_deep) is exp001.r1.
    store.open_thread(_reach_deep_thread(1, "exp001.r1"))
    conn.commit()

    snap = _snap()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    try:
        # Transition into exp001.r0 — enters expansion 1; quest projected active.
        notify_region_transition(snap, pc_name="Chico", from_region=None, to_region="exp001.r0")
        assert "dungeon:exp1" in snap.quest_log, (
            "quest not projected after entering expansion 1 region exp001.r0"
        )
        assert snap.quest_log["dungeon:exp1"].status == "active"

        # Transition to the anchor region — reach_deep beat fires; quest resolved.
        notify_region_transition(
            snap, pc_name="Chico", from_region="exp001.r0", to_region="exp001.r1"
        )
        assert snap.quest_log["dungeon:exp1"].status == "completed", (
            "quest not resolved after reaching anchor region exp001.r1"
        )
    finally:
        unregister_frontier_observer(observer)


# ---------------------------------------------------------------------------
# Observer does nothing for non-expansion regions
# ---------------------------------------------------------------------------


def test_no_projection_for_entrance_region() -> None:
    """Transitioning to 'entrance' (no expansion id) leaves quest_log empty."""
    conn, store = _store()
    store.open_thread(_reach_deep_thread(1, "exp001.r1"))
    conn.commit()

    snap = _snap()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    try:
        notify_region_transition(
            snap, pc_name="Chico", from_region=None, to_region="entrance"
        )
        assert "dungeon:exp1" not in snap.quest_log
    finally:
        unregister_frontier_observer(observer)


# ---------------------------------------------------------------------------
# Observer lifecycle: unregister on teardown
# ---------------------------------------------------------------------------


def test_observer_unregistered_after_test_does_not_leak() -> None:
    """Confirm that after unregister the observer no longer fires."""
    conn, store = _store()
    store.open_thread(_reach_deep_thread(1, "exp001.r1"))
    conn.commit()

    snap = _snap()
    before_count = frontier_hook.registered_observer_count()
    observer = make_expansion_quest_observer(store)
    register_frontier_observer(observer)
    assert frontier_hook.registered_observer_count() == before_count + 1
    unregister_frontier_observer(observer)
    assert frontier_hook.registered_observer_count() == before_count

    # After unregister the transition must NOT project anything.
    notify_region_transition(snap, pc_name="Chico", from_region=None, to_region="exp001.r0")
    assert "dungeon:exp1" not in snap.quest_log
