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
        # Story 118-8 / ADR-119: seat resolution is driven by the SERVER-
        # authenticated identity (``sd.player_id`` — the Cf-Access identity bound
        # at connect) as the SOLE source. Inbound ``msg.player_id`` is a client-
        # controlled OUTPUT annotation, NEVER trusted as inbound identity:
        # trusting it (the old ``getattr(msg, "player_id", "") or sd.player_id``)
        # let a client spoof another seat's player_id and act AS that PC — sealing
        # their commit, spending their fate, invoking their aspect (118-3 amplified
        # it: the spoofer also received the victim's roll). A non-empty inbound id
        # that disagrees with the authenticated one is a spoof attempt: surface it
        # to the GM panel (the lie detector) and proceed as the authenticated PC.
        acting_player_id = sd.player_id
        inbound_player_id = getattr(msg, "player_id", "") or ""
        if inbound_player_id and inbound_player_id != acting_player_id:
            logger.warning(
                "fate.action.player_id_spoof_rejected inbound=%s authenticated=%s",
                inbound_player_id,
                acting_player_id,
            )
            from sidequest.telemetry.watcher_hub import publish_event

            publish_event(
                "state_transition",
                {
                    "field": "session_binding",
                    "op": "fate_action_player_id_spoof_rejected",
                    "inbound_player_id": inbound_player_id,
                    "authenticated_player_id": acting_player_id,
                    "recovery": "auth_identity_enforced",
                    "source": "fate_action",
                },
                component="session",
                severity="warning",
            )
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
            result = dispatch_fate_action(
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

        # F3g (ADR-144 / Story 118-7): BROADCAST the acting PC's own 4dF roll to
        # the whole table so every seat SEES the faces + ladder + shift + tier
        # (SOUL "The Guitar Solo": the soloist's roll is visible to the band — not
        # just the socket that sent the action). The roll's dice/tier/shifts are
        # already on the ``fate.action_resolved`` span (the GM-panel polygraph);
        # this is the player-facing surface, attributed to the acting PC. A
        # concession is pre-roll (action_roll is None) → nothing to show.
        if result.action_roll is not None:
            from sidequest.game.ruleset.fate_projection import build_fate_roll_payload
            from sidequest.protocol.messages import FateRollMessage

            payload_out = build_fate_roll_payload(result.action_roll)
            roll_msg = FateRollMessage(payload=payload_out, player_id=acting_player_id)
            room = sd._room
            if room is None:
                # Playing state always has a room (slug-connect sets it); a None
                # here is a programming error, not a path to silently drop the
                # roll (CLAUDE.md No Silent Fallbacks).
                logger.error(
                    "fate.roll.broadcast_no_room actor=%s — roll not delivered",
                    character.core.name,
                )
                return []
            # Fan out to EVERY seat including the actor (exclude_socket_id=None),
            # mirroring the DICE_RESULT broadcast (websocket_session_handler.py).
            # Because the actor is reached by the broadcast, the handler must NOT
            # also return the message — that would double-deliver to the sender.
            delivered = room.broadcast(roll_msg, exclude_socket_id=None)
            logger.info(
                "fate.roll.broadcast actor=%s player_id=%s recipients=%d "
                "dice=%s ladder=%d shifts=%d tier=%s",
                character.core.name,
                acting_player_id,
                len(delivered),
                payload_out.dice,
                payload_out.ladder_total,
                payload_out.shifts,
                payload_out.tier,
            )
            # OTEL lie-detector (CLAUDE.md OTEL Observability Principle): record
            # the broadcast emit + recipient count so the GM panel can verify the
            # roll actually reached the table, not just that prose claims a roll.
            from sidequest.telemetry.watcher_hub import publish_event

            publish_event(
                "state_transition",
                {
                    "field": "fate_roll",
                    "op": "broadcast_emitted",
                    "player_id": acting_player_id,
                    "actor": character.core.name,
                    "recipients": len(delivered),
                    "tier": payload_out.tier,
                    "ladder_total": payload_out.ladder_total,
                    "shifts": payload_out.shifts,
                },
                component="fate",
                severity="info",
            )
            return []
        return []


HANDLER = FateActionHandler()
