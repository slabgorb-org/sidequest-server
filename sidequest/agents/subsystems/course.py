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

Story 158-27 ([SWN-ORBITAL-COURSE-INERT], same finding): the router is an LLM, so
the dispatched ``destination`` carries the player's phrasing / the human label
("the Broken Drift") rather than the snake_case body id ("broken_drift").
``_resolve_body_id`` matches it tolerantly (exact id → normalized id → label) to a
canonical id before plotting, so a naturally-phrased burn lands a real course; a
destination matching neither any id nor any label still fails LOUD.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def _normalize_destination(value: str) -> str:
    """Fold a destination string to a comparable form.

    Lowercase, drop a leading article, de-underscore, and collapse whitespace —
    so the player's phrasing ("the Broken Drift"), the authored label
    ("BROKEN DRIFT"), and the body id ("broken_drift") all map onto one form
    ("broken drift").
    """
    text = value.strip().lower().replace("_", " ")
    if text.startswith("the "):
        text = text[len("the ") :]
    return " ".join(text.split())


def _resolve_body_id(
    destination: str, bodies: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """Resolve a player-named ``destination`` to a canonical body id.

    The IntentRouter is an LLM, so ``destination`` carries the human label or
    phrasing the player used ("the Broken Drift") — never the internal
    snake_case body id ("broken_drift"). Resolve tolerantly, most specific
    first: exact id, then a normalized id, then the body's label.

    Returns ``(canonical_id, resolved_via)`` — ``resolved_via`` is
    ``"exact_id"`` / ``"normalized_id"`` / ``"label"`` for the OTEL span — or
    ``(None, None)`` when nothing matches. A miss is NOT a silent fallback: the
    caller rejects it LOUD.
    """
    if destination in bodies:
        return destination, "exact_id"

    target = _normalize_destination(destination)
    if not target:
        return None, None

    for body_id, body in bodies.items():
        if _normalize_destination(body_id) == target:
            return body_id, "normalized_id"
        label = getattr(body, "label", None)
        if isinstance(label, str) and _normalize_destination(label) == target:
            return body_id, "label"
    return None, None


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

    # The router is an LLM: ``destination`` carries the human label/phrasing the
    # player used ("the Broken Drift"), not the snake_case body id. Resolve it
    # to a canonical id before plotting; an unresolved destination still fails
    # LOUD below (No Silent Fallbacks).
    resolved_id, resolved_via = _resolve_body_id(destination, bodies)
    if resolved_id is None:
        emit_course_plot_rejected(
            course_id=destination, reason="unknown_destination", available_ids=available_ids
        )
        return SubsystemOutput(data={"error": "unknown_destination"})

    dest_body = bodies[resolved_id]
    eta_hours, delta_v = compute_eta_and_dv(bodies[party_id], dest_body, orbits)

    # A quest-anchored body keeps its QUEST_OBJECTIVE provenance; otherwise the
    # player named an in-scope body directly. Checked on the resolved id —
    # quest_anchors hold body ids, not labels.
    source = (
        CourseSource.QUEST_OBJECTIVE
        if resolved_id in (snapshot.quest_anchors or [])
        else CourseSource.IN_SCOPE
    )
    course = PlottedCourse(
        to_body_id=resolved_id,
        label=dest_body.label,
        eta_hours=eta_hours,
        delta_v=delta_v,
        plotted_at_t_hours=snapshot.clock_t_hours,
        source=source,
    )
    snapshot.plotted_course = course
    emit_course_plot_accepted(from_body=party_id, course=course, resolved_via=resolved_via)

    # The burn consumes story-time: advance the clock by a TRAVEL beat whose
    # duration is the computed ETA. advance_clock_via_beat emits clock.advance.
    clock = Clock(t_hours=snapshot.clock_t_hours)
    advance_clock_via_beat(
        clock,
        StoryBeat(
            kind=StoryBeatKind.TRAVEL,
            trigger=f"course:{party_id}->{resolved_id}",
            duration_hours=eta_hours,
        ),
    )
    snapshot.clock_t_hours = clock.t_hours

    return SubsystemOutput(
        data={"to_body": resolved_id, "eta_hours": eta_hours, "delta_v": delta_v}
    )
