"""VALLEY-zone seed-context renderer (Story 22-3).

Sibling of :mod:`sidequest.magic.context_builder`. Produces the prose
block the orchestrator registers as the ``seed_context`` PromptSection
on every narrator turn that has any active seed or any ghost.

Active seeds surface their full authored prose (name, description,
flavor_tags, delivery_hints, narrative_hint) so the narrator can
retroactively weave them into the macro arc (ADR-014 baited hook /
ADR-009 VALLEY zone).

Ghosts surface as ``[Faded]`` callbacks: name plus delivery_hints
only — the narrative_hint guidance is for *live* seeds; the bait is
gone, the memory remains. The renderer does NOT look the ghost up
against the original :class:`SeedTrope` for description/narrative_hint.
"""

from __future__ import annotations

from sidequest.game.session import SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope


def _render_active(state: SeedState, trope: SeedTrope | None) -> str:
    """Render one active seed as a bullet block.

    ``trope`` is the authored :class:`SeedTrope` looked up by id from
    the genre pack. When None (seed id missing from pack — possible on
    a content drift or test fixture), description and narrative_hint
    are omitted; the snapshot-resident fields still surface.
    """
    lines: list[str] = [f"- {state.name}"]
    if trope is not None and trope.description:
        lines.append(f"    {trope.description.strip()}")
    if state.flavor_tags:
        lines.append("    Tags: " + ", ".join(state.flavor_tags))
    if state.delivery_hints:
        lines.append("    Delivery hints:")
        for hint in state.delivery_hints:
            lines.append(f"      - {hint}")
    if trope is not None and trope.narrative_hint:
        lines.append(f"    Narrative hint: {trope.narrative_hint.strip()}")
    return "\n".join(lines)


def _render_ghost(ghost: SeedGhost) -> str:
    lines: list[str] = [f"- [Faded] {ghost.name} (expired turn {ghost.expired_at_turn})"]
    if ghost.delivery_hints:
        lines.append("    Original delivery anchors:")
        for hint in ghost.delivery_hints:
            lines.append(f"      - {hint}")
    return "\n".join(lines)


def build_seed_context_block(
    active_seeds: list[SeedState],
    seed_ghosts: list[SeedGhost],
    seed_trope_by_id: dict[str, SeedTrope],
) -> str | None:
    """Compose the Valley-zone seed-context block.

    Returns ``None`` when both lists are empty — caller must skip
    registration (zero-byte-leak per ``orchestrator.py``'s conditional
    Valley contributors).
    """

    if not active_seeds and not seed_ghosts:
        return None

    # Always open and close the wrapper tag together — sibling discipline
    # of <magic-context> / <game_state> in orchestrator.py. The empty-
    # input early-return above prevents emitting a stub wrapper.
    sections: list[str] = ["<seed-context>"]
    if active_seeds:
        body = [_render_active(s, seed_trope_by_id.get(s.id)) for s in active_seeds]
        sections.append("[ACTIVE SEEDS]\n" + "\n".join(body))
    if seed_ghosts:
        body = [_render_ghost(g) for g in seed_ghosts]
        sections.append("[FADED — cross-session callbacks only]\n" + "\n".join(body))
    return "\n".join(sections) + "\n</seed-context>"
