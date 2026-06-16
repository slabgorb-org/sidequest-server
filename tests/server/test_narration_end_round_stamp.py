"""Ping-pong 2026-06-07 — "[UX] STALE player-action quote pinned at the BOTTOM
of the narrative column" (perseus_cloud MP, seat 1, ship_combat round 3).

The UI anchors persisted peer-action quotes into the transcript by round
(Story 71-10), but the only round source is the local player's own
``PLAYER_ACTION`` message. Dice-driven turns (combat beat commits ride
DICE_THROW, not PLAYER_ACTION) carry no own-action round, so those rounds
never anchor and the trailing fallback dumps the quotes BELOW the newest
narration — the operator's "Free fix. We lift tonight." six turns stale at
the bottom of a dogfight.

Fix under test (server half): ``NARRATION_END`` stamps the round it just
resolved — captured BEFORE ``record_interaction()`` bumps the counter, so it
matches the round the turn's ACTION_REVEAL entries were submitted under. The
UI half (sidequest-ui) anchors by this round.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.protocol.messages import NarrationEndMessage


@pytest.mark.asyncio
async def test_narration_end_carries_the_resolved_round(
    session_handler_factory,
) -> None:
    """A normal (non-opening) turn submitted under round N must emit
    NARRATION_END with ``round=N`` — NOT the post-``record_interaction()``
    N+1 — so it matches the round on that turn's ACTION_REVEAL entries."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.round = 5
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="The blow lands true.")
    )

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "I strike at the bandit.",
        _build_turn_context(sd),
    )

    assert sd.snapshot.turn_manager.round == 6, (
        "sanity: record_interaction() must have bumped the round"
    )
    end_msgs = [m for m in msgs if isinstance(m, NarrationEndMessage)]
    assert end_msgs, f"no NARRATION_END in outbound; got {[type(m).__name__ for m in msgs]}"
    assert end_msgs[-1].payload.round == 5, (
        "NARRATION_END must carry the round it RESOLVED (pre-increment) so the "
        "UI can anchor that round's peer-action quotes; got "
        f"{end_msgs[-1].payload.round!r} (turn_manager now at "
        f"{sd.snapshot.turn_manager.round})"
    )


@pytest.mark.asyncio
async def test_opening_turn_narration_end_carries_round_one(
    session_handler_factory,
) -> None:
    """The opening scene-set bumps no counter (Story 45-5 / ADR-051) — its
    NARRATION_END stamps the current (un-bumped) round."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    assert sd.snapshot.turn_manager.round == 1
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="The tavern door creaks open.")
    )

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "[OPENING]",
        _build_turn_context(sd),
        is_opening_turn=True,
    )

    assert sd.snapshot.turn_manager.round == 1, "opening turn must not bump the round"
    end_msgs = [m for m in msgs if isinstance(m, NarrationEndMessage)]
    assert end_msgs, f"no NARRATION_END in outbound; got {[type(m).__name__ for m in msgs]}"
    assert end_msgs[-1].payload.round == 1
