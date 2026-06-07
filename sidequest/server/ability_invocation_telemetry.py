"""Ability-invocation decline telemetry — shared detector + emit seam.

sq-playtest 2026-06-07 (Reroute Power): a party character's ADR-097 signature
ability declared verbatim produced ZERO ability/dispatch/gate spans. Two
distinct submission paths needed the same lie-detector:

* the intent-router pre-narrator pass (``intent_router_pass``) — open-play
  actions; the ADR-123 dispatch bank has no ability subsystem, so the
  declaration has no mechanical route;
* the beat-commit dice path (``dispatch.dice``) — IN-confrontation actions
  ride ``DiceThrowPayload.player_action`` straight into a router-SUPPRESSED
  replay turn (story 91-2), so the router pass never sees them at all. This
  is the perseus repro path: "once per ship combat" abilities can only be
  declared here, and here is exactly where nothing scanned them.

One emit helper, one span name, both call sites — the GM panel gets a single
query for "declared abilities the engine could not engage", and a future
ability subsystem knows what it must absorb.
"""

from __future__ import annotations

import logging
import re

from sidequest.game.session import GameSnapshot
from sidequest.telemetry.spans.intent_router import (
    intent_router_ability_invocation_unrouted_span,
)

logger = logging.getLogger(__name__)


def ability_invocation_hits(action: str, snapshot: GameSnapshot) -> list[tuple[str, str]]:
    """``(character, ability)`` word-boundary hits between the action text and
    party characters' ADR-097 ability names. Deterministic — the lexical half;
    paraphrased invocations cannot be detected this way, by construction."""
    folded = action.casefold()
    hits: list[tuple[str, str]] = []
    for character in snapshot.characters:
        for ability in getattr(character, "abilities", None) or []:
            name = (getattr(ability, "name", "") or "").strip()
            # Length guard: skip degenerate one/two-letter names.
            if len(name) < 4:
                continue
            if re.search(rf"\b{re.escape(name.casefold())}\b", folded):
                hits.append((character.core.name, name))
    return hits


def emit_ability_invocation_unrouted(
    *, action: str, snapshot: GameSnapshot
) -> list[tuple[str, str]]:
    """Emit ``intent_router.ability_invocation_unrouted`` + a WARNING per
    declared-but-unrouted ability invocation in ``action``. Returns the hits
    (empty on quiet turns — no span, no noise)."""
    hits = ability_invocation_hits(action, snapshot)
    if not hits:
        return hits
    enc = snapshot.encounter
    in_confrontation = enc is not None and not getattr(enc, "resolved", False)
    for character_name, ability_name in hits:
        with intent_router_ability_invocation_unrouted_span(
            ability=ability_name,
            character=character_name,
            in_confrontation=in_confrontation,
            genre_slug=snapshot.genre_slug or "",
        ):
            pass
        logger.warning(
            "intent_router.ability_invocation_unrouted ability=%r character=%r "
            "in_confrontation=%s — the action declares an ADR-097 ability but "
            "the dispatch bank has no ability subsystem; no mechanical route "
            "exists (any engagement is narrator improv)",
            ability_name,
            character_name,
            in_confrontation,
        )
    return hits
