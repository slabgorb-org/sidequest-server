"""Turn-intake incapacitation gate (sq-playtest 2026-06-07 barsoom-3, BLOCKING).

A PC the genre lethality policy ruled ``dead`` kept full turn agency for four
rounds — the narrator kept responding to a corpse's actions and the player had
to infer his own death. ``post_resolution_lethality`` stamps an
``incapacitating`` status on the downed PC; the PLAYER_ACTION handler must read
that durable marker and REFUSE the action *before* the narrator dispatch, so:

  * the dead PC's action never reaches ``_execute_narration_turn`` (the
    dead-man-walking stops),
  * a CHARACTER_INCAPACITATED surface goes back to the client (banner / lock /
    re-roll CTA),
  * a ``session.player_action_blocked_incapacitated`` span fires (GM-panel
    lie-detector — missing span ⇒ the gate isn't wired).

This is the wiring test: it drives the real ``PlayerActionHandler.handle`` and
asserts the narrator is never reached.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.turn import TurnManager
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import _State
from sidequest.telemetry.spans.encounter import SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED

DEAD_PC = "Abinthe Moridusk"


def _snapshot_with_dead_pc() -> GameSnapshot:
    core = CreatureCore(
        name=DEAD_PC,
        description="Martian mentalist, fallen in the arena",
        personality="defiant",
        hp=HpPool(current=0, max=10, base_max=10),
    )
    core.statuses.append(
        Status(
            text="Downed — dead (mortally wounded)",
            severity=StatusSeverity.Scar,
            created_turn=5,
            created_in_encounter="combat",
            incapacitating=True,
        )
    )
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="barsoom",
        turn_manager=TurnManager(),
    )
    snap.characters.append(
        Character(core=core, char_class="Mentalist", race="Martian", backstory="Arena-born.")
    )
    return snap


def _playing_session(snapshot: GameSnapshot) -> MagicMock:
    """Solo session in Playing state, bound to a snapshot whose only PC is dead."""
    sd = MagicMock()
    sd.snapshot = snapshot
    sd.player_name = DEAD_PC
    sd.player_id = "p1"
    sd.game_slug = "2026-06-07-barsoom-3"
    sd.world_slug = "barsoom"

    session = MagicMock()
    session._state = _State.Playing
    session._room = None  # solo
    session._session_data = sd
    # The narrator dispatch — must NEVER be awaited for a dead PC.
    session._execute_narration_turn = AsyncMock(
        side_effect=AssertionError("a dead PC's action must not reach the narrator")
    )
    return session


def _action_msg() -> PlayerActionMessage:
    return PlayerActionMessage(
        type=MessageType.PLAYER_ACTION,
        payload=PlayerActionPayload(
            action=NonBlankString("I press my hand to the wound."), round=6
        ),
        player_id="p1",
    )


@pytest.mark.asyncio
async def test_dead_pc_action_is_refused_and_surfaces_death(monkeypatch, otel_capture):
    from sidequest.handlers.player_action import HANDLER

    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, fields: dict[str, Any], **kwargs: Any) -> None:
        captured.append({"event_type": event_type, **fields})

    # player_action.py binds the watcher entrypoint at module import
    # (``_watcher_publish``), so patch the bound name, not the source.
    monkeypatch.setattr("sidequest.handlers.player_action._watcher_publish", _spy)

    session = _playing_session(_snapshot_with_dead_pc())

    outbound = await HANDLER.handle(session, _action_msg())

    # The narrator was never reached (AsyncMock side_effect would have raised).
    session._execute_narration_turn.assert_not_called()

    # The death surface goes back to the client.
    assert len(outbound) == 1, f"expected one CHARACTER_INCAPACITATED frame; got {outbound!r}"
    msg = outbound[0]
    assert msg.type == MessageType.CHARACTER_INCAPACITATED
    assert msg.payload.character_name == DEAD_PC
    assert msg.payload.verdict == "dead"
    assert msg.payload.headline == f"{DEAD_PC} has fallen."
    assert msg.player_id == "p1"

    # The GM-panel lie-detector event.
    blocked = [e for e in captured if "blocked_incapacitated" in str(e.get("field", ""))]
    assert blocked, f"the gate must publish a blocked-incapacitated watcher event; got {captured!r}"
    assert blocked[0]["character"] == DEAD_PC
    assert blocked[0]["verdict"] == "dead"

    # And the OTEL span.
    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED
    ]
    assert spans, "the gate must emit session.player_action_blocked_incapacitated"
    assert (spans[0].attributes or {}).get("character") == DEAD_PC


class _ReachedNarrationPath(Exception):
    """Sentinel: control passed the incapacitation gate into the narration
    pipeline (the gate returns BEFORE lore retrieval, so reaching this proves
    the action was NOT gated)."""


@pytest.mark.asyncio
async def test_living_pc_action_passes_the_gate(monkeypatch, otel_capture):
    """A PC with no incapacitating status must NOT be gated. The gate returns
    early (before ``_retrieve_lore_for_turn``); a living PC therefore reaches
    that seam — we trip a sentinel there to prove the gate let the action
    through without driving the whole async narration pipeline."""
    from sidequest.handlers.player_action import HANDLER

    monkeypatch.setattr("sidequest.handlers.player_action._watcher_publish", lambda *a, **k: None)

    snap = _snapshot_with_dead_pc()
    # Resurrect: clear the incapacitating status.
    snap.characters[0].core.statuses = []
    session = _playing_session(snap)
    # Fire a sentinel the moment control reaches the post-gate narration path.
    session._retrieve_lore_for_turn = AsyncMock(side_effect=_ReachedNarrationPath)

    with pytest.raises(_ReachedNarrationPath):
        await HANDLER.handle(session, _action_msg())

    # The gate never tripped: no blocked span fired on the way through.
    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED
    ]
    assert not spans, "a living PC must not trip the incapacitation gate"
