"""Reactive RELATIONSHIPS emitter (ADR-136).

Mirrors map_emit.py's _maybe_emit_location_description: build a global payload
and broadcast via emit_fn. Change-gated on a lightweight per-roster signature so
the message fires only when the relationship set actually changes (a disposition
shift, a new NPC, a new beat/claim) — Cost Scales with Drama. The payload is the
same for every recipient (disposition/OCEAN/claims are global), so it broadcasts
directly rather than running the per-recipient projection chain.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sidequest.game.projection.relationships import build_relationship_entries
from sidequest.protocol.messages import RelationshipsMessage
from sidequest.protocol.models import RelationshipsPayload

logger = logging.getLogger(__name__)

_SIG_ATTR = "_last_relationships_sig"


def _relationships_signature(snapshot: Any) -> str:
    """Cheap change signature: name, disposition, log size, claim count per NPC.

    ``belief_state`` is a non-Optional ``BeliefState`` field on ``Npc`` (it
    defaults to an empty ``BeliefState``), so it is always present; the
    ``getattr(..., [])`` defensive default kept here only guards a duck-typed
    test snapshot whose NPCs omit the field — harmless robustness, not a silent
    fallback over a real-state gap.
    """
    parts: list[str] = []
    for npc in snapshot.npcs:
        claim_count = sum(
            1
            for b in getattr(npc.belief_state, "beliefs", [])
            if getattr(b, "variant", "") == "claim"
        )
        parts.append(
            f"{npc.core.name}:{int(npc.disposition)}:{len(npc.disposition_log)}:{claim_count}"
        )
    return "|".join(parts)


def _maybe_emit_relationships(
    handler: Any,
    *,
    snapshot: Any,
    emit_fn: Callable[[Any, str], None],
) -> None:
    """Emit a RELATIONSHIPS message when the roster signature changes.

    No NPCs → nothing to show (silent; not an error state). Unchanged signature
    → skip (Cost Scales with Drama).
    """
    if not snapshot.npcs:
        return

    sig = _relationships_signature(snapshot)
    if getattr(handler, _SIG_ATTR, None) == sig:
        return

    entries = build_relationship_entries(snapshot)
    msg = RelationshipsMessage(payload=RelationshipsPayload(entries=entries))

    from sidequest.telemetry.spans import SPAN_RELATIONSHIPS_EMITTED, Span

    with Span.open(
        SPAN_RELATIONSHIPS_EMITTED,
        {"entry_count": len(entries), "changed": True},
    ):
        pass
    logger.info("relationships.emitted entries=%d", len(entries))

    setattr(handler, _SIG_ATTR, sig)
    emit_fn(msg, "RELATIONSHIPS")
