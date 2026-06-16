"""DiceThrowHandler — handles DICE_THROW messages (UI-driven roll)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sidequest.server.session_handler import _State
from sidequest.server.session_helpers import _build_turn_context, _error_msg
from sidequest.telemetry.phase_timing import PhaseTimings

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


class DiceThrowHandler:
    """Resolve a DICE_THROW from the rolling player.

    The UI drives all rolls via confrontation beat selection: it builds
    the DiceRequest locally, auto-rolls in Rapier, and sends a single
    DICE_THROW carrying the beat_id + physics-settled faces. The server
    applies the beat, resolves the dice, broadcasts DiceRequest +
    DiceResult to the room, and then runs the narrator inline so the
    rolling player sees prose in the same round-trip.

    Returns [] — all outbound messages go through the room broadcast
    queue so every connected socket (rolling player included) sees the
    same event stream.
    """

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        from sidequest.server.dispatch.dice import (
            DiceDispatchError,
            dispatch_dice_throw,
        )

        # Handler-entry phase timer — same rationale as player_action:
        # the dice replay path also runs lore_retrieval + turn_context
        # build before invoking the narrator, and that pre-narrator work
        # is invisible without a timer that starts here.
        timings = PhaseTimings(action_received_monotonic=time.monotonic())

        if session._state != _State.Playing:
            # Playtest 2026-04-30: uvicorn reload zombies session binding.
            # See handlers/player_action.py for the full rationale —
            # tagging with ``session_unbound`` lets the client auto-
            # recover by re-firing SESSION_EVENT{connect}.
            logger.info(
                "session.message_rejected_unbound type=DICE_THROW state=%s",
                session._state.name,
            )
            return [
                _error_msg(
                    "Cannot process DICE_THROW: not in Playing state",
                    code="session_unbound",
                ),
            ]
        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]

        sd = session._session_data
        payload = msg.payload  # type: ignore[attr-defined]
        rolling_player_id = getattr(msg, "player_id", "") or sd.player_id

        snapshot = sd.snapshot
        encounter = snapshot.encounter
        # Resolve the rolling PC from the multiplayer seat map first
        # (snapshot.player_seats[rolling_player_id] -> character name) and
        # only fall back to snapshot.characters[0] for legacy / solo paths
        # where seats are absent. Pre-fix every DICE_THROW was attributed
        # to characters[0] regardless of who clicked — in 3-PC MP this
        # meant Donut's WIS roll resolved against Carl's stats, Carl's
        # actor name was stashed as the opposed_check pending actor for
        # everyone, and the secondary "opposed_check: no stat 'WIS' for
        # opponent 'Carl'" error surfaced when Donut clicked Turn Undead
        # against the moth. Playtest 2026-05-12 17:55–18:00 caverns_sunden.
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
        character_name = character.core.name if character is not None else "Unknown"
        stats: dict[str, int] = dict(character.stats) if character is not None else {}

        room_broadcast = None
        connected_player_ids: list[str] | None = None
        per_recipient_emit = None
        if session._room is not None:
            # Wrap the room's broadcast to a simple callable the dispatcher
            # can invoke without knowing about SessionRoom. exclude=None so
            # every connected socket (rolling + spectators) receives the
            # same DiceRequest + DiceResult stream.
            def _broadcast(m: object) -> None:
                assert session._room is not None  # captured under the guard above
                session._room.broadcast(m, exclude_socket_id=None)

            room_broadcast = _broadcast

            # Story 49-7: per-PC CONFRONTATION overlay. Capture the room
            # at handler entry so the dispatcher can fan a class-filtered
            # CONFRONTATION to each connected player after the canonical
            # full-union broadcast above. ``getattr`` with callable guards
            # because a couple of older stub-room test fixtures
            # (e.g. _StubRoom in test_dice_throw_*) don't expose the full
            # SessionRoom API — those fixtures keep the legacy single-
            # broadcast behavior and don't engage the per-PC overlay.
            connected_player_ids_fn = getattr(session._room, "connected_player_ids", None)
            socket_for_player_fn = getattr(session._room, "socket_for_player", None)
            queue_for_socket_fn = getattr(session._room, "queue_for_socket", None)
            if (
                callable(connected_player_ids_fn)
                and callable(socket_for_player_fn)
                and callable(queue_for_socket_fn)
            ):
                connected_player_ids = list(connected_player_ids_fn())

                def _per_recipient_emit(pid: str, m: object) -> None:
                    sid = socket_for_player_fn(pid)
                    if sid is None:
                        return
                    q = queue_for_socket_fn(sid)
                    if q is None:
                        return
                    q.put_nowait(m)

                per_recipient_emit = _per_recipient_emit

        try:
            outcome = dispatch_dice_throw(
                payload=payload,
                rolling_player_id=rolling_player_id,
                character_name=character_name,
                character_stats=stats,
                encounter=encounter,
                pack=sd.genre_pack,
                genre_slug=sd.genre_slug,
                session_id=f"{sd.genre_slug}:{sd.world_slug}:{sd.player_id}",
                round_number=snapshot.turn_manager.interaction,
                room_broadcast=room_broadcast,
                snapshot=snapshot,
                connected_player_ids=connected_player_ids,
                per_recipient_emit=per_recipient_emit,
            )
        except DiceDispatchError as exc:
            logger.warning("dice.dispatch_error error=%s", exc)
            # Defensive UI resync (playtest 2026-04-30 "Confrontation
            # UI/server state desync"). When the dispatch rejects a
            # DICE_THROW because the encounter is missing or already
            # resolved, the UI has stale state — it's still rendering
            # action buttons for an encounter that no longer exists
            # server-side. Causes include: (a) natural beat-driven
            # resolution where the prior_live → now_live emit at
            # session_handler.py was missed (defense-in-depth path),
            # (b) uvicorn ``--reload`` mid-session that wiped in-memory
            # encounter state while the React store kept the action
            # menu, (c) any future state-machine path that resolves
            # the encounter without emitting a clear. Whatever the
            # cause, the right user-facing recovery is the same:
            # force-resync by emitting a clear CONFRONTATION so the
            # overlay unmounts. The error message still flows so the
            # player sees the rejection and the GM panel sees the
            # span, but the UI doesn't get stuck in a state where
            # every click bounces.
            outbound: list[object] = [_error_msg(f"Dice throw failed: {exc}")]
            stale_encounter_type: str | None = None
            if encounter is not None:
                stale_encounter_type = encounter.encounter_type
            if stale_encounter_type and "active encounter" in str(exc):
                from sidequest.protocol.messages import (
                    ConfrontationMessage,
                    ConfrontationPayload,
                )
                from sidequest.server.dispatch.confrontation import (
                    build_clear_confrontation_payload,
                )

                clear_dict = build_clear_confrontation_payload(
                    encounter_type=stale_encounter_type,
                    genre_slug=sd.genre_slug,
                )
                outbound.append(
                    ConfrontationMessage(
                        payload=ConfrontationPayload(**clear_dict),
                        player_id=rolling_player_id,
                    )
                )
                logger.info(
                    "dice.stale_encounter_resync encounter_type=%r reason=%s",
                    stale_encounter_type,
                    "active encounter" if "active encounter" in str(exc) else "missing",
                )
            return outbound

        # Encounter just resolved via dice — front-door scene-end through
        # Session.end_scene (Task E.3 of session-aggregate strangler).
        # end_scene runs the Scratch sweep (Playtest 2026-04-26 Bug #1)
        # and advances the orbital clock by one ENCOUNTER beat, emitting
        # both encounter.status_cleared (per cleared status) and
        # clock.advance spans. Matches the front-door pattern used by the
        # narrator-beat resolution path in narration_apply.py and the
        # YIELD path in dispatch/yield_action.py.
        if outcome.encounter_resolved:
            if sd._room is None:
                # Slug-connect branch always sets _room; this is a
                # programming-error path. Surface as a hard error.
                raise RuntimeError(
                    "DiceThrowHandler: sd._room is None — slug-connect wiring missing"
                )
            sd._room.session.end_scene(
                "scene_end",
                turn=snapshot.turn_manager.interaction,
            )

        # Persist the resolved outcome so follow-up narrator runs can use it
        # (Rust parity: pending_roll_outcome). Stashed on session_data for
        # the next turn's TurnContext to pick up if needed.
        sd.pending_roll_outcome = outcome.outcome
        sd.pending_roll_actor = character_name
        # Opposed-check deferral (combat fairness, 2026-04-26). When the
        # dispatcher reports the beat was deferred, stash the player roll
        # + beat_id so ``_apply_narration_result_to_snapshot`` can pick
        # them up and run the resolver inline once the narrator emits the
        # opponent's beat.
        if outcome.opposed_pending:
            sd.pending_opposed_player_d20 = outcome.opposed_player_d20
            sd.pending_opposed_player_beat_id = outcome.opposed_player_beat_id

        # Run the narrator inline with the synthesized beat-resolved action
        # so the rolling player sees prose in the same WebSocket round-trip.
        # Matches the Rust deferred-narrator intent end-to-end, collapsed to
        # a single server tick since Python's handler is sync w.r.t. the
        # read loop.
        with timings.phase("lore_retrieval"):
            lore_context = await session._retrieve_lore_for_turn(
                sd,
                outcome.replay_action_text,
            )
        with timings.phase("turn_context_build"):
            turn_context = _build_turn_context(
                sd,
                lore_context=lore_context,
                room=session._room,
            )
        turn_context.phase_timings = timings
        return await session._execute_narration_turn(
            sd,
            outcome.replay_action_text,
            turn_context,
        )


HANDLER = DiceThrowHandler()
