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
    """Change signature over the spine, derived from the projected wire payload.

    Using ``build_quests_payload(...).model_dump_json()`` makes the signature
    track *exactly* what goes on the wire — including ``objective`` — so an
    objective-only ``record_quest`` update re-broadcasts (AC2). JSON encoding
    also escapes the field contents, so narrator free text containing ``:``/
    ``,``/``|``/``#`` can never collapse two distinct spines onto one signature
    (no delimiter-ambiguity collision)."""
    return build_quests_payload(snapshot).model_dump_json()


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

    # Build the payload once; the change signature is that same payload's JSON
    # (see _quests_signature), so we derive the sig from it rather than building
    # twice.
    payload = build_quests_payload(snapshot)
    sig = payload.model_dump_json()
    if getattr(handler, _SIG_ATTR, None) == sig:
        return

    msg = QuestsMessage(payload=payload)

    from sidequest.telemetry.spans import SPAN_QUESTS_EMITTED, Span

    # Lore coherence telemetry (Story 117-5): total discovered-lore fragments
    # cohered under quests this frame, so the GM panel can verify the anchor→
    # clue→fact projection actually engaged rather than the narrator improvising
    # a "what I've learned" surface (CLAUDE.md OTEL Observability Principle).
    lore_count = sum(len(q.related_lore) for q in payload.quest_log)

    with Span.open(
        SPAN_QUESTS_EMITTED,
        {
            "quest_count": len(payload.quest_log),
            "anchor_count": len(payload.quest_anchors),
            "has_stakes": bool(payload.active_stakes),
            "lore_count": lore_count,
            "changed": True,
        },
    ):
        pass
    logger.info(
        "quests.emitted quests=%d anchors=%d lore=%d",
        len(payload.quest_log),
        len(payload.quest_anchors),
        lore_count,
    )

    # Commit the signature only after the broadcast succeeds. If emit_fn raises,
    # the sig stays unchanged so the next turn retries rather than skipping a
    # never-delivered frame (client would otherwise stay stale).
    emit_fn(msg, "QUESTS")
    setattr(handler, _SIG_ATTR, sig)
