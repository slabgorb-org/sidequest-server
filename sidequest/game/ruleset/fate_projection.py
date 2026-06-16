"""Fate state projection — the single source of truth for "what Fate state exists
this turn" (ADR-144 F2b, Story 116-2).

F2a built this projection inside ``server/intent_router_pass.py`` (as
``_build_fate_summary``) for the Haiku router. F2b relocates it down to the ``game``
layer so BOTH consumers import one function:

  * the router (``_build_state_summary`` appends it to the classifier state summary), and
  * the narrator prompt builder (``orchestrator.build_narrator_prompt`` renders it into the
    ``fate_state`` prompt section via ``_build_fate_state_section``).

Keeping one projector means the router and the narrator can never drift on the aspects,
skills, and fate points in play.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sidequest.game.ruleset.fate_resolution import FateOutcome, FateTier, ladder_name
from sidequest.protocol.models import (
    FateAspectEntry,
    FateCharacterEntry,
    FateConflictEntry,
    FateConflictParticipant,
    FateConsequenceEntry,
    FatePendingCompel,
    FateRollPayload,
    FateSkillEntry,
    FateStatePayload,
    FateStressBox,
)
from sidequest.protocol.sanitize import sanitize_player_text

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot


def build_fate_projection(snapshot: GameSnapshot) -> dict[str, Any]:
    """Compact Fate vocabulary for a turn (ADR-144 F2a/F2b).

    The per-PC skills the narrator/router can name, the fate points they can spend, the
    character aspects + live situation aspects they can invoke, and whether a conflict is
    active (the in-conflict scope of ``dispatch_fate_action``). Only PCs with a Fate sheet
    contribute; on a non-Fate pack the caller never invokes this builder.
    """
    skills: dict[str, dict[str, int]] = {}
    fate_points: dict[str, int] = {}
    character_aspects: dict[str, list[str]] = {}
    for ch in snapshot.characters:
        sheet = ch.core.fate_sheet
        if sheet is None:
            continue
        skills[ch.core.name] = dict(sheet.skills)
        fate_points[ch.core.name] = sheet.fate_points
        # Aspect text is player-authored (chargen) or LLM-authored (create_advantage);
        # it flows into the narrator prompt, so sanitize at this single source of truth
        # (ADR-047) — covers both the narrator section and the router state summary.
        character_aspects[ch.core.name] = [
            sanitize_player_text(a.text) for a in sheet.all_aspects()
        ]
    enc = snapshot.encounter
    # A resolved encounter's situation aspects are stale fiction — gate them on
    # `not enc.resolved`, the same condition `active_conflict` reads below.
    scene_aspects = (
        [sanitize_player_text(a.text) for a in enc.situation_aspects]
        if enc is not None and not enc.resolved
        else []
    )
    return {
        "skills": skills,
        "fate_points": fate_points,
        "character_aspects": character_aspects,
        "scene_aspects": scene_aspects,
        "active_conflict": enc is not None and not enc.resolved,
    }


def build_fate_state_payload(snapshot: GameSnapshot) -> FateStatePayload:
    """The full client Fate projection (ADR-144 F3a / Story 118-1).

    The rich, structured sibling of :func:`build_fate_projection`: where the
    compact projection feeds the router/narrator a flat vocabulary, this builds
    the player-facing wire payload (``FATE_STATE``) — per-PC fate points/refresh,
    skills on the ladder, named aspects with kind + free-invoke counts, the two
    stress tracks, the four consequence slots (open vs filled), the scene's
    situation aspects + boosts, and the active conflict's participants by side.

    Reads the SAME snapshot state as the compact projection (one source of
    truth) — only PCs with a Fate sheet contribute; on a non-Fate pack the
    emitter never invokes this builder. Display text is presented raw: it feeds
    the UI (which escapes it), not the narrator prompt (the compact projection
    is the prompt path that sanitizes per ADR-047).
    """
    characters: list[FateCharacterEntry] = []
    for ch in snapshot.characters:
        sheet = ch.core.fate_sheet
        if sheet is None:
            continue
        characters.append(
            FateCharacterEntry(
                name=ch.core.name,
                fate_points=sheet.fate_points,
                refresh=sheet.refresh,
                skills=[
                    FateSkillEntry(name=name, rating=rating, ladder=ladder_name(rating))
                    for name, rating in sheet.skills.items()
                ],
                # Named aspects only — a FILLED consequence is invokable but
                # surfaces in ``consequences`` below, never duplicated here.
                aspects=[
                    FateAspectEntry(text=a.text, kind=a.kind, free_invokes=a.free_invokes)
                    for a in sheet.aspects
                ],
                stress={
                    track_name: [
                        FateStressBox(value=b.value, checked=b.checked) for b in track.boxes
                    ]
                    for track_name, track in sheet.stress.items()
                },
                consequences=[
                    FateConsequenceEntry(
                        level=c.level,
                        value=c.value,
                        filled=c.aspect is not None,
                        text=c.aspect.text if c.aspect is not None else "",
                    )
                    for c in sheet.consequences
                ],
            )
        )

    enc = snapshot.encounter
    # A resolved encounter's situation aspects are stale fiction and the
    # conflict is over — gate both on `not enc.resolved` (same condition the
    # compact projection's `active_conflict` reads). The guard is inlined per
    # branch so the type checker narrows `enc` to non-None.
    scene_aspects = (
        [
            FateAspectEntry(text=a.text, kind=a.kind, free_invokes=a.free_invokes)
            for a in enc.situation_aspects
        ]
        if enc is not None and not enc.resolved
        else []
    )
    conflict = (
        FateConflictEntry(
            active=True,
            # Seating order is the engine's tiebreak order
            # (fate_opponent._live_player_actors); preserve encounter.actors order.
            participants=[FateConflictParticipant(name=a.name, side=a.side) for a in enc.actors],
            # ADR-144 F3e: surface the narrator's offered compels so the player
            # surface can render its accept/refuse control. Display text is raw
            # (the UI escapes it), consistent with the rest of this builder.
            pending_compels=[
                FatePendingCompel(aspect=c.aspect, target=c.target, reason=c.reason)
                for c in enc.pending_compels
            ],
        )
        if enc is not None and not enc.resolved
        else None
    )
    return FateStatePayload(characters=characters, scene_aspects=scene_aspects, conflict=conflict)


def build_fate_roll_payload(outcome: FateOutcome) -> FateRollPayload:
    """Project a resolved 4dF roll onto the wire (ADR-144 F3c / Story 118-3).

    Faithful, lossless map of the engine's ``FateOutcome`` to the player-facing
    ``FATE_ROLL`` payload, adding the two derived legibility fields: the ladder
    ADJECTIVE (the player reads "Great", not "+4") and the succeed-with-style
    flag. The raw dice tuple previously reached only the OTEL span.
    """
    return FateRollPayload(
        dice=outcome.dice,
        roll_total=outcome.roll_total,
        ladder_total=outcome.ladder_total,
        ladder_name=ladder_name(outcome.ladder_total),
        opposition=outcome.opposition,
        shifts=outcome.shifts,
        tier=str(outcome.tier),
        succeeded_with_style=outcome.tier == FateTier.SucceedWithStyle,
    )
