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
        character_aspects[ch.core.name] = [a.text for a in sheet.all_aspects()]
    enc = snapshot.encounter
    scene_aspects = [a.text for a in enc.situation_aspects] if enc is not None else []
    return {
        "skills": skills,
        "fate_points": fate_points,
        "character_aspects": character_aspects,
        "scene_aspects": scene_aspects,
        "active_conflict": enc is not None and not enc.resolved,
    }
