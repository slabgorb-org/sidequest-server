"""OTEL spans for the course-plotting subsystem.

Pattern mirrors sidequest/telemetry/spans/chart.py and interior.py.
Per CLAUDE.md OTEL principle: every backend subsystem MUST emit
spans so the GM dashboard can verify the lie-detector pattern
(prose vs map disagreement is invisible without telemetry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ._core import FLAT_ONLY_SPANS
from .span import Span

if TYPE_CHECKING:
    from sidequest.orbital.course import PlottedCourse


CourseRenderDropReason = Literal[
    "none_course",
    "unknown_party",
    "unknown_destination",
    "root_party",
]

SPAN_COURSE_COMPUTE = "course.compute"
SPAN_COURSE_PLOT = "course.plot"
SPAN_COURSE_PLOT_REJECTED = "course.plot.rejected"
SPAN_COURSE_CANCEL = "course.cancel"
SPAN_COURSE_RENDER_OVERLAY = "course.render_overlay"

FLAT_ONLY_SPANS.update(
    {
        SPAN_COURSE_COMPUTE,
        SPAN_COURSE_PLOT,
        SPAN_COURSE_PLOT_REJECTED,
        SPAN_COURSE_CANCEL,
        SPAN_COURSE_RENDER_OVERLAY,
    }
)


def emit_course_compute(
    *,
    course_count: int,
    in_scope: int,
    recent: int,
    quest: int,
    dropped_by_cap: int,
) -> None:
    """Emit a ``course.compute`` span. Fired every prompt assembly that
    includes the <courses> block."""
    with Span.open(
        SPAN_COURSE_COMPUTE,
        attrs={
            "course_count": int(course_count),
            "in_scope_count": int(in_scope),
            "recent_count": int(recent),
            "quest_count": int(quest),
            "dropped_by_cap": int(dropped_by_cap),
        },
    ):
        pass


def emit_course_plot_accepted(
    *,
    from_body: str | None,
    course: PlottedCourse | None,
) -> None:
    """Emit a ``course.plot`` span when a plot_course state patch is accepted."""
    attrs: dict[str, object] = {"from_body": from_body or ""}
    if course is not None:
        attrs["to_body"] = course.to_body_id
        attrs["eta_hours"] = float(course.eta_hours)
        attrs["delta_v"] = float(course.delta_v)
        attrs["source"] = str(course.source.value)
    with Span.open(SPAN_COURSE_PLOT, attrs=attrs):
        pass


def emit_course_plot_rejected(
    *,
    course_id: str,
    reason: str,
    available_ids: list[str],
) -> None:
    """Emit a ``course.plot.rejected`` span when a plot_course patch is rejected."""
    with Span.open(
        SPAN_COURSE_PLOT_REJECTED,
        attrs={
            "course_id": course_id,
            "reason": reason,
            "available_ids": ",".join(available_ids),
        },
    ):
        pass


def emit_course_cancel(
    *,
    was_already_clear: bool,
) -> None:
    """Emit a ``course.cancel`` span when cancel_course is applied."""
    with Span.open(
        SPAN_COURSE_CANCEL,
        attrs={
            "was_already_clear": bool(was_already_clear),
        },
    ):
        pass


def emit_course_render_overlay(
    *,
    to_body: str,
    bezier_control_offset_au: float,
    drop_reason: CourseRenderDropReason | None = None,
) -> None:
    """Emit a ``course.render_overlay`` span on every chart re-render.

    Fires whether the overlay was drawn or dropped. ``drop_reason=None``
    indicates a successful render. Any non-None value means the overlay
    was skipped; the GM dashboard splits on the string so the four
    failure modes graph independently.
    """
    attrs: dict[str, object] = {
        "to_body": to_body,
        "bezier_control_offset_au": float(bezier_control_offset_au),
        "dropped": drop_reason is not None,
    }
    if drop_reason is not None:
        attrs["drop_reason"] = drop_reason
    with Span.open(SPAN_COURSE_RENDER_OVERLAY, attrs=attrs):
        pass
