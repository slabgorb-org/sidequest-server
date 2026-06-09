"""orbital.system_resolve OTEL span — per-region system-file resolution (Story 98-2).

Per CLAUDE.md OTEL principle: every per-region system-file resolution MUST emit
a span so the GM panel can verify the loader actually picked the right
``systems/<region_id>.yaml`` for the party's system — not just trust that a
chart appeared. A miss (a region with no authored system file) emits the span
with ``hit=False`` BEFORE the loader fails loud (No Silent Fallbacks): the miss
is a decision, observable, not swallowed.

Pattern mirrors sidequest/telemetry/spans/scope_bind.py.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_SYSTEM_RESOLVE = "orbital.system_resolve"

FLAT_ONLY_SPANS.update(
    {
        SPAN_SYSTEM_RESOLVE,
    }
)


def emit_system_resolve(
    *,
    region_id: str,
    system_file: str,
    hit: bool,
) -> None:
    """Emit ``orbital.system_resolve`` when the loader resolves a per-region file.

    The lie-detector record that per-region resolution fired: ``region_id`` was
    resolved to ``system_file`` and the file was present (``hit=True``) or absent
    (``hit=False``). On a miss this span fires immediately before the loader
    raises, so the GM panel can tell "drilled into an unauthored system" from a
    silently empty chart.
    """
    with Span.open(
        SPAN_SYSTEM_RESOLVE,
        attrs={
            "region_id": region_id,
            "system_file": system_file,
            "hit": hit,
        },
    ):
        pass


__all__ = ["SPAN_SYSTEM_RESOLVE", "emit_system_resolve"]
