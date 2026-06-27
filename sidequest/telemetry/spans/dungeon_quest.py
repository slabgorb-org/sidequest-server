"""dungeon.quest.* spans — per-expansion quest bind + resolve (ADR-137 × ADR-106).

Two spans:
- ``dungeon.quest.bound`` — emitted when a quest is bound to an expansion
  (the per-expansion quest has a signature_kind assigned and a ref_id resolved).
- ``dungeon.quest.resolved`` — emitted when the bound quest resolves via a
  game event (win condition, encounter outcome, etc.).

Both register into SPAN_ROUTES so the GM panel can verify the quest engine
engaged rather than the narrator improvising quest outcomes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_QUEST_BOUND = "dungeon.quest.bound"
SPAN_QUEST_MINTED = "dungeon.quest.minted"
SPAN_QUEST_RESOLVED = "dungeon.quest.resolved"


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_QUEST_MINTED] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    extract=lambda s: {
        "field": "quest_log",
        "op": "quest_minted",
        "expansion_id": _attr("expansion_id")(s),
        "quest_id": _attr("quest_id")(s),
        "signature_kind": _attr("signature_kind")(s),
    },
)

SPAN_ROUTES[SPAN_QUEST_BOUND] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    extract=lambda s: {
        "field": "complication_ledger",
        "op": "quest_bound",
        "expansion_id": _attr("expansion_id")(s),
        "signature_kind": _attr("signature_kind")(s),
        "ref_id": _attr("ref_id")(s),
        "degraded": _attr("degraded")(s),
        "theme": _attr("theme")(s),
    },
)

SPAN_ROUTES[SPAN_QUEST_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    extract=lambda s: {
        "field": "complication_ledger",
        "op": "quest_resolved",
        "expansion_id": _attr("expansion_id")(s),
        "signature_kind": _attr("signature_kind")(s),
        "resolving_event": _attr("resolving_event")(s),
        "ref_id": _attr("ref_id")(s),
    },
)


@contextmanager
def quest_bound_span(
    *,
    expansion_id: int,
    signature_kind: str,
    ref_id: str,
    degraded: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open a dungeon.quest.bound span.

    Emitted when a per-expansion quest is bound — the signature_kind has been
    selected and the ref_id resolved to a real quest definition. ``degraded``
    is True when the quest fell back to a generic signature_kind because the
    preferred kind had no eligible definition in the pack.
    """
    with Span.open(
        SPAN_QUEST_BOUND,
        {
            "expansion_id": expansion_id,
            "signature_kind": signature_kind,
            "ref_id": ref_id,
            "degraded": degraded,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def quest_minted_span(
    *,
    expansion_id: int,
    quest_id: str,
    signature_kind: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open a dungeon.quest.minted span.

    Emitted when an expansion-quest thread is first *projected* into
    ``snapshot.quest_log`` — the moment the quest becomes player-visible in the
    Quests tab. ``dungeon.quest.bound`` fires at seed time (when the signature
    beat is chosen) and ``dungeon.quest.resolved`` at completion; this span
    closes the gap between them so the GM panel sees the quest appear rather
    than discovering it as a silent state write.
    """
    with Span.open(
        SPAN_QUEST_MINTED,
        {
            "expansion_id": expansion_id,
            "quest_id": quest_id,
            "signature_kind": signature_kind,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def quest_resolved_span(
    *,
    expansion_id: int,
    signature_kind: str,
    resolving_event: str,
    ref_id: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open a dungeon.quest.resolved span.

    Emitted when the bound quest for an expansion resolves via a game event.
    ``resolving_event`` names the win-condition or encounter outcome that
    triggered the resolution (e.g. ``"hp_depletion"``, ``"scenario_clue"``).
    ``ref_id`` is the bound element id that closed the quest — for a big_bad
    signature it is the defeated antagonist's name, so the GM panel can verify
    WHICH antagonist closed the quest, not merely that one did.
    """
    with Span.open(
        SPAN_QUEST_RESOLVED,
        {
            "expansion_id": expansion_id,
            "signature_kind": signature_kind,
            "resolving_event": resolving_event,
            "ref_id": ref_id,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


__all__ = [
    "SPAN_QUEST_BOUND",
    "SPAN_QUEST_MINTED",
    "SPAN_QUEST_RESOLVED",
    "quest_bound_span",
    "quest_minted_span",
    "quest_resolved_span",
]
