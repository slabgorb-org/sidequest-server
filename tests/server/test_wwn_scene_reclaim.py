"""Scene-end WWN Effort reclaim hook (WWN magic engine, Plan 2 Task 8).

Wires the real scene-boundary reclaim (spec §H) into ``Session.end_scene``.
``Session`` is bound with its ruleset slug at connect time (``SessionRoom.bind_world``
threads ``pack.rules.ruleset`` through); ``end_scene`` gates on that slug so only
WWN sessions reclaim scene-committed Effort. Non-WWN sessions are untouched.

These tests drive the REAL ``Session.end_scene`` (no reimplementation, no
source-text grep) against a fixture-built snapshot carrying a PC core with a
seeded ``EffortPool``.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import EffortCommitment, EffortPool
from sidequest.server.session import Session


def _pc_with_effort() -> Character:
    """A PC whose High Mage Effort pool has one scene + one day commitment."""
    core = CreatureCore(
        name="Aria",
        description="An elementalist",
        personality="focused",
        inventory=Inventory(),
    )
    core.effort["high_mage"] = EffortPool(
        source="high_mage",
        max=3,
        commitments=[
            EffortCommitment(points=1, duration="scene", label="Burning Hands"),
            EffortCommitment(points=1, duration="day", label="Bound Familiar"),
        ],
    )
    return Character(
        core=core,
        char_class="High Mage",
        race="Human",
        backstory="A wandering elementalist.",
    )


def test_end_scene_reclaims_scene_effort_for_wwn_session():
    """A wwn-bound session reclaims the scene commitment, leaves day intact."""
    pc = _pc_with_effort()
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
        characters=[pc],
    )
    session = Session(snap, ruleset="wwn")

    pool = pc.core.effort["high_mage"]
    assert pool.available == 1  # 3 max - 2 committed (scene + day)

    session.end_scene("scene_end", turn=1)

    pool = pc.core.effort["high_mage"]
    # Scene commitment reclaimed: available restored from 1 -> 2.
    assert pool.available == 2
    durations = [c.duration for c in pool.commitments]
    assert "scene" not in durations
    # Day commitment is intact (only a night's rest reclaims it — Plan 3).
    assert "day" in durations
    assert len(pool.commitments) == 1


def test_end_scene_emits_reclaim_span_for_wwn_session(otel_capture):
    """The reclaim is observable on the GM panel (wwn.effort.reclaim span)."""
    pc = _pc_with_effort()
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
        characters=[pc],
    )
    session = Session(snap, ruleset="wwn")

    session.end_scene("scene_end", turn=1)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.effort.reclaim"]
    assert len(spans) == 1
    assert spans[0].attributes["actor"] == "Aria"
    assert spans[0].attributes["source"] == "high_mage"
    assert spans[0].attributes["trigger"] == "scene"


def test_end_scene_leaves_effort_untouched_for_non_wwn_session():
    """A dial/non-wwn session does NOT reclaim Effort — the gate is strict."""
    pc = _pc_with_effort()
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="test_world",
        turn_manager=TurnManager(),
        characters=[pc],
    )
    session = Session(snap, ruleset="dial")

    session.end_scene("scene_end", turn=1)

    pool = pc.core.effort["high_mage"]
    # Untouched: both commitments remain, available unchanged.
    assert pool.available == 1
    durations = sorted(c.duration for c in pool.commitments)
    assert durations == ["day", "scene"]


def test_end_scene_leaves_effort_untouched_when_ruleset_unset():
    """Default-constructed Session (ruleset=None) doesn't reclaim or crash."""
    pc = _pc_with_effort()
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="test_world",
        turn_manager=TurnManager(),
        characters=[pc],
    )
    session = Session(snap)  # ruleset defaults to None

    session.end_scene("scene_end", turn=1)

    pool = pc.core.effort["high_mage"]
    assert pool.available == 1
    assert len(pool.commitments) == 2
