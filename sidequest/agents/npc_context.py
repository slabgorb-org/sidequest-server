"""Budgeted NPC working-set selection (Story 75-2).

Python port of the Rust origin ``npc_context.rs:11-86``
(``build_npc_registry_context_budgeted``) and the deterministic *floor* of
ADR-118's universal-retrieval layer. The narrator used to receive
``snapshot.npc_pool`` + ``snapshot.npcs`` VERBATIM every turn, so prompt cost
grew without bound as the cast accreted. This module bounds that cost by
*selection, not eviction* (Diamonds-and-Coal / Living World, ADR-014): the full
roster always persists in the snapshot; only a relevance-budgeted working-set
enters the prompt.

Tiering (Operator ruling 2026-05-31, ADR-118 D4):

* **scene-present** stateful NPCs — ``last_seen_turn >= current_turn - window``
  — render FULL, ALWAYS. This is the floor: the physically-present scene is
  never dropped, even on a turn where the player referenced no NPC.
* **off-stage** stateful NPCs and ALL pool members (which carry no recency)
  render BRIEF (name + role) when the player referenced any NPC this turn, else
  COMPACT (name only) — the narrator still knows they exist, just cheaply.

Every call emits ``SPAN_NPC_WORKING_SET`` (the GM-panel lie detector) recording
considered-vs-selected counts per tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.telemetry.spans import npc_working_set_span

# Recency window (turns) inside which a stateful NPC is "scene-present" and
# earns a full profile. Matches the Rust origin's ``turn - 2`` threshold.
DEFAULT_RECENCY_WINDOW = 2


@dataclass(frozen=True)
class NpcWorkingSet:
    """The budgeted projection of the roster for one narrator turn.

    Holds the existing model objects (not re-projected dicts) so the roster
    renderer renders from canonical ``Npc`` / ``NpcPoolMember`` data without
    field drift. Nothing here is ever removed from the snapshot — this is a
    *view*, selected by relevance.
    """

    full_profiles: list[Npc] = field(default_factory=list)
    brief_entries: list[Npc | NpcPoolMember] = field(default_factory=list)
    compact_names: list[str] = field(default_factory=list)


def _name_of(entry: Npc | NpcPoolMember) -> str:
    """``Npc`` exposes ``.core.name`` (``.name`` is a method); ``NpcPoolMember``
    exposes a ``.name`` str. isinstance narrows the union cleanly."""
    if isinstance(entry, Npc):
        return entry.core.name
    return entry.name


def player_referenced_npcs_from_action(snapshot: GameSnapshot, action_text: str) -> set[str]:
    """Names from the roster the player referenced in ``action_text`` this turn.

    The brief-vs-compact toggle of :func:`build_npc_working_set` (ADR-118 §D4,
    story 75-10): when the player names any roster NPC, the off-stage tier renders
    BRIEF (name+role) instead of COMPACT (name only). The roster source is the
    full cast — stateful ``snapshot.npcs`` AND identity-only ``snapshot.npc_pool``
    — since a player can name either.

    Matching is case-insensitive and word-bounded (``\\b``): a name must occur as
    a whole word, so "Art" is not matched inside "start". A naive substring match
    would inflate the brief tier the budgeted floor exists to bound. Returns the
    matched names; the caller passes the set to ``build_npc_working_set``, which
    only reads its truthiness (any reference → brief mode for the whole off-stage
    tier — this is a turn-level signal, not per-entity promotion).
    """
    if not action_text or not action_text.strip():
        return set()
    referenced: set[str] = set()
    for entry in (*snapshot.npcs, *snapshot.npc_pool):
        name = _name_of(entry).strip()
        if not name:
            continue
        if re.search(rf"\b{re.escape(name)}\b", action_text, re.IGNORECASE):
            referenced.add(name)
    return referenced


def build_npc_working_set(
    snapshot: GameSnapshot,
    *,
    current_turn: int,
    player_referenced_npcs: set[str] | None = None,
    recency_window: int = DEFAULT_RECENCY_WINDOW,
) -> NpcWorkingSet:
    """Partition the roster into a budgeted working-set for the narrator prompt.

    Args:
        snapshot: the current game snapshot (carries ``npcs`` + ``npc_pool``).
        current_turn: the interaction count (``turn_manager.interaction``).
        player_referenced_npcs: names the player referenced this turn. Empty or
            ``None`` selects compact-only mode for the off-stage tier (the
            scene-present floor is unaffected).
        recency_window: turns within which a stateful NPC is scene-present.

    Returns:
        An :class:`NpcWorkingSet`. Every roster member surfaces in exactly one
        tier (no eviction); the snapshot is not mutated.
    """
    threshold = current_turn - recency_window
    references_present = bool(player_referenced_npcs)

    full_profiles: list[Npc] = []
    off_stage: list[Npc | NpcPoolMember] = []

    # Stateful NPCs carry recency — classify the floor by last_seen_turn.
    # ``last_seen_turn == 0`` is the unset sentinel (interaction starts at 1 and
    # only increments, so 0 means "never cited"). Guard it explicitly: at
    # session start the threshold goes <= 0, and without this guard a never-seen
    # NPC would satisfy ``>= threshold`` and be floored full — defeating the
    # budgeting at turns 1-2 and violating No Silent Fallbacks. Never-seen NPCs
    # are off-stage regardless of the threshold.
    for npc in snapshot.npcs:
        if npc.last_seen_turn > 0 and npc.last_seen_turn >= threshold:
            full_profiles.append(npc)
        else:
            off_stage.append(npc)

    # Pool members have no recency field; they can never be scene-present.
    off_stage.extend(snapshot.npc_pool)

    if references_present:
        brief_entries: list[Npc | NpcPoolMember] = off_stage
        compact_names: list[str] = []
    else:
        brief_entries = []
        compact_names = [_name_of(e) for e in off_stage]

    # OTEL: the GM panel verifies the budgeting fired (considered vs selected).
    with npc_working_set_span(
        full_count=len(full_profiles),
        brief_count=len(brief_entries),
        compact_count=len(compact_names),
        total_pool=len(snapshot.npcs) + len(snapshot.npc_pool),
        references_present=references_present,
    ):
        pass

    return NpcWorkingSet(
        full_profiles=full_profiles,
        brief_entries=brief_entries,
        compact_names=compact_names,
    )
