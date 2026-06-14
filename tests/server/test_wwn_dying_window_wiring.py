"""Story 108-6 (RED) — MANDATORY end-to-end solo dying-window wiring.

This proves the loop actually turns. It composes the TWO real production seams:

  1. The downed seam (run_cwn_wwn_downed_seam) opens the WWN dying window when a
     solo PC drops to 0 HP with NO live hostile — emitting wwn.dying_window.opened
     on the real path.
  2. The turn-intake gate (PlayerActionHandler.handle) PERMITS the downed
     soloist's next free-text submission and routes it onward — the game loop is
     NOT halted (the bug on develop today).

On develop today this fails twice over: the solo down goes terminal-only (no
window opens) AND the gate blocks any incapacitating status. It passes only when
both seams are wired through. No half-wired credit (CLAUDE.md). The two terminal
OUTCOMES — stabilize-before-deadline lives, stall-past-deadline dies — are pinned
by test_stabilize_dying_window_clock.py and test_dying_window_expiry.py
respectively; this test pins the gate→window wiring those outcomes ride on.

Wiring is asserted behaviorally + via OTEL spans (no source-text greps).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.ruleset import get_ruleset_module
from sidequest.server.dispatch.downed_seam import run_cwn_wwn_downed_seam
from sidequest.server.session_handler import _State
from tests.server.test_player_action_incapacitated_gate import _ReachedNarrationPath
from tests.server.test_reprisal_wn_downed_seam import (
    OPPONENT,
    PLAYER,
    _make_reprisal_pack,
    _make_snapshot_and_encounter,
)


def _open_solo_window():
    """Drive the real downed seam: PLAYER at 0 HP, opponent ablated to 0 (field
    cleared → no live hostile). Returns (snapshot, pack) with the window opened."""
    pack = _make_reprisal_pack("wwn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=0)
    opp = snap.find_creature_core(OPPONENT)
    assert opp is not None
    opp.hp.current = 0  # no live hostile remains
    run_cwn_wwn_downed_seam(
        ruleset=get_ruleset_module("wwn"),
        snapshot=snap,
        encounter=enc,
        cdef=pack.rules.confrontations[0],
        pack=pack,
        actor_side="opponent",
    )
    return snap, pack


def _solo_session(snap, pack) -> MagicMock:
    sd = MagicMock()
    sd.snapshot = snap
    sd.player_name = PLAYER
    sd.player_id = "p1"
    sd.game_slug = "2026-06-14-solo-dying-window"
    sd.world_slug = "test_world"
    sd.genre_pack = pack

    session = MagicMock()
    session._state = _State.Playing
    session._room = None  # solo
    session._session_data = sd
    session._execute_narration_turn = AsyncMock(
        side_effect=AssertionError("narration should be reached via the sentinel, not directly")
    )
    return session


def test_solo_down_opens_window_on_the_real_seam(otel_capture):
    from sidequest.game.ruleset.without_number import is_dying_window_status

    snap, _pack = _open_solo_window()
    core = snap.find_creature_core(PLAYER)
    assert core is not None
    assert any(is_dying_window_status(s) for s in core.statuses), (
        "the real downed seam must open a stabilizable window in solo play"
    )
    opened = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.dying_window.opened"]
    assert opened, "the real seam must emit wwn.dying_window.opened in solo play"


@pytest.mark.asyncio
async def test_downed_soloist_submission_is_not_halted(monkeypatch):
    """The core gap fix: after the window opens, the soloist's free-text action
    reaches the narration path instead of being blocked. Proven by the post-gate
    sentinel firing on the real PlayerActionHandler.handle."""
    from sidequest.game.ruleset.without_number import is_dying_window_status
    from sidequest.handlers.player_action import HANDLER
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload
    from sidequest.protocol.types import NonBlankString

    monkeypatch.setattr("sidequest.handlers.player_action._watcher_publish", lambda *a, **k: None)
    snap, pack = _open_solo_window()

    # The premise the gap is about: the opened window is INCAPACITATING (the PC is
    # out of normal play) yet STABILIZABLE. Today the window is created
    # non-incapacitating, so the gate trivially lets it by — that is NOT the gap.
    # The real halt only exists once the window is incapacitating; assert that
    # property so this test exercises the actual carve, not a degenerate pass.
    core = snap.find_creature_core(PLAYER)
    assert core is not None
    window = next((s for s in core.statuses if is_dying_window_status(s)), None)
    assert window is not None and window.incapacitating is True, (
        "the solo window must be incapacitating — the gate carve is what permits it through"
    )

    session = _solo_session(snap, pack)
    session._retrieve_lore_for_turn = AsyncMock(side_effect=_ReachedNarrationPath)

    msg = PlayerActionMessage(
        type=MessageType.PLAYER_ACTION,
        payload=PlayerActionPayload(
            action=NonBlankString("I press both hands to the wound and bind it with my cloak."),
            round=2,
        ),
        player_id="p1",
    )

    # The submission must pass the gate into the narration path — the loop turns.
    with pytest.raises(_ReachedNarrationPath):
        await HANDLER.handle(session, msg)
