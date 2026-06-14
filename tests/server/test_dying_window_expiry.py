"""Story 108-6 (RED) — the dying-window clock cannot be paused; it expires to death.

AC4: each submission burns a round, the deadline is fixed, and stalling spends
the clock rather than pausing it. When a downed soloist keeps submitting past
``created_turn + mortal_injury_rounds`` without stabilizing, the window must
convert to terminal-dead on the player's OWN turn — NOT only when some other
resolved-down encounter happens to run. The expiry therefore has to fire on the
player-action path (which has the bound pack's cfg — player_action.py reads
``session._session_data.genre_pack``), not solely in apply_post_resolution_lethality.

This test pins the OUTCOME (implementation-agnostic): a window whose deadline has
passed, when the PC submits again, is refused as terminal and the stabilizable
window is gone. The interaction round is set far past any reasonable deadline so
the assertion does not depend on the exact ``mortal_injury_rounds`` value.

Harness: the real PlayerActionHandler.handle, with a REAL WwnConfig genre pack
bound so the deadline derives from cfg.trauma.mortal_injury_rounds.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import WwnConfig
from sidequest.protocol.enums import MessageType
from tests.server.test_player_action_incapacitated_gate import (
    DEAD_PC,
    _action_msg,
    _playing_session,
)

_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}


@dataclass
class _WwnRules:
    ruleset: str = "wwn"
    _cfg: WwnConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cfg is None:
            self._cfg = WwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> WwnConfig:
        return self._cfg


@dataclass
class _WwnPack:
    rules: _WwnRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _WwnRules()


def _snapshot_past_deadline() -> GameSnapshot:
    core = CreatureCore(
        name=DEAD_PC,
        description="bled out while crawling for the door",
        personality="defiant",
        hp=HpPool(current=0, max=10, base_max=10),
    )
    core.statuses.append(
        Status(
            text="Mortal Injury — dies in 6 rounds unless stabilized",
            severity=StatusSeverity.Scar,
            created_turn=3,
            created_in_encounter="combat",
            incapacitating=True,
            stabilizable=True,
        )
    )
    # interaction 100 ≫ created_turn 3 + any mortal_injury_rounds → deadline passed.
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=100),
    )
    snap.characters.append(
        Character(core=core, char_class="Warrior", race="Human", backstory="Born underground.")
    )
    return snap


@pytest.mark.asyncio
async def test_submitting_past_deadline_is_refused_as_terminal(monkeypatch, otel_capture):
    from sidequest.handlers.player_action import HANDLER

    monkeypatch.setattr(
        "sidequest.handlers.player_action._watcher_publish", lambda *a, **k: None
    )
    session = _playing_session(_snapshot_past_deadline())
    session._session_data.genre_pack = _WwnPack()

    outbound = await HANDLER.handle(session, _action_msg())

    # Stalling past the deadline kills — it does NOT grant another permitted turn.
    session._execute_narration_turn.assert_not_called()
    assert any(m.type == MessageType.CHARACTER_INCAPACITATED for m in outbound), (
        "a dying-window submission past the deadline must be refused as terminal"
    )


@pytest.mark.asyncio
async def test_expiry_clears_the_window_and_leaves_terminal(monkeypatch, otel_capture):
    from sidequest.game.ruleset.without_number import is_dying_window_status
    from sidequest.handlers.player_action import HANDLER

    monkeypatch.setattr(
        "sidequest.handlers.player_action._watcher_publish", lambda *a, **k: None
    )
    snap = _snapshot_past_deadline()
    session = _playing_session(snap)
    session._session_data.genre_pack = _WwnPack()

    await HANDLER.handle(session, _action_msg())

    core = snap.find_creature_core(DEAD_PC)
    assert core is not None
    assert not any(is_dying_window_status(s) for s in core.statuses), (
        "an expired window must be removed (single coherent status)"
    )
    assert any(
        s.incapacitating and not getattr(s, "stabilizable", False) for s in core.statuses
    ), "an expired window must leave a terminal-dead status"
