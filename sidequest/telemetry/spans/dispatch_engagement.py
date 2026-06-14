"""Dispatch-engagement mismatch spans — Story 59-3 / ADR-113.

Fired by the post-turn watcher in
:mod:`sidequest.agents.dispatch_engagement_watcher` when the Intent Router
dispatched a subsystem but the engine did not engage on the post-turn
snapshot. The GM panel reads these spans as the lie-detector signal that
the narrator produced convincing prose with zero mechanical backing —
the SOUL "Illusionism" failure mode the Intent Router spine exists to
prevent.

One span name per subsystem in the dispatch vocabulary so the panel can
filter by subsystem without parsing the name; ``subsystem`` is also
surfaced as a span attribute so route consumers can branch on it cheaply.

Replaces the 59-1 ``confrontation.intent_mismatch_reprompt_failed`` self-
report-reprompt span — one mechanism per problem
(memory ``feedback_one_mechanism_per_problem``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH = "dispatch_engagement.confrontation.mismatch"
SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH = "dispatch_engagement.magic_working.mismatch"
SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH = "dispatch_engagement.scenario_clue.mismatch"
SPAN_DISPATCH_ENGAGEMENT_NPC_AGENCY_MISMATCH = "dispatch_engagement.npc_agency.mismatch"
SPAN_DISPATCH_ENGAGEMENT_DISTINCTIVE_DETAIL_HINT_MISMATCH = (
    "dispatch_engagement.distinctive_detail_hint.mismatch"
)
SPAN_DISPATCH_ENGAGEMENT_REFLECT_ABSENCE_MISMATCH = "dispatch_engagement.reflect_absence.mismatch"
SPAN_DISPATCH_ENGAGEMENT_WITNESSED_ACT_MISMATCH = "dispatch_engagement.witnessed_act.mismatch"
SPAN_DISPATCH_ENGAGEMENT_MOVEMENT_MISMATCH = "dispatch_engagement.movement.mismatch"

# The watcher itself crashed (playtest 2026-06-07: params["npc_name"]=None →
# AttributeError propagated from the bare call site in
# _execute_narration_turn, tore down the WebSocket mid-turn, and the turn
# never persisted). A pure-observability lie-detector must NEVER abort the
# turn pipeline — the wrapper catches, logs ERROR, and emits this span so
# the GM panel shows "the lie detector itself is broken" loudly instead of
# a dead socket.
SPAN_DISPATCH_ENGAGEMENT_WATCHER_CRASHED = "dispatch_engagement.watcher.crashed"

# Narration-vs-state lie-detector (sq-playtest 2026-06-14, heavy_metal/barsoom
# phantom-wound CRITICAL): the narrator described a PC taking a sword wound, but
# no encounter was active and the router dispatched NO confrontation (it errored
# on the schema). The dispatch-engagement watcher above cannot catch this — it
# only checks dispatches that EXIST, and here none did. This span fires when
# combat-injury prose appears with no mechanical backing (no live encounter, no
# confrontation dispatch): the canonical "convincing prose, zero mechanical
# backing" the OTEL panel exists to catch, in its narrator-improvised form.
SPAN_NARRATION_IMPROVISED_COMBAT = "narration.improvised_combat.suspected"

# QUEST-MAJOR (sq-playtest 2026-06-14, heavy_metal/barsoom): the narrator
# authored a concrete objective in prose (a giver + a task + a clock — "find the
# missing egg-keeper, settle the debt, claim the egg") but NEVER called the
# ``record_quest`` WRITE tool, so ``quest_log`` stayed empty and the engine filed
# the hook as a dormant ghost. The prose and the engine disagreed about whether a
# live thread exists. This span fires when objective-establishing prose appears
# with an empty quest_log — the quest analogue of improvised_combat: a promotion
# that happened in narration but not in state (SOUL: Diamonds & Coal / taken bait).
SPAN_NARRATION_UNMINTED_OBJECTIVE = "narration.unminted_objective.suspected"


def _extract(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "dispatch_engagement.mismatch",
        "subsystem": attrs.get("subsystem", ""),
        "idempotency_key": attrs.get("idempotency_key", ""),
        "dispatched_type": attrs.get("dispatched_type", ""),
        "evidence": attrs.get("evidence", ""),
    }


for _name in (
    SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_NPC_AGENCY_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_DISTINCTIVE_DETAIL_HINT_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_REFLECT_ABSENCE_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_WITNESSED_ACT_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_MOVEMENT_MISMATCH,
):
    SPAN_ROUTES[_name] = SpanRoute(
        event_type="state_transition",
        component="intent_router",
        extract=_extract,
    )


_SUBSYSTEM_TO_SPAN_NAME: dict[str, str] = {
    "confrontation": SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH,
    "magic_working": SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH,
    "scenario_clue": SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH,
    "npc_agency": SPAN_DISPATCH_ENGAGEMENT_NPC_AGENCY_MISMATCH,
    "distinctive_detail_hint": SPAN_DISPATCH_ENGAGEMENT_DISTINCTIVE_DETAIL_HINT_MISMATCH,
    "reflect_absence": SPAN_DISPATCH_ENGAGEMENT_REFLECT_ABSENCE_MISMATCH,
    "witnessed_act": SPAN_DISPATCH_ENGAGEMENT_WITNESSED_ACT_MISMATCH,
    "movement": SPAN_DISPATCH_ENGAGEMENT_MOVEMENT_MISMATCH,
}


def _extract_crashed(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "dispatch_engagement.watcher_crashed",
        "error_type": attrs.get("error_type", ""),
        "error": attrs.get("error", ""),
    }


SPAN_ROUTES[SPAN_DISPATCH_ENGAGEMENT_WATCHER_CRASHED] = SpanRoute(
    event_type="state_transition",
    component="intent_router",
    extract=_extract_crashed,
)


def _extract_improvised_combat(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "narration.improvised_combat",
        "evidence": attrs.get("evidence", ""),
    }


# Routed to component="narrator": here the SUSPECT under investigation is the
# narrator (it wrote a wound), not the router — the inverse attribution of the
# dispatch-engagement spans above.
SPAN_ROUTES[SPAN_NARRATION_IMPROVISED_COMBAT] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=_extract_improvised_combat,
)


def span_name_for_subsystem(subsystem: str) -> str:
    """Map a dispatched subsystem name to its mismatch span name.

    Raises :class:`KeyError` if ``subsystem`` is not in the dispatch
    vocabulary — fail loud (memory ``feedback_no_fallbacks_hard``) so a
    typo in the router's dispatch table surfaces immediately rather than
    silently routing to an "unknown subsystem" span the GM panel cannot
    render.
    """
    return _SUBSYSTEM_TO_SPAN_NAME[subsystem]


@contextmanager
def dispatch_engagement_mismatch_span(
    *,
    subsystem: str,
    idempotency_key: str = "",
    dispatched_type: str = "",
    evidence: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emit a ``dispatch_engagement.{subsystem}.mismatch`` span.

    The watcher emits one of these per detected mismatch — one turn with
    three failed dispatches produces three distinct spans, not one
    aggregated event. The GM panel needs per-subsystem accountability.
    """
    name = span_name_for_subsystem(subsystem)
    span_attrs: dict[str, Any] = {
        "subsystem": subsystem,
        "idempotency_key": idempotency_key,
        "dispatched_type": dispatched_type,
        "evidence": evidence,
    }
    span_attrs.update(attrs)
    with Span.open(name, span_attrs, tracer_override=_tracer) as span:
        yield span


@contextmanager
def dispatch_engagement_watcher_crashed_span(
    *,
    error_type: str,
    error: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Emit a ``dispatch_engagement.watcher.crashed`` span.

    Fired by ``run_dispatch_engagement_watcher``'s catch-all when the
    watcher (a pure-observability post-narration pass) raises. The turn
    pipeline continues — this span is the loud record that the lie
    detector itself failed and its mismatch coverage was lost this turn.
    """
    with Span.open(
        SPAN_DISPATCH_ENGAGEMENT_WATCHER_CRASHED,
        {"error_type": error_type, "error": error},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def narration_improvised_combat_span(
    *,
    evidence: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Emit a ``narration.improvised_combat.suspected`` span.

    Fired by ``run_improvised_combat_watcher`` when narration depicts combat
    injury with no active encounter and no confrontation dispatch — the
    narrator-improvised form of the Illusionism failure mode.
    """
    with Span.open(
        SPAN_NARRATION_IMPROVISED_COMBAT,
        {"evidence": evidence},
        tracer_override=_tracer,
    ) as span:
        yield span


def _extract_unminted_objective(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "narration.unminted_objective",
        "evidence": attrs.get("evidence", ""),
    }


# Routed to component="narrator": the SUSPECT is the narrator (it authored an
# objective in prose but did not call record_quest), the same attribution as the
# improvised-combat span above.
SPAN_ROUTES[SPAN_NARRATION_UNMINTED_OBJECTIVE] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=_extract_unminted_objective,
)


@contextmanager
def narration_unminted_objective_span(
    *,
    evidence: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Emit a ``narration.unminted_objective.suspected`` span.

    Fired by ``run_unminted_objective_watcher`` when narration establishes a
    concrete objective while ``quest_log`` is empty — the narrator promoted a
    hook in prose but never minted it via ``record_quest`` (QUEST-MAJOR).
    """
    with Span.open(
        SPAN_NARRATION_UNMINTED_OBJECTIVE,
        {"evidence": evidence},
        tracer_override=_tracer,
    ) as span:
        yield span


__all__ = [
    "SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_NPC_AGENCY_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_DISTINCTIVE_DETAIL_HINT_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_REFLECT_ABSENCE_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_WITNESSED_ACT_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_MOVEMENT_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_WATCHER_CRASHED",
    "SPAN_NARRATION_IMPROVISED_COMBAT",
    "SPAN_NARRATION_UNMINTED_OBJECTIVE",
    "dispatch_engagement_mismatch_span",
    "dispatch_engagement_watcher_crashed_span",
    "narration_improvised_combat_span",
    "narration_unminted_objective_span",
    "span_name_for_subsystem",
]
