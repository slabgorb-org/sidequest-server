"""Story 77-3 RED — wire promoted quest_anchors through to the orbital course.

This is the cross-subsystem WIRING test (CLAUDE.md "Every Test Suite Needs a
Wiring Test"): it drives a narrator WorldStatePatch through the REAL
``GameSnapshot.apply_world_patch`` path and asserts the promoted anchors are
consumed by ``compute_courses`` as QUEST_OBJECTIVE rows — proving the patch ->
snapshot -> orbital-scheduler flow is connected end to end, not just that the
field exists.

Reconciled contract (see session 77-3, SM ruling):
- compute_courses keeps reading the snapshot's quest_anchors list; its
  signature is UNCHANGED (it does not take a WorldStatePatch).
- AC2 OTEL: compute_courses emits ``orbital.course.quest_anchors_consumed``
  with ``anchor_count=N`` whenever it reads quest_anchors. The span is emitted
  inside compute_courses (the function that reads the anchors — the subsystem
  decision point), so it fires regardless of caller.
"""

from __future__ import annotations

from sidequest.orbital.course import CourseSource, compute_courses
from sidequest.orbital.models import (
    BodyDef,
    BodyType,
    ClockConfig,
    OrbitsConfig,
    TravelConfig,
    TravelRealism,
)


def _mini_orbits() -> OrbitsConfig:
    """Coyote star + four habitats at 1/2/3/4 AU (mirrors test_course_compute)."""
    bodies = {
        "coyote": BodyDef(type=BodyType.STAR),
        "near": BodyDef(
            type=BodyType.HABITAT, parent="coyote", semi_major_au=1.0,
            period_days=365.0, epoch_phase_deg=0.0,
        ),
        "mid": BodyDef(
            type=BodyType.HABITAT, parent="coyote", semi_major_au=2.0,
            period_days=720.0, epoch_phase_deg=90.0,
        ),
        "far": BodyDef(
            type=BodyType.HABITAT, parent="coyote", semi_major_au=3.0,
            period_days=1100.0, epoch_phase_deg=180.0,
        ),
        "edge": BodyDef(
            type=BodyType.HABITAT, parent="coyote", semi_major_au=4.0,
            period_days=1500.0, epoch_phase_deg=270.0,
        ),
    }
    return OrbitsConfig(
        version="0.1.0",
        clock=ClockConfig(),
        travel=TravelConfig(realism=TravelRealism.ORBITAL, travel_speed_factor=1.0),
        bodies=bodies,
    )


def test_patch_anchors_flow_through_snapshot_to_course_as_quest_objective() -> None:
    """AC2/AC5 WIRING: a narrator patch's quest_anchors reach the orbital course.

    Drives the real apply_world_patch path, then feeds the snapshot's anchors to
    compute_courses exactly as production does (narration_apply.py / orchestrator.py),
    and asserts the anchored body is selected at QUEST_OBJECTIVE priority.
    """
    from sidequest.game.session import GameSnapshot, WorldStatePatch

    snap = GameSnapshot()
    snap.apply_world_patch(WorldStatePatch(quest_anchors=["far"]))

    rows = compute_courses(
        orbits=_mini_orbits(),
        party_at="near",
        in_scope_body_ids={"mid"},
        recent_body_mentions=["mid"],
        quest_anchors=list(snap.quest_anchors),
    )

    assert "far" in rows
    assert rows["far"].source == CourseSource.QUEST_OBJECTIVE


def test_compute_courses_emits_quest_anchors_consumed_span(otel_capture) -> None:
    """AC2/AC5 OTEL: compute_courses emits orbital.course.quest_anchors_consumed
    with anchor_count reflecting the anchors it read."""
    compute_courses(
        orbits=_mini_orbits(),
        party_at="near",
        in_scope_body_ids=set(),
        recent_body_mentions=[],
        quest_anchors=["mid", "far"],
    )

    consumed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "orbital.course.quest_anchors_consumed"
    ]
    assert consumed, "orbital.course.quest_anchors_consumed span did not fire"
    attrs = dict(consumed[-1].attributes or {})
    assert attrs.get("anchor_count") == 2
