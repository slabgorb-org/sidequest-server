"""YieldHandler — handles YIELD messages (player withdraws from encounter)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.protocol.messages import ConfrontationMessage, ConfrontationPayload
from sidequest.server.session_handler import _State
from sidequest.server.session_helpers import _error_msg

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


class YieldHandler:
    """Handle a YIELD message — player withdraws from the active encounter.

    Marks the actor withdrawn; resolves the encounter when every
    player-side actor has yielded or been taken out; refunds edge.

    Playtest 2026-04-30 fix: pre-fix, this handler returned ``[]`` on
    success — `handle_yield` mutated the snapshot but emitted no
    outbound message, so the UI showed stale state and the player
    perceived a silent no-op. The watcher-hub events from
    `dispatch/yield_action.py` (`yield_received`, `yield_resolved`,
    `resolved`) publish to the GM dashboard but NOT to the player's
    own socket and NOT to the standard logger, so `grep yield
    /tmp/sidequest-server.log` returned nothing for the entire
    session — the canonical "no observable effect" failure mode.

    Post-fix:

    1. Log INFO at handler entry/exit so the session log carries a
       grep-able yield trail.
    2. Emit a fresh CONFRONTATION message to the player so the UI
       reflects ``actor.withdrawn=True`` and ``encounter.active=False``
       immediately. When the encounter is fully resolved (every
       player-side actor withdrawn) the payload is built with
       ``active=False`` so the overlay unmounts.

    Narration of the yielded outcome still flows through
    ``pending_resolution_signal`` on the next narrator turn — that's
    the existing spec contract (2026-04-25-dual-track-momentum-design.md
    §Yield action). The player gets immediate UI feedback here; the
    narrator gets the final word on the next exchange.
    """

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        from sidequest.server.dispatch.confrontation import (
            build_clear_confrontation_payload,
            build_confrontation_payload,
            find_confrontation_def,
            resolve_recipient_pc,
        )
        from sidequest.server.dispatch.yield_action import handle_yield

        if session._state != _State.Playing:
            # Playtest 2026-04-30: uvicorn reload zombies session binding.
            # See handlers/player_action.py for the full rationale —
            # tagging with ``session_unbound`` lets the client auto-
            # recover by re-firing SESSION_EVENT{connect}.
            logger.info(
                "session.message_rejected_unbound type=YIELD state=%s",
                session._state.name,
            )
            return [
                _error_msg(
                    "Cannot process YIELD: not in Playing state",
                    code="session_unbound",
                ),
            ]
        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]

        sd = session._session_data
        player_id = getattr(msg, "player_id", "") or sd.player_id
        player_name = sd.player_name

        # Capture pre-yield state so logging can compare resolved/unresolved
        # transitions. ``snapshot.encounter`` is the live mutable structure
        # — handle_yield mutates it in place; we have to read the type
        # before the call because a fully-resolved encounter remains on
        # ``snapshot.encounter`` (cleared only by the next narrator turn).
        pre_encounter = sd.snapshot.encounter
        encounter_type = pre_encounter.encounter_type if pre_encounter is not None else ""

        logger.info(
            "session.yield_received player_id=%s player_name=%s encounter_type=%r",
            player_id,
            player_name,
            encounter_type,
        )

        if sd._room is None:
            # Slug-connect branch always sets _room; this path is defensive.
            return [_error_msg("Internal error: session not bound to a room")]
        try:
            handle_yield(sd.snapshot, room=sd._room, player_id=player_id, player_name=player_name)
        except ValueError as exc:
            logger.warning(
                "session.yield_rejected player_id=%s player_name=%s reason=%s",
                player_id,
                player_name,
                exc,
            )
            return [_error_msg(str(exc))]

        # Surface the new encounter state to the player. The overlay
        # consumes the payload's ``active`` flag — if the encounter is
        # now resolved (all player-side actors withdrawn) we explicitly
        # build the clear payload so the UI unmounts. Otherwise emit
        # the live state with the updated actors list (Sage's actor
        # now ``withdrawn=True``) so the player sees their commitment
        # reflected immediately.
        outbound: list[object] = []
        post_encounter = sd.snapshot.encounter
        if post_encounter is not None:
            defs = sd.genre_pack.rules.confrontations if sd.genre_pack.rules else []
            cdef = find_confrontation_def(defs, post_encounter.encounter_type)
            if post_encounter.resolved:
                payload_dict = build_clear_confrontation_payload(
                    encounter_type=post_encounter.encounter_type,
                    genre_slug=sd.genre_slug,
                )
                logger.info(
                    "session.yield_resolved encounter_type=%r outcome=%s",
                    post_encounter.encounter_type,
                    post_encounter.outcome,
                )
            elif cdef is not None:
                # Encounter persists (multi-actor party, partial yield). Send
                # the live state so the UI mirrors actor.withdrawn=True.
                # Story 49-7: project beats to the yielding player's class so
                # the Confrontation tab reflects the actor's legal moves
                # (Fighter doesn't suddenly see Cast Spell mid-yield).
                recipient_pc, recipient_actor = resolve_recipient_pc(
                    snapshot=sd.snapshot,
                    genre_pack=sd.genre_pack,
                    player_id=player_id,
                )
                payload_dict = build_confrontation_payload(
                    encounter=post_encounter,
                    cdef=cdef,
                    genre_slug=sd.genre_slug,
                    recipient_pc=recipient_pc,
                    recipient_actor_name=recipient_actor,
                )
                # Seat-aware count: NPC companions on the player side are
                # excluded so the log line matches the actual yield gate
                # used by ``handle_yield``. Pre-fix this counted every
                # player-side actor including hirelings, which is what
                # surfaced as ``remaining_player_actors=1`` in the
                # 2026-05-06 sumpdrake-fight soft-lock (Donut counted as
                # the missing committer).
                seated_pc_names = set(sd.snapshot.player_seats.values())
                if seated_pc_names:
                    remaining = sum(
                        1
                        for a in post_encounter.actors
                        if a.side == "player" and a.name in seated_pc_names and not a.withdrawn
                    )
                else:
                    remaining = sum(
                        1 for a in post_encounter.actors if a.side == "player" and not a.withdrawn
                    )
                logger.info(
                    "session.yield_partial encounter_type=%r remaining_player_actors=%d",
                    post_encounter.encounter_type,
                    remaining,
                )
            else:
                # Confrontation def vanished mid-session (content swap). Fail
                # loud so the GM panel sees the orphan rather than silently
                # leaving the overlay stuck.
                logger.warning(
                    "session.yield_orphan_confrontation_def encounter_type=%r",
                    post_encounter.encounter_type,
                )
                payload_dict = build_clear_confrontation_payload(
                    encounter_type=post_encounter.encounter_type,
                    genre_slug=sd.genre_slug,
                )
            outbound.append(
                ConfrontationMessage(
                    payload=ConfrontationPayload(**payload_dict),
                    player_id=player_id,
                )
            )

        return outbound


HANDLER = YieldHandler()
