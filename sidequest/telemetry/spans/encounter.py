"""Encounter spans — phase transitions, beats, dual-track momentum, yield."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

# Routed encounter spans
SPAN_ENCOUNTER_PHASE_TRANSITION = "encounter.phase_transition"
SPAN_ROUTES[SPAN_ENCOUNTER_PHASE_TRANSITION] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.phase_transition",
        # Emission site uses keys "from" and "to".
        "from_phase": (span.attributes or {}).get("from", ""),
        "to_phase": (span.attributes or {}).get("to", ""),
    },
)
SPAN_ENCOUNTER_RESOLVED = "encounter.resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.resolved",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "outcome": (span.attributes or {}).get("outcome", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
# SWN P4 initiative spine: the engine rolls initiative and seats the turn
# order at instantiation. This span is the GM-panel polygraph proving the
# order is engine-rolled (not narrator improv). ``initiative_order`` is the
# human-readable order string (e.g. ``"Rux(9), Raider(5)"``); ``source``
# distinguishes the emission site (e.g. ``"instantiate"``).
SPAN_ENCOUNTER_INITIATIVE_ROLLED = "encounter.initiative_rolled"
SPAN_ROUTES[SPAN_ENCOUNTER_INITIATIVE_ROLLED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.initiative_rolled",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "initiative_order": (span.attributes or {}).get("initiative_order", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
SPAN_ENCOUNTER_BEAT_APPLIED = "encounter.beat_applied"
SPAN_ROUTES[SPAN_ENCOUNTER_BEAT_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.beat_applied",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "metric_delta": (span.attributes or {}).get("metric_delta", 0),
    },
)
SPAN_ENCOUNTER_CONFRONTATION_INITIATED = "encounter.confrontation_initiated"
SPAN_ROUTES[SPAN_ENCOUNTER_CONFRONTATION_INITIATED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.confrontation_initiated",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)
# ADR-116 ("A Confrontation Requires an Other"): confrontation participant
# membership is observable. ``participant.joined`` fires when the engine seats
# an actor (esp. an opponent sourced from the location roster for a chase);
# ``participant.left`` fires when an opponent withdraws and the encounter
# resolves because no Other remains. ``source`` distinguishes router-named
# from location-fallback seating so the GM panel can answer "why is this
# pursuer here?".
SPAN_PARTICIPANT_JOINED = "participant.joined"
SPAN_ROUTES[SPAN_PARTICIPANT_JOINED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "participant.joined",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "name": (span.attributes or {}).get("name", ""),
        "side": (span.attributes or {}).get("side", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
SPAN_PARTICIPANT_LEFT = "participant.left"
SPAN_ROUTES[SPAN_PARTICIPANT_LEFT] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "participant.left",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "name": (span.attributes or {}).get("name", ""),
        "side": (span.attributes or {}).get("side", ""),
        "reason": (span.attributes or {}).get("reason", ""),
    },
)
SPAN_ENCOUNTER_EMPTY_ACTOR_LIST = "encounter.empty_actor_list"
SPAN_ROUTES[SPAN_ENCOUNTER_EMPTY_ACTOR_LIST] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.empty_actor_list",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
    },
)
# Story 45-33: a combat encounter that resolves to zero opponents post-fallback
# is the original Playtest 3 (Orin) bug shape — narrator emits ``confrontation=combat``
# but neither the explicit ``npcs_present`` nor the location-scoped registry
# fallback yields an opponent. CLAUDE.md "No Silent Fallbacks" requires the
# pipeline to refuse this state and surface the lie-detector signal here so
# the GM panel can show Sebastien the guard engaged rather than the narrator
# improvising around an empty encounter.
SPAN_ENCOUNTER_NO_OPPONENT_AVAILABLE = "encounter.no_opponent_available"
SPAN_ROUTES[SPAN_ENCOUNTER_NO_OPPONENT_AVAILABLE] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.no_opponent_available",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "category": (span.attributes or {}).get("category", ""),
        # Story 45-52 silent-failure detector: ``False`` means the
        # location-scoped fallback returned [] because the player had no
        # resolved location, not because no NPCs were at that location.
        # Discriminates a genuine empty-scene from an unresolved-perspective
        # bug shape that would otherwise look identical on the GM panel.
        "location_available": (span.attributes or {}).get("location_available", False),
    },
)
# Playtest 2026-05-08: narrator staged a multi-NPC scene (drift gang pack) and
# triggered the ``dogfight`` sealed-letter encounter, whose 1v1 red/blue
# contract refused 3 npcs_present and crashed the turn (auto-save + reconnect
# = sticky crash loop). Guard now declines the instantiation gracefully and
# fires this span so the GM panel can see "narrator picked sealed-letter for
# pack scene; engine declined" rather than the narrator's prose proceeding
# without any signal that the mechanic disengaged.
SPAN_ENCOUNTER_SEALED_LETTER_ARITY_REJECTED = "encounter.sealed_letter_arity_rejected"
SPAN_ROUTES[SPAN_ENCOUNTER_SEALED_LETTER_ARITY_REJECTED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.sealed_letter_arity_rejected",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "npc_count": (span.attributes or {}).get("npc_count", 0),
    },
)
SPAN_ENCOUNTER_BEAT_FAILURE_BRANCH = "encounter.beat_failure_branch"
SPAN_ROUTES[SPAN_ENCOUNTER_BEAT_FAILURE_BRANCH] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.beat_failure_branch",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "base_delta": (span.attributes or {}).get("base_delta", 0),
        "failure_delta": (span.attributes or {}).get("failure_delta", 0),
    },
)
SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED = "encounter.opposed_roll_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opposed_roll_resolved",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "player_roll": (span.attributes or {}).get("player_roll", 0),
        "player_mod": (span.attributes or {}).get("player_mod", 0),
        "opponent_roll": (span.attributes or {}).get("opponent_roll", 0),
        "opponent_mod": (span.attributes or {}).get("opponent_mod", 0),
        "player_num_advantage": (span.attributes or {}).get("player_num_advantage", 0),
        "opponent_num_advantage": (span.attributes or {}).get("opponent_num_advantage", 0),
        "shift": (span.attributes or {}).get("shift", 0),
        "tier": (span.attributes or {}).get("tier", ""),
    },
)

# Story 71-21: server-driven opponent attack (SWN hp_depletion enemy turn).
# Fires on EVERY opponent reprisal — hit or miss — carrying the full to-hit math
# so the GM panel can tell a real, mechanically-backed enemy attack from narrator
# improv. The playtest bug: perseus_cloud hp_depletion combat had no enemy turn,
# so the player could never lose; this span is the lie-detector for the fix.
SPAN_ENCOUNTER_OPPONENT_ATTACK = "encounter.opponent_attack_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPONENT_ATTACK] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opponent_attack_resolved",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "attacker": (span.attributes or {}).get("attacker", ""),
        "target": (span.attributes or {}).get("target", ""),
        "d20": (span.attributes or {}).get("d20", 0),
        "modifier": (span.attributes or {}).get("modifier", 0),
        "attack_total": (span.attributes or {}).get("attack_total", 0),
        "target_ac": (span.attributes or {}).get("target_ac", 0),
        "hit": (span.attributes or {}).get("hit", False),
    },
)

# Story 45-3: Mid-turn momentum broadcast lie-detector. Fires whenever the
# server emits a CONFRONTATION frame carrying post-mutation momentum, so
# the GM panel can audit "the dial moved on screen because the engine
# moved a metric, not because the narrator improvised matching prose."
# ``source`` distinguishes the dice-throw site from the post-narration
# site; ``beat_id`` is non-null on dice_throw and may be null on
# narration_apply emits.
SPAN_ENCOUNTER_MOMENTUM_BROADCAST = "encounter.momentum_broadcast"
SPAN_ROUTES[SPAN_ENCOUNTER_MOMENTUM_BROADCAST] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.momentum_broadcast",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "player_metric_after": (span.attributes or {}).get("player_metric_after", 0),
        "opponent_metric_after": (span.attributes or {}).get("opponent_metric_after", 0),
        "source": (span.attributes or {}).get("source", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
    },
)

# Dual-track momentum constants — flat-only baseline; routes land with the
# GM-panel encounter timeline rollout.
SPAN_ENCOUNTER_BEAT_SKIPPED = "encounter.beat_skipped"
# C&C B/X class beats Task 7 — fires every time the narrator prompt is built
# with a class-filtered per-PC beat menu. Lets the GM panel verify the filter
# is wired, not just defined (CLAUDE.md OTEL-on-every-subsystem discipline).
SPAN_CONFRONTATION_BEAT_FILTER = "confrontation.beat_filter"
SPAN_ENCOUNTER_INVALID_SIDE = "encounter.invalid_side"
SPAN_ENCOUNTER_INVALID_OUTCOME_TIER = "encounter.invalid_outcome_tier"
SPAN_ENCOUNTER_METRIC_ADVANCE = "encounter.metric_advance"
SPAN_ENCOUNTER_TAG_CREATED = "encounter.tag_created"
SPAN_ENCOUNTER_TAG_BACKFIRE = "encounter.tag_backfire"
SPAN_ENCOUNTER_STATUS_ADDED = "encounter.status_added"
SPAN_ENCOUNTER_STATUS_CLEARED = "encounter.status_cleared"
SPAN_ENCOUNTER_YIELD_RECEIVED = "encounter.yield_received"
SPAN_ENCOUNTER_YIELD_RESOLVED = "encounter.yield_resolved"
SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED = "encounter.resolution_signal_emitted"
SPAN_ENCOUNTER_RESOLUTION_SIGNAL_CONSUMED = "encounter.resolution_signal_consumed"
# ADR-078 §4 — composure-break resolution + per-beat edge debits.
SPAN_ENCOUNTER_EDGE_DEBIT = "encounter.edge_debit"
SPAN_ENCOUNTER_COMPOSURE_BREAK = "encounter.composure_break"

FLAT_ONLY_SPANS.update(
    {
        SPAN_ENCOUNTER_BEAT_SKIPPED,
        SPAN_ENCOUNTER_INVALID_SIDE,
        SPAN_ENCOUNTER_INVALID_OUTCOME_TIER,
        SPAN_ENCOUNTER_METRIC_ADVANCE,
        SPAN_ENCOUNTER_TAG_CREATED,
        SPAN_ENCOUNTER_TAG_BACKFIRE,
        SPAN_ENCOUNTER_STATUS_ADDED,
        SPAN_ENCOUNTER_STATUS_CLEARED,
        SPAN_ENCOUNTER_YIELD_RECEIVED,
        SPAN_ENCOUNTER_YIELD_RESOLVED,
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED,
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_CONSUMED,
    }
)
# Promoted from flat-only — combat resolution lie-detector (sprint 3 cold-subsystem
# audit). Without typed events the GM panel's state_transition tab can't show damage
# application; the events sat in the firehose as agent_span_close only.
SPAN_ROUTES[SPAN_ENCOUNTER_EDGE_DEBIT] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "encounter.edge_debit",
        "source_actor": (span.attributes or {}).get("source_actor", ""),
        "target_actor": (span.attributes or {}).get("target_actor", ""),
        "debit_kind": (span.attributes or {}).get("debit_kind", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "before": (span.attributes or {}).get("before", 0),
        "after": (span.attributes or {}).get("after", 0),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "target_select": (span.attributes or {}).get("target_select", ""),
        "taunt_redirected": (span.attributes or {}).get("taunt_redirected", False),
    },
)
SPAN_ROUTES[SPAN_ENCOUNTER_COMPOSURE_BREAK] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "encounter.composure_break",
        "char_name": (span.attributes or {}).get("char_name", ""),
        "side": (span.attributes or {}).get("side", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
    },
)
# C&C B/X Task 12 — promote beat_filter from flat-only to routed so the GM
# panel's state_transition tab shows filter decisions (lie-detector discipline).
SPAN_ROUTES[SPAN_CONFRONTATION_BEAT_FILTER] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "beat_filter",
        "actor": (span.attributes or {}).get("actor", ""),
        "character_class": (span.attributes or {}).get("class_name", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
        "pool_size": (span.attributes or {}).get("pool_size", 0),
        "filtered_size": (span.attributes or {}).get("filtered_size", 0),
        "beat_ids": (span.attributes or {}).get("available_beat_ids", ""),
        "spell_slots_remaining": (span.attributes or {}).get("spell_slots_remaining", 0.0),
    },
)
# Story 59-16 — fail-loud when a SEATED, connected PC cannot be resolved to a
# class for the single filtered CONFRONTATION delivery. The encounter layer
# must NEVER fall back to the unfiltered union for that socket; it fires this
# ERROR span and sends nothing (a lobby/unseated socket is a different, silent
# case). The GM panel surfaces it as a delivery failure, not a clean turn.
SPAN_CONFRONTATION_RECIPIENT_UNRESOLVED = "confrontation.recipient_unresolved"
SPAN_ROUTES[SPAN_CONFRONTATION_RECIPIENT_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.recipient_unresolved",
        "player_id": (span.attributes or {}).get("player_id", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
    },
)


@contextmanager
def confrontation_recipient_unresolved_span(
    *,
    player_id: str,
    actor: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``confrontation.recipient_unresolved`` ERROR span for a
    seated, connected PC whose class could not be resolved. Marks the span
    status ERROR so the GM panel surfaces the delivery failure — never a
    silent fallback to the unfiltered union (Story 59-16, CLAUDE.md No
    Silent Fallbacks)."""
    with Span.open(
        SPAN_CONFRONTATION_RECIPIENT_UNRESOLVED,
        {"player_id": player_id, "actor": actor, "reason": reason, **attrs},
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span


@contextmanager
def encounter_phase_transition_span(
    *,
    from_phase: str,
    to_phase: str,
    encounter_type: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_PHASE_TRANSITION,
        {"from": from_phase, "to": to_phase, "encounter_type": encounter_type, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_resolved_span(
    *,
    encounter_type: str,
    outcome: str | None,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    span_attrs = {"encounter_type": encounter_type, "source": source}
    if outcome is not None:
        span_attrs["outcome"] = outcome
    span_attrs.update(attrs)
    with Span.open(SPAN_ENCOUNTER_RESOLVED, span_attrs, tracer_override=_tracer) as span:
        yield span


@contextmanager
def encounter_initiative_rolled_span(
    *,
    encounter_type: str,
    initiative_order: str,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """SWN P4 polygraph: the engine rolled initiative and seated the turn
    order. ``initiative_order`` is the human-readable order string (e.g.
    ``"Rux(9), Raider(5)"``); ``source`` names the emission site. Missing
    span on a SWN encounter → initiative wasn't engine-rolled → the narrator
    is improvising the order."""
    with Span.open(
        SPAN_ENCOUNTER_INITIATIVE_ROLLED,
        {
            "encounter_type": encounter_type,
            "initiative_order": initiative_order,
            "source": source,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_beat_applied_span(
    *,
    encounter_type: str,
    actor: str,
    beat_id: str,
    metric_delta: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_BEAT_APPLIED,
        {
            "encounter_type": encounter_type,
            "actor": actor,
            "beat_id": beat_id,
            "metric_delta": metric_delta,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_confrontation_initiated_span(
    *,
    encounter_type: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_CONFRONTATION_INITIATED,
        {"encounter_type": encounter_type, "genre_slug": genre_slug, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def participant_joined_span(
    *,
    encounter_type: str,
    name: str,
    side: str,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-116: an actor was seated into a confrontation. ``source`` is
    ``router_named`` or ``location_fallback`` so the GM panel can audit
    where a (esp. opponent-side) participant came from."""
    with Span.open(
        SPAN_PARTICIPANT_JOINED,
        {
            "encounter_type": encounter_type,
            "name": name,
            "side": side,
            "source": source,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def participant_left_span(
    *,
    encounter_type: str,
    name: str,
    side: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-116: an opponent withdrew. Emitted alongside end-on-no-Other
    resolution so the GM panel sees the confrontation ended because the
    Other left, not because a dial hit threshold."""
    with Span.open(
        SPAN_PARTICIPANT_LEFT,
        {
            "encounter_type": encounter_type,
            "name": name,
            "side": side,
            "reason": reason,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opposed_roll_resolved_span(
    *,
    encounter_type: str,
    player_roll: int,
    player_mod: int,
    opponent_roll: int,
    opponent_mod: int,
    shift: int,
    tier: str,
    player_num_advantage: int = 0,
    opponent_num_advantage: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lie-detector for the opposed-check path. Emit BEFORE apply_beat so the
    GM panel can correlate the engine-derived tier with the metric_advance.

    ``player_num_advantage`` / ``opponent_num_advantage`` carry the side-
    aggregate numerical-advantage modifiers from
    ``numerical_advantage_for``. Default to 0 for back-compat with legacy
    callers; production sites pass the values from ``OpposedRollResult``.
    """
    with Span.open(
        SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED,
        {
            "encounter_type": encounter_type,
            "player_roll": int(player_roll),
            "player_mod": int(player_mod),
            "opponent_roll": int(opponent_roll),
            "opponent_mod": int(opponent_mod),
            "player_num_advantage": int(player_num_advantage),
            "opponent_num_advantage": int(opponent_num_advantage),
            "shift": int(shift),
            "tier": tier,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opponent_attack_resolved_span(
    *,
    encounter_type: str,
    attacker: str,
    target: str,
    d20: int,
    modifier: int,
    attack_total: int,
    target_ac: int,
    hit: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lie-detector for the server-driven opponent attack turn (story 71-21,
    SWN hp_depletion combat). Emit on every reprisal so the GM panel can audit
    that the enemy's "shot" was a real d20-vs-AC decision, not narrator prose.
    ``hit`` is the verdict; ``attack_total`` == ``d20`` + ``modifier``."""
    with Span.open(
        SPAN_ENCOUNTER_OPPONENT_ATTACK,
        {
            "encounter_type": encounter_type,
            "attacker": attacker,
            "target": target,
            "d20": int(d20),
            "modifier": int(modifier),
            "attack_total": int(attack_total),
            "target_ac": int(target_ac),
            "hit": bool(hit),
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_empty_actor_list_span(
    *,
    encounter_type: str,
    genre_slug: str,
    player_name: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Narrator named adversaries in prose but the JSON game_patch omitted them."""
    with Span.open(
        SPAN_ENCOUNTER_EMPTY_ACTOR_LIST,
        {
            "encounter_type": encounter_type,
            "genre_slug": genre_slug,
            "player_name": player_name,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_no_opponent_available_span(
    *,
    encounter_type: str,
    genre_slug: str,
    player_name: str,
    category: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 45-33: combat encounter has no opponent post-fallback — guard fired.

    Distinct from ``encounter_empty_actor_list_span`` which fires when the
    narrator named adversaries in prose but the JSON extraction dropped
    them. This span fires the layer above: the entire instantiation path
    (explicit ``npcs_present`` AND the location-scoped registry fallback)
    produced zero opponents for a category=combat encounter, and the
    pipeline is refusing to advance.
    """
    with Span.open(
        SPAN_ENCOUNTER_NO_OPPONENT_AVAILABLE,
        {
            "encounter_type": encounter_type,
            "genre_slug": genre_slug,
            "player_name": player_name,
            "category": category,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_sealed_letter_arity_rejected_span(
    *,
    encounter_type: str,
    genre_slug: str,
    player_name: str,
    npc_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Playtest 2026-05-08: sealed-letter encounter (1v1 red/blue) declined
    because the narrator staged a multi-NPC scene. The encounter does NOT
    instantiate; the narration turn continues without a structured mechanic.
    GM panel reads this span as the lie-detector signal that the engine saw
    the inappropriate selection and refused, rather than improvising a 1v1
    against a pack.
    """
    with Span.open(
        SPAN_ENCOUNTER_SEALED_LETTER_ARITY_REJECTED,
        {
            "encounter_type": encounter_type,
            "genre_slug": genre_slug,
            "player_name": player_name,
            "npc_count": npc_count,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_beat_skipped_span(
    *,
    reason: str,
    actor: str,
    actor_side: str,
    beat_id: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_BEAT_SKIPPED,
        {"reason": reason, "actor": actor, "actor_side": actor_side, "beat_id": beat_id, **attrs},
    ) as s:
        yield s


@contextmanager
def confrontation_beat_filter_span(
    *,
    actor: str,
    class_name: str,
    confrontation_type: str,
    available_beat_ids: str,
    spell_slots_remaining: float,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted once per PC actor when the narrator prompt renders a
    class-filtered beat menu. Lets the GM panel verify class-distinct beat
    menus are actually shipped to the LLM (§5.6 criterion 1)."""
    with Span.open(
        SPAN_CONFRONTATION_BEAT_FILTER,
        {
            "actor": actor,
            "class_name": class_name,
            "confrontation_type": confrontation_type,
            "available_beat_ids": available_beat_ids,
            "spell_slots_remaining": spell_slots_remaining,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_invalid_side_span(
    *,
    actor_name: str,
    declared_side: str,
    valid_set: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_INVALID_SIDE,
        {"actor_name": actor_name, "declared_side": declared_side, "valid_set": valid_set, **attrs},
    ) as s:
        yield s


@contextmanager
def encounter_invalid_outcome_tier_span(
    *,
    beat_id: str,
    actor: str,
    declared_tier: str,
    valid_set: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_INVALID_OUTCOME_TIER,
        {
            "beat_id": beat_id,
            "actor": actor,
            "declared_tier": declared_tier,
            "valid_set": valid_set,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_metric_advance_span(
    *,
    side: str,
    delta_kind: str,
    delta: int,
    before: int,
    after: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_METRIC_ADVANCE,
        {
            "side": side,
            "delta_kind": delta_kind,
            "delta": delta,
            "before": before,
            "after": after,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_momentum_broadcast_span(
    *,
    encounter_type: str,
    player_metric_after: int,
    opponent_metric_after: int,
    source: str,
    beat_id: str | None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 45-3: Lie-detector for the mid-turn CONFRONTATION emit.

    Wraps every server site that broadcasts a CONFRONTATION frame
    carrying post-mutation momentum. ``source`` is ``"dice_throw"`` from
    the dice-dispatch site and ``"narration_apply"`` from the post-
    narration site. ``beat_id`` is the resolved beat on the dice path
    and may be ``None`` for narrator-driven emits.
    """
    with Span.open(
        SPAN_ENCOUNTER_MOMENTUM_BROADCAST,
        {
            "encounter_type": encounter_type,
            "player_metric_after": player_metric_after,
            "opponent_metric_after": opponent_metric_after,
            "source": source,
            "beat_id": beat_id or "",
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_tag_created_span(
    *,
    tag_text: str,
    created_by: str,
    target: str | None,
    leverage: int,
    fleeting: bool,
    created_via: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_TAG_CREATED,
        {
            "tag_text": tag_text,
            "created_by": created_by,
            "target": target or "",
            "leverage": leverage,
            "fleeting": fleeting,
            "created_via": created_via,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_tag_backfire_span(
    *,
    tag_text: str,
    created_by: str,
    target: str,
    triggering_beat: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_TAG_BACKFIRE,
        {
            "tag_text": tag_text,
            "created_by": created_by,
            "target": target,
            "triggering_beat": triggering_beat,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_status_added_span(
    *,
    actor: str,
    text: str,
    severity: str,
    source: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_STATUS_ADDED,
        {"actor": actor, "text": text, "severity": severity, "source": source, **attrs},
    ) as s:
        yield s


@contextmanager
def encounter_status_cleared_span(
    *,
    actor: str,
    text: str,
    severity: str,
    reason: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``reason``: ``scene_end`` | ``narrator_clear`` | ``location_change``."""
    with Span.open(
        SPAN_ENCOUNTER_STATUS_CLEARED,
        {"actor": actor, "text": text, "severity": severity, "reason": reason, **attrs},
    ) as s:
        yield s


@contextmanager
def encounter_yield_received_span(
    *,
    player_id: str,
    actor_name: str,
    prior_player_metric: int,
    prior_opponent_metric: int,
    statuses_taken_this_encounter: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_YIELD_RECEIVED,
        {
            "player_id": player_id,
            "actor_name": actor_name,
            "prior_player_metric": prior_player_metric,
            "prior_opponent_metric": prior_opponent_metric,
            "statuses_taken_this_encounter": statuses_taken_this_encounter,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_yield_resolved_span(
    *,
    outcome: str,
    yielded_actors: tuple[str, ...],
    edge_refreshed: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_YIELD_RESOLVED,
        {
            "outcome": outcome,
            "yielded_actors": ",".join(yielded_actors),
            "edge_refreshed": edge_refreshed,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_resolution_signal_emitted_span(
    *,
    outcome: str,
    final_player_metric: int,
    final_opponent_metric: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED,
        {
            "outcome": outcome,
            "final_player_metric": final_player_metric,
            "final_opponent_metric": final_opponent_metric,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_resolution_signal_consumed_span(
    *,
    outcome: str,
    final_player_metric: int,
    final_opponent_metric: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_CONSUMED,
        {
            "outcome": outcome,
            "final_player_metric": final_player_metric,
            "final_opponent_metric": final_opponent_metric,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_edge_debit_span(
    *,
    source_actor: str,
    target_actor: str,
    debit_kind: str,
    delta: int,
    before: int,
    after: int,
    beat_id: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Per-beat edge debit (ADR-078 §3-4).

    ``debit_kind``: ``"self"`` (acting actor pays) | ``"target"`` (opposing
    actor takes the hit). ``before``/``after`` are clamped current values.
    Lie-detector for "did the engine actually debit edge or did the
    narrator just describe a wound." Always one span per beat per debit.
    """
    with Span.open(
        SPAN_ENCOUNTER_EDGE_DEBIT,
        {
            "source_actor": source_actor,
            "target_actor": target_actor,
            "debit_kind": debit_kind,
            "delta": delta,
            "before": before,
            "after": after,
            "beat_id": beat_id,
            **attrs,
        },
    ) as s:
        yield s


SPAN_ENCOUNTER_CHECK_RESOLVED = "encounter.check_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_CHECK_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.check_resolved",
        "kind": (span.attributes or {}).get("check.kind", ""),
        "actor": (span.attributes or {}).get("check.actor", ""),
        "label": (span.attributes or {}).get("check.label", ""),
        "total": (span.attributes or {}).get("check.total", 0),
        "difficulty": (span.attributes or {}).get("check.difficulty", 0),
        "outcome": (span.attributes or {}).get("check.outcome", ""),
    },
)
SPAN_ENCOUNTER_SAVING_THROW_RESOLVED = "encounter.saving_throw_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_SAVING_THROW_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.saving_throw",
        "defender_actor": (span.attributes or {}).get("defender_actor", ""),
        "defender_class": (span.attributes or {}).get("defender_class", ""),
        "category": (span.attributes or {}).get("category", ""),
        "threat_label": (span.attributes or {}).get("threat_label", ""),
        "target": (span.attributes or {}).get("target", 0),
        "roll": (span.attributes or {}).get("roll", 0),
        "total": (span.attributes or {}).get("total", 0),
        "shift": (span.attributes or {}).get("shift", 0),
        "tier": (span.attributes or {}).get("tier", ""),
        "mindless_gate": (span.attributes or {}).get("mindless_gate", False),
        "spell_id": (span.attributes or {}).get("spell_id", ""),
    },
)


@contextmanager
def encounter_saving_throw_resolved_span(
    *,
    defender_actor: str,
    defender_class: str,
    category: str,
    ability: str | None,
    threat_label: str,
    target: int,
    roll: int,
    mod: int,
    total: int,
    shift: int,
    tier: str,
    spell_id: str,
    encounter_type: str,
    mindless_gate: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Span emitted on every saving-throw resolution.

    The lie-detector for B/X save resolution: missing span → save
    subsystem isn't engaged → narrator is improvising the save outcome.

    ``mindless_gate=True`` indicates the save was SKIPPED because the
    target was mindless and the spell had ``requires_mind=True``;
    in that case ``roll/total/shift/tier`` are zero/empty and only
    the gate decision is logged.
    """
    span_attrs: dict[str, Any] = {
        "defender_actor": defender_actor,
        "defender_class": defender_class,
        "category": category,
        "threat_label": threat_label,
        "target": target,
        "roll": roll,
        "mod": mod,
        "total": total,
        "shift": shift,
        "tier": tier,
        "spell_id": spell_id,
        "encounter_type": encounter_type,
        "mindless_gate": mindless_gate,
        **attrs,
    }
    if ability is not None:
        span_attrs["ability"] = ability
    with Span.open(
        SPAN_ENCOUNTER_SAVING_THROW_RESOLVED,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


# Story 2026-05-10: Taunt mechanic — force enemy attention.
# See docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md §8.
SPAN_ENCOUNTER_TAUNT_ACTIVATED = "encounter.taunt.activated"
SPAN_ROUTES[SPAN_ENCOUNTER_TAUNT_ACTIVATED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.taunt",
        "op": "activated",
        "actor_id": (span.attributes or {}).get("actor_id", ""),
        "round": (span.attributes or {}).get("round", 0),
    },
)
SPAN_ENCOUNTER_TAUNT_EXPIRED = "encounter.taunt.expired"
SPAN_ROUTES[SPAN_ENCOUNTER_TAUNT_EXPIRED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.taunt",
        "op": "expired",
        "actor_id": (span.attributes or {}).get("actor_id", ""),
        "round": (span.attributes or {}).get("round", 0),
    },
)


@contextmanager
def encounter_composure_break_span(
    *,
    char_name: str,
    side: str,
    beat_id: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Composure break — edge dropped to 0 (ADR-078 §4).

    ``side``: ``"self"`` if the acting actor broke, ``"target"`` if the
    opposing actor broke. Triggers ``encounter.resolved=True`` in the
    caller; this span fires before the encounter-level resolution.
    """
    with Span.open(
        SPAN_ENCOUNTER_COMPOSURE_BREAK,
        {
            "char_name": char_name,
            "side": side,
            "beat_id": beat_id,
            **attrs,
        },
    ) as s:
        yield s


def check_resolved_span(
    *,
    kind: str,
    actor: str,
    label: str,
    total: int,
    difficulty: int,
    outcome: str,
    **attrs: Any,
) -> None:
    """Span for a non-beat SWN check/save — the GM-panel polygraph for free rolls.

    ``kind``: ``"skill_check"`` | ``"save"``. ``actor`` is the character name.
    ``outcome`` is the ``RollOutcome`` value string (e.g. ``"Success"``).
    Plain function, not a contextmanager — emits and closes immediately.
    """
    with Span.open(
        SPAN_ENCOUNTER_CHECK_RESOLVED,
        {
            "check.kind": kind,
            "check.actor": actor,
            "check.label": label,
            "check.total": total,
            "check.difficulty": difficulty,
            "check.outcome": outcome,
            **attrs,
        },
    ):
        pass
