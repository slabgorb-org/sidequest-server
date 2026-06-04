"""NPC registry spans — auto-registration and identity drift."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

# Port-artifact constants — kept flat-only.
SPAN_NPC_MERGE_PATCH = "npc_merge_patch"
SPAN_NPC_REGISTRATION = "npc.registration"

FLAT_ONLY_SPANS.update({SPAN_NPC_MERGE_PATCH, SPAN_NPC_REGISTRATION})

# Live spans (NPC bundle).
SPAN_NPC_AUTO_REGISTERED = "npc.auto_registered"
SPAN_ROUTES[SPAN_NPC_AUTO_REGISTERED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_registry",
        "op": "auto_registered",
        "name": (span.attributes or {}).get("npc_name", ""),
        "pronouns": (span.attributes or {}).get("pronouns", ""),
        "role": (span.attributes or {}).get("role", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
        "registry_len": (span.attributes or {}).get("registry_len", 0),
    },
)
# Playtest 2026-04-29: PC names appearing in narration must NOT promote the PC
# into the NPC registry. The skip span lets the GM panel verify the filter
# fired (and surfaces "the narrator named your party member" events for
# Sebastien's mechanical visibility).
SPAN_NPC_PC_NAME_SKIPPED = "npc.pc_name_skipped"
SPAN_ROUTES[SPAN_NPC_PC_NAME_SKIPPED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_registry",
        "op": "pc_name_skipped",
        "name": (span.attributes or {}).get("npc_name", ""),
        "matched_pc": (span.attributes or {}).get("matched_pc", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

SPAN_NPC_REINVENTED = "npc.reinvented"
SPAN_ROUTES[SPAN_NPC_REINVENTED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_registry",
        "op": "reinvented",
        "name": (span.attributes or {}).get("npc_name", ""),
        "drift_field": (span.attributes or {}).get("drift_field", ""),
        "expected": (span.attributes or {}).get("expected", ""),
        "narrator": (span.attributes or {}).get("narrator", ""),
        # Story 72-7: whether the disagreeing value was written onto the
        # canonical entry (True) or merely observed (False).
        "applied": (span.attributes or {}).get("applied", False),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Wave 2A (story 45-47): every narrator-cite of an NPC name fires this span.
# ``match_strategy`` is the lie-detector dial: ``npcs_hit`` means the cited
# name had an active stateful Npc; ``pool_hit`` means the cite matched a
# known pool member; ``invented`` means the narrator made up a name not in
# either store. Per-session counts of ``invented`` tell the GM panel when
# the pool wasn't deep enough or wasn't seeded for the scene.
SPAN_NPC_REFERENCED = "npc.referenced"
SPAN_ROUTES[SPAN_NPC_REFERENCED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "referenced",
        "name": (span.attributes or {}).get("npc_name", ""),
        "match_strategy": (span.attributes or {}).get("match_strategy", ""),
        "pool_origin": (span.attributes or {}).get("pool_origin", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 72-1: interest-driven development tick — fires on every
# non-transactional engagement (an ``npcs_hit`` cite that resolves to a
# stateful ``Npc``). The lie-detector signal: the GM panel sees the engine
# *counting* interest and escalating ``resolution_tier``, so NPC depth is a
# mechanically-grounded fact rather than narrator improv (ADR-014 / ADR-020).
# Fires every engagement, including ticks that increment the counter without
# crossing a tier threshold (old == new). ``npc_name`` avoids the OTEL-reserved
# ``name`` span attribute.
SPAN_NPC_DEVELOPED = "npc.developed"
SPAN_ROUTES[SPAN_NPC_DEVELOPED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npcs",
        "op": "developed",
        "name": (span.attributes or {}).get("npc_name", ""),
        "non_transactional_interactions": (span.attributes or {}).get(
            "non_transactional_interactions", 0
        ),
        "resolution_tier_before": (span.attributes or {}).get("resolution_tier_before", ""),
        "resolution_tier_after": (span.attributes or {}).get("resolution_tier_after", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 45-53: recurring-presence detector — fires when narration prose
# names a known recurring NPC (in snapshot.npcs or snapshot.npc_pool) but
# the narrator failed to emit them in npcs_present. The lie-detector
# signal: per-session counts of these misses tell the GM panel when the
# narrator is "forgetting" recurring characters between turns.
SPAN_NPC_RECURRING_PRESENCE_MISSED = "npc.recurring_presence_missed"
SPAN_ROUTES[SPAN_NPC_RECURRING_PRESENCE_MISSED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        # ``field`` mirrors ``source``: stateful Npc misses surface as
        # ``npc_registry`` (parallel to npc.auto_registered / npc.reinvented),
        # pool-only misses surface as ``npc_pool`` (parallel to npc.referenced).
        # The GM panel filters on ``field``; mis-routing would put npcs-sourced
        # misses in the wrong column.
        "field": "npc_registry" if (span.attributes or {}).get("source") == "npcs" else "npc_pool",
        "op": "recurring_presence_missed",
        "name": (span.attributes or {}).get("npc_name", ""),
        "source": (span.attributes or {}).get("source", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
        "last_seen_turn": (span.attributes or {}).get("last_seen_turn", 0),
    },
)

# Story 49-2: prose-only auto-mint — fires when narrator names a person via
# role (Father, mother, the doctor, etc.) or honorific (Mrs. <Name>) in
# narration prose but omits them from npcs_present. Distinct from
# npc.auto_registered (which fires for structured-patch mints) so the GM
# panel can tell which path minted any given NPC.
SPAN_NPC_AUTO_MINTED_FROM_PROSE = "npc.auto_minted_from_prose"
SPAN_ROUTES[SPAN_NPC_AUTO_MINTED_FROM_PROSE] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "auto_minted_from_prose",
        "name": (span.attributes or {}).get("npc_name", ""),
        "role": (span.attributes or {}).get("role", ""),
        "pronouns": (span.attributes or {}).get("pronouns", ""),
        "source": (span.attributes or {}).get("source", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 49-2 (Reviewer rework): prose-only auto-mint skip path — fires when
# the auto-minter declines to mint a role/honorific because pronouns are
# ambiguous or because a gender-paired role conflict exists in the roster.
# Required by CLAUDE.md OTEL Observability Principle so the GM panel can
# see when the system bites its tongue. The ``reason`` attribute is the
# discriminator: ``ambiguous_pronouns_role`` / ``ambiguous_pronouns_
# honorific`` / ``gender_paired_conflict``.
SPAN_NPC_AUTO_MINT_SKIPPED = "npc.auto_mint_skipped"
SPAN_ROUTES[SPAN_NPC_AUTO_MINT_SKIPPED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "auto_mint_skipped",
        "name": (span.attributes or {}).get("npc_name", ""),
        "role": (span.attributes or {}).get("role", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 49-6: ratification gate — fires once per turn for each pool member
# that was auto-minted from prose on a prior turn (``observation_pending=True``).
# Promote fires when the narrator re-cites the member this turn (member stays
# in pool with flag cleared); purge fires when the narrator omits the member
# (member is removed from pool entirely). Two distinct spans so the GM panel
# can render ratifications and drops as separate event streams.
SPAN_NPC_OBSERVATION_GATE_PROMOTED = "npc.observation_gate_promoted"
SPAN_ROUTES[SPAN_NPC_OBSERVATION_GATE_PROMOTED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "observation_gate_promoted",
        "name": (span.attributes or {}).get("npc_name", ""),
        "role": (span.attributes or {}).get("role", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

SPAN_NPC_OBSERVATION_GATE_PURGED = "npc.observation_gate_purged"
SPAN_ROUTES[SPAN_NPC_OBSERVATION_GATE_PURGED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "observation_gate_purged",
        "name": (span.attributes or {}).get("npc_name", ""),
        "role": (span.attributes or {}).get("role", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 72-10: ordering-invariant violation. The ratification gate
# (``_apply_npc_observation_gate``) resolves EVERY prior-turn
# ``observation_pending`` pool member — promote or purge — so by the time
# ``_auto_mint_prose_only_npcs`` runs the pool must hold zero pending members.
# A surviving pending member at the mint call site proves the gate did not run
# first this turn: a reordering regression that silently turns the gate into a
# no-op and reopens the phantom-NPC failure mode (the 2026-05-11 Glenross
# invented-Mother slip). Fired at ``severity="warning"`` — mirroring
# ``npc.observation_gate_purged`` — so the GM panel surfaces the regression as a
# soft alert before the runtime invariant raises. Per CLAUDE.md "No Silent
# Fallbacks": the broken pipeline must be loud, not degrade into baseless drift.
SPAN_NPC_OBSERVATION_GATE_ORDER_VIOLATION = "npc.observation_gate_order_violation"
SPAN_ROUTES[SPAN_NPC_OBSERVATION_GATE_ORDER_VIOLATION] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "observation_gate_order_violation",
        "pending_count": (span.attributes or {}).get("pending_count", 0),
        "pending_names": (span.attributes or {}).get("pending_names", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 72-4: narrator-invented NPC names routed through the ADR-091
# culture-bound generator. When the narrator invents an NPC mid-scene it hands
# back a bare name string; the Step-3 "novel" branch of ``_apply_npc_mentions``
# now mints a culture-true *generated* name instead. This span is the
# provenance record the GM panel reads to confirm the route fired — without it
# you cannot tell a culture-bound mint from the narrator's raw improvisation.
# ``collision_reroll`` records whether ``has_stem_collision`` (or a hit against
# an existing store member) forced a re-roll past a bad candidate.
SPAN_NPC_INVENTED_NAME_ROUTED = "npc.invented_name_routed"
SPAN_ROUTES[SPAN_NPC_INVENTED_NAME_ROUTED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "invented_name_routed",
        "name": (span.attributes or {}).get("npc_name", ""),
        "original_name": (span.attributes or {}).get("original_name", ""),
        "culture": (span.attributes or {}).get("culture", ""),
        "culture_source": (span.attributes or {}).get("culture_source", ""),
        "collision_reroll": (span.attributes or {}).get("collision_reroll", False),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 72-4 (No Silent Fallbacks): the narrator invented a name but the active
# world resolved no culture (or the generator could not produce a name), so the
# route could NOT run. The engine must fail LOUD rather than silently mint the
# raw narrator string with no signal — ``severity="warning"`` renders this as a
# GM-panel alert. ``reason`` discriminates ``no_culture_bound`` vs
# ``generation_failed``.
SPAN_NPC_INVENTED_NAME_UNROUTED = "npc.invented_name_unrouted"
SPAN_ROUTES[SPAN_NPC_INVENTED_NAME_UNROUTED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "invented_name_unrouted",
        "name": (span.attributes or {}).get("original_name", ""),
        "original_name": (span.attributes or {}).get("original_name", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "world": (span.attributes or {}).get("world", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# ping-pong #74: the narrator marked a novel mention as a creature (wild
# animal / beast / monster). The invented-name seam DECLINED the culture-bound
# person namer and preserved the narrator's descriptive name verbatim, minting
# a creature-typed pool member with NO culture. This is the GM-panel lie
# detector for the fix: it proves the engine routed a creature away from the
# person namer (vs the old behavior of "a lion called Keeper Goldbraid of the
# Emerald City"). ``culture`` is intentionally absent — a creature has none.
SPAN_NPC_CREATURE_PRESERVED = "npc.creature_preserved"
SPAN_ROUTES[SPAN_NPC_CREATURE_PRESERVED] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_pool",
        "op": "creature_preserved",
        "name": (span.attributes or {}).get("npc_name", ""),
        "original_name": (span.attributes or {}).get("npc_name", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

# Story 45-21 / 45-52: combat-stats publish onto Npc.core.edge.
# Fired when an encounter handshake (or other combat-stats emit) writes the
# dial-derived edge pool onto a matched ``snapshot.npcs`` entry. Renamed from
# ``npc_registry.hp_set`` in story 45-52 — the legacy registry is gone; the
# canonical seam now writes ``current`` / ``max`` onto ``Npc.core.edge`` per
# ADR-078 (HP→Edge) and ADR-014 (materialization seam).
SPAN_NPC_EDGE_PUBLISHED = "npc.edge_published"
SPAN_ROUTES[SPAN_NPC_EDGE_PUBLISHED] = SpanRoute(
    event_type="state_transition",
    component="npcs",
    extract=lambda span: {
        "field": "npcs",
        "op": "edge_published",
        "name": (span.attributes or {}).get("npc_name", ""),
        "current": (span.attributes or {}).get("current", 0),
        "max": (span.attributes or {}).get("max", 0),
        "source": (span.attributes or {}).get("source", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)


@contextmanager
def npc_auto_registered_span(
    *,
    npc_name: str,
    pronouns: str,
    role: str,
    turn_number: int,
    registry_len: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Attribute key ``npc_name`` avoids the OTEL span ``name`` reserved attribute."""
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "pronouns": pronouns,
        "role": role,
        "turn_number": turn_number,
        "registry_len": registry_len,
        **attrs,
    }
    with Span.open(SPAN_NPC_AUTO_REGISTERED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def npc_referenced_span(
    *,
    npc_name: str,
    match_strategy: str,
    pool_origin: str | None,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Wave 2A (story 45-47): emitted on every narrator-cite of an NPC name.

    ``match_strategy`` is the lie-detector dial — ``npcs_hit`` means the
    name resolved to an active stateful ``Npc``; ``pool_hit`` means it
    matched a known ``NpcPoolMember``; ``invented`` means the narrator
    made up a name not in either store. Per-session counts of
    ``invented`` tell the GM panel when the pool wasn't seeded for the
    scene.

    ``pool_origin`` is the ``NpcPoolMember.name`` the resulting/existing
    ``Npc`` was promoted from, or ``None`` for narrator-invented or
    legacy-without-provenance NPCs.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "match_strategy": match_strategy,
        "pool_origin": pool_origin if pool_origin is not None else "",
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_REFERENCED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_invented_name_routed_span(
    *,
    original_name: str,
    npc_name: str,
    culture: str,
    culture_source: str,
    collision_reroll: bool,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 72-4: emitted when the Step-3 novel branch reroutes a
    narrator-invented name through the ADR-091 culture-bound generator.

    ``original_name`` is the narrator's bare string ("Bob Hegemonic");
    ``npc_name`` is the culture-true name actually minted ("Veyra Solnë").
    ``culture`` / ``culture_source`` record which culture (and whether it was
    resolved from the world or the genre tier via
    ``Pack.effective_cultures``) governed the generation. ``collision_reroll``
    is True when a candidate was rejected (stem collision or an existing-member
    name clash) and the route re-rolled. Attribute key ``npc_name`` avoids the
    OTEL reserved ``name`` attribute (mirrors the sibling NPC spans).
    """
    attributes: dict[str, Any] = {
        "original_name": original_name,
        "npc_name": npc_name,
        "culture": culture,
        "culture_source": culture_source,
        "collision_reroll": collision_reroll,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_INVENTED_NAME_ROUTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_invented_name_unrouted_span(
    *,
    original_name: str,
    reason: str,
    world: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 72-4 (No Silent Fallbacks): emitted when a narrator-invented name
    could NOT be routed through the generator — no culture bound for the active
    world, or generation failed.

    ``severity="warning"`` so the WatcherSpanProcessor renders this as a soft
    alert (parallel to ``npc_recurring_presence_missed_span``). ``reason``
    discriminates ``no_culture_bound`` vs ``generation_failed``. The engine then
    deliberately degrades to the raw narrator string — a span-recorded degrade,
    never a silent swallow.
    """
    attributes: dict[str, Any] = {
        "original_name": original_name,
        "reason": reason,
        "world": world,
        "turn_number": turn_number,
        "severity": "warning",
        **attrs,
    }
    with Span.open(
        SPAN_NPC_INVENTED_NAME_UNROUTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_creature_preserved_span(
    *,
    npc_name: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ping-pong #74: emitted when the Step-3 novel branch recognizes a
    creature mention (``NpcMention.is_creature``) and DECLINES the culture-bound
    person namer — the narrator's descriptive name ("The Forest Lions") is kept
    verbatim and the new pool member is creature-typed with no culture.

    This is the OTEL lie detector for the fix: a creature must never acquire a
    person-name or a culture. ``npc_name`` is the preserved descriptive name;
    there is deliberately no ``culture`` attribute. The full Monster Manual
    identity (creature_id / threat_level / hp, ADR-059) is a deferred follow-up.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_CREATURE_PRESERVED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_developed_span(
    *,
    npc_name: str,
    non_transactional_interactions: int,
    resolution_tier_before: str,
    resolution_tier_after: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 72-1: one interest-driven development tick on an engaged ``Npc``.

    Carries the new interest count and the ``resolution_tier`` transition
    (``before``/``after`` equal when the tick didn't cross a threshold).
    ``npc_name`` avoids the OTEL-reserved ``name`` span attribute.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "non_transactional_interactions": non_transactional_interactions,
        "resolution_tier_before": resolution_tier_before,
        "resolution_tier_after": resolution_tier_after,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(SPAN_NPC_DEVELOPED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def npc_pc_name_skipped_span(
    *,
    npc_name: str,
    matched_pc: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted when narration tries to auto-register a PC's name as an NPC.

    ``matched_pc`` is the canonical PC name we matched against (case-folded
    equality on ``character.core.name``). The span MUST fire whenever the
    filter triggers — it's the only way Sebastien's GM panel can see that
    the narrator is naming party members in NPC-registry contexts (one of
    the symptoms behind the playtest 2026-04-29 "narrator confused PC for
    NPC" report).
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "matched_pc": matched_pc,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_PC_NAME_SKIPPED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_edge_published_span(
    *,
    npc_name: str,
    current: int,
    max: int,  # noqa: A002 — wire name mirrors the EdgePool field
    source: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 45-21 / 45-52: emitted when combat stats publish onto an Npc's
    ``core.edge`` pool.

    Renamed from ``npc_registry_hp_set_span`` in story 45-52 — the legacy
    registry is gone; the canonical seam now writes ``current`` / ``max``
    onto ``Npc.core.edge`` per ADR-078 (HP→Edge) and ADR-014 (materialization
    seam).

    ``source`` labels which subsystem published the stats (e.g.
    ``encounter_handshake``, ``apply_beat``). Allows the GM panel to
    verify the publish seam is firing and not silently dropping.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "current": current,
        "max": max,
        "source": source,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_EDGE_PUBLISHED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_reinvented_span(
    *,
    npc_name: str,
    drift_field: str,
    expected: str,
    narrator: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``severity="warning"`` so the WatcherSpanProcessor renders this as a drift alert."""
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "drift_field": drift_field,
        "expected": expected,
        "narrator": narrator,
        "turn_number": turn_number,
        "severity": "warning",
        **attrs,
    }
    with Span.open(SPAN_NPC_REINVENTED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def npc_auto_minted_from_prose_span(
    *,
    npc_name: str,
    role: str,
    pronouns: str,
    source: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 49-2: emitted when the prose-only auto-minter appends a
    ``NpcPoolMember`` because the narrator named a person via role or
    honorific in this turn's prose but omitted them from
    ``npcs_present``.

    Distinct from ``npc.auto_registered`` (which fires for
    structured-patch mints via ``_apply_npc_mentions`` step 3). This
    split lets the GM panel filter "narrator-extracted" vs
    "server-extracted" first-mention NPCs.

    ``source`` labels the extraction provenance — currently always
    ``"dialogue_extraction"``; no other value is currently produced.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "role": role,
        "pronouns": pronouns,
        "source": source,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_AUTO_MINTED_FROM_PROSE,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_auto_mint_skipped_span(
    *,
    npc_name: str,
    role: str,
    reason: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 49-2 (Reviewer rework): emitted when the auto-minter declines
    to mint a role/honorific from prose. Distinct from
    ``npc.auto_minted_from_prose`` (the success span) so the GM panel
    can show mints vs declines as separate streams.

    ``reason`` discriminates the skip cause:
    - ``ambiguous_pronouns_role`` — bare-role mention with zero or
      conflicting subject pronouns in the local window.
    - ``ambiguous_pronouns_honorific`` — honorific (Mrs. Gow, Mr. Hodge,
      etc.) with no clean pronoun signal.
    - ``gender_paired_conflict`` — bare-role mention blocked because
      the paired-opposite role is already in the roster (e.g. Mother
      blocked when Father is in pool; the Glenross turn-6 scenario).

    ``severity="warning"`` so the WatcherSpanProcessor renders this as
    a soft alert (parallel to ``npc_recurring_presence_missed_span``).
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "role": role,
        "reason": reason,
        "turn_number": turn_number,
        "severity": "warning",
        **attrs,
    }
    with Span.open(
        SPAN_NPC_AUTO_MINT_SKIPPED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_observation_gate_promoted_span(
    *,
    npc_name: str,
    role: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 49-6: emitted when the ratification gate re-cites a
    previously-pending pool member (auto-minted from prose on a prior
    turn) and clears its ``observation_pending`` flag. Distinct from
    ``npc.auto_minted_from_prose`` (first-mint) and ``npc.auto_registered``
    (structured-patch mint) so the GM panel can show ratifications as a
    separate stream.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "role": role,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_OBSERVATION_GATE_PROMOTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_observation_gate_purged_span(
    *,
    npc_name: str,
    role: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 49-6: emitted when the ratification gate removes a
    previously-pending pool member that the narrator did NOT re-cite
    this turn. Destructive op (the entry is removed from
    ``snapshot.npc_pool``), surfaced at ``severity="warning"`` so
    Sebastien's GM panel renders the drop as a soft alert — parallel
    to ``npc_recurring_presence_missed_span``. Without this audit
    span, NPC deletions would be silent, which is exactly the failure
    mode CLAUDE.md "No Silent Fallbacks" prohibits.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "role": role,
        "turn_number": turn_number,
        "severity": "warning",
        **attrs,
    }
    with Span.open(
        SPAN_NPC_OBSERVATION_GATE_PURGED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_observation_gate_order_violation_span(
    *,
    pending_count: int,
    pending_names: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 72-10: emitted when the apply pipeline reaches
    ``_auto_mint_prose_only_npcs`` while prior-turn ``observation_pending``
    pool members still survive — i.e. ``_apply_npc_observation_gate`` did NOT
    run first this turn. ``pending_count`` is how many unresolved members were
    found; ``pending_names`` is a comma-joined sample for the GM panel.
    ``severity="warning"`` mirrors ``npc_observation_gate_purged_span`` so the
    panel renders the ordering regression as a soft alert. The span fires
    immediately before the runtime invariant raises, so the lie-detector
    records the violation even though the turn then fails loud.
    """
    attributes: dict[str, Any] = {
        "pending_count": pending_count,
        "pending_names": pending_names,
        "turn_number": turn_number,
        "severity": "warning",
        **attrs,
    }
    with Span.open(
        SPAN_NPC_OBSERVATION_GATE_ORDER_VIOLATION,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def npc_recurring_presence_missed_span(
    *,
    npc_name: str,
    source: str,
    turn_number: int,
    last_seen_turn: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 45-53: emitted when narration prose names a known recurring
    NPC but the narrator omitted them from ``npcs_present``.

    ``source`` is ``"npcs"`` (stateful Npc match) or ``"npc_pool"``
    (pool member match) — when both stores hold the same name, ``"npcs"``
    wins (parallel to the npcs-shadows-pool rule in
    ``_apply_npc_mentions``). ``last_seen_turn`` propagates from the
    matched Npc's ``last_seen_turn`` (0 for pool-only matches).
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "source": source,
        "turn_number": turn_number,
        "last_seen_turn": last_seen_turn,
        "severity": "warning",
        **attrs,
    }
    with Span.open(
        SPAN_NPC_RECURRING_PRESENCE_MISSED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


# Story 72-9: when a narrator-invented NPC first becomes mechanical (promoted
# from an ``NpcPoolMember`` to an ``Npc``), it is seeded with an OCEAN profile
# (ADR-042), a neutral disposition (ADR-020, carried by 72-2/72-5), and — when a
# scenario is active — registered into the scenario ``npc_roles`` graph with a
# live ``BeliefState`` surface (ADR-053). This span is the GM-panel lie-detector
# that proves the identity wiring fired rather than the narrator improvising:
# ``ocean_seeded`` confirms a real profile (not ``None``/``{}``), ``disposition``
# is the spawn value, and ``scenario_registered`` + ``scenario_role`` record
# whether the NPC joined an active scenario.
SPAN_NPC_IDENTITY_SEEDED = "npc.identity_seeded"
SPAN_ROUTES[SPAN_NPC_IDENTITY_SEEDED] = SpanRoute(
    event_type="state_transition",
    component="npc_identity",
    extract=lambda span: {
        "field": "npc.identity_seeded",
        "npc_name": (span.attributes or {}).get("npc_name", ""),
        "ocean_seeded": bool((span.attributes or {}).get("ocean_seeded", False)),
        "disposition": (span.attributes or {}).get("disposition", 0),
        "scenario_registered": bool((span.attributes or {}).get("scenario_registered", False)),
        "scenario_role": (span.attributes or {}).get("scenario_role", ""),
    },
)


@contextmanager
def npc_identity_seeded_span(
    *,
    npc_name: str,
    ocean_seeded: bool,
    disposition: int,
    scenario_registered: bool,
    scenario_role: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 72-9: emitted when a narrator-invented NPC is seeded with OCEAN /
    disposition / scenario ``belief_state`` at promotion.

    ``ocean_seeded`` is ``True`` when a real ``OceanProfile`` was attached;
    ``scenario_registered`` is ``True`` only when a scenario was active and the
    NPC was bound into ``npc_roles`` (``scenario_role`` carries the assigned
    role, defaulting to ``"innocent"`` for a mid-session walk-on — never the
    pre-selected ``guilty_npc``).
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "ocean_seeded": bool(ocean_seeded),
        "disposition": int(disposition),
        "scenario_registered": bool(scenario_registered),
        "scenario_role": scenario_role,
        **attrs,
    }
    with Span.open(
        SPAN_NPC_IDENTITY_SEEDED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


# Story 75-2: budgeted NPC working-set selection. Fires once per turn when the
# roster is partitioned into the prompt working-set (scene-present full floor /
# off-stage brief / off-stage compact). The GM-panel lie detector: it shows
# considered-vs-selected counts so the dev can verify the budgeting engaged and
# the prompt roster is bounded by relevance, not dumped verbatim (ADR-118 D5).
SPAN_NPC_WORKING_SET = "npc.working_set"
SPAN_ROUTES[SPAN_NPC_WORKING_SET] = SpanRoute(
    event_type="state_transition",
    component="npc_registry",
    extract=lambda span: {
        "field": "npc_working_set",
        "op": "budgeted_selection",
        "full_count": (span.attributes or {}).get("full_count", 0),
        "brief_count": (span.attributes or {}).get("brief_count", 0),
        "compact_count": (span.attributes or {}).get("compact_count", 0),
        "total_pool": (span.attributes or {}).get("total_pool", 0),
        "references_present": (span.attributes or {}).get("references_present", False),
    },
)


@contextmanager
def npc_working_set_span(
    *,
    full_count: int,
    brief_count: int,
    compact_count: int,
    total_pool: int,
    references_present: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 75-2: emitted once per ``build_npc_working_set`` call.

    ``full_count`` is the scene-present floor (always full detail);
    ``brief_count`` / ``compact_count`` are the off-stage tier in
    reference-present / no-reference mode respectively. ``total_pool`` is the
    full considered roster (``npcs`` + ``npc_pool``) — ``full+brief+compact``
    must equal it (no eviction), and a mismatch is a budgeting bug the GM panel
    can see. ``references_present`` records whether the off-stage tier was
    brief (a reference was made) or compact.
    """
    attributes: dict[str, Any] = {
        "full_count": full_count,
        "brief_count": brief_count,
        "compact_count": compact_count,
        "total_pool": total_pool,
        "references_present": references_present,
        **attrs,
    }
    with Span.open(SPAN_NPC_WORKING_SET, attributes, tracer_override=_tracer) as span:
        yield span
