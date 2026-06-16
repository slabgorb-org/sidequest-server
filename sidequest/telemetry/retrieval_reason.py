"""Per-card retrieval-reason decomposition — ADR-118 Amendment §A5 (Story 84-4, WI-6).

The §A1 scorer (84-1) already populates ``RetrievedEntities.card_scores`` with a
:class:`~sidequest.game.pertinence.PertinenceScore` per selected fill card — each
carrying the four per-signal CONTRIBUTIONS, the final ``score``, and ``embed_used``.
WI-6 turns that struct into a GM-panel-readable *reason payload* so the dashboard
can headline WHY each note surfaced.

This module is PURE: no span, no watcher, no store, no I/O. The two emit sites
consume these helpers and own the two distinct encodings:

  * ``retrieval.card.reason`` span attribute — a JSON-encoded *string* (OTEL
    attributes cannot hold a list of dicts) built by ``json.dumps`` of the list of
    :func:`card_reason_payload` dicts.
  * ``card_reasons`` watcher-event field — the *native list* of the same dicts
    (the WatcherHub passes the fields dict through verbatim).

Keeping the decomposition here, pure, means both encodings derive from ONE payload
shape — no drift between Jaeger and the GM panel.
"""

from __future__ import annotations

from sidequest.game.pertinence import (
    SIGNAL_HERE,
    SIGNAL_MENTION,
    SIGNAL_RECENCY,
    SIGNAL_SIM,
    PertinenceScore,
)

# The present projection tier for every selected fill card. There is exactly ONE
# tier today — a selected card is served at full detail. Tiered projection
# (demote / lazy-rehydrate) is WI-3 (84-6); until it lands, this is the honest
# single tier, NOT a fabricated placeholder for a system that doesn't exist yet.
PRESENT_TIER = "full"

# §A1 priority order for resolving an exact contribution tie: mention dominates,
# then here, then recency, then sim. Deterministic — never arbitrary dict order.
_TIE_PRIORITY: tuple[str, ...] = (SIGNAL_MENTION, SIGNAL_HERE, SIGNAL_RECENCY, SIGNAL_SIM)


def dominant_signal(score: PertinenceScore) -> str:
    """The name of the highest-CONTRIBUTING signal for this card.

    Returns one of ``"mention" | "here" | "recency" | "sim"`` — the signal whose
    weighted contribution (not its raw value) is largest. An exact tie resolves by
    the §A1 priority order ``mention > here > recency > sim``, so the headline is
    deterministic rather than dependent on dict iteration order.
    """
    contributions = {
        SIGNAL_MENTION: score.mention_contribution,
        SIGNAL_HERE: score.here_contribution,
        SIGNAL_RECENCY: score.recency_contribution,
        SIGNAL_SIM: score.sim_contribution,
    }
    # Max by (contribution, -priority_index): the largest contribution wins, and on
    # a tie the lower priority index (higher priority) wins.
    return max(
        _TIE_PRIORITY,
        key=lambda name: (contributions[name], -_TIE_PRIORITY.index(name)),
    )


def card_reason_payload(score: PertinenceScore) -> dict:
    """Build the JSON-serializable per-card reason payload for the GM panel.

    Keys: ``card_id``, the four signal CONTRIBUTIONS (``mention`` / ``here`` /
    ``recency`` / ``sim``), the final ``score``, the ``dominant`` signal (==
    :func:`dominant_signal`, one source of truth), ``embed_used``, and the present
    ``tier``. All values are plain JSON scalars/strings — the payload round-trips
    through ``json.dumps`` (the span encoding) and rides the watcher event natively.
    """
    return {
        "card_id": score.card_id,
        SIGNAL_MENTION: score.mention_contribution,
        SIGNAL_HERE: score.here_contribution,
        SIGNAL_RECENCY: score.recency_contribution,
        SIGNAL_SIM: score.sim_contribution,
        "score": score.score,
        "dominant": dominant_signal(score),
        "embed_used": score.embed_used,
        "tier": PRESENT_TIER,
    }
