"""FateActionHandler — handles FATE_ACTION messages (ADR-144 F1d).

Mirrors DiceThrowHandler's entry shape: state guard, resolve the rolling PC from
the seat map, resolve the bound ruleset, and route to ``dispatch_fate_action``
(which isinstance-gates to the Fate engine). Broadcast/narration re-entry is F2/F3
— this handler routes the action and mutates engine state; it returns [].
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from sidequest.server.session_handler import _State
from sidequest.server.session_helpers import _emit_unbound_rejection_event, _error_msg

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


class FateActionHandler:
    """Resolve a FATE_ACTION from the acting player onto the Fate exchange."""

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        from sidequest.game.ruleset import get_ruleset_module
        from sidequest.server.dispatch.fate_conflict import (
            FateConflictError,
            dispatch_fate_action,
        )

        if session._state != _State.Playing:
            logger.info(
                "session.message_rejected_unbound type=FATE_ACTION state=%s",
                session._state.name,
            )
            # Story 67-7 (AC5): surface the rejection to the GM panel so a
            # genuine unbound guard is distinguishable from reconnect churn.
            _emit_unbound_rejection_event("FATE_ACTION", session._state.name)
            return [
                _error_msg(
                    "Cannot process FATE_ACTION: not in Playing state",
                    code="session_unbound",
                )
            ]
        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]

        sd = session._session_data
        payload = msg.payload  # type: ignore[attr-defined]
        acting_player_id = getattr(msg, "player_id", "") or sd.player_id
        snapshot = sd.snapshot
        encounter = snapshot.encounter

        # Resolve the acting PC from the seat map (MP) then the solo fallback —
        # mirrors DiceThrowHandler so a multi-PC Fate table attributes the action
        # to whoever sent it, not characters[0].
        acting_pc_name = (
            snapshot.player_seats.get(acting_player_id) if snapshot.player_seats else None
        )
        if acting_pc_name is not None:
            character = next(
                (c for c in snapshot.characters if c.core.name == acting_pc_name), None
            )
        else:
            character = snapshot.characters[0] if snapshot.characters else None
        if character is None:
            return [_error_msg("FATE_ACTION: no character to act", code="fate_no_actor")]

        ruleset = get_ruleset_module(sd.genre_pack.rules.ruleset)
        try:
            dispatch_fate_action(
                payload=payload,
                actor_name=character.core.name,
                encounter=encounter,
                ruleset=ruleset,
                snapshot=snapshot,
                rng=random.Random(),  # fresh RNG; F2/F3 own real-roll seeding
                round_number=snapshot.turn_manager.interaction,
            )
        except FateConflictError as exc:
            logger.warning("fate.dispatch_error error=%s", exc)
            return [_error_msg(f"FATE_ACTION rejected: {exc}", code="fate_dispatch_error")]
        return []


HANDLER = FateActionHandler()
