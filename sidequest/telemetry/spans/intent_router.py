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
        # Story 71-40 env-vs-code latency attribution: raw SDK round-trip
        # (env) vs serialized state-summary size (code), so the GM panel can
        # localize the 4-12s decompose blowup.
        "sdk_latency_ms": (span.attributes or {}).get("sdk_latency_ms", 0),
        "state_summary_bytes": (span.attributes or {}).get("state_summary_bytes", 0),
        "retry_count": (span.attributes or {}).get("retry_count", 0),
        "confidence_global": (span.attributes or {}).get("confidence_global", 0.0),
        # Degrade-path marker (Story 71-29): True when this decompose span was
        # emitted from the operator-opt-in degrade branch (decompose raised
        # IntentRouterFailure and SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL let the
        # turn continue with dispatch_package=None). The routed state_transition
        # event reaches the live GM dashboard via WatcherSpanProcessor →
        # hub.publish; without it the GM panel is blind on a degraded turn — it
        # would see no intent_router.decompose event and could not tell a degraded
        # turn from a turn where the spine never executed. (Span routing is a live
        # hub broadcast, not a turn_telemetry write.)
        "degraded": (span.attributes or {}).get("degraded", False),
        # Replay-suppression marker (Story 91-2): True when this decompose span
        # records an INTENTIONAL router skip on a dice-resolution replay
        # re-entry of ``_execute_narration_turn`` — the dice outcome adds no new
        # player intent to classify, and re-classifying it was the structural
        # driver of the [COST-1] 8x/turn Haiku volume. Same doctrine as
        # ``degraded``: the GM panel must distinguish "router intentionally
        # skipped (replay)" from "router dark" (never a silent skip).
        "replay_suppressed": (span.attributes or {}).get("replay_suppressed", False),
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
        "turn_number": (span.attributes or {}).get("turn_number", 0),
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
        # Turn attribution (off-by-one fix, 2026-06-04): the dashboard activity
        # grid buckets each span by turn_number. The dispatch bank runs BEFORE
        # record_interaction(), so without the effective turn number threaded in
        # here the per-subsystem dot inherits the PRIOR turn and the row reads
        # dark on the turn it actually engaged.
        "turn_number": (span.attributes or {}).get("turn_number", 0),
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


SPAN_INTENT_ROUTER_REGION_EXITS = "intent_router.region_exits"
SPAN_ROUTES[SPAN_INTENT_ROUTER_REGION_EXITS] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.region_exits",
        "exit_count": (span.attributes or {}).get("exit_count", 0),
        "seam_count": (span.attributes or {}).get("seam_count", 0),
        "region_id": (span.attributes or {}).get("region_id", ""),
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


SPAN_INTENT_ROUTER_CALL_BUDGET_BREACH = "intent_router.call_budget.breach"
SPAN_ROUTES[SPAN_INTENT_ROUTER_CALL_BUDGET_BREACH] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.call_budget.breach",
        "turn_id": (span.attributes or {}).get("turn_id", 0),
        "observed": (span.attributes or {}).get("observed", 0),
        "budget": (span.attributes or {}).get("budget", 0),
    },
)


@contextmanager
def intent_router_call_budget_breach_span(
    *,
    turn_id: int,
    observed: int,
    budget: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Per-turn classification call budget breached (Story 91-2, epic 91
    "Dark Spend") — ERROR-level.

    Fires when a single pre-narrator pass spent more Haiku SDK round-trips
    than ``intent_router_pass.INTENT_ROUTER_CALL_BUDGET_PER_TURN`` allows.
    The [COST-1] forensics measured ~8 classification calls per turn against
    a design expectation of ~1; the budget assertion is the GM-panel
    lie-detector that surfaces a recurrence in production, not just in CI.
    ERROR status matches the register ``intent_router.failed`` uses — a
    breach is a contract violation, not a breadcrumb. The breach is evidence,
    NOT a circuit breaker: the turn continues (hard-kill is ADR-134's job).
    """
    with Span.open(
        SPAN_INTENT_ROUTER_CALL_BUDGET_BREACH,
        {"turn_id": turn_id, "observed": observed, "budget": budget, **attrs},
        tracer_override=_tracer,
    ) as span:
        span.set_status(
            StatusCode.ERROR,
            description=f"classification calls {observed} > budget {budget}",
        )
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
def intent_router_region_exits_span(
    *,
    exit_count: int,
    seam_count: int,
    region_id: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires when the PC's current cartography region's real exits — adjacency
    neighbors + seam routes — are injected into the router's state summary
    (Story 105-2 Piece 2, the lexical bridge for movement classification).

    A ``seam_count`` of zero on a region that DOES own a seam route would mean
    the descent vocabulary never reached the router — exactly the turn-3 miss
    this projection closes (the router was asked to recognize a descent it was
    never told existed)."""
    with Span.open(
        SPAN_INTENT_ROUTER_REGION_EXITS,
        {
            "exit_count": exit_count,
            "seam_count": seam_count,
            "region_id": region_id,
            "genre_slug": genre_slug,
            **attrs,
        },
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


SPAN_INTENT_ROUTER_FATE_VOCABULARY = "intent_router.fate_vocabulary"
SPAN_ROUTES[SPAN_INTENT_ROUTER_FATE_VOCABULARY] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.fate_vocabulary",
        "skill_count": (span.attributes or {}).get("skill_count", 0),
        "aspects_dropped": (span.attributes or {}).get("aspects_dropped", 0),
        "bytes_before": (span.attributes or {}).get("bytes_before", 0),
        "bytes_after": (span.attributes or {}).get("bytes_after", 0),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def intent_router_fate_vocabulary_span(
    *,
    skill_count: int,
    aspects_dropped: int,
    bytes_before: int,
    bytes_after: int,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fires when the TRIMMED Fate vocabulary is injected into the router's
    state summary (Fate packs only, Story 126-10).

    The router needs the PCs' skills (to classify a freeform action into one of
    the four Fate actions) and whether a conflict is live — NOT the narrator's
    full live-aspect dump. ``bytes_before``/``bytes_after`` are the GM-panel
    evidence the trim engaged: the full projection vs the trimmed router block.
    A turn with no live aspects yet shows ``bytes_before == bytes_after``
    (nothing to trim) — honest, not a regression."""
    with Span.open(
        SPAN_INTENT_ROUTER_FATE_VOCABULARY,
        {
            "skill_count": skill_count,
            "aspects_dropped": aspects_dropped,
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "genre_slug": genre_slug,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# sq-playtest 2026-06-07 (standoff seat seam): the router had the pack's
# confrontation vocabulary and the action lexically matched authored
# intent_verbs, yet no confrontation dispatch was emitted — and NOTHING
# recorded the decline ("silence indistinguishable from 'feature doesn't
# exist'"). Fires when the action hits >=1 authored intent_verb token OR a
# confrontation dispatch was emitted; quiet turns (no verb hit, no dispatch)
# stay quiet. emitted=0 with verb_hits non-empty is the unrouted shape.
SPAN_INTENT_ROUTER_CONFRONTATION_CLASSIFIED = "intent_router.confrontation_classified"
SPAN_ROUTES[SPAN_INTENT_ROUTER_CONFRONTATION_CLASSIFIED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.confrontation_classified",
        "emitted": (span.attributes or {}).get("emitted", 0),
        "types": (span.attributes or {}).get("types", ""),
        "verb_hits": (span.attributes or {}).get("verb_hits", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def intent_router_confrontation_classified_span(
    *,
    emitted: int,
    types: str,
    verb_hits: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Front-door confrontation classification evidence (twin of the
    witnessed_act span below). ``emitted`` counts confrontation dispatches in
    the package; ``verb_hits`` is the comma-joined ``type:verb`` lexical
    matches between the action and the pack's authored intent_verbs.
    ``emitted=0`` with non-empty ``verb_hits`` is the standoff-seam decline
    the GM panel must be able to see."""
    with Span.open(
        SPAN_INTENT_ROUTER_CONFRONTATION_CLASSIFIED,
        {
            "emitted": emitted,
            "types": types,
            "verb_hits": verb_hits,
            "genre_slug": genre_slug,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# sq-playtest 2026-06-07 (Reroute Power): a party character's ADR-097
# signature ability was declared VERBATIM in the action text and produced zero
# ability/dispatch/gate spans — the ADR-123 dispatch bank has no ability
# subsystem, so the declaration has no mechanical route, and nothing recorded
# the decline. This span is the lie-detector: ``ability``/``character`` name
# the declared-but-unrouted invocation. When an ability subsystem lands, this
# span is the measure of what it must absorb.
SPAN_INTENT_ROUTER_ABILITY_INVOCATION_UNROUTED = "intent_router.ability_invocation_unrouted"
SPAN_ROUTES[SPAN_INTENT_ROUTER_ABILITY_INVOCATION_UNROUTED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.ability_invocation_unrouted",
        "ability": (span.attributes or {}).get("ability", ""),
        "character": (span.attributes or {}).get("character", ""),
        "in_confrontation": (span.attributes or {}).get("in_confrontation", False),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def intent_router_ability_invocation_unrouted_span(
    *,
    ability: str,
    character: str,
    in_confrontation: bool,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """A party character's ADR-097 ability name appeared word-boundary in the
    action text, and no dispatch route exists for abilities — the declared
    invocation could not be mechanically engaged. ``in_confrontation`` records
    whether a live encounter was active at declaration time (the only context
    some abilities can legally fire in)."""
    with Span.open(
        SPAN_INTENT_ROUTER_ABILITY_INVOCATION_UNROUTED,
        {
            "ability": ability,
            "character": character,
            "in_confrontation": in_confrontation,
            "genre_slug": genre_slug,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# sq-playtest 2026-06-07 (Size Up outside the standoff): a confrontation-only
# beat invoked BY NAME with no confrontation active was freehanded as prose
# with zero gate telemetry — no unregistered gate, no precondition gate, no
# refusal. Fires only for MULTI-WORD beat labels (single-word labels like
# "Shoot" are ordinary verbs, not invocations) and only when the router
# emitted no confrontation dispatch this turn (a seated confrontation makes
# the beat playable — not a decline).
SPAN_INTENT_ROUTER_BEAT_OUTSIDE_CONFRONTATION = "intent_router.beat_invoked_outside_confrontation"
SPAN_ROUTES[SPAN_INTENT_ROUTER_BEAT_OUTSIDE_CONFRONTATION] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.beat_invoked_outside_confrontation",
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def intent_router_beat_outside_confrontation_span(
    *,
    beat_id: str,
    confrontation_type: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """A multi-word beat label from the pack's confrontation defs appeared in
    the action text while no confrontation was active and none was seated this
    turn — the invocation had no playable surface and the decline must be
    visible to the GM panel."""
    with Span.open(
        SPAN_INTENT_ROUTER_BEAT_OUTSIDE_CONFRONTATION,
        {
            "beat_id": beat_id,
            "confrontation_type": confrontation_type,
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
    turn_number: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``turn_number`` is ``snapshot.turn_manager.interaction`` so the dashboard
    grids this span to the correct turn column (Bug A fix)."""
    with Span.open(
        SPAN_INTENT_ROUTER_DISPATCH_BANK,
        {"turn_id": turn_id, "dispatch_count": dispatch_count, "turn_number": turn_number, **attrs},
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


SPAN_INTENT_ROUTER_STATE_SUMMARY_SLIMMED = "intent_router.state_summary_slimmed"
SPAN_ROUTES[SPAN_INTENT_ROUTER_STATE_SUMMARY_SLIMMED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=lambda span: {
        "field": "intent_router.state_summary_slimmed",
        "bytes_before": (span.attributes or {}).get("bytes_before", 0),
        "bytes_after": (span.attributes or {}).get("bytes_after", 0),
        "npcs_dropped": (span.attributes or {}).get("npcs_dropped", 0),
        "room_states_dropped": (span.attributes or {}).get("room_states_dropped", 0),
        "projection_skipped": (span.attributes or {}).get("projection_skipped", False),
    },
)


@contextmanager
def intent_router_state_summary_slimmed_span(
    *,
    bytes_before: int,
    bytes_after: int,
    npcs_dropped: int,
    room_states_dropped: int,
    projection_skipped: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-110 amendment / Story 82-10 — the router's state_summary got the
    shared Phase B + C slimming cut. ``bytes_before``/``bytes_after`` is the
    GM-panel before/after evidence the amendment mandates ("the slimming
    story must show the before/after on state_summary_bytes"). A pass where
    ``bytes_after`` tracks ``bytes_before`` (no cut) with
    ``projection_skipped=True`` means actor location was unresolvable
    (party split / pre-chargen) and the room/NPC projections passed through
    — degraded, loud, never silent.
    """
    with Span.open(
        SPAN_INTENT_ROUTER_STATE_SUMMARY_SLIMMED,
        {
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "npcs_dropped": npcs_dropped,
            "room_states_dropped": room_states_dropped,
            "projection_skipped": projection_skipped,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
