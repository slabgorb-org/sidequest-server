"""Story 108-6 (RED) — the turn-intake gate carves the stabilizable window out.

The incapacitation gate (handlers/player_action.py) blocks EVERY incapacitating
status today. The dying-window carve makes it distinguish two kinds:

  - TERMINAL-dead (incapacitating, NOT stabilizable) → block, as today. The
    CHARACTER_INCAPACITATED banner, zero turn, narrator never reached.
  - STABILIZABLE window (incapacitating AND stabilizable) → PERMIT the
    submission and route it to the narrator. The downed soloist gets a real
    part (SOUL.md "the Guitar Solo"); the lane is free-text, not a verb menu
    (SOUL.md "the Zork Problem").

This is the gap fix: permitting the submission re-supplies the loop driver that
solo play lost. Mirrors test_player_action_incapacitated_gate.py — drives the
REAL PlayerActionHandler.handle and proves the narration path is (window) /
is not (terminal) reached, plus the blocked-span (non-)fire.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.turn import TurnManager
from sidequest.protocol.enums import MessageType
from sidequest.telemetry.spans.encounter import SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED
from tests.server.test_dying_window_expiry import _WwnPack
from tests.server.test_player_action_incapacitated_gate import (
    DEAD_PC,
    _action_msg,
    _playing_session,
    _ReachedNarrationPath,
)


def _snapshot_with_status(status: Status, *, interaction: int = 1) -> GameSnapshot:
    core = CreatureCore(
        name=DEAD_PC,
        description="Martian mentalist, bleeding out alone",
        personality="defiant",
        hp=HpPool(current=0, max=10, base_max=10),
    )
    core.statuses.append(status)
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="barsoom",
        turn_manager=TurnManager(interaction=interaction),
    )
    snap.characters.append(
        Character(core=core, char_class="Mentalist", race="Martian", backstory="Arena-born.")
    )
    return snap


def _window_status() -> Status:
    # created_turn=0 with the snapshot at interaction=1 → demonstrably WITHIN the
    # deadline (0 + mortal_injury_rounds), so the permit must come from the real
    # carve (a real WwnConfig deadline computation), not the no-cfg fallback.
    return Status(
        text="Mortal Injury — dies in 6 rounds unless stabilized",
        severity=StatusSeverity.Scar,
        created_turn=0,
        created_in_encounter="combat",
        incapacitating=True,
        stabilizable=True,
    )


def _terminal_status() -> Status:
    return Status(
        text="Downed — dead (mortally wounded)",
        severity=StatusSeverity.Scar,
        created_turn=5,
        created_in_encounter="combat",
        incapacitating=True,
    )


@pytest.mark.asyncio
async def test_stabilizable_window_permits_action_into_narration(monkeypatch, otel_capture):
    """A downed soloist carrying the stabilizable window must NOT be blocked —
    the action passes the gate and reaches the narration path (proven by the
    post-gate sentinel)."""
    from sidequest.handlers.player_action import HANDLER

    monkeypatch.setattr("sidequest.handlers.player_action._watcher_publish", lambda *a, **k: None)
    session = _playing_session(_snapshot_with_status(_window_status(), interaction=1))
    # Bind a REAL WwnConfig pack so the carve exercises the real deadline
    # computation (within-deadline → permit), NOT the no-cfg capability-gate
    # fallback. Without this the test would pass even if the permit logic broke.
    session._session_data.genre_pack = _WwnPack()
    session._retrieve_lore_for_turn = AsyncMock(side_effect=_ReachedNarrationPath)

    with pytest.raises(_ReachedNarrationPath):
        await HANDLER.handle(session, _action_msg())

    # The blocked span must NOT fire — the window was permitted, not refused.
    blocked = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED
    ]
    assert not blocked, "a stabilizable window must not trip the incapacitation block"


@pytest.mark.asyncio
async def test_terminal_status_still_blocks(monkeypatch, otel_capture):
    """Regression guard: a true terminal-dead status still blocks (the #846
    coherence and the barsoom-3 dead-man-walking fix stay intact)."""
    from sidequest.handlers.player_action import HANDLER

    monkeypatch.setattr("sidequest.handlers.player_action._watcher_publish", lambda *a, **k: None)
    session = _playing_session(_snapshot_with_status(_terminal_status()))

    outbound = await HANDLER.handle(session, _action_msg())

    session._execute_narration_turn.assert_not_called()
    assert len(outbound) == 1
    assert outbound[0].type == MessageType.CHARACTER_INCAPACITATED
    blocked = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED
    ]
    assert blocked, "a terminal status must still emit the blocked-incapacitated span"
