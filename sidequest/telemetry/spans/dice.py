"""Dice-dispatch span events.

Span *events* (not standalone spans) — they attach to the enclosing turn span
via ``span.add_event(...)`` so the timing-relative-to-turn matters more than
carving out a child span. No ``SPAN_ROUTES`` entry — events ride only the
flat ``agent_span_close`` fan-out.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS
from .emitter import Emitter

SPAN_DICE_REQUEST_SENT = "dice.request_sent"
SPAN_DICE_THROW_RECEIVED = "dice.throw_received"
SPAN_DICE_RESULT_BROADCAST = "dice.result_broadcast"

FLAT_ONLY_SPANS.update(
    {
        SPAN_DICE_REQUEST_SENT,
        SPAN_DICE_THROW_RECEIVED,
        SPAN_DICE_RESULT_BROADCAST,
    }
)


def emit_dice_request_sent(
    *,
    request_id: str,
    rolling_player_id: str,
    stat: str,
    difficulty: int,
    modifier: int,
) -> None:
    Emitter.fire(
        SPAN_DICE_REQUEST_SENT,
        {
            "request_id": request_id,
            "rolling_player_id": rolling_player_id,
            "stat": stat,
            "difficulty": int(difficulty),
            "modifier": int(modifier),
        },
    )


def emit_dice_throw_received(
    *,
    request_id: str,
    rolling_player_id: str,
    face: list[int],
) -> None:
    """Fires only after correlation to a pending request — absence on a
    known request_id means a real correlation drop, not noise.
    """
    Emitter.fire(
        SPAN_DICE_THROW_RECEIVED,
        {
            "request_id": request_id,
            "rolling_player_id": rolling_player_id,
            "face": list(face),
        },
    )


def emit_dice_result_broadcast(
    *,
    request_id: str,
    rolling_player_id: str,
    total: int,
    outcome: str,
    seed: int,
) -> None:
    Emitter.fire(
        SPAN_DICE_RESULT_BROADCAST,
        {
            "request_id": request_id,
            "rolling_player_id": rolling_player_id,
            "total": int(total),
            "outcome": outcome,
            "seed": int(seed),
        },
    )
