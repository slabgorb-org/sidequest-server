"""Reactive FATE_STATE emitter (ADR-144 F3a / Story 118-1).

Mirrors quests_emit.py / relationships_emit.py: build a global Fate payload and
broadcast via ``emit_fn``, change-gated on a signature derived from the projected
wire payload so the message fires only when the Fate state actually changes (a
fate-point spend, an aspect created, stress/consequence taken, a conflict
starting/ending) — Cost Scales with Drama, NOT per-turn.

The load-bearing DIFFERENCE from its siblings (ADR-144 / epic 118): the emitter
is gated on ``sd.genre_pack.rules.ruleset == "fate"`` so a Fate surface never
co-renders with the WN/native beat/dial ConfrontationOverlay. An empty payload
(no Fate-sheet PC) is a silent no-op — nothing to show yet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.protocol.messages import FateStateMessage

logger = logging.getLogger(__name__)

_SIG_ATTR = "_last_fate_state_sig"


def _maybe_emit_fate_state(
    handler: Any,
    *,
    sd: Any,
    snapshot: Any,
    emit_fn: Callable[[Any, str], None],
) -> None:
    """Emit a FATE_STATE message when the Fate state changes, on a Fate pack only.

    Non-fate pack → never emit (the surface must not collide with the WN/native
    overlay). Empty Fate state → nothing to show (silent; not an error state).
    Unchanged signature → skip (Cost Scales with Drama).
    """
    if sd.genre_pack.rules.ruleset != "fate":
        return

    payload = build_fate_state_payload(snapshot)
    # No Fate-sheet PC → nothing to project yet (mirrors the quests empty-spine
    # no-op). This is the only emptiness gate: a Fate pack with a sheet always
    # has state worth showing.
    if not payload.characters:
        return

    sig = payload.model_dump_json()
    if getattr(handler, _SIG_ATTR, None) == sig:
        return

    msg = FateStateMessage(payload=payload)

    from sidequest.telemetry.spans import (
        SPAN_FATE_CONFLICT_PROJECTED,
        SPAN_FATE_PROJECTION_EMITTED,
        Span,
    )

    with Span.open(
        SPAN_FATE_PROJECTION_EMITTED,
        {
            "character_count": len(payload.characters),
            "scene_aspect_count": len(payload.scene_aspects),
            "in_conflict": payload.conflict is not None,
            "changed": True,
        },
    ):
        pass
    logger.info(
        "fate.projection.emitted characters=%d scene_aspects=%d in_conflict=%s",
        len(payload.characters),
        len(payload.scene_aspects),
        payload.conflict is not None,
    )

    # 150-2 follow-up: when a conflict is seated, confirm the OPPONENT track + the
    # win-meter number actually reached the wire (ADR-143 stress-fill toward
    # taken-out, NOT the native dial). This is the GM-panel lie detector for the
    # win meter — distinct from fate.projection.emitted, fired only in-conflict.
    if payload.conflict is not None:
        from sidequest.game.ruleset.fate_projection import conflict_opponent_progress

        progress = conflict_opponent_progress(payload.conflict)
        max_progress = max((pr for _, pr in progress), default=0.0)
        with Span.open(
            SPAN_FATE_CONFLICT_PROJECTED,
            {
                "opponent_count": len(progress),
                "max_taken_out_progress": max_progress,
                "opponents": ",".join(f"{name}:{pr:.3f}" for name, pr in progress),
            },
        ):
            pass
        logger.info(
            "fate.conflict.projected opponents=%d max_taken_out_progress=%.3f",
            len(progress),
            max_progress,
        )

    # Commit the signature only AFTER the broadcast succeeds (the quests/
    # relationships discipline): if emit_fn raises, the sig stays unchanged so
    # the next turn retries rather than skipping a never-delivered frame.
    emit_fn(msg, "FATE_STATE")
    setattr(handler, _SIG_ATTR, sig)
