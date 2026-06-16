"""Session-handler wiring: DICE_THROW reaches dispatcher + broadcasts fan out.

Story 34 port (2026-04-24). Asserts that:

1. ``handle_message`` routes ``DICE_THROW`` to ``_handle_dice_throw`` and
   not to the fallback "Unsupported message type" path.
2. When a room is attached, ``DICE_REQUEST`` and ``DICE_RESULT`` are
   broadcast to every socket's outbound queue — multiplayer visibility.
3. Stashed ``pending_roll_outcome`` is set so the next narration turn can
   see the roll outcome via TurnContext.
4. The narrator runs inline with a synthesized beat-resolved action, so
   the rolling player gets ``NARRATION`` in the same round trip.

Task 12 (2026-04-25): Rewritten for dual-dial encounters. The module-level
skip added in Task 8 (MetricDirection removed) is lifted. Helpers updated
to use player_metric + opponent_metric.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef
from sidequest.protocol.dice import (
    DiceThrowPayload,
    RollOutcome,
    ThrowParams,
)
from sidequest.protocol.messages import (
    DiceRequestMessage,
    DiceResultMessage,
    DiceThrowMessage,
    NarrationMessage,
)


def _install_combat_def(sd) -> None:
    """Replace the loaded pack's confrontations with one deterministic combat def."""
    cdef = ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STRENGTH",
                }
            ),
        ],
    )
    sd.genre_pack.rules.confrontations = [cdef]


def _install_active_encounter(sd) -> None:
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[EncounterActor(name="Rux", role="combatant", side="player")],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    sd.snapshot.encounter = enc


def _throw(face: int = 15, beat_id: str = "attack") -> DiceThrowMessage:
    return DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id="wire-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id=beat_id,
        ),
        player_id="player-1",
    )


class _StubRoom:
    """Minimal SessionRoom stand-in — captures broadcasts for assertions."""

    slug = "test-slug"

    def __init__(self) -> None:
        self.broadcasts: list[tuple[object, str | None]] = []
        from sidequest.game.session import GameSnapshot
        from sidequest.server.session import Session

        self.session = Session(GameSnapshot(genre_slug="caverns_and_claudes"))

    def broadcast(self, msg: object, *, exclude_socket_id: str | None = None) -> None:
        self.broadcasts.append((msg, exclude_socket_id))

    def is_paused(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_dice_throw_routes_through_handle_message(session_handler_factory):
    """handle_message must route DICE_THROW — not fall through to the
    ``Unsupported message type in Phase 1`` error."""
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    # Give Rux a mechanical stat — D&D stats use TitleCase in some packs.
    sd.snapshot.characters[0].stats["STRENGTH"] = 14  # +2 modifier

    # Narrator returns a simple text — just enough for _execute_narration_turn
    # to build a NarrationMessage and return.
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You land the blow!"),
    )

    msgs = await handler.handle_message(_throw())

    # Narrator invoked exactly once with the synthesized beat-resolved action.
    sd.orchestrator.run_narration_turn.assert_called_once()
    call_args = sd.orchestrator.run_narration_turn.call_args
    action_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("action")
    assert "[BEAT_RESOLVED]" in action_arg
    assert "Attack" in action_arg
    assert "STRENGTH" in action_arg
    assert "Roll:" in action_arg

    # Narration flows to the rolling client via the handler return path.
    narration = [m for m in msgs if isinstance(m, NarrationMessage)]
    assert len(narration) == 1


@pytest.mark.asyncio
async def test_dice_throw_broadcasts_request_and_result_to_room(
    session_handler_factory,
):
    """Multiplayer visibility: DICE_REQUEST + DICE_RESULT fan out to every
    socket queue so spectators' dice overlays render in sync."""
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    room = _StubRoom()
    handler._room = room  # type: ignore[assignment]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You land the blow!"),
    )

    await handler.handle_message(_throw())

    # Dice messages broadcast in order: DiceRequest first (spectator overlay
    # opens), DiceResult second (everyone sees the outcome).
    dice_broadcasts = [
        m
        for m, _exclude in room.broadcasts
        if isinstance(m, (DiceRequestMessage, DiceResultMessage))
    ]
    assert len(dice_broadcasts) == 2
    assert isinstance(dice_broadcasts[0], DiceRequestMessage)
    assert isinstance(dice_broadcasts[1], DiceResultMessage)
    assert dice_broadcasts[0].payload.request_id == "wire-req-1"
    assert dice_broadcasts[1].payload.request_id == "wire-req-1"
    # Rolling player included in the broadcast — same request_id, UI is
    # idempotent so no double-open on the rolling client.
    assert all(exclude is None for _m, exclude in room.broadcasts)


@pytest.mark.asyncio
async def test_dice_throw_stashes_pending_roll_outcome(session_handler_factory):
    """Rust parity: pending_roll_outcome stashed for the next narration turn.

    The DICE_THROW handler inlines the narration turn (handler returns the
    narration messages in the same WebSocket round-trip), and the narration
    turn's beat-apply path reads + clears ``sd.pending_roll_outcome`` (ADR-074
    failure-branch wiring — see ``_execute_narration_turn``). To observe the
    stash before it's consumed, stub ``_execute_narration_turn`` so the
    dispatch returns before the consumer runs.

    face=14, STRENGTH=14 (+2), DC=14 → total 16 → Success (margin 2 < DECISIVE_MARGIN=3).
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    # Capture pending_roll_outcome at the moment the dispatch hands off to
    # the narration turn, BEFORE the turn's beat-apply consumes it.
    stashed: list[object] = []

    async def _capture_and_skip(sd_, action, ctx):  # noqa: ANN001
        stashed.append(sd_.pending_roll_outcome)
        return []

    handler._execute_narration_turn = _capture_and_skip  # type: ignore[method-assign]

    await handler.handle_message(_throw(face=14))

    assert stashed == [RollOutcome.Success], (
        f"DICE_THROW must stash the RollOutcome on sd.pending_roll_outcome "
        f"before invoking the narration turn; got {stashed!r}"
    )


@pytest.mark.asyncio
async def test_dice_throw_returns_error_when_not_playing(session_handler_factory):
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    # _state is default (AwaitingConnect) — not Playing.
    _install_combat_def(sd)
    _install_active_encounter(sd)

    msgs = await handler.handle_message(_throw())

    from sidequest.protocol.messages import ErrorMessage

    errors = [m for m in msgs if isinstance(m, ErrorMessage)]
    assert len(errors) == 1
    assert "not in Playing state" in str(errors[0].payload.message)


@pytest.mark.asyncio
async def test_dice_throw_error_surfaces_when_no_active_encounter(
    session_handler_factory,
):
    """Graceful failure: beat_id without an active encounter → ERROR, no crash."""
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    # Note: no _install_active_encounter — snapshot.encounter stays None.

    msgs = await handler.handle_message(_throw())

    from sidequest.protocol.messages import ErrorMessage

    errors = [m for m in msgs if isinstance(m, ErrorMessage)]
    assert len(errors) == 1
    assert "active encounter" in str(errors[0].payload.message)
    # Narrator not invoked when dispatch fails.
    if hasattr(sd.orchestrator, "run_narration_turn"):
        run = sd.orchestrator.run_narration_turn
        if hasattr(run, "assert_not_called"):
            run.assert_not_called()


def test_dice_throw_message_type_is_routable() -> None:
    """Regression: the Dice* messages must parse through GameMessage."""
    from sidequest.protocol import GameMessage

    raw = DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id="r",
            throw_params=ThrowParams(
                velocity=(0.0, 0.0, 0.0),
                angular=(0.0, 0.0, 0.0),
                position=(0.5, 0.5),
            ),
            face=[12],
            beat_id="attack",
        ),
        player_id="p",
    ).model_dump_json()
    parsed = GameMessage.model_validate_json(raw)
    assert parsed.type.value == "DICE_THROW"
    # Smooth re-serialization — no extra/stripped fields.
    out = parsed.model_dump_json()
    assert '"type":"DICE_THROW"' in out


# asyncio marker for the test module
_ = asyncio  # keep import — some sub-fixtures assume asyncio is imported
