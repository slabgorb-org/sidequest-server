"""Quest-spine payload builder (ADR-137 / Story 77-8).

Converts the stored quest spine — ``snapshot.quest_log`` (dict[str, QuestEntry]),
``snapshot.quest_anchors`` (list[str] of body ids), and ``snapshot.active_stakes``
(str) — into a protocol ``QuestsPayload`` for the client quest/objective panel
(Story 77-5). Pure projection: no game logic, no writes. The RELATIONSHIPS-snapshot
analog for quests.

Anchors are associated to their owning quest via ``QuestEntry.anchor_id``. An
anchor no quest claims is still projected, with ``quest_id=None`` — surfaced
explicitly, never silently dropped (No Silent Fallbacks).
"""

from __future__ import annotations

from typing import Any

from sidequest.protocol.models import (
    QuestAnchorEntry,
    QuestLogEntry,
    QuestsPayload,
)


def build_quests_payload(snapshot: Any) -> QuestsPayload:
    """Project the stored quest spine into a ``QuestsPayload``.

    An empty spine yields a clean empty-but-valid payload (empty lists + ""),
    never None and never a throw.
    """
    quest_log = getattr(snapshot, "quest_log", {}) or {}
    quest_anchors = getattr(snapshot, "quest_anchors", []) or []
    active_stakes = getattr(snapshot, "active_stakes", "") or ""

    log_entries = [
        QuestLogEntry(
            quest_id=quest_id,
            title=entry.title,
            objective=entry.objective,
            status=entry.status,
            anchor_id=entry.anchor_id,
        )
        for quest_id, entry in quest_log.items()
    ]

    # Reverse map: anchor body id -> owning quest id (first quest that claims it).
    anchor_owner: dict[str, str] = {}
    for quest_id, entry in quest_log.items():
        if entry.anchor_id is not None and entry.anchor_id not in anchor_owner:
            anchor_owner[entry.anchor_id] = quest_id

    anchor_entries = [
        QuestAnchorEntry(anchor_id=anchor_id, quest_id=anchor_owner.get(anchor_id))
        for anchor_id in quest_anchors
    ]

    return QuestsPayload(
        quest_log=log_entries,
        quest_anchors=anchor_entries,
        active_stakes=active_stakes,
    )
