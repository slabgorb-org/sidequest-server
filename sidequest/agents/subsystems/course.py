"""course — plot-a-course / story-clock dispatch (ADR-130 → ADR-113 spine).

Story 153-5 ([SWN-ORBITAL-COURSE-INERT]). ADR-130's orbital course + story-time
clock already exist as pure resolvers (``orbital/course.py``,
``orbital/beats.py``, ``orbital/clock.py``) but, before this, the only way to
engage them in play was the narrator's *post-hoc* ``plot_course`` game_patch
sidecar — so a player who said "we burn for the Red Prospect" relied on the
narrator improvising the burn, and the story clock went stale. This subsystem
wires the course/clock engine onto the pre-narrator IntentRouter dispatch bank:
when the router classifies a travel intent at high confidence, BOTH halves of
ADR-130 engage in a single dispatch (the SOUL "Cut the Dull Bits" fast-travel):

  * the **course model** — ``compute_eta_and_dv`` computes the cost and we commit
    a ``PlottedCourse`` to the snapshot (``course.plot`` OTEL span); and
  * the **clock model** — the travel consumes story-time via a TRAVEL
    ``StoryBeat`` of ``duration_hours == eta_hours`` (``clock.advance`` span).

Reuses the existing pure resolvers — no new course math (Don't Reinvent). An
unresolvable destination (no orbital tier, no party anchor, or a body the world
does not have) fails LOUD via a ``course.plot.rejected`` span; it never plots a
phantom course or silently no-ops (No Silent Fallbacks).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.orbital.beats import StoryBeat, StoryBeatKind, advance_clock_via_beat
from sidequest.orbital.clock import Clock
from sidequest.orbital.course import CourseSource, PlottedCourse, compute_eta_and_dv
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.telemetry.spans.course import (
    emit_course_plot_accepted,
    emit_course_plot_rejected,
)

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.orbital.loader import OrbitalContent


async def run_course_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    orbital_content: OrbitalContent | None = None,
) -> SubsystemOutput:
    """Plot a course to the dispatched destination and advance the story clock.

    Resolves ``params["destination"]`` against the world's ``orbits.bodies``,
    commits a ``PlottedCourse`` (``course.plot`` span), and advances the
    story-time clock by a TRAVEL beat of ``duration_hours == eta_hours``
    (``clock.advance`` span). Every failure path emits a ``course.plot.rejected``
    span and returns an error-coded output — never a silent no-op.
    """
    destination = dispatch.params.get("destination")
    if not isinstance(destination, str) or not destination:
        emit_course_plot_rejected(
            course_id=str(destination), reason="malformed_destination", available_ids=[]
        )
        return SubsystemOutput(data={"error": "malformed_destination"})

    if orbital_content is None:
        # The world has no orbital tier (or it was not threaded into the bank
        # context) — there is nothing to plot a course against.
        emit_course_plot_rejected(course_id=destination, reason="no_orbital_tier", available_ids=[])
        return SubsystemOutput(data={"error": "no_orbital_tier"})

    orbits = orbital_content.orbits
    bodies = orbits.bodies
    available_ids = sorted(bodies)

    party_id = snapshot.party_body_id
    if party_id is None or party_id not in bodies:
        emit_course_plot_rejected(
            course_id=destination, reason="no_party_anchor", available_ids=available_ids
        )
        return SubsystemOutput(data={"error": "no_party_anchor"})

    dest_body = bodies.get(destination)
    if dest_body is None:
        emit_course_plot_rejected(
            course_id=destination, reason="unknown_destination", available_ids=available_ids
        )
        return SubsystemOutput(data={"error": "unknown_destination"})

    eta_hours, delta_v = compute_eta_and_dv(bodies[party_id], dest_body, orbits)

    # A quest-anchored body keeps its QUEST_OBJECTIVE provenance; otherwise the
    # player named an in-scope body directly.
    source = (
        CourseSource.QUEST_OBJECTIVE
        if destination in (snapshot.quest_anchors or [])
        else CourseSource.IN_SCOPE
    )
    course = PlottedCourse(
        to_body_id=destination,
        label=dest_body.label,
        eta_hours=eta_hours,
        delta_v=delta_v,
        plotted_at_t_hours=snapshot.clock_t_hours,
        source=source,
    )
    snapshot.plotted_course = course
    emit_course_plot_accepted(from_body=party_id, course=course)

    # The burn consumes story-time: advance the clock by a TRAVEL beat whose
    # duration is the computed ETA. advance_clock_via_beat emits clock.advance.
    clock = Clock(t_hours=snapshot.clock_t_hours)
    advance_clock_via_beat(
        clock,
        StoryBeat(
            kind=StoryBeatKind.TRAVEL,
            trigger=f"course:{party_id}->{destination}",
            duration_hours=eta_hours,
        ),
    )
    snapshot.clock_t_hours = clock.t_hours

    return SubsystemOutput(
        data={"to_body": destination, "eta_hours": eta_hours, "delta_v": delta_v}
    )
