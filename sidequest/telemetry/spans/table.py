"""Free-for-all N-seat table resolution spans.

Eight spans cover the lifecycle: dealt, commit, npc_commit, cheat, read,
accuse, fold, showdown. Each subsystem decision emits one so the GM panel
(lie detector) can confirm the cheat fired / the read returned a real value /
the accuse checked an actual trace — narration claiming "you catch him palming
an ace" with no table.accuse/table.cheat span is a logged mismatch
(dispatch_engagement_watcher). See
docs/superpowers/specs/2026-05-29-free-for-all-n-seat-table-design.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_TABLE_DEALT = "table.dealt"
SPAN_ROUTES[SPAN_TABLE_DEALT] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "dealt",
        "seat_count": (span.attributes or {}).get("seat_count", 0),
        "game_kind": (span.attributes or {}).get("game_kind", ""),
        "stake_kind": (span.attributes or {}).get("stake_kind", ""),
    },
)
SPAN_TABLE_COMMIT = "table.commit"
SPAN_ROUTES[SPAN_TABLE_COMMIT] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "commit",
        "seat": (span.attributes or {}).get("seat", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "amount": (span.attributes or {}).get("amount", 0),
        "decision_point": (span.attributes or {}).get("decision_point", 0),
    },
)
SPAN_TABLE_NPC_COMMIT = "table.npc_commit"
SPAN_ROUTES[SPAN_TABLE_NPC_COMMIT] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "npc_commit",
        "seat": (span.attributes or {}).get("seat", ""),
        "strength_band": (span.attributes or {}).get("strength_band", ""),
        "pot": (span.attributes or {}).get("pot", 0),
        "chosen_beat": (span.attributes or {}).get("chosen_beat", ""),
    },
)
SPAN_TABLE_CHEAT = "table.cheat"
SPAN_ROUTES[SPAN_TABLE_CHEAT] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "cheat",
        "seat": (span.attributes or {}).get("seat", ""),
        "strength_before": (span.attributes or {}).get("strength_before", 0),
        "strength_after": (span.attributes or {}).get("strength_after", 0),
        "new_trace": (span.attributes or {}).get("new_trace", 0.0),
    },
)
SPAN_TABLE_READ = "table.read"
SPAN_ROUTES[SPAN_TABLE_READ] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "read",
        "reader": (span.attributes or {}).get("reader", ""),
        "target": (span.attributes or {}).get("target", ""),
        "info_returned": (span.attributes or {}).get("info_returned", ""),
    },
)
SPAN_TABLE_ACCUSE = "table.accuse"
SPAN_ROUTES[SPAN_TABLE_ACCUSE] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "accuse",
        "accuser": (span.attributes or {}).get("accuser", ""),
        "target": (span.attributes or {}).get("target", ""),
        "accuser_total": (span.attributes or {}).get("accuser_total", 0),
        "dc": (span.attributes or {}).get("dc", 0),
        "landed": (span.attributes or {}).get("landed", False),
    },
)
SPAN_TABLE_FOLD = "table.fold"
SPAN_ROUTES[SPAN_TABLE_FOLD] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "fold",
        "seat": (span.attributes or {}).get("seat", ""),
        "decision_point": (span.attributes or {}).get("decision_point", 0),
    },
)
SPAN_TABLE_SHOWDOWN = "table.showdown"
SPAN_ROUTES[SPAN_TABLE_SHOWDOWN] = SpanRoute(
    event_type="state_transition",
    component="table",
    extract=lambda span: {
        "field": "table",
        "op": "showdown",
        "winner": (span.attributes or {}).get("winner", ""),
        "forfeits": (span.attributes or {}).get("forfeits", ""),
        "pot_awarded": (span.attributes or {}).get("pot_awarded", ""),
        "revealed_strengths": (span.attributes or {}).get("revealed_strengths", ""),
    },
)


@contextmanager
def table_dealt_span(
    *,
    seat_count: int,
    game_kind: str,
    stake_kind: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_DEALT,
        {"seat_count": seat_count, "game_kind": game_kind, "stake_kind": stake_kind, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_commit_span(
    *,
    seat: str,
    beat_id: str,
    amount: int,
    decision_point: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_COMMIT,
        {
            "seat": seat,
            "beat_id": beat_id,
            "amount": amount,
            "decision_point": decision_point,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_npc_commit_span(
    *,
    seat: str,
    strength_band: str,
    pot: int,
    chosen_beat: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_NPC_COMMIT,
        {
            "seat": seat,
            "strength_band": strength_band,
            "pot": pot,
            "chosen_beat": chosen_beat,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_cheat_span(
    *,
    seat: str,
    strength_before: int,
    strength_after: int,
    new_trace: float,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_CHEAT,
        {
            "seat": seat,
            "strength_before": strength_before,
            "strength_after": strength_after,
            "new_trace": new_trace,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_read_span(
    *,
    reader: str,
    target: str,
    info_returned: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_READ,
        {"reader": reader, "target": target, "info_returned": info_returned, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_accuse_span(
    *,
    accuser: str,
    target: str,
    accuser_total: int,
    dc: int,
    landed: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_ACCUSE,
        {
            "accuser": accuser,
            "target": target,
            "accuser_total": accuser_total,
            "dc": dc,
            "landed": landed,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_fold_span(
    *,
    seat: str,
    decision_point: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_FOLD,
        {"seat": seat, "decision_point": decision_point, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def table_showdown_span(
    *,
    winner: str | None,
    forfeits: list[str],
    pot_awarded: str | None,
    revealed_strengths: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TABLE_SHOWDOWN,
        {
            "winner": winner or "",
            "forfeits": ",".join(forfeits),
            "pot_awarded": pot_awarded or "",
            "revealed_strengths": revealed_strengths,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
