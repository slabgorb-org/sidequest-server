"""Seed-at-creation quest spine (Story 77-1, ADR-137 Option A).

At session creation, derive a campaign spine — one ``quest_log`` entry, one
``quest_anchor``, and ``active_stakes`` — from the PC's ``drive`` (falling back
to ``calling_label``) so every session starts with a non-empty mechanical spine
from turn 1 instead of pure narrator improvisation (the wry_whimsy/oz turn-13
failure: ``quest_log: {}``, ``quest_anchors: []``, ``active_stakes: ""``).

When neither ``drive`` nor ``calling_label`` is set (the prose-pack case, e.g.
wry_whimsy, whose ``Character.drive`` defaults to ``""``), the seed does NOT
fabricate a spine — it emits the ``quest.seeded_at_creation`` span carrying
``severity="warning"`` so the empty seed is visible on the GM panel rather than
a silent no-op (CLAUDE.md "No Silent Fallbacks"). The load-bearing fix for
prose packs is the typed ``record_quest``/``set_stakes`` tools (story 77-2);
this story's job is the turn-0 spine when a drive exists, plus the loud tell
when it doesn't.

This story writes the seeded anchor directly onto the snapshot at creation; it
does NOT add a ``WorldStatePatch`` field or touch ``orbital/course.py`` (that
is story 77-3).
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.session import GameSnapshot
from sidequest.telemetry.spans import SPAN_QUEST_SEEDED_AT_CREATION, Span

# Stable ids for the single creation-time seed entry. One quest, one anchor —
# a quiet town walk does not mint a quest (SOUL "Cost Scales with Drama"); the
# seed fires once at init, not per turn.
_SEED_QUEST_ID = "seed_drive"
_SEED_ANCHOR_ID = "seed_drive_anchor"


def seed_quest_spine(snapshot: GameSnapshot, character: Character) -> None:
    """Seed ``quest_log`` + ``quest_anchors`` + ``active_stakes`` from the PC's
    drive/calling at creation. Mutates ``snapshot`` in place.

    Emits exactly one ``quest.seeded_at_creation`` span — ``severity="info"``
    with the seeded ids on a real seed, ``severity="warning"`` with empty ids
    on an empty drive AND calling (no fabrication).
    """
    source = (character.drive or "").strip() or (character.calling_label or "").strip()

    if not source:
        # No drive and no calling to seed from. Degrade LOUDLY — emit the span
        # with a warning severity so the GM panel sees the empty seed, but do
        # NOT invent a quest (that would be a silent fallback masking the
        # content/chargen gap story 77-2 covers).
        with Span.open(
            SPAN_QUEST_SEEDED_AT_CREATION,
            {
                "quest_id": "",
                "anchor_id": "",
                "source_drive": "",
                "has_stakes": False,
                "severity": "warning",
            },
        ):
            pass
        return

    snapshot.quest_log[_SEED_QUEST_ID] = f"Active: {source}"
    if _SEED_ANCHOR_ID not in snapshot.quest_anchors:
        snapshot.quest_anchors.append(_SEED_ANCHOR_ID)
    snapshot.active_stakes = source

    with Span.open(
        SPAN_QUEST_SEEDED_AT_CREATION,
        {
            "quest_id": _SEED_QUEST_ID,
            "anchor_id": _SEED_ANCHOR_ID,
            "source_drive": source,
            "has_stakes": True,
            "severity": "info",
        },
    ):
        pass
