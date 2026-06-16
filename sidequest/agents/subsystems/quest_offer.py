"""quest_offer subsystem dispatch handler — Intent Router live engager
(Story 117-3, ADR-146 §2).

The deterministic acceptance trigger for an authored ``QuestSeed``. The Intent
Router (ADR-113) runs BEFORE the narrator and classifies the player's turn
against the named pending offers surfaced in its ``<game_state>``
(``snapshot.pending_quest_offers``). When it emits ``subsystem="quest_offer"``
with ``params={"quest_id": "<seed id>", "decision": "accept"|"decline"}`` at a
confidence that clears the bank's per-subsystem gate, this handler engages the
engine:

- ``accept`` → :func:`mint_quest_offer` deterministically mints a QuestEntry
  from the stashed seed (no narrator tool call), firing ``quest.seeded``. This
  is a thin wrapper: it reads ``params`` and calls the engine seam.
- ``decline`` → consume the offer (declined); no mint, no ``quest.seeded`` span.
- an UNKNOWN ``quest_id`` (router named an offer with no matching pending seed)
  → emit a ``quest_offer.mismatch`` watcher event and return; never fabricate a
  phantom quest from nothing (ADR-146 §2 handler pseudocode).
- an EMPTY or UNKNOWN ``decision`` (a router defect) → emit a
  ``quest_offer.mismatch`` (reason ``unknown_decision``) and return; it must
  FAIL LOUD and mint nothing, never fall through to accept and silently mint a
  quest the player never accepted (No Silent Fallbacks).

The below-threshold (low-confidence) case never reaches this handler — the bank
degrades it to a narrator hint and leaves the offer live (``run_dispatch_bank``
confidence gate).
"""

from __future__ import annotations

import logging

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.quest_offer import mint_quest_offer
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)


async def run_quest_offer_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
) -> SubsystemOutput:
    """Engage the authored-seed mint for an accepted offer (ADR-146 §2).

    Returns an empty-directives ``SubsystemOutput`` on a real mint (the minted
    QuestEntry is the engine truth; the narrator reads ``quest_log`` downstream).
    On decline / unknown-offer it returns ``data`` carrying the outcome for the
    downstream audit, applying no mint.
    """
    quest_id = str(dispatch.params.get("quest_id", "") or "")
    decision = str(dispatch.params.get("decision", "") or "")

    if not quest_id:
        # Malformed dispatch — the router omitted the offer id. Surface loud,
        # mint nothing (the engagement witness flags quest_id absent from
        # quest_log after accept).
        _watcher_publish(
            "quest_offer.mismatch",
            {"reason": "missing_quest_id", "decision": decision},
            component="quest_log",
            severity="warning",
        )
        return SubsystemOutput(data={"error": "missing_quest_id"})

    # The decision MUST be one of the two contract values. An empty or unknown
    # decision (a router defect) must FAIL LOUD and mint NOTHING — never fall
    # through to the accept path and silently mint a quest the player never
    # accepted (No Silent Fallbacks, the exact doctrine this epic exists to
    # enforce). Surface it as a mismatch and return without touching state.
    if decision not in ("accept", "decline"):
        _watcher_publish(
            "quest_offer.mismatch",
            {"reason": "unknown_decision", "quest_id": quest_id, "decision": decision},
            component="quest_log",
            severity="warning",
        )
        return SubsystemOutput(data={"error": "unknown_decision", "quest_id": quest_id})

    if decision == "decline":
        # Consume the offer (declined) — not left dangling for a re-prompt.
        snapshot.pending_quest_offers.pop(quest_id, None)
        _watcher_publish(
            "quest.offer_declined",
            {"quest_id": quest_id},
            component="quest_log",
            severity="info",
        )
        return SubsystemOutput(data={"quest_id": quest_id, "decision": "declined"})

    # decision == "accept": mint if the offer is real.
    if quest_id not in snapshot.pending_quest_offers and quest_id not in snapshot.quest_log:
        # The router named an offer that was never pending — emit the mismatch
        # and return. Never fabricate a quest from nothing (ADR-146 §2).
        _watcher_publish(
            "quest_offer.mismatch",
            {"reason": "unknown_offer", "quest_id": quest_id},
            component="quest_log",
            severity="warning",
        )
        return SubsystemOutput(data={"error": "unknown_offer", "quest_id": quest_id})

    entry = mint_quest_offer(snapshot, quest_id, confidence=float(dispatch.confidence))
    minted = entry is not None
    logger.debug(
        "quest_offer.accept quest_id=%s minted=%s confidence=%.2f",
        quest_id,
        minted,
        dispatch.confidence,
    )
    return SubsystemOutput(data={"quest_id": quest_id, "minted": minted})


__all__ = ["run_quest_offer_dispatch"]
