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

from dataclasses import dataclass, field

from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.telemetry.spans import npc_working_set_span

# Recency window (turns) inside which a stateful NPC is "scene-present" and
# earns a full profile. Matches the Rust origin's ``turn - 2`` threshold.
DEFAULT_RECENCY_WINDOW = 2


def _same_scene(loc_a: str | None, loc_b: str | None) -> bool:
    """Co-location test for the scene-present floor.

    Tolerant of the phrasing drift that creeps into ``character_locations``
    strings across turns ("the Yellow Brick Road" vs "Yellow Brick Road"),
    matching the Monster Manual's activated-location heuristic
    (``monster_manual_inject._npc_patches_for_available_humans``): case-folded
    equality OR substring overlap either direction.

    Returns ``False`` when either side is blank — a missing location is *not*
    treated as a match. Callers gate on a known party location before applying
    the filter, so the blank-party case never reaches here as a false prune.
    """
    if not loc_a or not loc_b:
        return False
    a = loc_a.strip().casefold()
    b = loc_b.strip().casefold()
    if not a or not b:
        return False
    return a == b or a in b or b in a


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

    Story 84-2 (WI-5, ADR-118 §A4): the match now also resolves through each
    stateful NPC's ``aliases`` — a reference by epithet ("the old man") registers
    the aliased NPC. The word-boundary discipline is NOT forked: this delegates to
    :func:`sidequest.game.alias_resolution.resolve_mention`, which carries the same
    ``\\b``, case-insensitive, multi-word-phrase matcher for names AND aliases. Pool
    members carry no aliases (identity-only), so only ``snapshot.npcs`` contribute
    an alias list; both still match by canonical name.
    """
    if not action_text or not action_text.strip():
        return set()
    from sidequest.game.alias_resolution import resolve_mention

    names: set[str] = set()
    aliases_by_name: dict[str, list[str]] = {}
    for entry in (*snapshot.npcs, *snapshot.npc_pool):
        name = _name_of(entry).strip()
        if not name:
            continue
        names.add(name)
        if isinstance(entry, Npc) and entry.aliases:
            aliases_by_name.setdefault(name, []).extend(entry.aliases)
    return resolve_mention(action_text, names=names, aliases_by_name=aliases_by_name)


def build_npc_working_set(
    snapshot: GameSnapshot,
    *,
    current_turn: int,
    player_referenced_npcs: set[str] | None = None,
    recency_window: int = DEFAULT_RECENCY_WINDOW,
    current_location: str | None = None,
) -> NpcWorkingSet:
    """Partition the roster into a budgeted working-set for the narrator prompt.

    Args:
        snapshot: the current game snapshot (carries ``npcs`` + ``npc_pool``).
        current_turn: the interaction count (``turn_manager.interaction``).
        player_referenced_npcs: names the player referenced this turn. Empty or
            ``None`` selects compact-only mode for the off-stage tier (the
            scene-present floor is unaffected).
        recency_window: turns within which a stateful NPC is scene-present.
        current_location: the party's scene location (``character_locations``
            coordinate, same source as ``Npc.last_seen_location``). When
            ``None`` it is resolved from ``snapshot.party_location()``. The
            scene-present floor additionally requires CO-LOCATION: an NPC last
            cited in another scene/region is demoted off-stage even within the
            recency window (the entourage-bloat fix — sq-playtest 2026-06-13).
            When no party location can be resolved (party split / pre-chargen),
            the co-location filter is inactive and the floor falls back to
            recency-only (No Silent Fallbacks: we never prune on an unknown
            location).

    Returns:
        An :class:`NpcWorkingSet`. Every roster member surfaces in exactly one
        tier (no eviction); the snapshot is not mutated.
    """
    threshold = current_turn - recency_window
    references_present = bool(player_referenced_npcs)

    # Co-location floor (sq-playtest 2026-06-13 "everyone is hanging out with
    # me"): the recency floor alone kept an NPC cited in region A "scene-present"
    # in region B for the whole window, so the narrator kept it on stage and
    # re-cited it — a self-perpetuating entourage that never shed on movement.
    # Refining the floor to *recent AND co-located* drops the left-behind NPC to
    # off-stage the moment the party's scene diverges from where it was last
    # seen. This does NOT violate ADR-118's "never drop the present scene"
    # ruling — an NPC in a different region was never physically present; this
    # corrects what "present" means.
    resolved_location = (
        current_location if current_location is not None else snapshot.party_location()
    )
    location_filter_active = bool(resolved_location)

    full_profiles: list[Npc] = []
    off_stage: list[Npc | NpcPoolMember] = []
    location_pruned = 0

    # Stateful NPCs carry recency — classify the floor by last_seen_turn.
    # ``last_seen_turn == 0`` is the unset sentinel (interaction starts at 1 and
    # only increments, so 0 means "never cited"). Guard it explicitly: at
    # session start the threshold goes <= 0, and without this guard a never-seen
    # NPC would satisfy ``>= threshold`` and be floored full — defeating the
    # budgeting at turns 1-2 and violating No Silent Fallbacks. Never-seen NPCs
    # are off-stage regardless of the threshold.
    for npc in snapshot.npcs:
        recent = npc.last_seen_turn > 0 and npc.last_seen_turn >= threshold
        if not recent:
            off_stage.append(npc)
            continue
        # Recency says scene-present. Require co-location too. An NPC whose
        # ``last_seen_location`` is unknown (None) is kept full — a cite always
        # stamps a location, so None is the legacy/pre-bind case and we fail
        # toward keeping a possibly-present NPC rather than pruning on no data.
        if (
            location_filter_active
            and npc.last_seen_location is not None
            and not _same_scene(npc.last_seen_location, resolved_location)
        ):
            off_stage.append(npc)
            location_pruned += 1
            continue
        full_profiles.append(npc)

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
        location_pruned=location_pruned,
        location_filter_active=location_filter_active,
    ):
        pass

    return NpcWorkingSet(
        full_profiles=full_profiles,
        brief_entries=brief_entries,
        compact_names=compact_names,
    )
