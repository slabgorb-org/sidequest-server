"""Intent dispatch for orbital chart messages.

Per spec §6.3: each intent → render new SVG → return OrbitalIntentResponse.
The Session holds the current scope so drill_out can return to its parent.

Pure function — does not touch the WebSocket transport. The handler
module under ``sidequest/handlers/`` (Task 15b) wires this into the
inbound message router.
"""

from __future__ import annotations

from sidequest.orbital.conjunction import next_conjunction
from sidequest.orbital.course_render import _resolve_drop_reason, render_course_overlay
from sidequest.orbital.render import Scope, render_chart
from sidequest.protocol.orbital_intent import (
    ConjunctionEventPayload,
    DrillInIntent,
    DrillOutIntent,
    OrbitalIntent,
    OrbitalIntentResponse,
    ViewMapIntent,
)
from sidequest.server.session import Session
from sidequest.telemetry.spans.course import emit_course_render_overlay


class OrbitalContentUnavailableError(RuntimeError):
    """Intent received for a session whose world has no orbital tier."""


def handle_orbital_intent(session: Session, intent: OrbitalIntent) -> OrbitalIntentResponse:
    """Resolve an orbital intent against the session's content + state.

    Side effect: updates ``session.orbital_scope`` so a subsequent
    drill_out resolves against the new center. Renders a fresh SVG and
    emits the ``chart.render`` OTEL span via ``render_chart``.
    """
    content = session.orbital_content
    if content is None:
        raise OrbitalContentUnavailableError(
            "session has no orbital content; world is not orbital-tier"
        )

    inner = intent.root
    if isinstance(inner, ViewMapIntent):
        scope = (
            Scope.system_root()
            if inner.scope == "system_root"
            else Scope(center_body_id=inner.scope)
        )
    elif isinstance(inner, DrillInIntent):
        scope = Scope(center_body_id=inner.body_id)
    elif isinstance(inner, DrillOutIntent):
        current = session.orbital_scope
        if current.center_body_id == "<root>":
            scope = Scope.system_root()
        else:
            body = content.orbits.bodies[current.center_body_id]
            scope = Scope(center_body_id=body.parent) if body.parent else Scope.system_root()
    else:  # pragma: no cover — exhaustive
        raise TypeError(f"Unknown orbital intent: {inner!r}")

    svg = render_chart(
        orbits=content.orbits,
        chart=content.chart,
        scope=scope,
        t_hours=session.clock.t_hours,
        party_at=session.party_body_id,
    )

    plotted_course = session._snapshot.plotted_course
    if plotted_course is not None:
        drop_reason = _resolve_drop_reason(
            course=plotted_course,
            orbits=content.orbits,
            party_body_id=session.party_body_id,
        )
        emit_course_render_overlay(
            to_body=plotted_course.to_body_id,
            bezier_control_offset_au=0.3,
            drop_reason=drop_reason,
        )
        svg = render_course_overlay(
            chart_svg=svg,
            course=plotted_course,
            orbits=content.orbits,
            party_body_id=session.party_body_id,
            t_hours=session.clock.t_hours,
        )

    session.orbital_scope = scope

    actual_center = (
        scope.center_body_id if scope.center_body_id != "<root>" else _system_primary_id(content)
    )

    event = next_conjunction(content.orbits, session.clock.t_hours)
    next_conj_payload: ConjunctionEventPayload | None
    next_conj_payload = (
        ConjunctionEventPayload(
            body_a_id=event.body_a_id,
            body_b_id=event.body_b_id,
            label=event.label,
            t_hours_event=event.t_hours_event,
            t_hours_until=event.t_hours_until,
        )
        if event is not None
        else None
    )

    return OrbitalIntentResponse(
        scope_center=actual_center,
        svg=svg,
        t_hours=session.clock.t_hours,
        epoch_days=content.orbits.clock.epoch_days,
        party_at=session.party_body_id,
        next_conjunction=next_conj_payload,
    )


def _system_primary_id(content) -> str:
    return next(bid for bid, b in content.orbits.bodies.items() if b.parent is None)
