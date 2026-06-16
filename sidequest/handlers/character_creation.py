"""CharacterCreationHandler — handles CHARACTER_CREATION messages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.builder import BuilderError
from sidequest.protocol.messages import CharacterCreationPayload
from sidequest.server.session_handler import _State
from sidequest.server.session_helpers import _error_msg

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


class CharacterCreationHandler:
    """Route CHARACTER_CREATION traffic through the chargen state machine.

    Port of ``dispatch_character_creation`` in
    ``sidequest-api/crates/sidequest-server/src/dispatch/connect.rs``.

    Navigation actions (back / edit) are handled before phase dispatch;
    the UI sends them as a separate channel that can fire in any phase.
    Phase dispatch covers ``scene`` (player submitted a choice or
    freeform answer), ``continue`` (player acknowledged a display-only
    scene), and ``confirmation`` (player committed — builder.build()
    runs and the Character lands on snapshot).

    Every error path returns a structured ERROR message rather than
    raising — the WebSocket contract says we never leak exceptions to
    the client (2.2 acceptance: "Invalid inputs produce structured error
    messages, never exceptions through the WebSocket").

    Starting-equipment wiring from ``pack.inventory.starting_equipment``
    and archetype-resolver wiring (``resolve_archetype`` into
    ``character.resolved_archetype``) are deferred to Story 2.3 — their
    seat is right after ``builder.build()`` below, next to the SQLite
    save and world-materialization pipeline.
    """

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        if session._state != _State.Creating:
            return [
                _error_msg(
                    f"Cannot process CHARACTER_CREATION: session state is "
                    f"{session._state.name}, expected Creating"
                )
            ]
        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]
        sd = session._session_data
        if sd.builder is None:
            return [
                _error_msg(
                    f"No character builder active for genre '{sd.genre_slug}' "
                    f"— genre pack has no character_creation scenes"
                )
            ]

        builder = sd.builder
        payload: CharacterCreationPayload = msg.payload  # type: ignore[attr-defined]
        player_id: str = getattr(msg, "player_id", "") or sd.player_id
        span = trace.get_current_span()

        # ---- Navigation actions (back / unknown) --------------------------
        if payload.action is not None:
            action = payload.action
            if action == "back":
                span.add_event(
                    "character_creation.back",
                    {
                        "action": "back",
                        "from_scene": builder.current_scene_index(),
                        "player_id": player_id,
                    },
                )
                try:
                    builder.go_back()
                except BuilderError as exc:
                    return [_error_msg(f"Cannot go back: {exc!r}")]
                return [builder.to_scene_message(player_id)]

            return [_error_msg(f"Unknown chargen action: {action}")]

        # ---- Phase dispatch ----------------------------------------------
        phase = payload.phase
        logger.info("chargen.phase phase=%s player_id=%s", phase, player_id)

        if phase == "scene":
            return session._chargen_scene(builder, payload, sd, player_id, span)
        if phase == "continue":
            return session._chargen_continue(builder, sd, player_id, span)
        if phase == "confirmation":
            return await session._chargen_confirmation(builder, sd, player_id, span)
        if phase == "arrange_assign":
            return session._chargen_arrange_assign(builder, payload, sd, player_id, span)
        if phase == "arrange_clear":
            return session._chargen_arrange_clear(builder, payload, sd, player_id, span)
        if phase == "arrange_confirm":
            return session._chargen_arrange_confirm(builder, sd, player_id, span)
        if phase == "arrange_reject":
            return session._chargen_arrange_reject(builder, sd, player_id, span)
        if phase == "story_autogen":
            return session._chargen_story_autogen(builder, payload, sd, player_id, span)
        if phase == "story_confirm":
            return session._chargen_story_confirm(builder, payload, sd, player_id, span)
        return [_error_msg(f"Unknown chargen phase: {phase}")]


HANDLER = CharacterCreationHandler()
