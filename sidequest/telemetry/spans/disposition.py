"""Disposition spans — NPC affinity shifts and spawn defaults."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_DISPOSITION_SHIFT = "disposition.shift"

# Promoted from FLAT_ONLY (sprint 3 cold-subsystem audit). Without a typed
# event the GM panel could not show NPC affinity drift — narrator-described
# warming/cooling looked the same as engine-applied disposition changes.
# Story 50-11 added `before_attitude`/`after_attitude`/`crossed` so the panel
# can distinguish a band flip (neutral→friendly) from intra-band drift.
SPAN_ROUTES[SPAN_DISPOSITION_SHIFT] = SpanRoute(
    event_type="state_transition",
    component="disposition",
    extract=lambda span: {
        "field": "disposition.shift",
        "npc_name": (span.attributes or {}).get("npc_name", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "before": (span.attributes or {}).get("before", 0),
        "after": (span.attributes or {}).get("after", 0),
        "before_attitude": (span.attributes or {}).get("before_attitude", ""),
        "after_attitude": (span.attributes or {}).get("after_attitude", ""),
        "crossed": bool((span.attributes or {}).get("crossed", False)),
    },
)


# Story 72-5 (epic 72 — NPC Identity Hardening): the disposition an NPC
# receives *at materialization*. The deep-dive flagged that a disposition
# could "materialize from nowhere" — a narrator-invented person must spawn
# neutral (0), a Monster Manual creature stays born-hostile (-20), and the
# GM panel must be able to tell which fired rather than trusting prose.
# ``provenance`` is the lie-detector dial: ``default_neutral`` (person /
# invented NPC) vs ``default_creature_hostile`` (creature-shape patch).
SPAN_NPC_SPAWN_DISPOSITION = "npc.spawn_disposition"
SPAN_ROUTES[SPAN_NPC_SPAWN_DISPOSITION] = SpanRoute(
    event_type="state_transition",
    component="disposition",
    extract=lambda span: {
        "field": "npc.spawn_disposition",
        "npc_name": (span.attributes or {}).get("npc_name", ""),
        "disposition": (span.attributes or {}).get("disposition", 0),
        "provenance": (span.attributes or {}).get("provenance", ""),
        "is_creature": bool((span.attributes or {}).get("is_creature", False)),
        "pool_origin": (span.attributes or {}).get("pool_origin", ""),
        # Story 72-3: provenance authorship marker — True when the Monster
        # Manual (ADR-059) authored this NPC, so the GM panel can attribute
        # MM-seeded NPCs vs narrator improv at the materialization decision.
        "manual_origin": bool((span.attributes or {}).get("manual_origin", False)),
    },
)


@contextmanager
def npc_spawn_disposition_span(
    *,
    npc_name: str,
    disposition: int,
    provenance: str,
    is_creature: bool,
    pool_origin: str | None = None,
    manual_origin: bool = False,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 72-5: emitted whenever an ``Npc`` is materialized, recording
    the disposition it spawned with and *why*.

    ``provenance`` is the GM-panel lie-detector dial:
    ``"default_neutral"`` — a narrator-invented person or a non-creature
    patch (spawned at 0); ``"default_creature_hostile"`` — a Monster
    Manual creature patch carrying a creature-shape field (spawned at -20).
    ``pool_origin`` is the ``NpcPoolMember.name`` an invented NPC was
    promoted from, or ``None`` for patch-materialized NPCs.
    """
    attributes: dict[str, Any] = {
        "npc_name": npc_name,
        "disposition": int(disposition),
        "provenance": provenance,
        "is_creature": bool(is_creature),
        "pool_origin": pool_origin if pool_origin is not None else "",
        "manual_origin": bool(manual_origin),
        **attrs,
    }
    with Span.open(
        SPAN_NPC_SPAWN_DISPOSITION,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span
