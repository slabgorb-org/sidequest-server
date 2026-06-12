"""Handler: CHECK_THROW -> dispatch_check.

Pulls the rolling character's stats and level from the session snapshot
exactly as the dice_throw handler does (player_seats → character lookup
with fallback to characters[0] for solo/legacy paths), then resolves a
non-beat SWN skill check (2d6) or save (d20) via dispatch_check.

Exposes:
- ``handle_check_throw`` — the synchronous resolution core (used by tests).
- ``CheckThrowHandler`` / ``HANDLER`` — the ``MessageHandler``-protocol class
  that extracts session context and delegates to ``handle_check_throw``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

from sidequest.server.dispatch.check import CheckThrowOutcome, dispatch_check

logger = logging.getLogger(__name__)


def handle_check_throw(
    payload,
    *,
    snapshot,
    pack,
    rolling_player_id: str,
    session_id: str,
    room_broadcast,
) -> CheckThrowOutcome:
    """Resolve a CHECK_THROW payload via dispatch_check.

    Character resolution mirrors dice_throw.py exactly:
    - If snapshot.player_seats has an entry for rolling_player_id, use that
      character name to look up the matching Character in snapshot.characters.
    - Otherwise fall back to snapshot.characters[0] for solo / legacy paths.
    - If no characters at all, use empty stats and level=1.
    """
    # Mirror dice_throw.py's rolling_pc_name → character lookup verbatim.
    rolling_pc_name = (
        snapshot.player_seats.get(rolling_player_id) if snapshot.player_seats else None
    )
    if rolling_pc_name is not None:
        character = next(
            (c for c in snapshot.characters if c.core.name == rolling_pc_name),
            None,
        )
    else:
        character = snapshot.characters[0] if snapshot.characters else None

    stats: dict[str, int] = dict(character.stats) if character is not None else {}
    level: int = character.core.level if character is not None else 1
    name: str = character.core.name if character is not None else "Unknown"

    return dispatch_check(
        kind=payload.kind,
        attribute=payload.attribute,
        save=payload.save,
        skill_level=payload.skill_level,
        difficulty_key=payload.difficulty_key,
        level=level,
        label=payload.label or payload.kind,
        character_stats=stats,
        faces=payload.faces,
        pack=pack,
        rolling_player_id=rolling_player_id,
        character_name=name,
        session_id=session_id,
        room_broadcast=room_broadcast,
    )


class CheckThrowHandler:
    """Resolve a CHECK_THROW message from the rolling player.

    Extracts session context (snapshot, pack, rolling_player_id, room_broadcast)
    from the ``WebSocketSessionHandler``, then delegates to ``handle_check_throw``
    which calls ``dispatch_check``.

    Returns [] — the roll result is broadcast to the room via ``room_broadcast``
    inside ``dispatch_check`` (DiceRequest + DiceResult); there is no narrator
    run for a non-beat check.
    """

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        from sidequest.server.session_handler import _State
        from sidequest.server.session_helpers import _error_msg

        if session._state != _State.Playing:
            logger.info(
                "session.message_rejected_unbound type=CHECK_THROW state=%s",
                session._state.name,
            )
            return [
                _error_msg(
                    "Cannot process CHECK_THROW: not in Playing state",
                    code="session_unbound",
                ),
            ]
        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]

        sd = session._session_data
        payload = msg.payload  # type: ignore[attr-defined]
        rolling_player_id = getattr(msg, "player_id", "") or sd.player_id

        room_broadcast = None
        if session._room is not None:
            def _broadcast(m: object) -> None:
                assert session._room is not None
                session._room.broadcast(m, exclude_socket_id=None)

            room_broadcast = _broadcast

        session_id = f"{sd.genre_slug}:{sd.world_slug}:{sd.player_id}"

        try:
            handle_check_throw(
                payload,
                snapshot=sd.snapshot,
                pack=sd.genre_pack,
                rolling_player_id=rolling_player_id,
                session_id=session_id,
                room_broadcast=room_broadcast,
            )
        except (ValueError, NotImplementedError, KeyError) as exc:
            logger.warning("check.dispatch_error error=%s", exc)
            return [_error_msg(f"Check throw failed: {exc}")]
        return []


HANDLER = CheckThrowHandler()
