"""OrbitalIntentHandler — routes ORBITAL_INTENT to the orbital chart renderer.

Per orbital-map plan Task 15b: dispatch wires ``handle_orbital_intent``
into the WebSocket router. The response is a single ``ORBITAL_CHART``
message returned to the requesting socket — chart UI is per-player, not
broadcast.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.orbital.intent import (
    OrbitalContentUnavailableError,
    handle_orbital_intent,
)
from sidequest.protocol.messages import OrbitalChartMessage
from sidequest.server.session_helpers import _error_msg

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


class OrbitalIntentHandler:
    """Resolve an ORBITAL_INTENT against the room's bound Session."""

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        room = session._room
        if room is None or room.snapshot is None:
            # Same auto-recovery contract as PLAYER_ACTION — UI fires
            # SESSION_EVENT{connect} when it sees session_unbound.
            logger.info(
                "session.message_rejected_unbound type=ORBITAL_INTENT state=%s",
                session._state.name,
            )
            return [
                _error_msg(
                    "Cannot process ORBITAL_INTENT: room not bound",
                    code="session_unbound",
                )
            ]

        intent = msg.payload  # type: ignore[attr-defined]
        try:
            response = handle_orbital_intent(room.session, intent)
        except OrbitalContentUnavailableError as exc:
            logger.info(
                "session.orbital_intent_no_content slug=%s error=%s",
                room.slug,
                exc,
            )
            return [
                _error_msg(
                    "Orbital chart unavailable: world has no orbital tier",
                    code="orbital_unavailable",
                )
            ]

        return [
            OrbitalChartMessage(
                payload=response,
                player_id=getattr(msg, "player_id", "") or "",
            )
        ]


HANDLER = OrbitalIntentHandler()
