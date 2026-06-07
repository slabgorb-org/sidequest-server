"""Story 77-3 RED — wire promoted quest_anchors through to the orbital course.

Cross-subsystem WIRING tests (CLAUDE.md "Every Test Suite Needs a Wiring Test").

Reconciled FINAL contract (SM/Neo ruling, session 77-3):
- ``compute_courses`` keeps reading the snapshot's quest_anchors list; its
  signature is UNCHANGED (it does not take a WorldStatePatch).
- ``_apply_world_patch_inner`` merges ``patch.quest_anchors`` into
  ``snapshot.quest_anchors`` as an order-preserving dedup UNION (not replace).
- AC5 OTEL: ``orbital.course.quest_anchors_consumed`` (anchor_count) must fire
  at BOTH compute_courses call sites — orchestrator.py:2415 (already has
  ``emit_course_compute``) AND narration_apply.py:1972 (currently has NO
  course-compute span). Preferred impl: extend ``emit_course_compute`` and call
  it from the narration_apply path. The second test below drives the REAL
  narration_apply path (``_apply_course_sidecar``) and asserts the span fires
  there — proving the currently-blind read is no longer blind.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.orbital.course import CourseSource, compute_courses
from sidequest.orbital.models import (
    BodyDef,
    BodyType,
    ClockConfig,
    OrbitsConfig,
    TravelConfig,
    TravelRealism,
)

_WORLD_MINIMAL = Path(__file__).resolve().parent / "fixtures" / "world_minimal"


def _mini_orbits() -> OrbitsConfig:
    """Coyote star + four habitats at 1/2/3/4 AU (mirrors test_course_compute)."""
    bodies = {
        "coyote": BodyDef(type=BodyType.STAR),
        "near": BodyDef(
            type=BodyType.HABITAT,
            parent="coyote",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0.0,
        ),
        "mid": BodyDef(
            type=BodyType.HABITAT,
            parent="coyote",
            semi_major_au=2.0,
            period_days=720.0,
            epoch_phase_deg=90.0,
        ),
        "far": BodyDef(
            type=BodyType.HABITAT,
            parent="coyote",
            semi_major_au=3.0,
            period_days=1100.0,
            epoch_phase_deg=180.0,
        ),
        "edge": BodyDef(
            type=BodyType.HABITAT,
            parent="coyote",
            semi_major_au=4.0,
            period_days=1500.0,
            epoch_phase_deg=270.0,
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
    compute_courses exactly as production does (narration_apply / orchestrator),
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


def test_narration_apply_course_path_emits_quest_anchors_consumed_span(otel_capture) -> None:
    """AC5 WIRING (SM final spec): the narration_apply course path
    (``_apply_course_sidecar``, narration_apply.py:1972) must emit
    ``orbital.course.quest_anchors_consumed`` with anchor_count — closing the
    blind-spot where this read currently has no course-compute span at all.

    Drives the REAL path: a plot_course narration result over a snapshot that
    carries a quest anchor, against the world_minimal orbital fixture.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.orbital.loader import load_orbital_content
    from sidequest.server.narration_apply import _apply_course_sidecar
    from sidequest.server.session import Session
    from sidequest.server.session_room import SessionRoom

    content = load_orbital_content(_WORLD_MINIMAL)
    snap = GameSnapshot(party_body_id="turning_hub", quest_anchors=["red_prospect"])

    room = SessionRoom(slug="orbital-77-3", mode=GameMode.SOLO)
    # Attach a session carrying the orbital content directly — bind_world would
    # require resolving a world dir from genre/world slugs (heavy); the apply
    # path only reads room.session.orbital_content / orbital_scope / mentions.
    room._session = Session(snap, orbital_content=content)

    result = NarrationTurnResult(
        narration="You chart a course toward the Red Prospect.",
        game_patch_dict={"intent": "plot_course", "course_id": "red_prospect"},
    )

    _apply_course_sidecar(snapshot=snap, result=result, room=room)

    consumed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "orbital.course.quest_anchors_consumed"
    ]
    assert consumed, (
        "orbital.course.quest_anchors_consumed span did not fire from the "
        "narration_apply course path"
    )
    attrs = dict(consumed[-1].attributes or {})
    assert attrs.get("anchor_count") == 1
