"""FateThrowHandler — handles FATE_THROW messages (ADR-148, Story 126-7).

The player's PROACTIVE Fate roll is physics-is-the-roll: the four settled dF
faces on the wire ARE the roll. This handler mirrors ``FateActionHandler`` — same
state guard, seat resolution, and ruleset routing — but routes the action's
authoritative ``face[4]`` into ``dispatch_fate_action(thrown_faces=...)`` (so the
server never calls ``roll_4df`` on the player path) and echoes the thrower's
``throw_params`` on the broadcast ``FATE_ROLL`` so every seat replays the same
tumble. NPC/opponent and defense rolls inside the exchange stay server-side.
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


class FateThrowHandler:
    """Resolve a FATE_THROW from the acting player onto the Fate exchange,
    resolving the proactive action from the thrown dF faces (ADR-148)."""

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        from sidequest.game.ruleset import get_ruleset_module
        from sidequest.game.ruleset.fate import FateEconomyError
        from sidequest.protocol.fate import FateActionPayload
        from sidequest.server.dispatch.fate_conflict import (
            FateConflictError,
            dispatch_fate_action,
        )

        if session._state != _State.Playing:
            logger.info(
                "session.message_rejected_unbound type=FATE_THROW state=%s",
                session._state.name,
            )
            _emit_unbound_rejection_event("FATE_THROW", session._state.name)
            return [
                _error_msg(
                    "Cannot process FATE_THROW: not in Playing state",
                    code="session_unbound",
                )
            ]
        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]

        sd = session._session_data
        payload = msg.payload  # type: ignore[attr-defined]  # FateThrowPayload

        # Seat resolution is driven by the SERVER-authenticated identity
        # (``sd.player_id``) as the SOLE source; inbound ``msg.player_id`` is a
        # client-controlled OUTPUT annotation, never trusted as inbound identity
        # (Story 118-8 / ADR-119 — mirrors FateActionHandler). A non-empty inbound
        # id that disagrees is a spoof attempt: surface it to the GM panel and
        # proceed as the authenticated PC.
        acting_player_id = sd.player_id
        inbound_player_id = getattr(msg, "player_id", "") or ""
        if inbound_player_id and inbound_player_id != acting_player_id:
            logger.warning(
                "fate.throw.player_id_spoof_rejected inbound=%s authenticated=%s",
                inbound_player_id,
                acting_player_id,
            )
            from sidequest.telemetry.watcher_hub import publish_event

            publish_event(
                "state_transition",
                {
                    "field": "session_binding",
                    "op": "fate_throw_player_id_spoof_rejected",
                    "inbound_player_id": inbound_player_id,
                    "authenticated_player_id": acting_player_id,
                    "recovery": "auth_identity_enforced",
                    "source": "fate_throw",
                },
                component="session",
                severity="warning",
            )
        snapshot = sd.snapshot
        encounter = snapshot.encounter

        # Resolve the acting PC from the seat map (MP) then the solo fallback —
        # mirrors FateActionHandler so a multi-PC Fate table attributes the throw
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
            return [_error_msg("FATE_THROW: no character to act", code="fate_no_actor")]

        # ADR-148/149 (Story 126-8): a defend throw answers a FATE_DEFEND_REQUEST —
        # the defender's faces ARE the roll (never roll_4df). Route it to the defense
        # path BEFORE the proactive-commit build (FateActionPayload has no "defend").
        if payload.action == "defend":
            return await self._handle_defend(
                session=session,
                sd=sd,
                snapshot=snapshot,
                encounter=encounter,
                character=character,
                payload=payload,
                acting_player_id=acting_player_id,
            )

        # Build the dispatch intent from the throw payload — the FateThrowPayload
        # intent fields map 1:1 onto FateActionPayload — and route it with the
        # authoritative thrown faces.
        action_payload = FateActionPayload(
            request_id=payload.request_id,
            action=payload.action,
            skill=payload.skill,
            target=payload.target,
            difficulty=payload.difficulty,
            invoke_aspect=payload.invoke_aspect,
            invoke_mode=payload.invoke_mode,
            aspect_text=payload.aspect_text,
            player_action=payload.player_action,
        )

        ruleset = get_ruleset_module(sd.genre_pack.rules.ruleset)
        try:
            result = dispatch_fate_action(
                payload=action_payload,
                actor_name=character.core.name,
                encounter=encounter,
                ruleset=ruleset,
                snapshot=snapshot,
                rng=random.Random(),  # NPC/opponent + defense rolls only (server-side)
                round_number=snapshot.turn_manager.interaction,
                thrown_faces=payload.face,
            )
        except (FateConflictError, FateEconomyError) as exc:
            logger.warning("fate.throw.dispatch_error error=%s", exc)
            return [_error_msg(f"FATE_THROW rejected: {exc}", code="fate_dispatch_error")]

        # PARK at the DEFEND barrier (ADR-148/149, Story 126-8 §5): the proactive
        # commit closed the barrier and REVEAL seated an NPC attack on a PC. Broadcast
        # one FATE_DEFEND_REQUEST per incoming attack (the client filters by defender)
        # and PERSIST the parked checkpoint — the exchange now waits, resume-safe, on
        # the PCs' defenses. NO narration here (narration only at RESOLVE). Falls
        # through to broadcast the attacker's own proactive roll below.
        if result.awaiting_defense:
            from sidequest.protocol.messages import FateDefendRequestMessage

            room = sd._room
            if room is None:
                logger.error("fate.defend.broadcast_no_room — defend requests not delivered")
                return []
            for req in result.defend_requests:
                defender_pid = _seat_player_id(snapshot, req.defender)
                room.broadcast(
                    FateDefendRequestMessage(payload=req, player_id=defender_pid),
                    exclude_socket_id=None,
                )
            logger.info(
                "fate.defend.requests_emitted count=%d defenders=%s",
                len(result.defend_requests),
                ",".join(r.defender for r in result.defend_requests),
            )
            room.save()

        # BROADCAST the acting PC's own 4dF roll to the whole table (SOUL "The
        # Guitar Solo" — Story 118-7), echoing the THROWER's gesture so every seat
        # replays the same tumble and snaps to the authoritative dice (ADR-148).
        if result.action_roll is not None:
            from sidequest.game.dice import generate_dice_seed
            from sidequest.game.ruleset.fate_projection import build_fate_roll_payload
            from sidequest.protocol.messages import FateRollMessage

            roll_seed = generate_dice_seed(
                f"{sd.genre_slug}:{sd.world_slug}:{acting_player_id}",
                snapshot.turn_manager.interaction,
            )
            payload_out = build_fate_roll_payload(
                result.action_roll, seed=roll_seed, throw_params=payload.throw_params
            )
            roll_msg = FateRollMessage(payload=payload_out, player_id=acting_player_id)
            room = sd._room
            if room is None:
                # Playing state always has a room; a None here is a programming
                # error, not a path to silently drop the roll (No Silent Fallbacks).
                logger.error(
                    "fate.throw.broadcast_no_room actor=%s — roll not delivered",
                    character.core.name,
                )
                return []
            delivered = room.broadcast(roll_msg, exclude_socket_id=None)
            logger.info(
                "fate.throw.broadcast actor=%s player_id=%s recipients=%d "
                "dice=%s ladder=%d shifts=%d tier=%s",
                character.core.name,
                acting_player_id,
                len(delivered),
                payload_out.dice,
                payload_out.ladder_total,
                payload_out.shifts,
                payload_out.tier,
            )
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
                    "source": "player_thrown",
                },
                component="fate",
                severity="info",
            )
            return []
        return []

    async def _handle_defend(
        self,
        *,
        session: WebSocketSessionHandler,
        sd,
        snapshot,
        encounter,
        character,
        payload,
        acting_player_id: str,
    ) -> list[object]:
        """Record a PC's interactive defense (ADR-148/149, Story 126-8). The faces
        ARE the roll (never roll_4df); when the ledger fills, resume + narrate once."""
        from sidequest.game.ruleset import get_ruleset_module
        from sidequest.server.dispatch.fate_conflict import (
            FateConflictError,
            dispatch_fate_defense,
        )

        ruleset = get_ruleset_module(sd.genre_pack.rules.ruleset)
        try:
            defense = dispatch_fate_defense(
                encounter=encounter,
                snapshot=snapshot,
                ruleset=ruleset,
                actor_name=character.core.name,
                request_id=payload.request_id,
                skill=payload.skill,
                thrown_faces=payload.face,
            )
        except FateConflictError as exc:
            logger.warning("fate.defend.dispatch_error error=%s", exc)
            return [_error_msg(f"FATE_THROW(defend) rejected: {exc}", code="fate_defend_error")]

        return await self._finish_defense(
            session=session,
            sd=sd,
            ruleset=ruleset,
            character=character,
            payload=payload,
            defense=defense,
            acting_player_id=acting_player_id,
        )

    async def _finish_defense(
        self,
        *,
        session: WebSocketSessionHandler,
        sd,
        ruleset,
        character,
        payload,
        defense,
        acting_player_id: str,
    ) -> list[object]:
        """Broadcast the defender's own roll, then — once every pending defense is
        filled — RESUME the exchange, resolve it, and invoke the narrator EXACTLY
        once to render the whole woven exchange (ADR-148/149, Story 126-8 §3 step 5).
        While still parked, checkpoint and wait (No Silent Fallbacks: no auto-roll)."""
        room = sd._room

        # Broadcast the defender's own roll (role=defense) so the table sees it,
        # unless they conceded (no throw).
        if defense.defense_roll is not None and room is not None:
            from sidequest.game.dice import generate_dice_seed
            from sidequest.game.ruleset.fate_projection import build_fate_roll_payload
            from sidequest.protocol.messages import FateRollMessage

            seed = generate_dice_seed(
                f"{sd.genre_slug}:{sd.world_slug}:{acting_player_id}",
                sd.snapshot.turn_manager.interaction,
            )
            roll = build_fate_roll_payload(
                defense.defense_roll, seed=seed, throw_params=payload.throw_params
            )
            room.broadcast(
                FateRollMessage(payload=roll, player_id=acting_player_id),
                exclude_socket_id=None,
            )

        if not defense.ledger_full:
            # Still parked — checkpoint and wait for the remaining defenders.
            if room is not None:
                room.save()
            return []

        # RESUME → RESOLVE → NARRATE ONCE. This branch is reached only when
        # ``defense.ledger_full`` first flips True, so the narrator fires exactly once
        # per resolved exchange — the RESOLVE floor is THIS ledger gate, not a guard
        # inside the narration seam. Resolve the woven exchange mechanically, then
        # narrate it through the SAME server-resolved-replay seam the dice handler
        # uses (``_narrate_resolved_fate_exchange`` → ``_execute_narration_turn`` with
        # ``suppress_intent_router=True``); that seam owns persistence, husk-reaping,
        # and the per-peer NARRATION fan-out — so we do NOT hand-roll a
        # NarrationMessage or a second ``room.save()`` here. The ``ruleset`` resolved
        # in ``_handle_defend`` is threaded in (no redundant registry lookup).
        from sidequest.server.dispatch.fate_conflict import resume_fate_exchange

        resume_fate_exchange(
            encounter=sd.snapshot.encounter,
            snapshot=sd.snapshot,
            ruleset=ruleset,
            round_number=sd.snapshot.turn_manager.interaction,
        )
        action = "[FATE_EXCHANGE_RESOLVED] " + " ".join(sd.snapshot.encounter.narrator_hints)
        return await session._narrate_resolved_fate_exchange(sd, action)


def _seat_player_id(snapshot, name: str) -> str:
    """The player_id seated to PC ``name`` (invert ``snapshot.player_seats``), or
    "" if unseated — the FATE_DEFEND_REQUEST is broadcast and the client filters by
    ``defender``, so an empty id is a safe annotation, not a delivery failure."""
    return next((pid for pid, pc in (snapshot.player_seats or {}).items() if pc == name), "")


HANDLER = FateThrowHandler()
