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
