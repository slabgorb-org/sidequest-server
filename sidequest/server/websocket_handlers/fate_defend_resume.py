"""Re-emit parked FATE_DEFEND_REQUESTs on reconnect (Story 153-7, ADR-151).

The DEFEND barrier (ADR-151) emits one ``FATE_DEFEND_REQUEST`` per incoming attack
*once*, when the round PARKS. A targeted player who disconnects mid-DEFEND never
sees the request again on reconnect — the connect/resume bootstrap re-emits
FATE_STATE / PARTY_STATUS / LOCATION_DESCRIPTION / MAP_UPDATE but nothing re-emits
the pending defend, so the defender can't throw, the ledger never fills, and
``resume_fate_exchange`` never fires (the round wedges).

This is the sibling of ``fate_state_emit._maybe_emit_fate_state`` for the DEFEND
barrier: on connect/resume, for a Fate pack only, re-broadcast a
``FateDefendRequestMessage`` for every UNFILLED ``pending_defenses`` entry belonging
to the reconnecting defender. It is READ-ONLY on the ledger — it never re-rolls or
mutates an entry (ADR-128 resume-safety; the locked attack data simply rides the
snapshot) — and it fires a ``fate.defend_phase`` lie-detector span tagged
``reason=reconnect_reemit`` so the GM panel can verify the wedge unblocked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _maybe_reemit_pending_defenses(
    *,
    sd: Any,
    snapshot: Any,
    defender_name: str,
    player_id: str,
    emit_fn: Callable[[Any, str], None],
) -> None:
    """Re-emit a FATE_DEFEND_REQUEST per unfilled pending defense for the
    reconnecting defender.

    Fate pack only (``sd.genre_pack.rules.ruleset == "fate"`` — never collide with
    the WN/native ConfrontationOverlay). No encounter / empty ledger ⇒ silent no-op
    (ADR-151's blessed default: nothing parked ⇒ nothing to re-emit). Filled or
    conceded entries, and entries for other defenders, are skipped. Read-only on the
    ledger.
    """
    if sd.genre_pack.rules.ruleset != "fate":
        return

    encounter = getattr(snapshot, "encounter", None)
    if encounter is None:
        return

    from sidequest.protocol.fate import FateDefendRequestPayload
    from sidequest.protocol.messages import FateDefendRequestMessage
    from sidequest.telemetry.spans.fate import fate_defend_phase_span

    reemitted = 0
    for entry in encounter.pending_defenses:
        if entry.defender != defender_name:
            continue
        if entry.defense_total is not None or entry.conceded:
            continue  # already answered — the barrier is not waiting on this one
        payload = FateDefendRequestPayload(
            request_id=entry.request_id,
            defender=entry.defender,
            attacker=entry.attacker,
            attack_skill=entry.attack_skill,
            attack_total=entry.attack_total,
            mental=entry.mental,
        )
        emit_fn(
            FateDefendRequestMessage(payload=payload, player_id=player_id), "FATE_DEFEND_REQUEST"
        )
        fate_defend_phase_span(
            defender=entry.defender,
            attacker=entry.attacker,
            request_id=entry.request_id,
            responded=False,
            reason="reconnect_reemit",
        )
        reemitted += 1

    if reemitted:
        logger.info(
            "fate.defend.reemitted_on_resume count=%d defender=%s player_id=%s",
            reemitted,
            defender_name,
            player_id,
        )
