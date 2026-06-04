"""Reactive QUESTS emitter (ADR-137 / Story 77-8).

Mirrors relationships_emit.py: build a global payload and broadcast via emit_fn.
Change-gated on a lightweight spine signature so the message fires only when the
quest spine actually changes (a quest minted/updated, an anchor added, stakes
set) — Cost Scales with Drama. The payload is the same for every recipient (the
spine is global), so it broadcasts directly.

An entirely empty spine is a no-op (nothing to show yet — the 77-1 seed populates
it at session start). The builder still yields a clean empty payload for any
caller that wants it; the emitter simply does not broadcast emptiness, preserving
the wire-parity omission contract.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sidequest.game.projection.quests import build_quests_payload
from sidequest.protocol.messages import QuestsMessage

logger = logging.getLogger(__name__)

_SIG_ATTR = "_last_quests_sig"


def _quests_signature(snapshot: Any) -> str:
    """Cheap change signature over the spine: per-quest id/title/status/anchor,
    the anchor list, and the stakes string. Any spine mutation changes it."""
    quest_log = getattr(snapshot, "quest_log", {}) or {}
    quest_anchors = getattr(snapshot, "quest_anchors", []) or []
    active_stakes = getattr(snapshot, "active_stakes", "") or ""

    quest_parts = [f"{qid}:{e.title}:{e.status}:{e.anchor_id}" for qid, e in quest_log.items()]
    anchors_part = ",".join(quest_anchors)
    return f"{'|'.join(quest_parts)}#{anchors_part}#{active_stakes}"


def _is_empty_spine(snapshot: Any) -> bool:
    quest_log = getattr(snapshot, "quest_log", {}) or {}
    quest_anchors = getattr(snapshot, "quest_anchors", []) or []
    active_stakes = getattr(snapshot, "active_stakes", "") or ""
    return not quest_log and not quest_anchors and not active_stakes


def _maybe_emit_quests(
    handler: Any,
    *,
    snapshot: Any,
    emit_fn: Callable[[Any, str], None],
) -> None:
    """Emit a QUESTS message when the spine signature changes.

    Empty spine → nothing to show (silent; not an error state). Unchanged
    signature → skip (Cost Scales with Drama).
    """
    if _is_empty_spine(snapshot):
        return

    sig = _quests_signature(snapshot)
    if getattr(handler, _SIG_ATTR, None) == sig:
        return

    payload = build_quests_payload(snapshot)
    msg = QuestsMessage(payload=payload)

    from sidequest.telemetry.spans import SPAN_QUESTS_EMITTED, Span

    with Span.open(
        SPAN_QUESTS_EMITTED,
        {
            "quest_count": len(payload.quest_log),
            "anchor_count": len(payload.quest_anchors),
            "has_stakes": bool(payload.active_stakes),
            "changed": True,
        },
    ):
        pass
    logger.info(
        "quests.emitted quests=%d anchors=%d",
        len(payload.quest_log),
        len(payload.quest_anchors),
    )

    setattr(handler, _SIG_ATTR, sig)
    emit_fn(msg, "QUESTS")
