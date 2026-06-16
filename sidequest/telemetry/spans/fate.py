"""Fate ruleset OTEL spans (ADR-144). The GM panel is the lie detector: a Fate
roll that fired emits ``fate.action_resolved`` carrying the full math."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span


def fate_action_resolved_span(
    *,
    actor: str,
    skill_rating: int,
    dice: tuple[int, int, int, int],
    ladder_total: int,
    opposition: int,
    opposition_kind: str,
    shifts: int,
    tier: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.action_resolved`` — one Fate roll resolved."""
    attributes: dict[str, Any] = {
        "field": "action_resolved",
        "actor": actor,
        "skill_rating": skill_rating,
        "dice": ",".join(str(d) for d in dice),
        "ladder_total": ladder_total,
        "opposition": opposition,
        "opposition_kind": opposition_kind,
        "shifts": shifts,
        "tier": tier,
        **attrs,
    }
    with Span.open("fate.action_resolved", attributes, tracer_override=_tracer):
        pass


# --- F3a: Fate spine projection span (GM panel = lie detector) ---------------
# ``fate.projection.emitted`` confirms a FATE_STATE message was broadcast to the
# client — the GM-panel evidence that the engine projected the Fate sheet to the
# player rather than the narrator improvising the mechanics (CLAUDE.md OTEL
# principle). SPAN_* module constant, so it needs a SPAN_ROUTES entry (the
# routing-completeness lint). The RELATIONSHIPS/QUESTS siblings are
# ``relationships.emitted`` / ``quests.emitted``.
SPAN_FATE_PROJECTION_EMITTED = "fate.projection.emitted"

SPAN_ROUTES[SPAN_FATE_PROJECTION_EMITTED] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "projection_emitted",
        "character_count": (span.attributes or {}).get("character_count", 0),
        "scene_aspect_count": (span.attributes or {}).get("scene_aspect_count", 0),
        "in_conflict": bool((span.attributes or {}).get("in_conflict", False)),
        "changed": bool((span.attributes or {}).get("changed", False)),
    },
)


# --- F1b: fate-point economy + facet spans (GM panel = lie detector) ---------
# Registered as typed state_transition routes so the GM panel surfaces each
# economy delta and each stress/consequence mark in a typed tab (not just the
# always-on agent_span_close fan-out). Literal keys, no SPAN_* constants — the
# routing-completeness lint (tests/telemetry/test_routing_completeness.py) only
# inspects SPAN_* module constants, so these need no FLAT_ONLY entry.
SPAN_ROUTES["fate.fate_point.delta"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "fate_point_delta",
        "actor": (span.attributes or {}).get("actor", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "before": (span.attributes or {}).get("before", 0),
        "after": (span.attributes or {}).get("after", 0),
    },
)
SPAN_ROUTES["fate.aspect.invoked"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "aspect_invoked",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
        "free": (span.attributes or {}).get("free", False),
        "mode": (span.attributes or {}).get("mode", ""),
        "fate_points_after": (span.attributes or {}).get("fate_points_after", 0),
    },
)
SPAN_ROUTES["fate.compel.offered"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "compel_offered",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
    },
)
SPAN_ROUTES["fate.compel.accepted"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "compel_accepted",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
        "fate_points_after": (span.attributes or {}).get("fate_points_after", 0),
    },
)
SPAN_ROUTES["fate.stress.applied"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "stress_applied",
        "actor": (span.attributes or {}).get("actor", ""),
        "track": (span.attributes or {}).get("track", ""),
        "box_value": (span.attributes or {}).get("box_value", 0),
    },
)
SPAN_ROUTES["fate.consequence.taken"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "consequence_taken",
        "actor": (span.attributes or {}).get("actor", ""),
        "level": (span.attributes or {}).get("level", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
    },
)


def fate_point_delta_span(
    *,
    actor: str,
    reason: str,
    before: int,
    after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.fate_point.delta`` — one fate-point change with its reason."""
    attributes: dict[str, Any] = {
        "field": "fate_point_delta",
        "actor": actor,
        "reason": reason,
        "before": before,
        "after": after,
        **attrs,
    }
    with Span.open("fate.fate_point.delta", attributes, tracer_override=_tracer):
        pass


def fate_aspect_invoked_span(
    *,
    actor: str,
    aspect: str,
    free: bool,
    mode: str,
    fate_points_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.aspect.invoked`` — an aspect invoked for +2 or a reroll.
    ``free`` distinguishes a free invocation from a fate-point-paid one."""
    attributes: dict[str, Any] = {
        "field": "aspect_invoked",
        "actor": actor,
        "aspect": aspect,
        "free": free,
        "mode": mode,
        "fate_points_after": fate_points_after,
        **attrs,
    }
    with Span.open("fate.aspect.invoked", attributes, tracer_override=_tracer):
        pass


def fate_flavor_rider_span(
    *,
    actor: str,
    affected_mechanics: bool = False,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.action.flavor_rider`` (Story 118-10; the Fate analog of
    ``{slug}.action.flavor_rider``, Story 108-5).

    The GM-panel lie-detector that the player's RP-flavor rider — the freeform
    "chandelier swing" typed alongside a Fate action tile, riding
    ``FateActionPayload.player_action`` — was attached as narrator color ONLY and
    did NOT enter the 4dF resolution. The roll is resolved from the skill rating,
    opposition, and any aspect invoke; the rider is downstream cosmetic context
    the narrator uses to flavor the resolved outcome (SOUL: "Rule of Cool" /
    "Yes, And" without mechanical advantage). The span fires ONLY when text is
    actually attached, so ``attached`` is always True. ``affected_mechanics`` is
    the structural attestation that the dispatch resolved the dice without
    consulting the rider — the green guard ``test_player_action_is_mechanically_
    inert`` is its runtime proof.

    Without this span the GM panel cannot tell an inert RP affordance from a
    covert freeform-adjudication regression — the El Dorado failure the
    mechanical-scaffold architecture exists to prevent (CLAUDE.md OTEL
    Observability Principle).
    """
    attributes: dict[str, Any] = {
        "field": "flavor_rider",
        "actor": actor,
        "attached": True,
        "affected_mechanics": affected_mechanics,
        **attrs,
    }
    with Span.open("fate.action.flavor_rider", attributes, tracer_override=_tracer):
        pass


def fate_compel_offered_span(
    *,
    actor: str,
    aspect: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.compel.offered`` — the narrator proposed a compel (no economy
    change until accepted)."""
    attributes: dict[str, Any] = {
        "field": "compel_offered",
        "actor": actor,
        "aspect": aspect,
        **attrs,
    }
    with Span.open("fate.compel.offered", attributes, tracer_override=_tracer):
        pass


def fate_compel_accepted_span(
    *,
    actor: str,
    aspect: str,
    fate_points_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.compel.accepted`` — a compel accepted (earns one fate point)."""
    attributes: dict[str, Any] = {
        "field": "compel_accepted",
        "actor": actor,
        "aspect": aspect,
        "fate_points_after": fate_points_after,
        **attrs,
    }
    with Span.open("fate.compel.accepted", attributes, tracer_override=_tracer):
        pass


def fate_stress_applied_span(
    *,
    actor: str,
    track: str,
    box_value: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.stress.applied`` — one stress box checked to absorb a hit."""
    attributes: dict[str, Any] = {
        "field": "stress_applied",
        "actor": actor,
        "track": track,
        "box_value": box_value,
        **attrs,
    }
    with Span.open("fate.stress.applied", attributes, tracer_override=_tracer):
        pass


def fate_consequence_taken_span(
    *,
    actor: str,
    level: str,
    aspect: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.consequence.taken`` — a consequence slot filled (becomes an
    aspect). ``aspect`` is the consequence's free-text."""
    attributes: dict[str, Any] = {
        "field": "consequence_taken",
        "actor": actor,
        "level": level,
        "aspect": aspect,
        **attrs,
    }
    with Span.open("fate.consequence.taken", attributes, tracer_override=_tracer):
        pass


# --- F1c: conflict exchange spans (GM panel = lie detector) ------------------
SPAN_ROUTES["fate.exchange.committed"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "exchange_committed",
        "committed_actors": (span.attributes or {}).get("committed_actors", ""),
    },
)
SPAN_ROUTES["fate.exchange.order"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "exchange_order",
        "order": (span.attributes or {}).get("order", ""),
        "skill": (span.attributes or {}).get("skill", ""),
    },
)
SPAN_ROUTES["fate.exchange.resolved"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "exchange_resolved",
        "resolution_order": (span.attributes or {}).get("resolution_order", ""),
        "resolved": (span.attributes or {}).get("resolved", False),
        "round_number": (span.attributes or {}).get("round_number", 0),
    },
)
SPAN_ROUTES["fate.aspect.created"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "aspect_created",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
        "free_invokes": (span.attributes or {}).get("free_invokes", 0),
    },
)
SPAN_ROUTES["fate.taken_out"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "taken_out",
        "actor": (span.attributes or {}).get("actor", ""),
        "by": (span.attributes or {}).get("by", ""),
        "shifts": (span.attributes or {}).get("shifts", 0),
    },
)
SPAN_ROUTES["fate.conceded"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "conceded",
        "actor": (span.attributes or {}).get("actor", ""),
        "fate_points_earned": (span.attributes or {}).get("fate_points_earned", 0),
    },
)
# --- F2a: classification span (GM panel = lie detector) ----------------------
# The router classified a freeform action into one of the four Fate actions and
# the bank engaged dispatch_fate_action. Literal key (no SPAN_* constant) — the
# routing-completeness lint only inspects SPAN_* module constants.
SPAN_ROUTES["fate.action.classified"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "action_classified",
        "actor": (span.attributes or {}).get("actor", ""),
        "action": (span.attributes or {}).get("action", ""),
        "skill": (span.attributes or {}).get("skill", ""),
        "target": (span.attributes or {}).get("target", ""),
        "confidence": (span.attributes or {}).get("confidence", 0.0),
    },
)
SPAN_ROUTES["fate.action.flavor_rider"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "flavor_rider",
        "actor": (span.attributes or {}).get("actor", ""),
        "attached": (span.attributes or {}).get("attached", True),
        "affected_mechanics": (span.attributes or {}).get("affected_mechanics", False),
    },
)
# --- F2d: deterministic opponent decision span (GM panel = lie detector) ------
# The opponent AI chose a proactive action against the player. The GM-panel
# evidence that the swing was an engine decision, not narrator improvisation.
# Literal key (no SPAN_* constant) — the routing-completeness lint only inspects
# SPAN_* module constants.
SPAN_ROUTES["fate.opponent.decided"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "opponent_decided",
        "actor": (span.attributes or {}).get("actor", ""),
        "action": (span.attributes or {}).get("action", ""),
        "skill": (span.attributes or {}).get("skill", ""),
        "target": (span.attributes or {}).get("target", ""),
        "ladder_total": (span.attributes or {}).get("ladder_total", 0),
    },
)
# --- F2c: narration-vs-state honesty span (GM panel = lie detector) -----------
# The narrator claimed a Fate outcome (an advantage created, a foe taken out) the
# engine state does not show. The F2 analogue of the dispatch-engagement /
# improvised-combat watchers: convincing prose with zero mechanical backing. One
# span per detected mismatch; ``subsystem`` ∈ {create_advantage, taken_out}.
# Literal key (no SPAN_* constant) — the routing-completeness lint only inspects
# SPAN_* module constants (the F2a/F2d precedent).
SPAN_ROUTES["fate.narration.mismatch"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "narration_mismatch",
        "subsystem": (span.attributes or {}).get("subsystem", ""),
        "claim": (span.attributes or {}).get("claim", ""),
        "reason": (span.attributes or {}).get("reason", ""),
    },
)
# --- F4a: chargen-seeding span (GM panel = lie detector) ---------------------
# The engine seeded a Fate sheet at character creation (skills + aspects +
# refresh from the pack's FateConfig). The GM-panel evidence that a fate-bound
# PC actually has mechanical backing rather than an empty sheet the narrator
# improvises over. Literal key (no SPAN_* constant) — the routing-completeness
# lint only inspects SPAN_* module constants (the F2a/F2d/F2c precedent).
SPAN_ROUTES["fate.chargen.seeded"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_seeded",
        "actor": (span.attributes or {}).get("actor", ""),
        "skill_count": (span.attributes or {}).get("skill_count", 0),
        "aspect_count": (span.attributes or {}).get("aspect_count", 0),
        "refresh": (span.attributes or {}).get("refresh", 0),
    },
)
# --- F4a2: interactive chargen spans (GM panel = lie detector) ----------------
# The player walked the interactive Fate chargen flow (archetype -> aspects ->
# pyramid -> stunts) and the server validated it to a legal sheet. One span per
# step + a validated/completed pair so the GM panel can confirm the sheet was
# engine-built from explicit choices, not narrator-improvised. Literal keys (no
# SPAN_* constant) — the routing-completeness lint only inspects SPAN_* constants.
SPAN_ROUTES["fate.chargen.archetype_selected"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_archetype_selected",
        "archetype": (span.attributes or {}).get("archetype", ""),
    },
)
SPAN_ROUTES["fate.chargen.aspects_authored"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_aspects_authored",
        "high_concept_present": bool((span.attributes or {}).get("high_concept_present", False)),
        "trouble_present": bool((span.attributes or {}).get("trouble_present", False)),
        "free_count": (span.attributes or {}).get("free_count", 0),
    },
)
SPAN_ROUTES["fate.chargen.pyramid_allocated"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_pyramid_allocated",
        "rung_counts": (span.attributes or {}).get("rung_counts", ""),
        "skills_placed": (span.attributes or {}).get("skills_placed", 0),
        "legal": bool((span.attributes or {}).get("legal", False)),
    },
)
SPAN_ROUTES["fate.chargen.stunts_selected"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_stunts_selected",
        "count": (span.attributes or {}).get("count", 0),
        "refresh_before": (span.attributes or {}).get("refresh_before", 0),
        "refresh_after": (span.attributes or {}).get("refresh_after", 0),
    },
)
SPAN_ROUTES["fate.chargen.validated"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_validated",
        "legal": bool((span.attributes or {}).get("legal", False)),
        "violations": (span.attributes or {}).get("violations", ""),
    },
)
SPAN_ROUTES["fate.chargen.completed"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "chargen_completed",
        "aspect_count": (span.attributes or {}).get("aspect_count", 0),
        "skill_count": (span.attributes or {}).get("skill_count", 0),
        "stunt_count": (span.attributes or {}).get("stunt_count", 0),
        "refresh": (span.attributes or {}).get("refresh", 0),
    },
)
# --- 114-10: chargen gear-compile span (GM panel = lie detector) -------------
# The engine materialized a character's starting gear onto its FateSheet at
# chargen — the GM-panel evidence that gear actually fired (aspects/stunts placed,
# refresh debited by stunt-gear) rather than the narrator improvising an item.
# Literal key (no SPAN_* constant) — the routing-completeness lint only inspects
# SPAN_* module constants (the F2a/F2d/F4a precedent).
SPAN_ROUTES["fate.gear_compiled"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "gear_compiled",
        "actor": (span.attributes or {}).get("actor", ""),
        "archetype": (span.attributes or {}).get("archetype", ""),
        # gear_ids names WHICH gear fired — the GM panel (lie detector) needs it,
        # not just the aspect/stunt counts, to verify the right gear materialized.
        "gear_ids": (span.attributes or {}).get("gear_ids", ""),
        "aspects_placed": (span.attributes or {}).get("aspects_placed", 0),
        "stunts_added": (span.attributes or {}).get("stunts_added", 0),
        "refresh_debited": (span.attributes or {}).get("refresh_debited", 0),
    },
)


def fate_exchange_committed_span(
    *, committed_actors: str, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.exchange.committed`` — the sealed-commit barrier closed."""
    attributes: dict[str, Any] = {
        "field": "exchange_committed",
        "committed_actors": committed_actors,
        **attrs,
    }
    with Span.open("fate.exchange.committed", attributes, tracer_override=_tracer):
        pass


def fate_exchange_order_span(
    *, order: str, skill: str, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.exchange.order`` — the resolved turn order for this exchange,
    keyed by the initiative skill (Notice for physical, Empathy for mental conflicts)."""
    attributes: dict[str, Any] = {
        "field": "exchange_order",
        "order": order,
        "skill": skill,
        **attrs,
    }
    with Span.open("fate.exchange.order", attributes, tracer_override=_tracer):
        pass


def fate_exchange_resolved_span(
    *, resolution_order: str, resolved: bool, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.exchange.resolved`` — the exchange walk finished. ``resolved``
    is whether the confrontation itself ended this exchange."""
    attributes: dict[str, Any] = {
        "field": "exchange_resolved",
        "resolution_order": resolution_order,
        "resolved": resolved,
        **attrs,
    }
    with Span.open("fate.exchange.resolved", attributes, tracer_override=_tracer):
        pass


def fate_aspect_created_span(
    *, actor: str, aspect: str, free_invokes: int, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.aspect.created`` — create-advantage placed a situation aspect
    (or a boost) with ``free_invokes`` free invocations."""
    attributes: dict[str, Any] = {
        "field": "aspect_created",
        "actor": actor,
        "aspect": aspect,
        "free_invokes": free_invokes,
        **attrs,
    }
    with Span.open("fate.aspect.created", attributes, tracer_override=_tracer):
        pass


def fate_taken_out_span(
    *, actor: str, by: str, shifts: int, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.taken_out`` — an actor's stress+consequences could not absorb a
    hit and they are out of the conflict."""
    attributes: dict[str, Any] = {
        "field": "taken_out",
        "actor": actor,
        "by": by,
        "shifts": shifts,
        **attrs,
    }
    with Span.open("fate.taken_out", attributes, tracer_override=_tracer):
        pass


def fate_conceded_span(
    *, actor: str, fate_points_earned: int, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.conceded`` — a player conceded (pre-roll), leaving on their
    terms and earning fate points."""
    attributes: dict[str, Any] = {
        "field": "conceded",
        "actor": actor,
        "fate_points_earned": fate_points_earned,
        **attrs,
    }
    with Span.open("fate.conceded", attributes, tracer_override=_tracer):
        pass


def fate_action_classified_span(
    *,
    actor: str,
    action: str,
    skill: str,
    target: str = "",
    confidence: float = 0.0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.action.classified`` — the Intent Router classified a freeform
    player action into one of the four Fate actions (F2a). The GM-panel evidence
    that a Fate action was engaged from natural language, not improvised."""
    attributes: dict[str, Any] = {
        "field": "action_classified",
        "actor": actor,
        "action": action,
        "skill": skill,
        "target": target,
        "confidence": confidence,
        **attrs,
    }
    with Span.open("fate.action.classified", attributes, tracer_override=_tracer):
        pass


def fate_opponent_decided_span(
    *,
    actor: str,
    action: str,
    skill: str,
    target: str,
    ladder_total: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.opponent.decided`` — the deterministic opponent AI chose a
    proactive action against the player (F2d). The GM-panel evidence that the
    swing was an engine decision, not narrator improvisation."""
    attributes: dict[str, Any] = {
        "field": "opponent_decided",
        "actor": actor,
        "action": action,
        "skill": skill,
        "target": target,
        "ladder_total": ladder_total,
        **attrs,
    }
    with Span.open("fate.opponent.decided", attributes, tracer_override=_tracer):
        pass


def fate_narration_mismatch_span(
    *,
    subsystem: str,
    claim: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.narration.mismatch`` — the narrator claimed a Fate outcome the
    engine state does not show (F2c). ``subsystem`` is the claimed outcome kind
    (``create_advantage`` | ``taken_out``), ``claim`` the prose evidence, and
    ``reason`` why state contradicts it. The GM-panel polygraph for Fate prose."""
    attributes: dict[str, Any] = {
        "field": "narration_mismatch",
        "subsystem": subsystem,
        "claim": claim,
        "reason": reason,
        **attrs,
    }
    with Span.open("fate.narration.mismatch", attributes, tracer_override=_tracer):
        pass


def fate_chargen_seeded_span(
    *,
    skill_count: int,
    aspect_count: int,
    refresh: int,
    actor: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.chargen.seeded`` — a Fate sheet was seeded at chargen (F4a). The
    GM-panel evidence that a fate-bound PC has mechanical backing (skills, aspects,
    refresh) from the pack's FateConfig, not an empty sheet."""
    attributes: dict[str, Any] = {
        "field": "chargen_seeded",
        "actor": actor,
        "skill_count": skill_count,
        "aspect_count": aspect_count,
        "refresh": refresh,
        **attrs,
    }
    with Span.open("fate.chargen.seeded", attributes, tracer_override=_tracer):
        pass


def fate_chargen_archetype_selected_span(
    *, archetype: str, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.chargen.archetype_selected`` — the player picked a chargen
    archetype template (F4a2). The seed-then-edit starting point."""
    attributes: dict[str, Any] = {
        "field": "chargen_archetype_selected",
        "archetype": archetype,
        **attrs,
    }
    with Span.open("fate.chargen.archetype_selected", attributes, tracer_override=_tracer):
        pass


def fate_chargen_aspects_authored_span(
    *,
    high_concept_present: bool,
    trouble_present: bool,
    free_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.chargen.aspects_authored`` — the player authored/confirmed their
    aspects (High Concept + Trouble + N free) at chargen (F4a2)."""
    attributes: dict[str, Any] = {
        "field": "chargen_aspects_authored",
        "high_concept_present": high_concept_present,
        "trouble_present": trouble_present,
        "free_count": free_count,
        **attrs,
    }
    with Span.open("fate.chargen.aspects_authored", attributes, tracer_override=_tracer):
        pass


def fate_chargen_pyramid_allocated_span(
    *,
    rung_counts: str,
    skills_placed: int,
    legal: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.chargen.pyramid_allocated`` — the player allocated the skill
    pyramid (F4a2). ``rung_counts`` is the rating->count census; ``legal`` is the
    validator verdict for the allocation."""
    attributes: dict[str, Any] = {
        "field": "chargen_pyramid_allocated",
        "rung_counts": rung_counts,
        "skills_placed": skills_placed,
        "legal": legal,
        **attrs,
    }
    with Span.open("fate.chargen.pyramid_allocated", attributes, tracer_override=_tracer):
        pass


def fate_chargen_stunts_selected_span(
    *,
    count: int,
    refresh_before: int,
    refresh_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.chargen.stunts_selected`` — the player picked stunts at chargen
    (F4a2). The refresh delta is the economy cost (each stunt over ``free_stunts``
    debits 1 refresh, floored at 1)."""
    attributes: dict[str, Any] = {
        "field": "chargen_stunts_selected",
        "count": count,
        "refresh_before": refresh_before,
        "refresh_after": refresh_after,
        **attrs,
    }
    with Span.open("fate.chargen.stunts_selected", attributes, tracer_override=_tracer):
        pass


def fate_chargen_validated_span(
    *, legal: bool, violations: str, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.chargen.validated`` — the server ran ``validate_fate_sheet`` on
    the candidate sheet (F4a2). Fires on BOTH success and failure so the GM panel
    sees a rejected sheet's violations, not only accepted ones."""
    attributes: dict[str, Any] = {
        "field": "chargen_validated",
        "legal": legal,
        "violations": violations,
        **attrs,
    }
    with Span.open("fate.chargen.validated", attributes, tracer_override=_tracer):
        pass


def fate_chargen_completed_span(
    *,
    aspect_count: int,
    skill_count: int,
    stunt_count: int,
    refresh: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.chargen.completed`` — a legal interactive Fate sheet was built
    and attached (F4a2). The GM-panel census of the finished sheet. Fires only on a
    legal sheet (an illegal one raises after ``fate.chargen.validated``)."""
    attributes: dict[str, Any] = {
        "field": "chargen_completed",
        "aspect_count": aspect_count,
        "skill_count": skill_count,
        "stunt_count": stunt_count,
        "refresh": refresh,
        **attrs,
    }
    with Span.open("fate.chargen.completed", attributes, tracer_override=_tracer):
        pass


def fate_gear_compiled_span(
    *,
    archetype: str,
    aspects_placed: int,
    stunts_added: int,
    permission_aspects: int,
    refresh_before: int,
    refresh_after: int,
    refresh_debited: int,
    actor: str = "",
    gear_ids: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.gear_compiled`` — a character's starting gear was materialized
    onto its FateSheet at chargen (114-10). The GM-panel evidence that gear fired:
    which aspects/stunts were placed and how much refresh the stunt-gear debited
    (the whole balance story). ``gear_ids`` is a comma-joined list of the compiled
    GearDef ids."""
    attributes: dict[str, Any] = {
        "field": "gear_compiled",
        "actor": actor,
        "archetype": archetype,
        "gear_ids": gear_ids,
        "aspects_placed": aspects_placed,
        "stunts_added": stunts_added,
        "permission_aspects": permission_aspects,
        "refresh_before": refresh_before,
        "refresh_after": refresh_after,
        "refresh_debited": refresh_debited,
        **attrs,
    }
    with Span.open("fate.gear_compiled", attributes, tracer_override=_tracer):
        pass


__all__ = [
    "SPAN_FATE_PROJECTION_EMITTED",
    "fate_action_classified_span",
    "fate_action_resolved_span",
    "fate_chargen_archetype_selected_span",
    "fate_chargen_aspects_authored_span",
    "fate_chargen_completed_span",
    "fate_chargen_pyramid_allocated_span",
    "fate_chargen_seeded_span",
    "fate_chargen_stunts_selected_span",
    "fate_chargen_validated_span",
    "fate_aspect_created_span",
    "fate_aspect_invoked_span",
    "fate_compel_accepted_span",
    "fate_compel_offered_span",
    "fate_conceded_span",
    "fate_flavor_rider_span",
    "fate_consequence_taken_span",
    "fate_exchange_committed_span",
    "fate_exchange_order_span",
    "fate_exchange_resolved_span",
    "fate_gear_compiled_span",
    "fate_narration_mismatch_span",
    "fate_opponent_decided_span",
    "fate_point_delta_span",
    "fate_stress_applied_span",
    "fate_taken_out_span",
]
