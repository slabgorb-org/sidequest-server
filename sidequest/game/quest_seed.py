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
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.telemetry.spans import quest_seeded_at_creation_span

# Stable ids for the single creation-time seed entry. One quest, one anchor —
# a quiet town walk does not mint a quest (SOUL "Cost Scales with Drama"); the
# seed fires once at init, not per turn.
_SEED_QUEST_ID = "seed_drive"
_SEED_ANCHOR_ID = "seed_drive_anchor"


def seed_quest_spine(snapshot: GameSnapshot, character: Character) -> None:
    """Seed ``quest_log`` + ``quest_anchors`` + ``active_stakes`` from the PC's
    drive/calling at creation. Mutates ``snapshot`` in place.

    Fill, don't clobber: this runs after ``materialize_from_genre_pack``, which
    sets ``active_stakes`` from the world's FRESH opening chapter (live on
    flickering_reach, annees_folles, ...). If a spine is already authored, the
    seed DEFERS — it preserves the authored stakes and adds no competing seed
    quest/anchor. Otherwise it seeds from the drive, or (empty drive AND
    calling) degrades loudly. Emits exactly one ``quest.seeded_at_creation``
    span on every path — never a silent skip (CLAUDE.md "No Silent Fallbacks").
    """
    # Fill, don't clobber (Review RT1): a world-authored spine wins. The
    # presence of authored ``active_stakes`` is the "spine already exists"
    # signal; defer to it rather than overwriting deliberate authored content
    # (SOUL "Diamonds and Coal" / "Crunch in Genre, Flavor in World"). Still
    # emit the span so the defer is visible on the GM panel.
    if snapshot.active_stakes.strip():
        quest_seeded_at_creation_span(
            quest_id="",
            anchor_id="",
            source_drive="",
            has_stakes=True,
            severity="info",
            deferred=True,
        )
        return

    # Seed source is the drive, falling back to the calling_label (story 77-1).
    # BUT a calling_label that is merely the bare class identifier ("Channeler")
    # is not a stakes source — the Character model treats calling_label as empty
    # when it IS the archetype (character.py), yet some WWN chargen paths populated
    # it with the class name, which then surfaced as `active_stakes: "Channeler"`
    # (Story 153-19 oddity 3). Drop that case so it degrades loudly below rather
    # than seeding a meaningless class-name stake. A flavorful calling that differs
    # from char_class is unaffected.
    calling = (character.calling_label or "").strip()
    if calling and calling.casefold() == (character.char_class or "").strip().casefold():
        calling = ""
    source = (character.drive or "").strip() or calling

    if not source:
        # No authored spine, and no drive/calling to seed from. Degrade LOUDLY
        # so the GM panel sees the empty seed, but do NOT invent a quest (that
        # would be a silent fallback masking the content/chargen gap story 77-2
        # covers).
        quest_seeded_at_creation_span(
            quest_id="",
            anchor_id="",
            source_drive="",
            has_stakes=False,
            severity="warning",
        )
        return

    # Story 77-2: quest_log is now dict[str, QuestEntry]. The drive both names
    # the spine (title) and is its objective at creation; status starts active.
    snapshot.quest_log[_SEED_QUEST_ID] = QuestEntry(
        title=source, objective=source, status="active", anchor_id=_SEED_ANCHOR_ID
    )
    if _SEED_ANCHOR_ID not in snapshot.quest_anchors:
        snapshot.quest_anchors.append(_SEED_ANCHOR_ID)
    snapshot.active_stakes = source

    quest_seeded_at_creation_span(
        quest_id=_SEED_QUEST_ID,
        anchor_id=_SEED_ANCHOR_ID,
        source_drive=source,
        has_stakes=True,
        severity="info",
    )
