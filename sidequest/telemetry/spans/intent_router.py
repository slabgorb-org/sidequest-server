"""Intent Router spans — decomposer producer, dispatch bank, and per-subsystem
execution (ADR-113).

Replaces the legacy ``local_dm.*`` span family (retired in Story 59-2). Every
mechanical decision the router makes emits a span so the GM panel can audit
whether the producer engaged (per CLAUDE.md "OTEL Observability Principle").

Span names:

* ``intent_router.decompose`` (INFO) — fires once per successful ``decompose``
  call. Attributes: ``action_length``, ``model``, ``dispatch_count``,
  ``latency_ms``, ``retry_count``, ``confidence_global``.
* ``intent_router.failed`` (ERROR) — fires once per failed attempt (including
  the first attempt of a retry-success turn, so the GM panel sees the
  contract violation even when the turn recovered). Attributes: ``reason``,
  ``raw_preview``, ``retry_count``.
* ``intent_router.dispatch_bank`` (INFO) — fires once per ``run_dispatch_bank``
  invocation. Attributes: ``turn_id``, ``dispatch_count``.
* ``intent_router.subsystem`` (INFO) — fires once per subsystem dispatch.
  Attributes: ``subsystem``, ``idempotency_key``, ``produced_directives``,
  ``error``.
* ``intent_router.lethality_arbitrate`` (INFO) — fires once per lethality
  arbiter invocation. Attributes: ``turn_id``, ``genre_key``,
  ``verdict_count``.

The dispatch-bank / subsystem / lethality spans are renames of the dormant
``SPAN_LOCAL_DM_*`` constants (same routing, same attributes, new names) so
the consumer-side code in ``sidequest/agents/subsystems/__init__.py`` can be
updated in one move.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_INTENT_ROUTER_DECOMPOSE = "intent_router.decompose"
SPAN_ROUTES[SPAN_INTENT_ROUTER_DECOMPOSE] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.decompose",
        "action_length": (span.attributes or {}).get("action_length", 0),
        "model": (span.attributes or {}).get("model", ""),
        "dispatch_count": (span.attributes or {}).get("dispatch_count", 0),
        "latency_ms": (span.attributes or {}).get("latency_ms", 0),
        "retry_count": (span.attributes or {}).get("retry_count", 0),
        "confidence_global": (span.attributes or {}).get("confidence_global", 0.0),
    },
)

SPAN_INTENT_ROUTER_FAILED = "intent_router.failed"
SPAN_ROUTES[SPAN_INTENT_ROUTER_FAILED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.failed",
        "reason": (span.attributes or {}).get("reason", ""),
        "raw_preview": (span.attributes or {}).get("raw_preview", ""),
        "retry_count": (span.attributes or {}).get("retry_count", 0),
    },
)

SPAN_INTENT_ROUTER_DISPATCH_BANK = "intent_router.dispatch_bank"
SPAN_ROUTES[SPAN_INTENT_ROUTER_DISPATCH_BANK] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.dispatch_bank",
        "turn_id": (span.attributes or {}).get("turn_id", ""),
        "dispatch_count": (span.attributes or {}).get("dispatch_count", 0),
    },
)

SPAN_INTENT_ROUTER_SUBSYSTEM = "intent_router.subsystem"
SPAN_ROUTES[SPAN_INTENT_ROUTER_SUBSYSTEM] = SpanRoute(
    event_type="subsystem_exercise_summary",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.subsystem",
        "subsystem": (span.attributes or {}).get("subsystem", ""),
        "idempotency_key": (span.attributes or {}).get("idempotency_key", ""),
        "produced_directives": (span.attributes or {}).get("produced_directives", 0),
        "error": (span.attributes or {}).get("error", ""),
        # ADR-113 confidence gate (Story 71-16): the GM panel audits every gate
        # decision — confidence scored, threshold applied, and engage vs degrade.
        "confidence": (span.attributes or {}).get("confidence", 0.0),
        "threshold": (span.attributes or {}).get("threshold", 0.0),
        "decision": (span.attributes or {}).get("decision", ""),
    },
)

SPAN_INTENT_ROUTER_LETHALITY_ARBITRATE = "intent_router.lethality_arbitrate"
SPAN_ROUTES[SPAN_INTENT_ROUTER_LETHALITY_ARBITRATE] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.lethality_arbitrate",
        "turn_id": (span.attributes or {}).get("turn_id", ""),
        "genre_key": (span.attributes or {}).get("genre_key", ""),
        "verdict_count": (span.attributes or {}).get("verdict_count", 0),
    },
)


SPAN_INTENT_ROUTER_CONFRONTATION_VOCABULARY = "intent_router.confrontation_vocabulary"
SPAN_ROUTES[SPAN_INTENT_ROUTER_CONFRONTATION_VOCABULARY] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.confrontation_vocabulary",
        "type_count": (span.attributes or {}).get("type_count", 0),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


SPAN_INTENT_ROUTER_DISPATCH_GATED = "intent_router.dispatch.gated"
SPAN_ROUTES[SPAN_INTENT_ROUTER_DISPATCH_GATED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.dispatch.gated",
        "subsystem": (span.attributes or {}).get("subsystem", ""),
        "idempotency_key": (span.attributes or {}).get("idempotency_key", ""),
        "reason": (span.attributes or {}).get("reason", ""),
    },
)


@contextmanager
def intent_router_dispatch_gated_span(
    *,
    subsystem: str,
    idempotency_key: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires once per dispatch the pre-narrator precondition gate drops.

    A dropped dispatch is structurally inert on this snapshot (a world-level
    precondition is unmet — e.g. ``scenario_clue`` with no ADR-053 scenario
    graph loaded), so engaging it could only ever produce a guaranteed
    ``dispatch_engagement.{subsystem}.mismatch`` false-positive. The gate
    removes it before the bank and the watcher; this span is the LOUD record
    of the skip the GM panel reads — never a silent fallback
    (CLAUDE.md "No Silent Fallbacks").
    """
    with Span.open(
        SPAN_INTENT_ROUTER_DISPATCH_GATED,
        {"subsystem": subsystem, "idempotency_key": idempotency_key, "reason": reason, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


SPAN_INTENT_ROUTER_DISPATCH_UNREGISTERED = "intent_router.dispatch.unregistered"
SPAN_ROUTES[SPAN_INTENT_ROUTER_DISPATCH_UNREGISTERED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.dispatch.unregistered",
        "subsystem": (span.attributes or {}).get("subsystem", ""),
        "idempotency_key": (span.attributes or {}).get("idempotency_key", ""),
    },
)


@contextmanager
def intent_router_dispatch_unregistered_span(
    *,
    subsystem: str,
    idempotency_key: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires once per dispatch the pre-narrator pass drops because its
    ``subsystem`` names no registered handler (Story 71-27).

    Distinct from :func:`intent_router_dispatch_gated_span`: a *gated* dispatch
    is a valid subsystem that is merely inert on this snapshot (a world-shape
    skip), whereas an *unregistered* dispatch is a ROUTER DEFECT — the router
    emitted a subsystem name (e.g. ``combat``, which is a confrontation *type*,
    not a subsystem key) that has no handler in the registry and could never
    engage. Keeping the two spans separate lets the GM-panel lie-detector tell
    "this world has no clue graph" apart from "the router emitted garbage". The
    dispatch is removed before the bank and before the post-turn watcher reads
    ``turn_context.dispatch_package``; this span is the LOUD record of the drop
    (CLAUDE.md "No Silent Fallbacks").
    """
    with Span.open(
        SPAN_INTENT_ROUTER_DISPATCH_UNREGISTERED,
        {"subsystem": subsystem, "idempotency_key": idempotency_key, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def intent_router_confrontation_vocabulary_span(
    *,
    type_count: int,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires when confrontation type vocabulary is injected into the router's
    state summary."""
    with Span.open(
        SPAN_INTENT_ROUTER_CONFRONTATION_VOCABULARY,
        {"type_count": type_count, "genre_slug": genre_slug, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY = "intent_router.witnessed_act_vocabulary"
SPAN_ROUTES[SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.witnessed_act_vocabulary",
        "act_count": (span.attributes or {}).get("act_count", 0),
        "present_npc_count": (span.attributes or {}).get("present_npc_count", 0),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def intent_router_witnessed_act_vocabulary_span(
    *,
    act_count: int,
    present_npc_count: int,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires when the witnessed-act vocabulary + present-NPC witness set is
    injected into the router's state summary (wry_whimsy political worlds only).

    The GM-panel record that the acts were surfaced — the precondition for the
    router being able to classify an action as a witnessed act at all."""
    with Span.open(
        SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY,
        {
            "act_count": act_count,
            "present_npc_count": present_npc_count,
            "genre_slug": genre_slug,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED = "intent_router.witnessed_act_classified"
SPAN_ROUTES[SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.witnessed_act_classified",
        "emitted": (span.attributes or {}).get("emitted", 0),
        "act_ids": (span.attributes or {}).get("act_ids", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def intent_router_witnessed_act_classified_span(
    *,
    emitted: int,
    act_ids: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires after ``decompose`` in a political world where the vocabulary was
    surfaced. ``emitted`` is the count of ``witnessed_act`` dispatches the router
    produced this turn (0 = it had the vocabulary and judged the action NOT a
    witnessed act). The GM-panel lie-detector for the front door: distinguishes
    "router classified this as witnessed_act:X" from "router declined to emit"."""
    with Span.open(
        SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED,
        {"emitted": emitted, "act_ids": act_ids, "genre_slug": genre_slug, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def intent_router_decompose_span(
    *,
    action_length: int,
    model: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Producer-success span. Caller sets ``dispatch_count``, ``latency_ms``,
    ``retry_count``, and ``confidence_global`` before return."""
    with Span.open(
        SPAN_INTENT_ROUTER_DECOMPOSE,
        {"action_length": action_length, "model": model, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def intent_router_failed_span(
    *,
    reason: str,
    raw_preview: str = "",
    retry_count: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Producer-failure span (ERROR-level).

    Marks the OTEL span status as ERROR so the GM panel and OTEL backends
    surface this as a real failure, not an INFO breadcrumb. The router calls
    this once per failed attempt (so two ERROR spans fire on retry-also-fails;
    one ERROR span + one INFO success span fire on retry-success).
    """
    with Span.open(
        SPAN_INTENT_ROUTER_FAILED,
        {
            "reason": reason,
            "raw_preview": raw_preview,
            "retry_count": retry_count,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(StatusCode.ERROR, description=reason)
        yield span


@contextmanager
def intent_router_dispatch_bank_span(
    turn_id: str,
    dispatch_count: int,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_INTENT_ROUTER_DISPATCH_BANK,
        {"turn_id": turn_id, "dispatch_count": dispatch_count, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def intent_router_subsystem_span(
    subsystem: str,
    idempotency_key: str,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Caller records ``produced_directives`` on success or ``error`` on failure."""
    with Span.open(
        SPAN_INTENT_ROUTER_SUBSYSTEM,
        {"subsystem": subsystem, "idempotency_key": idempotency_key, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def intent_router_lethality_arbitrate_span(
    turn_id: str,
    genre_key: str,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Caller sets ``verdict_count`` before return."""
    with Span.open(
        SPAN_INTENT_ROUTER_LETHALITY_ARBITRATE,
        {"turn_id": turn_id, "genre_key": genre_key, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
