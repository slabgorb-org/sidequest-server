"""Story 91-4 — production aside path passes the real session id (RED).

The unit suite (``tests/agents/test_91_4_cross_model_runaway_coverage.py``)
pins that a session-bound ``_AsideLlm`` participates in the ADR-134
detector and the per-session cumulative ceiling. This file pins the half
that can silently rot: the REAL ``PlayerActionHandler`` aside branch must
construct its adapter with the room's slug — the same canonical session
id the narrator uses (``room.slug`` / ``sd.game_slug``,
``websocket_session_handler``) — so the aside's Haiku spend lands in the
SAME per-session pot the ceiling guards.

Without this wiring test, ``build_aside_llm(session_id=...)`` can exist
and be fully covered while the handler passes nothing (or ``None``),
re-creating the exact uncovered-spender hole this story closes
("Verify Wiring, Not Just Existence", server CLAUDE.md).

Mechanism: the handler obtains its adapter through the
``llm_factory.build_aside_llm`` seam (function-level import, late-bound
through the module dict — the same seam ``tests/handlers/_harness.py``
already swaps). We re-swap it with a capturing factory and assert the
handler passed ``session_id == <room slug>``. This is seam-level
behavior, not source grepping — identical in kind to 91-1's
sentinel-injection wiring tests.
"""

from __future__ import annotations

from typing import Any

import pytest

import sidequest.agents.llm_factory as _llm_factory
from sidequest.protocol.enums import MessageType
from tests.handlers._harness import (
    _WORLD,
    fake_aside_llm,
    make_mp_room,
    submit,
)

_PAYLOAD = '{"answer":"Knee-deep.","outcome":"answered","grounded_on":["region.water_depth"]}'


@pytest.mark.asyncio
async def test_player_action_handler_passes_room_slug_to_aside_llm_factory() -> None:
    """The real PLAYER_ACTION aside branch must call
    ``build_aside_llm(session_id=<room slug>)`` — the canonical session id
    (the harness room's slug is ``f"{_WORLD}-mp"``). A handler that omits
    the kwarg fails the factory's required-keyword contract; a handler
    that passes ``None`` (or the wrong id) fails the assertion here."""
    room = make_mp_room(
        players=["Carl", "Donut", "Katia"],
        llm_aside=fake_aside_llm(_PAYLOAD),
    )
    try:
        captured: dict[str, Any] = {}
        inner = fake_aside_llm(_PAYLOAD)

        def _capturing_build(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return inner

        # Re-swap the seam ON TOP of the harness's swap (teardown restores
        # the original production factory regardless).
        _llm_factory.build_aside_llm = _capturing_build  # type: ignore[assignment]

        out = await submit(room, "Katia", "can I wade across?", aside=True)

        assert out and out[0].type == MessageType.ASIDE_ANSWER, (
            f"aside did not resolve through the captured factory: {out!r}"
        )
        assert "session_id" in captured, (
            "the handler constructed its aside adapter without a session_id — "
            "an uncovered Haiku spender (the dark-spend hole 91-4 closes). "
            f"Factory kwargs seen: {captured!r}"
        )
        expected_slug = f"{_WORLD}-mp"
        assert captured["session_id"] == expected_slug, (
            "the aside adapter must be keyed by the CANONICAL session id (the "
            "room slug — the same key the narrator's detector/ceiling state "
            f"uses); expected {expected_slug!r}, got {captured['session_id']!r}"
        )
    finally:
        room.teardown()
