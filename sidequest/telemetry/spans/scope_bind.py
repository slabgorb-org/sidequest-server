"""orbital.scope_bind.* OTEL spans — region -> orbital-scope binding (Story 95-1).

Per CLAUDE.md OTEL principle: every region->orbital-scope bind decision MUST
emit a span so the GM panel can verify the chart actually re-centered on the
party's system — not just trust the narration. A no-match relocation emits a
loud-skip span (No Silent Fallbacks): the skip is a decision, observable, not
swallowed.

Pattern mirrors sidequest/telemetry/spans/course.py.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_SCOPE_BIND = "orbital.scope_bind"
SPAN_SCOPE_BIND_SKIPPED = "orbital.scope_bind_skipped"

FLAT_ONLY_SPANS.update(
    {
        SPAN_SCOPE_BIND,
        SPAN_SCOPE_BIND_SKIPPED,
    }
)


def emit_scope_bind(
    *,
    region_id: str,
    body_id: str,
    trigger: str,
) -> None:
    """Emit ``orbital.scope_bind`` when a region binds to its body.

    The lie-detector record that the chart re-centered: ``region_id`` joined
    body ``body_id`` and the scope now centers on it. ``trigger`` distinguishes
    bind-on-connect (``"init"``) from a mid-session relocation
    (``"relocation"``).
    """
    with Span.open(
        SPAN_SCOPE_BIND,
        attrs={
            "region_id": region_id,
            "body_id": body_id,
            "trigger": trigger,
        },
    ):
        pass


def emit_scope_bind_skipped(
    *,
    region_id: str,
    reason: str,
) -> None:
    """Emit ``orbital.scope_bind_skipped`` when a relocation has no matching body.

    A loud skip — the chart stays where it was, but the miss is observable so
    the GM panel can tell "the party moved to a region with no charted body"
    from "the bind silently failed".
    """
    with Span.open(
        SPAN_SCOPE_BIND_SKIPPED,
        attrs={
            "region_id": region_id,
            "reason": reason,
        },
    ):
        pass
