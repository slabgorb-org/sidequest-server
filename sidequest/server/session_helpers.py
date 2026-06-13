"""Module-level helpers extracted from session_handler.py.

Pure functions only — no references to ``WebSocketSessionHandler``.
``_SessionData`` and ``SessionRoom`` appear here only as type annotations
(stringified by ``from __future__ import annotations``) so this module
imports them under ``TYPE_CHECKING`` to avoid circular imports.

Re-exported by ``session_handler.py`` for back-compat with tests and
external callers that import these symbols from there.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from sidequest.agents.npc_context import build_npc_working_set
from sidequest.agents.orchestrator import (
    RECENT_NARRATIVE_WINDOW_K,
    NpcMention,
    TurnContext,
)
from sidequest.game.builder import humanize_snake_case
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.retrieval_orchestration import RetrievedEntities, render_entity_section
from sidequest.game.session import (
    GameSnapshot,
    PartyPeer,
)
from sidequest.game.shared_world_delta import (
    build_shared_world_delta,
    merge_shared_delta_into_snapshot,
)
from sidequest.game.status import Status
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.protocol.enums import NarratorVerbosity, NarratorVocabulary
from sidequest.protocol.messages import (
    CartographyMapMessage,
    CartographyMapPayload,
    ErrorMessage,
    ErrorPayload,
    PlayerPresenceMessage,
    PlayerPresencePayload,
)
from sidequest.protocol.types import NonBlankString

# Story 82-10 / ADR-110 amendment — the Phase B drop list and Phase C
# projection helper moved to ``sidequest.server.snapshot_slimming`` so the
# Intent Router pass (the second snapshot-dump consumer) can apply the same
# audited cut. Re-exported here verbatim so the 61-5 governance gate
# (``test_snapshot_field_governance.py``) and the 61-2 projection contract
# tests keep their import surface unchanged. Field-audit provenance for the
# drop list: context-story-57-5.md §Phase B + .session/57-5-session.md;
# ``narrative_log`` joined via the 61-5 architecture-gate amendment.
from sidequest.server.snapshot_slimming import (
    _DISCOVERED_CLUES_CAP,  # noqa: F401  (re-export: 61-2 test contract)
    _KNOWN_FACTS_TAIL_K,  # noqa: F401  (re-export: 61-2 test contract)
    _PHASE_B_DROP_FIELDS,  # noqa: F401  (re-export: 61-5 governance gate)
    _apply_phase_c_projections,  # noqa: F401  (re-export: 61-2 test contract)
    apply_snapshot_slimming,
)
from sidequest.telemetry.spans import (
    cartography_map_emitted_span,
    narrator_settings_span,
    npc_auto_mint_skipped_span,
    npc_auto_minted_from_prose_span,
    npc_observation_gate_promoted_span,
    npc_observation_gate_purged_span,
    npc_recurring_presence_missed_span,
    npc_reinvented_span,
    orchestrator_notorious_party_gate_span,
    pacing_hint_span,
    prompt_game_state_bytes_span,
    room_state_injected_span,
)

# Story 61-5 / ADR-110 architecture gate — fields that DO ride into
# ``snapshot.model_dump()`` and have specific projection behavior that
# bounds their dump-side size:
#
# * ``room_states`` — payload is rewritten to keep only the acting PC's
#   current room; all other room ids are dropped (story 61-2).
# * ``npcs`` — payload is rewritten to keep entries passing the
#   ``is_npc_in_scene`` predicate (location match OR encounter-actor
#   anchor, story 61-7); nested ``belief_state`` is stripped from each
#   kept entry (story 61-2).
# * ``characters`` — nested ``known_facts`` list is truncated to the
#   last ``_KNOWN_FACTS_TAIL_K`` entries per PC (story 61-2).
# * ``scenario_state`` — nested ``discovered_clues`` set is capped at
#   ``_DISCOVERED_CLUES_CAP`` entries (story 61-2).
#
# **Governance vs. dispatch.** This registry is a governance artefact
# consumed by ``test_snapshot_field_governance.py``, NOT a runtime
# dispatch table. ``_apply_phase_c_projections`` (now in
# ``sidequest.server.snapshot_slimming``, story 82-10) independently
# hard-codes the same four names — the registry asserts the
# categorization decision; the helper performs the work. Keeping them
# in sync is the un-tightened seam called out as a deferred deviation
# in story 61-5 (see ``.session/61-5-session.md`` §Architect
# (spec-check)) — a follow-up story may extend the gate to verify
# projection-consistency by reflecting over the helper's actual payload
# mutations. The behavior is tested by
# ``test_61_2_snapshot_seven_field_projection.py`` and
# ``test_57_5_snapshot_slimming.py``.
_PHASE_C_PROJECTIONS: tuple[str, ...] = (
    "room_states",
    "npcs",
    "characters",
    "scenario_state",
)

# Story 61-5 / ADR-110 architecture gate — fields declared on
# ``GameSnapshot`` but absent from ``snapshot.model_dump()`` output
# because their ``Field(...)`` carries ``exclude=True``. These are
# transient runtime queues that must NEVER ride into a serialization
# (the narrator prompt, a save file, a state-mirror message) — they
# re-initialize empty each turn and are reconstructed from durable
# state. They contribute zero bytes to the dump because pydantic
# strips them at serialization time.
#
# Adding a field here means: the field has ``Field(..., exclude=True)``
# on its declaration and is verifiably absent from ``model_dump()``.
# ``test_snapshot_field_governance.py`` enforces this — removing
# ``exclude=True`` from a field listed here fails the gate, forcing
# the author to either keep the exclusion or re-categorize the field
# (project, drop, or document why it's now bounded-by-construction).
_EXCLUDED_FROM_DUMP: tuple[str, ...] = (
    # ADR/story reference: session.py:798-799 — transient outbound
    # dispatch queues. ``exclude=True`` keeps them out of the dump so
    # a save mid-handler cannot persist a partial queue. They
    # re-initialize empty on load — correct because auto-fires and
    # outcomes are derivable from snapshot state on the next
    # narration turn.
    "pending_magic_auto_fires",
    "pending_magic_confrontation_outcome",
)

# Story 61-5 / ADR-110 architecture gate — fields whose growth is
# bounded by their own structure rather than by projection logic.
# Bounded-by-construction means one of:
#
#   (a) Scalar primitive (int/float/bool/str/datetime) — fixed wire size.
#   (b) Bounded enum-shaped string (e.g. ``time_of_day``,
#       ``campaign_maturity``, ``current_region`` slug).
#   (c) Single-record optional (``encounter``, ``magic_state``,
#       ``plotted_course``, ``pending_*``) — one structured value max.
#   (d) Dict keyed by a finite domain (PC names, body ids, seat ids,
#       resource pool keys, quest ids, region/room/route slugs) where
#       the key cardinality is itself a finite gameplay quantity.
#   (e) List bounded by gameplay convention to small cardinality
#       (companions, active_seeds, next_turn_directives, etc).
#
# Genuinely growing lists that the narrator reads in full but are
# small-by-gameplay-convention (``lore_established``, ``world_history``,
# ``npc_pool``) sit in (e) for now. If any of them grows large enough
# in real play to matter, the bounding decision moves to
# ``_PHASE_C_PROJECTIONS`` in a follow-up story — that conversation is
# what this gate exists to force. ``world_history`` is currently
# P3-deferred (campaign maturity / world materialization not populated
# in the live build per ``session.py:684``). ``npc_pool`` is the
# anti-confabulation anchor (context-story-61-2.md §"gaslighting
# doctrine, MUST survive") — it cannot be projected without breaking
# the narrator's ability to cite off-stage NPCs.
_BOUNDED_BY_CONSTRUCTION: tuple[str, ...] = (
    # scalars / enum-shaped strings
    "active_stakes",
    "atmosphere",
    "campaign_maturity",
    "clock_t_hours",
    "current_region",
    "days_elapsed",
    "genre_slug",
    "last_lull_fire_turn",
    "last_saved_at",
    # 82-2 (ADR-049) added these enum-scalar narrator-tuning fields to
    # GameSnapshot but never categorized them; classify here (enums are
    # bounded by construction) so the 61-5 governance gate is green.
    "narrator_verbosity",
    "narrator_vocabulary",
    "party_body_id",
    "pending_escalation_directive",
    "player_dead",
    "time_of_day",
    "total_beats_fired",
    "turns_since_meaningful",
    "world_slug",
    # single-record optionals / single-record structs
    "encounter",
    "magic_state",
    # AWN mutation state (Plan 2) — single-record optional, same
    # rationale as ``magic_state``.
    "mutation_state",
    "pending_resolution_signal",
    "pending_time_skip_summary",
    "plotted_course",
    "turn_manager",
    # dicts keyed by finite gameplay domains
    "character_locations",
    # Movement subsystem §Q0: per-PC region map (player_name -> region_id),
    # bounded by the seated-PC count exactly like character_locations.
    "pc_regions",
    "chassis_autofire_cooldowns",
    "chassis_registry",
    "player_seats",
    "quest_log",
    "resources",
    # lists bounded by gameplay convention (small cardinality)
    "active_seeds",
    "companions",
    "discovered_regions",
    "discovered_rooms",
    "discovered_routes",
    "lore_established",
    "next_turn_directives",
    "notes",
    "npc_pool",
    "quest_anchors",
    "seed_ghosts",
    "world_history",
)

if TYPE_CHECKING:
    from sidequest.server.session_room import SessionRoom
    from sidequest.server.session_state import _SessionData

logger = logging.getLogger(__name__)


def build_secret_note_events(
    removed: list,
    *,
    turn_id: str,
) -> list[MessageEnvelope]:
    """Build SECRET_NOTE envelopes from prompt-redacted dispatch entries.

    Group G Task 6. ``removed`` is the second element of the tuple returned
    by :func:`sidequest.agents.prompt_redaction.redact_dispatch_package`.
    Only ``SubsystemDispatch`` entries produce SECRET_NOTE events;
    ``NarratorDirective`` and ``LethalityVerdict`` fall through.
    ``origin_seq=0`` — the event-log append assigns the real seq.
    """
    from sidequest.protocol.dispatch import SubsystemDispatch

    out: list[MessageEnvelope] = []
    for entry in removed:
        if not isinstance(entry, SubsystemDispatch):
            continue
        payload = {
            "turn_id": turn_id,
            "idempotency_key": entry.idempotency_key,
            "subsystem": entry.subsystem,
            "params": entry.params,
            "_visibility": {
                "visible_to": entry.visibility.visible_to,
                "fidelity": entry.visibility.perception_fidelity,
            },
        }
        out.append(
            MessageEnvelope(
                kind="SECRET_NOTE",
                payload_json=json.dumps(payload),
                origin_seq=0,
            )
        )
    return out


def emit_secret_notes(
    *,
    secret_routes: list,
    turn_id: str,
    event_log,
) -> None:
    """Append SECRET_NOTE events for every redacted dispatch on the turn."""
    for envelope in build_secret_note_events(secret_routes, turn_id=turn_id):
        event_log.append(kind=envelope.kind, payload_json=envelope.payload_json)


def union_visible_to(values: Iterable[list[str] | str]) -> list[str] | str:
    """Union of ``visible_to`` lists; ``"all"`` is a stop-word.

    Asymmetric-visibility doctrine (ADR-028/104): a single ``"all"`` tag
    means everyone sees it regardless of narrower sibling tags. Result is
    the literal string ``"all"`` or a sorted, de-duplicated player-id
    list. Single source of the stop-word rule — consumed by
    :func:`aggregate_visibility` (DispatchPackage path) and
    :func:`sidequest.server.visibility_classifier.classify_narration_visibility`
    (ADR-105 B2 private-route derivation) so the two cannot drift.
    """
    any_all = False
    union: set[str] = set()
    for v in values:
        if v == "all":
            any_all = True
        else:
            union.update(v)
    return "all" if any_all else sorted(union)


def aggregate_visibility(pkg: DispatchPackage) -> dict:
    """Build the _visibility sidecar for the canonical narration payload.

    visible_to = union of non-redacted tags' visible_to lists; "all" is
    a stop-word that collapses the union. fidelity maps merge.
    """
    visibilities: list[list[str] | str] = []
    fidelity: dict[str, str] = {}
    for pd in pkg.per_player:
        for d in pd.dispatch:
            if d.visibility.redact_from_narrator_canonical:
                continue
            visibilities.append(d.visibility.visible_to)
            fidelity.update(d.visibility.perception_fidelity)
    return {
        "visible_to": union_visible_to(visibilities),
        "fidelity": fidelity,
    }


def _resolve_acting_character_name(sd: _SessionData, room: SessionRoom | None) -> str:
    """Identify the requesting socket's PC by player_id via the room seat
    map. Returning the wrong name causes the narrator's party-peer block
    to misidentify peers (the playtest "Shirley is Laverne's hireling" bug).

    Resolution: room seat → snapshot match by player_name → first PC →
    lobby player_name (empty-snapshot fallback).
    """
    snapshot = sd.snapshot
    if not snapshot.characters:
        return sd.player_name
    seat_lookup = getattr(room, "slot_to_player_id", None) if room is not None else None
    if callable(seat_lookup) and sd.player_id:
        seat_map = seat_lookup()
        # Story 61-8 §D6 — pyright can't statically determine that the
        # duck-typed ``slot_to_player_id`` returns a ``dict``. The
        # ``isinstance`` narrows for the type checker AND surfaces a
        # hook-contract bug loudly (review-fix round 2): pre-§D6 a
        # non-dict return caused ``.items()`` to raise AttributeError;
        # the isinstance guard alone would silently mask that. CLAUDE.md
        # No Silent Fallbacks — log warning on the unexpected shape so
        # the bug surfaces rather than going invisible.
        if isinstance(seat_map, dict):
            for slot, pid in seat_map.items():
                if pid == sd.player_id and any(c.core.name == slot for c in snapshot.characters):
                    return slot
        else:
            logger.warning(
                "_resolve_acting_character_name.seat_lookup_non_dict — "
                "room.slot_to_player_id() returned %s (expected dict); "
                "falling back to player_name match. Hook contract drift.",
                type(seat_map).__name__,
            )
    for char in snapshot.characters:
        if char.core.name == sd.player_name:
            return char.core.name
    return snapshot.characters[0].core.name


def player_log_content(action: str, merged_player_actions: list[tuple[str, str]] | None) -> str:
    """The verbatim player text to persist into ``narrative_log``.

    sq-playtest 2026-06-07 barsoom (#177 defect a): the narrative_log
    player-turn entry recorded the *scaffolded* narrator input — the
    ``[INITIATIVE ORDER] …`` preamble (``initiative_preamble``) prepended to
    the ``"Name: action"`` join that ``dispatch_fired_barrier`` hands the
    narrator. James's actual words were buried (or, in the transcript view,
    lost entirely) behind the engine's resolution-order scaffold, and the
    corruption rode through to journal / scrapbook / replay.

    The clean source is ``TurnContext.merged_player_actions`` — the per-PC
    ``(character_name, raw_action)`` tuples drained from the barrier buffer,
    which NEVER carry the preamble. Solo (one tuple) → the raw text alone;
    MP (N tuples) → name-tagged declarations, one per line, so the GM panel
    keeps per-speaker attribution. When ``merged`` is absent (the room-is-None
    solo path and the dice-replay re-entry both pass a clean ``action`` with
    no preamble), fall back to ``action`` verbatim.
    """
    if not merged_player_actions:
        return action
    if len(merged_player_actions) == 1:
        return merged_player_actions[0][1]
    return "\n".join(f"{name}: {act}" for name, act in merged_player_actions)


def _project_current_region(sd: _SessionData, snapshot: GameSnapshot) -> object | None:
    """Beneath Sünden BETTER fix (seam 1+2) — per-turn region projection.

    Re-derive the party's current region from the live ``DungeonRepository``
    (Postgres is the single source of truth, ADR-115 — never mirrored onto
    the persisted snapshot, which has a documented divergence disease). The
    result rides ``TurnContext.region_projection`` and renders as the
    Early-zone "you are here" section + the constrained move vocabulary.

    Returns ``None`` for every non-beneath_sunden turn AND for the
    loud-but-non-fatal failure modes (no schema, empty map, blank
    current_region, phantom region, missing theme). Every path emits
    exactly one ``dungeon.region_projection`` event so the GM panel can
    tell "narrator was fed the real region" from "narrator improvised"
    (the OTEL lie-detector mandate). A live turn must never hard-fail on
    a dungeon defect — the region_init wired-call-site precedent: log
    loud, emit OTEL, continue.

    Lazy imports: ``sidequest.dungeon`` depends on game models, so a
    top-level import here would invert the layering (the
    ``session._apply_world_patch_inner`` frontier-hook lazy-import
    precedent).
    """
    from sidequest.dungeon.region_projection import applies_to, project_region
    from sidequest.telemetry.spans import dungeon_region_projection_span

    current_region = snapshot.current_region or ""
    with dungeon_region_projection_span(current_region=current_region) as span:
        if not applies_to(sd.genre_slug, sd.world_slug):
            # Not the procedural megadungeon — but cartography region-mode
            # worlds (e.g. tea_and_murder/glenross) have an authored region
            # graph in cartography.yaml. Project from it so the narrator gets
            # the same YOU-ARE-HERE section + MOVEMENT RULE and emits
            # current_region (the frozen-Location-panel fix, 2026-05-21).
            from sidequest.server.cartography_region_projection import (
                project_cartography_region,
            )

            world_obj = sd.genre_pack.worlds.get(sd.world_slug)
            cartography_projection = project_cartography_region(world_obj, current_region)
            if cartography_projection is not None:
                span.set_attribute("outcome", "cartography_projection")
                span.set_attribute("source", "cartography")
                span.set_attribute("region_id", cartography_projection.region_id)
                span.set_attribute("exit_count", len(cartography_projection.exits))
                return cartography_projection

            span.set_attribute("outcome", "no_dungeon")
            span.set_attribute(
                "reason",
                f"other_world genre={sd.genre_slug!r} world={sd.world_slug!r}",
            )
            return None

        from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
        from sidequest.dungeon.themes import load_theme_palette
        from sidequest.game.persistence import DatabaseError
        from sidequest.genre.loader import (
            DEFAULT_GENRE_PACK_SEARCH_PATHS,
            GenreLoader,
        )

        try:
            graph = sd.dungeon_repository.load_map(entrance_id=ENTRANCE_ID)
        except DatabaseError as exc:
            span.set_attribute("outcome", "no_dungeon")
            span.set_attribute("reason", f"no_dungeon_schema: {exc}")
            logger.warning(
                "dungeon.region_projection no dungeon schema (genre=%s world=%s): %s",
                sd.genre_slug,
                sd.world_slug,
                exc,
            )
            return None

        if not graph.nodes:
            span.set_attribute("outcome", "no_dungeon")
            span.set_attribute("reason", "empty_map")
            logger.warning(
                "dungeon.region_projection empty dungeon_map for a "
                "beneath_sunden session — the bootstrap materialize did "
                "not persist any regions (genre=%s world=%s)",
                sd.genre_slug,
                sd.world_slug,
            )
            return None

        if not current_region or current_region not in graph.nodes:
            # Playtest 2026-05-20: a third face surfaced — current_region
            # is a deliberately-authored CARTOGRAPHY region (e.g.
            # beneath_sunden's surface ``ropefoot`` waiting-camp), NOT a
            # phantom and NOT a graph node. The player is genuinely on
            # the surface; the procedural dungeon graph is the
            # *underground* lane. Self-healing this case to ``entrance``
            # silently teleports the player into the dungeon every turn
            # AND mutates the persisted snapshot, destroying the
            # narrative anchor. Distinguish the two:
            #
            #   (a) current_region is a cartography region → outside the
            #       graph lane is correct. Return None gracefully, no
            #       projection, no mutation. Span carries
            #       outcome=cartography_region so the GM panel sees the
            #       turn ran without procedural geography (intentional,
            #       not a failure).
            #   (b) current_region is blank OR a phantom → original
            #       SELF-HEAL applies (bind to graph entrance, mutate
            #       snapshot, error-log the recovery).
            world_obj = sd.genre_pack.worlds.get(sd.world_slug)
            cartography_regions: set[str] = set()
            if world_obj is not None:
                cart = getattr(world_obj, "cartography", None)
                if cart is not None and getattr(cart, "regions", None):
                    cartography_regions = set(cart.regions.keys())
            if current_region and current_region in cartography_regions:
                span.set_attribute("outcome", "cartography_region")
                span.set_attribute(
                    "reason",
                    f"current_region={current_region!r} is a static "
                    f"cartography region (surface lane), not a node of "
                    f"the procedural dungeon graph — no projection",
                )
                return None
            # Two faces of ONE disease: a fully materialized dungeon whose
            # current_region is not a real graph node — either blank (the
            # #314 entrance-bind seam never fired; OQ-1's 2026-05-17
            # verification proved a slug_resume connect returns before
            # connect.py's attach call site, so a RESUMED beneath_sunden
            # save has a materialized dungeon but a blank current_region
            # forever) OR a PHANTOM (narration title-parsing wrote a prose
            # name not in cartography — not a node id;
            # this fired dungeon.region_projection FAILED every single
            # turn until the constrained-move-vocab seam lands). Both
            # cases mean the same thing here and have the same only-safe
            # truth: bind the graph entrance (the BFS root). #314 lives at
            # a seam the resume branch never reaches; THIS seam runs every
            # narration turn on every connect branch, so it is the correct
            # place to self-heal. NOT a silent fallback — the span carries
            # bound_entrance=true with the healed-from value and the log
            # is error-level so the GM panel shows the recovery fired.
            _unbound_from = current_region or "<blank>"
            entrance = graph.entrance_id
            if entrance not in graph.nodes:
                span.set_attribute("outcome", "no_dungeon")
                span.set_attribute(
                    "reason",
                    f"unbound_region AND entrance {entrance!r} absent from "
                    f"graph (have: {sorted(graph.nodes)}) — corrupt seed",
                )
                logger.error(
                    "dungeon.region_projection UNBOUND + entrance %r not "
                    "in graph — corrupt dungeon, cannot self-heal",
                    entrance,
                )
                return None
            snapshot.current_region = entrance
            if entrance not in snapshot.discovered_regions:
                snapshot.discovered_regions.append(entrance)
            # Movement subsystem §Q0: per-turn self-heal also re-seeds seated
            # PCs' per-PC region so region_for(perspective=pc) resolves (no
            # current_region fallback).
            snapshot.seed_pc_regions(entrance)
            span.set_attribute("bound_entrance", True)
            span.set_attribute("healed_from", _unbound_from)
            logger.error(
                "dungeon.region_projection SELF-HEAL: beneath_sunden save "
                "had a materialized dungeon (%d regions) but current_region "
                "%r was not a graph node (blank, or a narration-title "
                "phantom the constrained-move-vocab seam does not yet "
                "prevent) — bound current_region=%r at the per-turn "
                "projection seam so the narrator gets real geography this "
                "turn instead of failing every turn",
                len(graph.nodes),
                _unbound_from,
                entrance,
            )
            current_region = entrance

        loader = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS)
        world_dir = loader.find(sd.genre_slug) / "worlds" / sd.world_slug
        # pack root holds themes/ — mirrors session_integration._theme_pack_root
        palette = load_theme_palette(world_dir.parent.parent)

        try:
            proj = project_region(graph, current_region, palette)
        except (ValueError, KeyError) as exc:
            # current_region set but not a graph node, or its theme id is
            # absent from the palette — a real seed/binding bug. Loud,
            # but never hard-fail a live turn (region_init precedent).
            span.set_attribute("outcome", "no_dungeon")
            span.set_attribute("reason", f"projection_failed: {exc}")
            logger.error(
                "dungeon.region_projection FAILED region=%r: %s",
                current_region,
                exc,
            )
            return None

        span.set_attribute("outcome", "projected")
        span.set_attribute("region_id", proj.region_id)
        span.set_attribute("theme_id", proj.theme_id)
        if proj.depth_score is not None:
            span.set_attribute("depth_score", proj.depth_score)
        span.set_attribute("exit_count", len(proj.exits))
        return proj


def _build_turn_context(
    sd: _SessionData,
    *,
    opening_directive: str | None = None,
    opening_seed_shown: bool = False,
    lore_context: str | None = None,
    entity_retrieval: RetrievedEntities | None = None,
    room: SessionRoom | None = None,
) -> TurnContext:
    """Assemble :class:`TurnContext` for one narration turn (Slice H).

    ``opening_directive`` is consumed turn 0 only (caller clears the
    session field). ``opening_seed_shown`` marks the seeded-opening case
    where the action IS the already-cold-opened ``first_turn_invitation``
    (pingpong 2026-06-05 [BAR-1] — the prompt builder reframes the
    recency action block so the narrator does not restate it).
    ``lore_context`` is the pre-rendered <lore> block. ``room`` provides
    the seat map so MP can identify the acting PC by player_id rather
    than guessing snapshot.characters[0].
    """
    from sidequest.agents.encounter_render import render_encounter_summary
    from sidequest.server.dispatch.confrontation import find_confrontation_def

    snapshot = sd.snapshot
    char_name = _resolve_acting_character_name(sd, room)

    # Encounter flags from snapshot.encounter (Story 3.4). Category-based
    # flags from the matched ConfrontationDef; skip resolved encounters
    # so a closed combat doesn't keep flipping in_combat=True.
    encounter = snapshot.encounter
    confrontation_def = None
    encounter_summary = None
    in_combat = False
    in_chase = False
    in_encounter = False
    all_defs = sd.genre_pack.rules.confrontations if sd.genre_pack.rules else []
    available_confrontations: list[tuple[str, str, str]] = [
        (
            cd.confrontation_type,
            cd.label,
            getattr(cd, "category", "") or "",
        )
        for cd in all_defs
    ]
    if encounter is not None and not encounter.resolved:
        in_encounter = True
        confrontation_def = find_confrontation_def(all_defs, encounter.encounter_type)
        if confrontation_def is not None:
            in_combat = confrontation_def.category == "combat"
            in_chase = confrontation_def.category == "movement"
        encounter_summary = render_encounter_summary(encounter)

    # Group C — LethalityArbiter inputs. PCs mapped to owning player_id
    # via the room seat table; the acting socket's PC also lands under
    # sd.player_id so the arbiter can find the actor directly.
    pc_cores_by_player: dict[str, CreatureCore] = {}
    seat_lookup_fn = getattr(room, "slot_to_player_id", None) if room is not None else None
    # Story 61-8 §D6 — same pattern as ``_resolve_acting_character_name``:
    # pyright can't statically prove the duck-typed callable returns a
    # dict, so narrow before use. Review-fix round 2: surface a non-dict
    # return as a warning rather than silently substituting an empty
    # dict (CLAUDE.md No Silent Fallbacks).
    _raw_seat_map = seat_lookup_fn() if callable(seat_lookup_fn) else {}
    if not isinstance(_raw_seat_map, dict):
        logger.warning(
            "_build_turn_context.seat_lookup_non_dict — "
            "room.slot_to_player_id() returned %s (expected dict); "
            "falling back to empty seat map. Hook contract drift.",
            type(_raw_seat_map).__name__,
        )
        seat_map: dict[str, str] = {}
    else:
        seat_map = _raw_seat_map
    char_to_player = dict(seat_map.items())
    for pc in snapshot.characters:
        owner_pid = char_to_player.get(pc.core.name)
        if owner_pid is None and pc.core.name == char_name:
            owner_pid = sd.player_id
        if owner_pid:
            pc_cores_by_player[owner_pid] = pc.core
    npc_cores_by_name: dict[str, CreatureCore] = {npc.core.name: npc.core for npc in snapshot.npcs}

    # Story 45-8 — Notorious-party gating on session.player_count.
    #
    # Playtest 3 regression (evropi/pumblestone): a solo session whose
    # snapshot still carried the canonical full-party cast (Rux, Hant,
    # Ludzo, ...) leaked those names into the narrator's prose because
    # the peer filter only excluded ``char_name`` — it did not check
    # session player_count. The gate below drops every snapshot peer
    # when the room reports a single playing player, and redacts the
    # ``snapshot.characters`` JSON in ``state_summary`` so the names
    # cannot ride in via the game-state block either.
    #
    # AC4 (No silent fallbacks): if ``room`` is None the gate machinery
    # is unreachable. We default to safe-empty (no peers) AND emit a
    # WARNING — never silently fall through to "all snapshot characters
    # are peers".
    if room is None:
        player_count_for_gate = 0
        gate_engaged = True
        logger.warning(
            "orchestrator.notorious_party_gate room=None — gate machinery "
            "unreachable, defaulting to safe-empty party_peers "
            "(notorious_party_gated=true, party_context_available=false). "
            "session.player_count unknown.",
        )
    else:
        # ``non_abandoned_player_count`` is the right source of truth: it
        # counts seats in CHARGEN/PLAYING (every "live" lobby slot), which
        # matches the bug surface — a solo save where only Pumblestone has
        # a seat — without requiring every peer to have transitioned to
        # PLAYING (the failing-precondition for ``playing_player_count``
        # mid-chargen). ABANDONED orphans correctly drop out.
        count_method = getattr(room, "non_abandoned_player_count", None) or getattr(
            room, "playing_player_count", None
        )
        try:
            # Story 61-8 §D6 — pyright sees ``count_method`` as
            # ``Callable | None`` and complains. The except-Exception
            # wrapper below catches the AttributeError when count_method
            # is None as documented (fail-loud-default-to-safe-empty),
            # so the assert that it's not None is correct contract-wise
            # — but we use a defensive runtime check so the type-system
            # narrowing is explicit and pyright stops complaining.
            if count_method is None:
                raise TypeError(
                    "room exposes neither non_abandoned_player_count nor playing_player_count"
                )
            player_count_for_gate = int(count_method())
        except Exception:  # noqa: BLE001 — fail loud on any contract drift
            logger.warning(
                "orchestrator.notorious_party_gate "
                "player_count lookup raised — defaulting to safe-empty "
                "(notorious_party_gated=true, party_context_available=false).",
                exc_info=True,
            )
            player_count_for_gate = 0
        # AC1/AC2: gate is strict `== 1` (solo). `> 1` passes. `<= 0` is
        # an impossible state in normal operation (no seated players AND
        # we're trying to build a turn) — treat as fail-loud-empty rather
        # than re-opening the leak.
        if player_count_for_gate == 1:
            gate_engaged = True
        elif player_count_for_gate > 1:
            gate_engaged = False
        else:
            logger.warning(
                "orchestrator.notorious_party_gate "
                "player_count=%d (<= 0) — impossible state, defaulting "
                "to safe-empty (notorious_party_gated=true).",
                player_count_for_gate,
            )
            gate_engaged = True

    if gate_engaged:
        party_peers: list[PartyPeer] = []
    else:
        # Story 37-36: peer-identity packets, acting PC excluded.
        party_peers = [
            PartyPeer.from_character(pc) for pc in snapshot.characters if pc.core.name != char_name
        ]
    party_context_available = bool(party_peers)

    # AC3 — Fire the gate-decision span on EVERY turn. The GM panel
    # filters on this so Sebastien can see whether the gate engaged.
    with orchestrator_notorious_party_gate_span(
        player_count=player_count_for_gate,
        notorious_party_gated=gate_engaged,
        party_context_available=party_context_available,
    ):
        logger.info(
            "orchestrator.notorious_party_gate "
            "session.player_count=%d notorious_party_gated=%s "
            "party_context_available=%s",
            player_count_for_gate,
            gate_engaged,
            party_context_available,
        )

    # Story 45-1 — sealed-letter shared-world handshake. Build the
    # canonical delta, merge it back, and attach to state_summary so the
    # narrator sees ground-truth party adjacency. Without this the
    # narrator fabricates separations ("collapsed corridor" — playtest 3).
    # The merge is idempotent on a fresh snapshot (all fields already
    # match) — its job is to fire the OTEL event and provide MergeResult.
    handshake_delta = build_shared_world_delta(snapshot, room=room)
    merge_shared_delta_into_snapshot(snapshot, handshake_delta)
    # Story 57-5 / ADR-110 Phase A — compact, default-pruned JSON encode.
    # ``exclude_defaults=True`` drops pydantic default-equal fields
    # (empty lists/dicts, zero counters); ``exclude_none=True`` drops
    # ``None``-valued options. Pydantic v2 round-trip equivalence holds
    # (defaults reconstruct on parse); see
    # ``test_compact_form_round_trips_to_model_dump_equivalent``.
    state_summary_payload = snapshot.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )
    # Story 49-1 — drop narrative_log from the Valley-zone state_summary
    # JSON dump. The last K=2 entries (57-1) now ride into the narrator prompt
    # via the Recency-zone ``recent_narrative_context`` section (see
    # orchestrator.build_narrator_prompt). Keeping the duplicate here
    # would put the same prose in two zones — high-attention Recency
    # AND decayed Valley — re-creating the attention-decay disease this
    # story exists to cure.
    # Also enforced via ``apply_snapshot_slimming``'s Phase B drop below
    # (story 61-5 added ``narrative_log`` to the registry); this pop is
    # kept for defense-in-depth pending follow-up consolidation.
    state_summary_payload.pop("narrative_log", None)
    # Story 45-8 — when the gate is engaged, also redact non-self PCs
    # from the state_summary JSON. Without this redaction the canonical
    # party names ride into the narrator's <game_state> block via the
    # snapshot dump even though ``ctx.party_peers`` is empty.
    if gate_engaged and isinstance(state_summary_payload.get("characters"), list):
        state_summary_payload["characters"] = [
            entry
            for entry in state_summary_payload["characters"]
            if isinstance(entry, dict)
            and (
                entry.get("core", {}).get("name") == char_name
                if isinstance(entry.get("core"), dict)
                else entry.get("name") == char_name
            )
        ]
    # Resolve the acting PC's current room ONCE for both the 61-2
    # projection seam and the 45-13 room-state-injection span below.
    # Single resolution avoids fan-out of ``snapshot.party_location_query``
    # OTEL spans (party_location emits one per call; the previous shape
    # called it once per NPC inside the projection loop = N+1 spans per
    # turn, drowning the GM panel's lie-detector signal).
    current_room_id = snapshot.party_location(perspective=char_name)
    if not current_room_id:
        # No silent fallback (CLAUDE.md): a turn without a canonical
        # actor location renders both the room-state injection gate
        # (45-13) AND the 61-2 room_states/npcs projection unsafe —
        # projecting "current room only" when there IS no current room
        # would strip every room and every NPC from the narrator's
        # <game_state>, which is the projection-as-gaslighter pattern
        # (see ``project_narrator_gaslighting_doctrine.md``: don't
        # strip state silently when the narrator depends on it).
        # Fire the warning HERE — before the projection seam — and
        # pass ``None`` into ``_apply_phase_c_projections`` so the
        # room_states/npcs branches noop and the original snapshot
        # data rides through. Other 61-2 projections (known_facts,
        # discovered_clues) are PC/scenario-scoped and still run.
        logger.warning(
            "state.room_state_injected_unreachable reason=actor_location_empty interaction=%d",
            snapshot.turn_manager.interaction,
        )

    # Story 57-5/61-2 → 82-10 / ADR-110 Phase B + C via the shared
    # ``apply_snapshot_slimming`` seam (extracted so the Intent Router
    # pass applies the same audited cut): Phase B field-pruning drop
    # list, then projections for the four growing snapshot fields —
    # room_states (project to acting PC's current room), npcs (in-scene
    # only + drop nested belief_state), characters[*].known_facts
    # (tail-K=8 per PC), scenario_state.discovered_clues (cap=12).
    # Counts ride on the prompt.game_state.bytes span below so the GM
    # panel can verify the cut engaged per-field (the lie-detector
    # contract). Phase B running after the 45-8 redaction above is
    # order-independent: the redaction touches only ``characters``,
    # which is not in the drop list.
    _projection_counts = apply_snapshot_slimming(
        snapshot,
        state_summary_payload,
        current_room_id=current_room_id,
    )

    state_summary_payload["party_formation"] = [
        entry.model_dump() for entry in handshake_delta.party_formation
    ]
    state_summary_payload["shared_world_delta"] = handshake_delta.model_dump()

    # Story 45-13 — per-room container retrieved-state injection. Read
    # the current room's RoomState (if any) and surface a count via the
    # ``room.state_injected`` span. The span fires on EVERY narrator
    # turn — including the no-prior-retrievals case
    # (``retrieved_container_count=0``) — because Sebastien's
    # lie-detector must be able to distinguish "gate engaged with
    # nothing to report" from "gate not engaged at all". Per Wave 2B
    # (story 45-48), the room is keyed off the acting PC's location
    # (``snapshot.party_location(perspective=char_name)``, resolved
    # above and reused here). The retrieved-container payload also
    # flows into ``state_summary_payload`` automatically because
    # ``snapshot.room_states`` was serialized above (and projected to
    # the current room only when actor location was resolvable).
    if not current_room_id:
        current_room_id = ""
    current_room_state = snapshot.room_states.get(current_room_id)
    retrieved_container_count = (
        sum(1 for c in current_room_state.containers.values() if c.retrieved)
        if current_room_state is not None
        else 0
    )
    with room_state_injected_span(
        room_id=current_room_id,
        retrieved_container_count=retrieved_container_count,
        interaction=snapshot.turn_manager.interaction,
    ):
        logger.info(
            "state.room_state_injected room=%s retrieved_count=%d",
            current_room_id,
            retrieved_container_count,
        )

    # Story 57-5 / ADR-110 — compact JSON (no whitespace) for the
    # serialized state_summary. Together with Phase A + Phase B above this
    # delivers the >=50% byte reduction the ADR commits to.
    state_summary_json = json.dumps(state_summary_payload, separators=(",", ":"))

    # Story 57-5 / ADR-110 §Observability — emit the
    # ``prompt.game_state.bytes`` span every narrator turn so the GM panel
    # can verify the cut is live and within its acceptance gate.
    # ``bytes_before`` is the size of the pre-slimming encoding pattern
    # (``model_dump_json`` + ``indent=2``) on the same payload — the
    # reference the >=50% gate is measured against. Sebastien's
    # lie-detector contract: the span fires regardless of phase flags.
    _baseline_payload = json.loads(snapshot.model_dump_json())
    _bytes_before = len(json.dumps(_baseline_payload, indent=2).encode("utf-8"))
    _bytes_after = len(state_summary_json.encode("utf-8"))
    with prompt_game_state_bytes_span(
        phase_a_applied=True,
        phase_b_applied=True,
        bytes_before=_bytes_before,
        bytes_after=_bytes_after,
        # Story 61-8 §D6 (review-fix round 2): pyright emits
        # ``reportArgumentType`` because ``**dict[str, int]`` is not
        # statically compatible with the span's typed keyword
        # parameters (the spread could in principle supply any key,
        # including the keyword-only ``_tracer: Tracer | None``). At
        # runtime the dict is constructed locally a few lines above
        # and contains only projection-count integer keys. The
        # ``pyright: ignore`` documents the deliberate type-check
        # waiver; the structural fix (rename ``_tracer`` → a non-
        # underscore-prefixed name on every span helper) is filed in
        # this story's Delivery Findings.
        **_projection_counts,  # pyright: ignore[reportArgumentType]
    ):
        logger.info(
            "prompt.game_state.bytes phase_a=1 phase_b=1 bytes_before=%d bytes_after=%d ratio=%.3f",
            _bytes_before,
            _bytes_after,
            (_bytes_after / _bytes_before) if _bytes_before else 0.0,
        )

    # Story 45-27 — trope foreground / background prompt zones.
    # ``pending_trope_context`` is the Early-zone load-bearing block
    # (FOREGROUND_K most-active tropes, full beat directives);
    # ``active_trope_summary`` is the Valley-zone summary (remaining
    # progressing tropes, one line each). Both default to None when
    # there are no progressing tropes so the orchestrator's prompt-
    # section registry skips registration entirely (zero-byte-leak
    # discipline matching the state_summary pattern at
    # orchestrator.py:1320).
    from sidequest.game.trope_tick import (  # noqa: PLC0415
        render_background_block,
        render_foreground_block,
        select_foreground_tropes,
    )

    foreground_tropes, background_tropes = select_foreground_tropes(snapshot.active_tropes)
    pack_tropes_by_id = {td.id: td for td in (sd.genre_pack.tropes or []) if td.id is not None}
    foreground_block = render_foreground_block(foreground_tropes, pack_tropes_by_id)
    background_block = render_background_block(background_tropes, pack_tropes_by_id)
    pending_trope_context = foreground_block or None
    active_trope_summary = background_block or None

    # Orbital tier fields — populated from Session when the room has one.
    # When room is None (test fixtures, legacy paths) the fields default
    # to None/empty, which causes build_narrator_prompt to skip the
    # <courses> block entirely — zero byte leak, no silent fallback.
    orbital_content = None
    orbital_scope = None
    party_body_id = None
    recent_body_mentions: list[str] = []
    quest_anchors: list[str] = []
    if room is not None:
        sess = room.session
        orbital_content = sess.orbital_content
        orbital_scope = sess.orbital_scope  # always returns Scope (never None)
        party_body_id = sess.party_body_id
        recent_body_mentions = list(sess.recent_body_mentions)
        quest_anchors = list(snapshot.quest_anchors)

    # Story 75-5 (ADR-118 §D4): render the universal-retrieval fill into typed
    # Valley blocks. Only non-empty tiers render — an empty tier stays None so
    # orchestrator registers no section (zero-byte-leak). The floor is NOT
    # rendered here; it rides ``npc_working_set`` already (no double-injection).
    retrieved_entity_npcs: str | None = None
    retrieved_entity_locations: str | None = None
    retrieved_entity_factions: str | None = None
    retrieved_entity_relationships: str | None = None
    retrieved_entity_quests: str | None = None
    retrieved_entity_tropes: str | None = None
    if entity_retrieval is not None:
        if entity_retrieval.retrieved_npcs:
            retrieved_entity_npcs = render_entity_section(
                "retrieved_npcs", entity_retrieval.retrieved_npcs
            )
        if entity_retrieval.retrieved_locations:
            retrieved_entity_locations = render_entity_section(
                "retrieved_locations", entity_retrieval.retrieved_locations
            )
        if entity_retrieval.retrieved_factions:
            retrieved_entity_factions = render_entity_section(
                "retrieved_factions", entity_retrieval.retrieved_factions
            )
        # Story 84-3 (WI-4, ADR-118 §A2, Reviewer blocker): render the §A2
        # floor-companion relationship cards into a typed Valley block, mirroring
        # the npc/location/faction sections. Without this the relationship card the
        # retrieval surfaced dies at the render seam and never reaches the narrator.
        if entity_retrieval.retrieved_relationships:
            retrieved_entity_relationships = render_entity_section(
                "retrieved_relationships", entity_retrieval.retrieved_relationships
            )
        # Story 84-5 (WI-2, ADR-118 §A2): render the DORMANT quest / trope recall
        # cards into typed Valley blocks. These are for items surfaced via RETRIEVAL
        # (a completed quest / resolved trope the player referenced) — distinct from
        # the ACTIVE quest/trope render paths (state_summary / trope foreground),
        # which are untouched. No double-render: only dormant items reach here.
        if entity_retrieval.retrieved_quests:
            retrieved_entity_quests = render_entity_section(
                "retrieved_quests", entity_retrieval.retrieved_quests
            )
        if entity_retrieval.retrieved_tropes:
            retrieved_entity_tropes = render_entity_section(
                "retrieved_tropes", entity_retrieval.retrieved_tropes
            )

    # Story 81-3 (ADR-025): derive the pacing hint from the per-session
    # TensionTracker (the 81-2 producer on _SessionData) using the genre's
    # DramaThresholds, then stamp it onto TurnContext so the orchestrator's
    # [PACING] injection (the `if context.pacing_hint is not None` guard) finally
    # fires. ``drama_thresholds`` is None when the pack ships no pacing.yaml
    # (e.g. caverns_and_claudes) — DramaThresholds()'s own defaults are the
    # model's documented absent-pacing behavior, not a silent fallback. The span
    # is the GM-panel lie detector: it records the hint the narrator received so
    # the dev can confirm it tracks real tension state (OTEL Observability).
    pacing_thresholds = sd.genre_pack.drama_thresholds or DramaThresholds()
    pacing_hint = sd.tension_tracker.pacing_hint(pacing_thresholds)
    # Story 77-7 (ADR-024/025/128): if the lull-escalation engine fired a seed
    # last turn, its narrative_hint is THIS turn's concrete escalation directive.
    # Override the generic 'environment shifts' escalation_beat with it and
    # consume the one-shot directive (sibling of next_turn_directives'
    # populate-then-consume discipline). PacingHint is frozen → dataclasses.replace.
    # Applied regardless of the current boring_streak: the directive can outlive
    # the streak the fire itself reset.
    _lull_directive = snapshot.pending_escalation_directive
    if _lull_directive:
        import dataclasses  # noqa: PLC0415

        pacing_hint = dataclasses.replace(pacing_hint, escalation_beat=_lull_directive)
        snapshot.pending_escalation_directive = None
    with pacing_hint_span(
        drama_weight=pacing_hint.drama_weight,
        target_sentences=pacing_hint.target_sentences,
        delivery_mode=str(pacing_hint.delivery_mode),
        escalation_present=pacing_hint.escalation_beat is not None,
    ):
        pass

    # Story 82-2 (ADR-049) — resolve the active narrator verbosity + vocabulary
    # the player chose (carried on _SessionData, hydrated from the CONNECT
    # payload / persisted snapshot). When the player made no choice we fall back
    # to default_for_player_count using the same live player count the
    # notorious-party gate computed above — NEVER a hardcoded literal (No Silent
    # Fallbacks). The span is the GM-panel lie detector: it records the active
    # setting AND whether it came from the player or the default.
    verbosity_source = "player" if sd.narrator_verbosity is not None else "default_for_player_count"
    vocabulary_source = (
        "player" if sd.narrator_vocabulary is not None else "default_for_player_count"
    )
    resolved_verbosity = sd.narrator_verbosity or NarratorVerbosity.default_for_player_count(
        player_count_for_gate
    )
    resolved_vocabulary = sd.narrator_vocabulary or NarratorVocabulary.default_for_player_count(
        player_count_for_gate
    )
    with narrator_settings_span(
        narrator_verbosity=str(resolved_verbosity),
        narrator_vocabulary=str(resolved_vocabulary),
        verbosity_source=verbosity_source,
        vocabulary_source=vocabulary_source,
    ):
        pass

    # Light & Darkness survival clock (Task 7.1). Surface the ``light``
    # ResourcePool (current/max + threshold narrator_hints) and the acting PC's
    # active darkness statuses so the narrator's guttering/dark prose is
    # state-driven, not improvised. Both default to None/empty on packs without
    # a light clock (zero-byte-leak). The darkness status is matched by its
    # structured ``source`` marker (never by wording), so a combat wound that
    # happens to share text can never leak into the light block.
    light_pool = snapshot.resources.get("light")
    darkness_statuses: list[Status] = []
    if light_pool is not None:
        # Lazy import: ``environment_clock`` pulls ``game.session`` which
        # transitively reaches ``narration_apply`` → ``session_helpers``, so a
        # module-top import here is circular. Local import breaks the cycle.
        from sidequest.agents.subsystems.environment_clock import (  # noqa: PLC0415
            DARKNESS_STATUS_SOURCE,
        )

        acting_core = snapshot.find_creature_core(char_name)
        if acting_core is not None:
            darkness_statuses = [
                st for st in acting_core.statuses if st.source == DARKNESS_STATUS_SOURCE
            ]

    return TurnContext(
        pacing_hint=pacing_hint,
        in_combat=in_combat,
        in_chase=in_chase,
        in_encounter=in_encounter,
        encounter=encounter if in_encounter else None,
        confrontation_def=confrontation_def,
        available_confrontations=available_confrontations,
        encounter_summary=encounter_summary,
        state_summary=state_summary_json,
        narrator_verbosity=resolved_verbosity,
        narrator_vocabulary=resolved_vocabulary,
        genre=sd.genre_slug,
        genre_prompts=sd.genre_pack.prompts,
        # Phase E wiring (completes the deferral the ToolContext docstring
        # flags as "Phase E wires this at the production call site"). Before
        # this, TurnContext carried none of these and the SDK narrator path
        # degraded to world_id=unknown / session_id=adhoc / turn_number=0
        # with lore_store=None — query_lore returned hit_count=0 every turn
        # (~103×/session) and the narrator confabulated canon. world_id ←
        # world_slug; session_id ← the slug-based game_slug; turn_number ←
        # snapshot.turn_manager.interaction (the per-turn interaction count
        # Jaeger showed stuck at 0); store/lore_store/monster_manual are the
        # live session-handler references the query_lore / lookup_monster
        # tools read through ToolContext.
        world_id=sd.world_slug,
        session_id=sd.game_slug,
        repository=sd.repository,
        # Story 59-1: the SDK ToolContext stamps this so begin_confrontation
        # can validate the requested confrontation type against the genre. The
        # tool signals; narration_apply creates the encounter on the canonical
        # snapshot via result.confrontation (the SDK engagement path the
        # narrator lacked).
        pack=sd.genre_pack,
        lore_store=sd.lore_store,
        monster_manual=sd.monster_manual,
        # Story 24-10: world-grounding pass-through (same Phase-E seam as
        # lore_store / monster_manual). Loaded once at session bootstrap and
        # stamped on every turn so get_world_grounding returns real data
        # instead of None. None on a pack/world with no grounding authored.
        weather_state=sd.weather_state,
        world_demographics=sd.world_demographics,
        world_calendar=sd.world_calendar,
        turn_number=snapshot.turn_manager.interaction,
        character_name=char_name,
        current_location=(
            _resolve_location_display(
                sd.genre_pack,
                sd.world_slug,
                snapshot.party_location(perspective=char_name),
            )
            or "Unknown"
        ),
        available_sfx=_sfx_ids_from_genre(sd.genre_pack),
        npc_pool=list(snapshot.npc_pool),
        npcs=list(snapshot.npcs),
        # Story 75-2: budgeted working-set selection (the floor of ADR-118's
        # universal retrieval). Scene-present NPCs render full, off-stage
        # collapse to compact — bounding prompt cost without eviction.
        #
        # Story 75-10: consume the floor already computed by the retrieval
        # delegate (``retrieve_turn_context``), which built it with the real
        # per-turn ``player_referenced_npcs`` signal derived from the player's
        # action — so off-stage NPCs the player named render BRIEF, not compact.
        # This also removes a double-computation (the floor was built once
        # upstream and again here). The recompute path survives only for
        # legacy/fixture callers that pass no ``entity_retrieval`` (dice-replay /
        # error turns, unit tests) — those carry no fresh player reference, so
        # ``None`` (compact off-stage) is correct; the present-scene floor holds
        # in both paths.
        npc_working_set=(
            entity_retrieval.floor
            if entity_retrieval is not None
            else build_npc_working_set(
                snapshot,
                current_turn=snapshot.turn_manager.interaction,
                player_referenced_npcs=None,
            )
        ),
        party_peers=party_peers,
        opening_directive=opening_directive,
        opening_seed_shown=opening_seed_shown,
        world_context=sd.world_context,
        lore_context=lore_context,
        retrieved_entity_npcs=retrieved_entity_npcs,
        retrieved_entity_locations=retrieved_entity_locations,
        retrieved_entity_factions=retrieved_entity_factions,
        retrieved_entity_relationships=retrieved_entity_relationships,
        retrieved_entity_quests=retrieved_entity_quests,
        retrieved_entity_tropes=retrieved_entity_tropes,
        lethality_policy=sd.genre_pack.lethality_policy,
        pc_cores_by_player=pc_cores_by_player,
        npc_cores_by_name=npc_cores_by_name,
        orbital_content=orbital_content,
        orbital_scope=orbital_scope,
        party_body_id=party_body_id,
        recent_body_mentions=recent_body_mentions,
        quest_anchors=quest_anchors,
        pending_trope_context=pending_trope_context,
        active_trope_summary=active_trope_summary,
        # Source the recency window from the durable narrative_log (Postgres,
        # ADR-115) rather than the in-memory snapshot mirror.
        # sd.repository.append_narrative only writes to Postgres — the in-memory
        # snapshot.narrative_log is populated *only* by world_materialization
        # (one-shot at startup) and lore_seeding (chargen), never by the
        # per-turn narrator append site at
        # websocket_session_handler.py:2832/2840. Reading from snapshot
        # made the recency injection emit turn_count=0/total_tokens=0 forever
        # (sq-playtest 2026-05-15 — story 49-1's safety-net was dormant).
        # Postgres is the same ground-truth source Story 45-11's round
        # invariant lie-detector relies on; aligning here closes the same
        # snapshot/store divergence class.
        recent_narrative_log=sd.repository.recent_narrative(RECENT_NARRATIVE_WINDOW_K),
        # Story 102-7 (Plan 2 §5.4) — the AWN mutation surface for the
        # narrator context block. Both None on packs without mutations.yaml
        # or sessions with no seeded mutants; the orchestrator chokepoint
        # registers nothing in that case (zero token cost).
        mutation_state=snapshot.mutation_state,
        mutation_catalog=(sd.genre_pack.mutations if sd.genre_pack is not None else None),
        # Story 50-4: thread the live snapshot so build_narrator_prompt can
        # render + clear pending_time_skip_summary (one-shot lifecycle).
        snapshot=snapshot,
        # Beneath Sünden BETTER fix (seam 1+2): per-turn region projection
        # from the live DungeonStore. None for every non-beneath_sunden
        # turn (zero-byte leak) and for loud-but-non-fatal dungeon defects
        # (each emits a dungeon.region_projection span). This is what stops
        # the narrator improvising geography and gives it the real
        # adjacent region ids as the constrained move vocabulary.
        region_projection=_project_current_region(sd, snapshot),
        # Light & Darkness survival clock (Task 7.1) — see the block above.
        light_pool=light_pool,
        darkness_statuses=darkness_statuses,
    )


def _find_confrontation_def(pack: GenrePack, confrontation_type: str) -> object | None:
    """Match the narrator's confrontation hint to a pack ConfrontationDef.

    None → caller skips encounter context injection (narration-only fallback).
    """
    rules = getattr(pack, "rules", None)
    if rules is None:
        return None
    confrontations = getattr(rules, "confrontations", None) or []
    for conf_def in confrontations:
        if conf_def.confrontation_type == confrontation_type:
            return conf_def
    return None


def _world_history_value(pack: GenrePack, world_slug: str) -> object | None:
    """Raw world ``history.yaml`` payload, or None when absent.

    ``materialize_from_genre_pack`` treats None as zero chapters,
    yielding a snapshot with just genre/world slugs set.
    """
    world = pack.worlds.get(world_slug)
    if world is None:
        return None
    return world.history


def _error_msg(
    message: str,
    reconnect_required: bool = False,
    *,
    code: str | None = None,
) -> ErrorMessage:
    return ErrorMessage(
        type="ERROR",  # type: ignore[arg-type]
        payload=ErrorPayload(
            message=NonBlankString(message),
            reconnect_required=reconnect_required,
            code=code,
        ),
        player_id="",
    )


def _emit_unbound_rejection_event(message_type: str, state_name: str) -> None:
    """Surface a genuine session-unbound rejection to the GM panel (story 67-7).

    The transport guard correctly rejects action frames that arrive before the
    ``AwaitingConnect``→``Playing`` handshake binds the session — but a bare
    ``logger.info`` is invisible to the GM panel, so it cannot tell a genuine
    guard from the reconnect churn of a duplicate-socket loop. Emit a structured
    watcher event carrying the rejected frame type, the session state, and the
    ``session_unbound`` recovery classification. That classification is the
    discriminator (AC5): reconnect churn does not carry it, so the panel can
    separate a real unbound rejection from ordinary transport noise.

    Per the OTEL Observability Principle, every subsystem decision must emit a
    watcher event — the panel is the lie-detector. Call this only on the genuine
    ``session_unbound`` branch (the one tagged ``code="session_unbound"``), never
    on the Creating-state / data-missing rejection class, so the signal stays
    trustworthy.
    """
    from sidequest.telemetry.watcher_hub import publish_event

    publish_event(
        "state_transition",
        {
            "field": "session_binding",
            "op": "message_rejected_unbound",
            "message_type": message_type,
            "state": state_name,
            "recovery": "session_unbound",
        },
        component="session",
        severity="warning",
    )


def _presence_msg(player_id: str, state: str) -> PlayerPresenceMessage:
    """PLAYER_PRESENCE message for connect/disconnect (MP-02 Task 4)."""
    return PlayerPresenceMessage(
        payload=PlayerPresencePayload(player_id=player_id, state=state),  # type: ignore[arg-type]
    )


def _resolve_location_display(
    pack: GenrePack | None,
    world_slug: str | None,
    location: str | None,
) -> str:
    """Render a location id as a UI display name.

    Cartography room name > snake_case humanization > raw value.
    Empty input → empty string.
    """
    if not location:
        return ""
    if pack is not None and world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None:
            cart = getattr(world, "cartography", None)
            rooms = getattr(cart, "rooms", None) if cart is not None else None
            if rooms:
                for room in rooms:
                    if getattr(room, "id", None) == location:
                        return room.name
    if "_" in location and location == location.lower():
        return humanize_snake_case(location)
    return location


def _build_cartography_map_message(
    pack: GenrePack | None,
    world_slug: str | None,
    current_location: str | None,
    player_id: str = "",
    discovered_regions: list[str] | None = None,
) -> CartographyMapMessage | None:
    """Build a MAP_UPDATE message from cartography region data.

    Returns None when the pack has no region-mode cartography.

    ``discovered_regions`` is the PC's visited-region list (from
    ``snapshot.discovered_regions``). Each entry that is a REAL region
    slug in this world's cartography becomes an ``{id, name}`` entry in
    ``payload.explored`` so the client (``MapOverlay.tsx``) can highlight
    visited-but-not-current regions. Entries that are NOT valid region
    slugs are dropped — ``discovered_regions`` is known to be polluted
    with scene titles (ping-pong #329, e.g. ``"A Field of Blue Flowers,
    Munchkin Country"``), so filtering to ``regions`` doubles as a
    cleanup. This is a legitimate filter (we emit exactly the valid
    visited regions; the dropped count is recorded in the
    ``cartography.map_emitted`` OTEL span), NOT a silent fallback. The
    default ``None`` preserves an empty ``explored`` for existing
    callers/tests.
    """
    if pack is None or not world_slug or not current_location:
        return None
    world = pack.worlds.get(world_slug)
    if world is None:
        return None
    cart = getattr(world, "cartography", None)
    if cart is None:
        return None
    regions = getattr(cart, "regions", None)
    if not regions:
        return None
    nav_mode = getattr(cart, "navigation_mode", None)
    if nav_mode is not None and str(nav_mode) == "room_graph":
        return None

    # Player-map disclosure (sq-playtest 2026-06-07 spoiler leak): under
    # ``discovery_mode: fog`` only discovered regions ship with full lore;
    # their undiscovered neighbors ship name-only (the explorable frontier,
    # flagged ``undiscovered``); everything else never reaches the wire.
    # ``public`` (default) preserves the full-catalog behavior for worlds
    # whose map is common knowledge (a town, a neighborhood).
    discovery_mode = str(getattr(cart, "discovery_mode", "public") or "public")
    incoming = discovered_regions or []
    if discovery_mode == "fog":
        known: set[str] = {rid for rid in incoming if rid in regions}
        if current_location in regions:
            # The party is standing there — discovered by definition, even
            # when the visited-region ledger lags.
            known.add(current_location)
        frontier: set[str] = set()
        for rid in known:
            for adj in getattr(regions[rid], "adjacent", []):
                if adj in regions and adj not in known:
                    frontier.add(adj)
    else:
        known = set(regions)
        frontier = set()

    region_dict: dict[str, dict] = {}
    for slug, region in regions.items():
        if slug in known:
            region_dict[slug] = {
                "name": region.name,
                "description": getattr(region, "description", None)
                or getattr(region, "summary", None),
                "adjacent": [
                    a
                    for a in getattr(region, "adjacent", [])
                    if discovery_mode != "fog" or a in known or a in frontier
                ],
            }
        elif slug in frontier:
            # Name-only frontier entry: no description, no lore, no onward
            # adjacency (edges past the frontier would leak the graph).
            region_dict[slug] = {
                "name": region.name,
                "description": None,
                "adjacent": [],
                "undiscovered": True,
            }

    routes_list: list[dict] = []
    for route in getattr(cart, "routes", []):
        from_id = getattr(route, "from_id", None)
        to_id = getattr(route, "to_id", None)
        if discovery_mode == "fog":
            # A route reaches the wire only when both endpoints are visible
            # AND at least one is genuinely discovered (two frontier nodes
            # joined by a route would leak undiscovered topology).
            endpoints_visible = (from_id in known or from_id in frontier) and (
                to_id in known or to_id in frontier
            )
            if not endpoints_visible or (from_id not in known and to_id not in known):
                continue
        routes_list.append(
            {
                "name": route.name,
                "description": getattr(route, "description", None),
                "from_id": from_id,
                "to_id": to_id,
            }
        )

    # Visited-region overlay: keep only valid region slugs (drops the
    # scene-title pollution), de-duplicate while preserving first-seen
    # order. The client reads ``id`` (falls back to ``name``) for the
    # visited set; emit both.
    explored: list[dict] = []
    _seen: set[str] = set()
    for rid in incoming:
        if rid in regions and rid not in _seen:
            _seen.add(rid)
            explored.append(
                {
                    "id": rid,
                    "name": regions[rid].name,
                    # Node-graph edges: the region's adjacency list. MapOverlay
                    # draws one edge per connection; region-mode worlds author
                    # adjacency as ``adjacent`` in cartography.yaml, so without
                    # this the explored payload arrived with no ``connections``
                    # field — isolated nodes at best, a GameBoard crash at worst
                    # (ui #330 unguarded ``for…of``). (server #632)
                    "connections": list(getattr(regions[rid], "adjacent", [])),
                }
            )

    with cartography_map_emitted_span(
        current_location=current_location,
        world_slug=world_slug,
        visited_count=len(explored),
        discovered_total=len(incoming),
        dropped_count=len(incoming) - len(explored),
        # Disclosure accounting (fog mode): the GM panel must be able to see
        # how much of the catalog reached the wire vs stayed hidden.
        discovery_mode=discovery_mode,
        regions_shipped=len(region_dict),
        regions_total=len(regions),
    ):
        return CartographyMapMessage(
            payload=CartographyMapPayload(
                current_location=current_location,
                region=world_slug,
                explored=explored,
                cartography={
                    "navigation_mode": str(nav_mode) if nav_mode else "region",
                    "starting_region": getattr(cart, "starting_region", ""),
                    "regions": region_dict,
                    "routes": routes_list,
                    # Story 104-1 / M-A: the loader-cached multi-system flag,
                    # supersedes the UI's regionCount>1 heuristic (M-B). Always a
                    # concrete bool on the wire (never absent/None — the UI reads
                    # it unconditionally).
                    "is_cluster": bool(getattr(world, "is_cluster", False)),
                },
            ),
            player_id=player_id,
        )


def _sfx_ids_from_genre(genre_pack: GenrePack) -> list[str]:
    """Extract SFX IDs from genre audio config."""
    if genre_pack.audio is None:
        return []
    sfx_lib = getattr(genre_pack.audio, "sfx_library", None)
    if not sfx_lib:
        return []
    if isinstance(sfx_lib, list):
        return [str(getattr(s, "id", s)) for s in sfx_lib]
    return []


def _render_url_from_path(image_path: str) -> str:
    """Translate a daemon filesystem path into a /renders/* URL.

    Returns the absolute path verbatim when it isn't inside
    SIDEQUEST_OUTPUT_DIR — UI 404 beats silent replacement. Each
    fallthrough emits an ``image_unavailable`` watcher event so the GM
    panel surfaces "image generated but not deliverable" rather than
    looking like nothing happened (CLAUDE.md OTEL principle).
    """
    import os as _os
    import pathlib as _pathlib

    root = _os.environ.get("SIDEQUEST_OUTPUT_DIR")
    if not root or not image_path:
        _publish_image_unavailable(image_path, reason="output_dir_unset")
        return image_path
    try:
        rel = _pathlib.Path(image_path).resolve().relative_to(_pathlib.Path(root).resolve())
    except ValueError:
        _publish_image_unavailable(image_path, reason="path_outside_output_dir")
        return image_path
    return "/renders/" + str(rel).replace(_os.sep, "/")


def _publish_image_unavailable(image_path: str, *, reason: str) -> None:
    """Emit a watcher event for an unrewriteable render path. Lazy import
    avoids a server↔telemetry import cycle at module load."""
    try:
        from sidequest.telemetry.watcher_hub import publish_event
    except ImportError:
        return
    publish_event(
        "image_unavailable",
        {"image_path": image_path, "reason": reason},
        component="render",
        severity="warning",
    )


def _detect_missed_recurring_npcs(
    *,
    snapshot: GameSnapshot,
    narration_text: str,
    emitted_mentions: list[NpcMention],
    turn_num: int,
) -> None:
    """Story 45-53: emit a warning span for every known recurring NPC whose
    name appears in ``narration_text`` but is missing from
    ``emitted_mentions``.

    Known recurring NPCs are names found in ``snapshot.npcs`` (stateful) or
    ``snapshot.npc_pool``. PC names are filtered out. Match is
    word-boundary case-insensitive on the name. When a name lives in both
    ``npcs`` and ``npc_pool``, ``npcs`` wins (single span,
    ``source="npcs"``).

    Side-effect only: emits ``SPAN_NPC_RECURRING_PRESENCE_MISSED`` and a
    ``logger.warning`` per miss. No exception is raised — the runtime
    pattern is "subsystem emits span; GM panel surfaces; human notices"
    (CLAUDE.md OTEL Observability Principle).
    """
    if not narration_text:
        return

    pc_names = _pc_name_skip_set(snapshot)

    # Build the emitted-name set (case-folded). Narrator emission, even
    # bare, suppresses the miss warning.
    emitted_names = {m.name.casefold() for m in emitted_mentions if m.name}

    # Build candidate map: case-folded name → (canonical_name, source, last_seen_turn).
    # ``npcs`` wins on conflict — pool entries with the same name are
    # shadowed (parallel to ``_apply_npc_mentions``).
    candidates: dict[str, tuple[str, str, int]] = {}
    for member in snapshot.npc_pool:
        if not member.name:
            continue
        key = member.name.casefold()
        if key in pc_names:
            continue
        candidates[key] = (member.name, "npc_pool", 0)
    for npc in snapshot.npcs:
        name = npc.core.name
        if not name:
            continue
        key = name.casefold()
        if key in pc_names:
            continue
        candidates[key] = (name, "npcs", npc.last_seen_turn)

    if not candidates:
        return

    folded_text = narration_text.casefold()
    for key, (canonical_name, source, last_seen_turn) in candidates.items():
        if key in emitted_names:
            continue
        # Word-boundary match on case-folded prose. ``re.escape`` guards
        # against names containing regex metacharacters.
        if not re.search(rf"\b{re.escape(key)}\b", folded_text):
            continue
        with npc_recurring_presence_missed_span(
            npc_name=canonical_name,
            source=source,
            turn_number=turn_num,
            last_seen_turn=last_seen_turn,
        ):
            logger.warning(
                "npc.recurring_presence_missed name=%r source=%s turn=%d "
                "last_seen_turn=%d — narration named the NPC but npcs_present omitted them",
                canonical_name,
                source,
                turn_num,
                last_seen_turn,
            )


# Story 49-2: prose-only auto-mint vocabulary.
#
# Bare-role tokens — the token IS the public name when minting. The narrator
# uses these as quasi-proper-nouns in dense prose ("Father lies pale", "the
# wee one's mother kneels").
_BARE_ROLE_PUBLIC_NAMES: dict[str, str] = {
    "father": "Father",
    "mother": "Mother",
    "son": "Son",
    "daughter": "Daughter",
    "brother": "Brother",
    "sister": "Sister",
}

# Article+role tokens — public name preserves the article form so the GM
# panel surfaces them as ``the doctor`` rather than just ``doctor``.
_ARTICLE_ROLE_PUBLIC_NAMES: dict[str, str] = {
    "doctor": "the doctor",
    "reverend": "the Reverend",
    "constable": "the constable",
    "priest": "the priest",
    "physician": "the physician",
    "midwife": "the midwife",
    "innkeeper": "the innkeeper",
    "magistrate": "the magistrate",
}

# Honorific patterns — ``Mrs. <Name>``, ``Mr. <Name>``, ``Dr. <Name>``,
# ``Reverend <Name>``, ``Father <Name>``, ``Mother <Name>``, ``Captain
# <Name>``, ``Sergeant <Name>``, ``Sir <Name>``, ``Lady <Name>``, ``Lord
# <Name>``, ``Dame <Name>``. The proper name must be Capitalized
# (``[A-Z][a-z]+``) so common mid-sentence words don't false-match.
#
# NOTE: ``Father``/``Mother``/``Reverend`` overlap with bare-role tokens
# in ``_BARE_ROLE_PUBLIC_NAMES`` / ``_ARTICLE_ROLE_PUBLIC_NAMES``. The
# ``consumed_spans`` guard in Phase 2 of ``_auto_mint_prose_only_npcs``
# prevents the bare-role scan from re-firing on positions already
# matched by the honorific pattern. Without that guard, "Reverend
# Murchison" would mint twice — once as the full honorific name, once
# as the bare role.
_HONORIFIC_PROPER_RE = re.compile(
    r"\b(Mrs|Mr|Dr|Reverend|Father|Mother|Captain|Sergeant|Sir|Lady|Lord|Dame)\.?\s+([A-Z][a-z]+)\b"
)

# Honorifics that take a trailing period in English style. Knighthoods
# and ecclesiastical titles spelled out ("Sir Iain", "Lady Annabel",
# "Reverend Lachlan", "Father Tomas") do NOT take a period; abbreviated
# titles ("Mrs. Gow", "Mr. Hodge", "Dr. Sallow") do. Glenross 2026-05-12
# playtest surfaced the bug as "Sir. Iain" appearing in npc.auto_mint
# spans — the construction ``f"{title}. {proper}"`` was unconditional.
_PERIOD_BEARING_HONORIFICS: frozenset[str] = frozenset({"Mrs", "Mr", "Dr"})

# Subject pronouns by gender group — the disambiguator. Object pronouns
# (him, her, them) and possessive pronouns (his, hers, their) are
# intentionally NOT in this map; in dense prose those refer to
# surrounding NPCs as often as to the role-mentioned one, and including
# them mis-genders too freely (Glenross 2026-05-11 "Mrs. Gow laid him
# after" — ``him`` is Father, not Mrs. Gow).
_SUBJECT_PRONOUN_GROUPS: dict[str, tuple[str, ...]] = {
    "he/him": ("he",),
    "she/her": ("she",),
    "they/them": ("they",),
    "it/its": ("it",),
}

# Forward-only window after a role mention. Tuned so a one-clause-later
# pronoun resolves cleanly while not reaching across a paragraph break to
# steal a pronoun that belongs to a different antecedent.
_AUTO_MINT_FORWARD_WINDOW = 50

# Gender-paired roles — if one is in the roster (in any source), don't
# auto-mint the other from prose. Defensive against the Glenross 2026-05-11
# pattern: turn 5 narrator referenced Father in prose; turn 6 narrator
# slipped and wrote "mother" with no name. Without this rule the
# auto-minter would canonize the slip as a separate NPC. Limitation: in
# legitimate scenes with BOTH parents named without proper names, the
# second-listed bare role won't auto-mint (the narrator must emit it in
# ``npcs_present`` or use a proper name).
_GENDER_PAIRED_ROLES: dict[str, str] = {
    "father": "mother",
    "mother": "father",
    "brother": "sister",
    "sister": "brother",
    "son": "daughter",
    "daughter": "son",
}

# Bare-role token regexes — compiled once at module load. Auto-minter
# runs on every narration turn; recompiling these 14 patterns per call
# is wasted work. Keyed by the same role token used in
# ``_BARE_ROLE_PUBLIC_NAMES`` / ``_ARTICLE_ROLE_PUBLIC_NAMES``.
_BARE_ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    role_token: re.compile(rf"\b{re.escape(role_token)}\b", re.IGNORECASE)
    for role_token in (*_BARE_ROLE_PUBLIC_NAMES, *_ARTICLE_ROLE_PUBLIC_NAMES)
}

# Subject-pronoun token regexes — compiled once at module load
# (parallel to ``_BARE_ROLE_PATTERNS``). ``_infer_pronouns_from_role_
# context`` runs once per role/honorific mention per turn, so any
# regex-recompilation in that hot path is wasted work.
_SUBJECT_PRONOUN_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    group: tuple(re.compile(rf"\b{re.escape(tok)}\b") for tok in tokens)
    for group, tokens in _SUBJECT_PRONOUN_GROUPS.items()
}

# Skip-reason taxonomy for the auto-minter. Each constant matches the
# ``reason`` attribute on ``SPAN_NPC_AUTO_MINT_SKIPPED`` and lets the GM
# panel filter by skip cause. Extracted from inline string literals
# during verify-round-2 simplify so future skip reasons (e.g.
# ``"pc_role_collision"``) get a single grep target instead of being
# scattered across emit sites.
_SKIP_REASON_PRONOUNS_HONORIFIC = "ambiguous_pronouns_honorific"
_SKIP_REASON_PRONOUNS_ROLE = "ambiguous_pronouns_role"
_SKIP_REASON_GENDER_PAIRED = "gender_paired_conflict"

# Gendered-token pronoun fallback (sq-playtest 2026-06-07 purge/mint
# deadlock): when the forward-window pronoun scan is ambiguous but the
# honorific or bare-role token ITSELF declares gender, use the token's
# pronouns instead of skipping. "Mother Demus" was named in narration four
# consecutive turns (five_points-4 t28-31) while the minter skipped her
# every turn on ``ambiguous_pronouns_honorific`` — composing with the
# observation gate's purge into a deadlock where the NPC existed in prose
# and nowhere in state. Reading "Mother"/"Mrs."/"Sir" as a pronoun source
# is not guessing (the AC2 prohibition); it is reading what the narrator
# wrote. Neutral titles (Dr, Reverend, Captain, Sergeant) and article
# roles (the doctor, ...) stay window-only and still skip on ambiguity.
_HONORIFIC_PRONOUN_FALLBACK: dict[str, str] = {
    "Mrs": "she/her",
    "Lady": "she/her",
    "Dame": "she/her",
    "Mother": "she/her",
    "Mr": "he/him",
    "Sir": "he/him",
    "Lord": "he/him",
    "Father": "he/him",
}
_ROLE_PRONOUN_FALLBACK: dict[str, str] = {
    "mother": "she/her",
    "sister": "she/her",
    "daughter": "she/her",
    "father": "he/him",
    "brother": "he/him",
    "son": "he/him",
}


def _emit_auto_mint_skip(
    *,
    public_name: str,
    role: str,
    reason: str,
    turn_num: int,
    **extra_attrs: object,
) -> None:
    """Emit ``SPAN_NPC_AUTO_MINT_SKIPPED`` + a matching ``logger.warning``.

    Centralizes the three skip emit sites in ``_auto_mint_prose_only_npcs``
    (honorific-ambiguous, role-ambiguous, gender-paired-conflict). The
    ``reason`` value (one of the ``_SKIP_REASON_*`` constants) drives
    both the span attribute and the human-readable log explanation.
    ``extra_attrs`` flows into the span (currently used for
    ``paired_role`` on the gender-paired path).
    """
    if reason == _SKIP_REASON_GENDER_PAIRED:
        explanation = (
            "gender-paired role conflict; narrator may have slipped between turns; not canonizing"
        )
    else:
        explanation = (
            "pronouns ambiguous in local window (no clean subject pronoun); "
            "skipping mint rather than guessing"
        )
    with npc_auto_mint_skipped_span(
        npc_name=public_name,
        role=role,
        reason=reason,
        turn_number=turn_num,
        # Story 61-8 §D6 (review-fix round 2): see twin pragma above
        # on ``prompt_game_state_bytes_span``. Same ``reportArgumentType``
        # mechanism: ``**dict[str, object]`` is not statically
        # compatible with the span's typed keyword parameters; at
        # runtime ``extra_attrs`` is fed by the caller above with
        # explicit telemetry-attribute keys.
        **extra_attrs,  # pyright: ignore[reportArgumentType]
    ):
        logger.warning(
            "npc.auto_mint_skipped name=%r role=%r turn=%d reason=%s — %s",
            public_name,
            role,
            turn_num,
            reason,
            explanation,
        )


def _pc_name_skip_set(snapshot: GameSnapshot) -> set[str]:
    """Return the case-folded set of PC names — the always-deny list for
    NPC promotion. Shared by ``_detect_missed_recurring_npcs`` and
    ``_auto_mint_prose_only_npcs`` (and any future NPC-detector
    sibling). The MP joiner-orientation auto-narration playtest
    2026-04-29 demonstrated why both detectors need an identical filter:
    a narrator naming a PC must NEVER promote them into the NPC stores.
    """
    return {
        c.core.name.casefold()
        for c in snapshot.characters
        if getattr(getattr(c, "core", None), "name", None)
    }


def _infer_pronouns_from_role_context(narration_text: str, role_end: int) -> str | None:
    """Return the pronoun group inferred from the local prose window after
    a role mention, or ``None`` if pronouns are ambiguous.

    Story 49-2 — pronoun inference for prose-only auto-mint. AC2 forbids
    guessing: when the window contains zero subject pronouns, or subject
    pronouns from two distinct gender groups, the caller must skip the
    mint (warn + no span).

    Forward-only window: pronouns BEFORE the role mention often refer to
    different antecedents (the prior subject of the paragraph), so the
    scanner restricts itself to text after ``role_end``. Object/possessive
    pronouns are deliberately ignored — in dense prose ``him`` can refer
    to a different on-scene actor than the role-mentioned one (the
    Glenross 2026-05-11 ``Mrs. Gow laid him after`` where ``him`` is
    Father, not Mrs. Gow).
    """
    hi = min(len(narration_text), role_end + _AUTO_MINT_FORWARD_WINDOW)
    window = narration_text[role_end:hi].casefold()
    seen_groups: list[str] = []
    for group, patterns in _SUBJECT_PRONOUN_PATTERNS.items():
        if any(p.search(window) for p in patterns):
            seen_groups.append(group)
    if len(seen_groups) == 1:
        return seen_groups[0]
    # Zero pronouns → no signal. Multiple genders → ambiguous. AC2: skip.
    return None


def _auto_mint_prose_only_npcs(
    *,
    snapshot: GameSnapshot,
    narration_text: str,
    emitted_mentions: list[NpcMention],
    turn_num: int,
) -> None:
    """Story 49-2: server-side catch-loop for NPCs the narrator named in
    prose but omitted from ``npcs_present``.

    Sibling to ``_detect_missed_recurring_npcs`` (which warns about
    KNOWN names that got skipped). This function handles the FIRST-mention
    path — role-named or honorific-named individuals not yet in any store
    (``snapshot.npcs``, ``snapshot.npc_pool``, ``emitted_mentions``).

    Detection paths:
      1. **Honorifics** (Mrs. <Name>, Mr. <Name>, Dr. <Name>, Reverend
         <Name>, Father <Name>, Mother <Name>, Captain <Name>, Sergeant
         <Name>, Sir <Name>, Lady <Name>, Lord <Name>) — capture the
         full ``Title. Proper`` form as the public name. Father/Mother/
         Reverend overlap with bare-role tokens; the consumed-span
         tracker prevents Phase 2 from double-processing those positions.
      2. **Bare roles** (Father, mother, the doctor, the Reverend, ...) —
         the token IS the public name (with article for ``the doctor``-
         style cases).

    Pronoun inference (AC2): forward-window subject-pronoun scan via
    ``_infer_pronouns_from_role_context``. Ambiguous → warn + skip; never
    guess. Side-effect only: appends to ``snapshot.npc_pool`` and emits
    ``SPAN_NPC_AUTO_MINTED_FROM_PROSE`` per mint (CLAUDE.md OTEL
    Observability Principle — the GM panel must see what got minted).

    Gender-paired role guard: if a mention's role has a paired-opposite
    role already in the roster (mother↔father, brother↔sister,
    son↔daughter), skip the mint. Prevents the auto-minter from
    canonizing a narrator gender-flip slip (Glenross 2026-05-11 turn 6).
    """
    if not narration_text:
        return

    pc_names = _pc_name_skip_set(snapshot)

    # Known-name and known-role skip sets, seeded from existing stores and
    # the narrator's structured emission this turn.
    known_names: set[str] = set()
    known_roles: set[str] = set()
    for m in emitted_mentions:
        if m.name:
            known_names.add(m.name.casefold())
        if m.role:
            known_roles.add(m.role.casefold())
    for npc in snapshot.npcs:
        if npc.core.name:
            known_names.add(npc.core.name.casefold())
    for member in snapshot.npc_pool:
        if member.name:
            known_names.add(member.name.casefold())
        if member.role:
            known_roles.add(member.role.casefold())

    # Track positions matched by the honorific scan so the bare-role scan
    # doesn't double-process them ("Reverend Murchison" → honorific match;
    # the bare ``Reverend`` inside it must not also fire).
    consumed_spans: list[tuple[int, int]] = []

    def _mint(
        *,
        public_name: str,
        role_token: str,
        pronouns: str,
        pronoun_source: str = "window_inference",
    ) -> None:
        snapshot.npc_pool.append(
            NpcPoolMember(
                name=public_name,
                role=role_token or None,
                pronouns=pronouns,
                drawn_from="dialogue_extraction",
                observation_pending=True,
            )
        )
        known_names.add(public_name.casefold())
        if role_token:
            known_roles.add(role_token.casefold())
        with npc_auto_minted_from_prose_span(
            npc_name=public_name,
            role=role_token,
            pronouns=pronouns,
            source="dialogue_extraction",
            turn_number=turn_num,
            pronoun_source=pronoun_source,
        ):
            logger.info(
                "npc.auto_minted_from_prose name=%r role=%r pronouns=%r "
                "source=dialogue_extraction pronoun_source=%s turn=%d",
                public_name,
                role_token,
                pronouns,
                pronoun_source,
                turn_num,
            )

    # Phase 1 — honorific + proper-name (Mrs. Gow, Mr. Hodge, Dr. Sallow).
    # Each match is at most one mint; same honorific with the same proper
    # name appearing twice in a turn does not double-mint (dedup by name).
    for hm in _HONORIFIC_PROPER_RE.finditer(narration_text):
        start, end = hm.span()
        title = hm.group(1)
        proper = hm.group(2)
        if title in _PERIOD_BEARING_HONORIFICS:
            public_name = f"{title}. {proper}"
        else:
            public_name = f"{title} {proper}"
        cf_name = public_name.casefold()
        # Always mark the span as consumed so the bare-role scan skips it.
        consumed_spans.append((start, end))
        if cf_name in pc_names or cf_name in known_names:
            continue
        pronouns = _infer_pronouns_from_role_context(narration_text, end)
        pronoun_source = "window_inference"
        if pronouns is None:
            # Gendered honorific fallback — the title itself declares the
            # pronouns ("Mother Demus" → she/her). See
            # ``_HONORIFIC_PRONOUN_FALLBACK`` for the deadlock this breaks.
            pronouns = _HONORIFIC_PRONOUN_FALLBACK.get(title)
            pronoun_source = "honorific_fallback"
        if pronouns is None:
            _emit_auto_mint_skip(
                public_name=public_name,
                role="",
                reason=_SKIP_REASON_PRONOUNS_HONORIFIC,
                turn_num=turn_num,
            )
            continue
        # Honorifics carry no canonical role tag (Mrs./Mr./Dr. are titles,
        # not roles). Role is None — narrator may refine via a later
        # structured patch.
        _mint(
            public_name=public_name,
            role_token="",
            pronouns=pronouns,
            pronoun_source=pronoun_source,
        )

    # Phase 2 — bare role tokens (Father, mother, the doctor, ...). Process
    # each role at most once per turn; first matching occurrence wins.
    # Patterns are pre-compiled at module load (``_BARE_ROLE_PATTERNS``).
    all_role_names: dict[str, str] = {
        **_BARE_ROLE_PUBLIC_NAMES,
        **_ARTICLE_ROLE_PUBLIC_NAMES,
    }
    for role_token, public_name in all_role_names.items():
        pattern = _BARE_ROLE_PATTERNS[role_token]
        match = None
        for candidate in pattern.finditer(narration_text):
            c_start, c_end = candidate.span()
            # Skip occurrences inside an honorific consumed-span (e.g.
            # ``Reverend`` inside ``Reverend Murchison``).
            if any(s <= c_start < e for s, e in consumed_spans):
                continue
            match = candidate
            break
        if match is None:
            continue

        cf_name = public_name.casefold()
        cf_role = role_token.casefold()

        # Dedup checks.
        if cf_name in pc_names or cf_role in pc_names:
            continue
        if cf_name in known_names or cf_role in known_roles:
            continue

        # Gender-paired role conflict — refuse to canonize a slip.
        paired = _GENDER_PAIRED_ROLES.get(cf_role)
        if paired and paired in known_roles:
            _emit_auto_mint_skip(
                public_name=public_name,
                role=role_token,
                reason=_SKIP_REASON_GENDER_PAIRED,
                turn_num=turn_num,
                paired_role=paired,
            )
            continue

        pronouns = _infer_pronouns_from_role_context(narration_text, match.end())
        pronoun_source = "window_inference"
        if pronouns is None:
            # Gendered bare-role fallback — "Father"/"Son" declare their own
            # pronouns (barsoom-4 t6-9: "Father" skipped 4 consecutive turns
            # on window ambiguity). Article roles (the doctor, ...) are not
            # in the map and still skip.
            pronouns = _ROLE_PRONOUN_FALLBACK.get(cf_role)
            pronoun_source = "role_fallback"
        if pronouns is None:
            _emit_auto_mint_skip(
                public_name=public_name,
                role=role_token,
                reason=_SKIP_REASON_PRONOUNS_ROLE,
                turn_num=turn_num,
            )
            continue

        _mint(
            public_name=public_name,
            role_token=role_token,
            pronouns=pronouns,
            pronoun_source=pronoun_source,
        )


def _apply_npc_observation_gate(
    *,
    snapshot: GameSnapshot,
    emitted_mentions: list[NpcMention],
    turn_num: int,
    narration_text: str = "",
) -> None:
    """Story 49-6: ratification gate for prose-mint NPCs.

    Sibling to ``_auto_mint_prose_only_npcs``. The mint loop appends a
    ``NpcPoolMember(observation_pending=True)`` whenever the narrator
    role-names or honorific-names a person in prose but omits them
    from ``npcs_present``. That mint is fire-and-forget — if the
    narrator never re-cites the name, the pool fills with phantom
    NPCs the playgroup never actually encountered (the failure mode
    behind the 2026-05-11 Glenross turn-6 invented-Mother slip).

    This gate runs once at the start of every narration-apply turn,
    BEFORE the auto-minter scans this turn's prose. For each member
    with ``observation_pending=True``, it checks whether the
    member's name (case-folded) OR role (case-folded) appears in
    ``emitted_mentions`` — the narrator's structured ``npcs_present``
    patch for THIS turn. Two outcomes:

    - **Promote.** Name or role matched. Flip the flag to ``False``
      (member is now ratified — treat as canonical pool entry). Emit
      ``npc.observation_gate_promoted``.
    - **Purge.** No match. Remove the entry from ``snapshot.npc_pool``.
      Emit ``npc.observation_gate_purged`` at severity=warning so the
      GM panel renders the drop as a soft alert.

    A re-citation in THIS turn's **prose** also ratifies (sq-playtest
    2026-06-07 purge/mint deadlock, five_points-4 t28-31): the narrator
    kept naming "Mother Demus"/"Son" in narration while omitting them
    from ``npcs_present``, so the gate purged them every turn while the
    minter re-skipped them — the NPC existed in prose and nowhere in
    state for four consecutive turns. Prose recurrence is exactly the
    observation this gate exists to detect (it is the same signal
    ``_detect_missed_recurring_npcs`` warns about); a word-boundary
    name match in ``narration_text`` promotes with
    ``ratified_by="prose"`` instead of purging.

    Pipeline ordering is load-bearing: the gate examines pending
    members from PRIOR turns against THIS turn's mentions. Running
    after the auto-minter would self-cancel — this turn's mints
    would be evaluated against this turn's own (omitting) mentions
    and immediately purged.

    Non-pending members (``observation_pending=False`` — world-authored,
    name-generator, legacy, or already-ratified) are not touched.
    Durable retention (memory note feedback_durable_retention): pool
    members that survive ratification are forever.
    """
    if not snapshot.npc_pool:
        return

    mention_names: set[str] = set()
    mention_roles: set[str] = set()
    for m in emitted_mentions:
        if m.name:
            mention_names.add(m.name.casefold())
        if m.role:
            mention_roles.add(m.role.casefold())

    folded_text = narration_text.casefold() if narration_text else ""

    survivors: list[NpcPoolMember] = []
    for member in snapshot.npc_pool:
        if not member.observation_pending:
            survivors.append(member)
            continue

        cf_name = member.name.casefold() if member.name else ""
        cf_role = (member.role or "").casefold()
        matched = (cf_name and cf_name in mention_names) or (cf_role and cf_role in mention_roles)
        ratified_by = "structured_mention"
        # Prose re-citation ratifies too — see docstring (2026-06-07
        # purge/mint deadlock). Word-boundary match, same idiom as
        # ``_detect_missed_recurring_npcs``.
        if (
            not matched
            and cf_name
            and folded_text
            and re.search(rf"\b{re.escape(cf_name)}\b", folded_text)
        ):
            matched = True
            ratified_by = "prose"

        if matched:
            member.observation_pending = False
            survivors.append(member)
            with npc_observation_gate_promoted_span(
                npc_name=member.name,
                role=member.role or "",
                turn_number=turn_num,
                ratified_by=ratified_by,
            ):
                logger.info(
                    "npc.observation_gate_promoted name=%r role=%r turn=%d ratified_by=%s",
                    member.name,
                    member.role,
                    turn_num,
                    ratified_by,
                )
        else:
            with npc_observation_gate_purged_span(
                npc_name=member.name,
                role=member.role or "",
                turn_number=turn_num,
            ):
                logger.warning(
                    "npc.observation_gate_purged name=%r role=%r turn=%d",
                    member.name,
                    member.role,
                    turn_num,
                )

    snapshot.npc_pool[:] = survivors


def _detect_npc_identity_drift(
    *,
    existing_name: str,
    existing_role: str | None,
    existing_pronouns: str | None,
    mention: NpcMention,
    turn_num: int,
    applied: bool = False,
) -> None:
    """Emit a drift span when a narrator NPC mention disagrees with canonical.

    Story 37-44. Empty fields on the mention = "no opinion"; only explicit
    disagreement triggers. Side-effect only (logger.warning + watcher).

    Wave 2A (story 45-47): refactored to take primitive fields rather than
    a typed registry entry, since callers may now hold either an ``Npc``
    or an ``NpcPoolMember``.

    Story 72-7: drift is now authoritative at the pool-hit upsert site. The
    ``applied`` flag records whether the disagreeing value is being written
    onto the canonical entry (True) or merely observed (False — e.g. a
    human-authored ``world_authored`` member the narrator must not overwrite,
    or the warn-only ``npcs_hit`` path). The marker rides the span so the GM
    panel can tell "the record moved" from "a mismatch was noticed".
    """
    for field, m_val, e_val in (
        ("pronouns", mention.pronouns, existing_pronouns),
        ("role", mention.role, existing_role),
    ):
        if m_val and e_val and m_val.strip().lower() != e_val.strip().lower():
            # Span emission replaces the prior direct ``_watcher_publish`` —
            # ``WatcherSpanProcessor`` re-emits via
            # ``SPAN_ROUTES[SPAN_NPC_REINVENTED]`` and propagates the
            # ``severity="warning"`` attribute set by the helper.
            with npc_reinvented_span(
                npc_name=existing_name,
                drift_field=field,
                expected=e_val,
                narrator=m_val,
                turn_number=turn_num,
                applied=applied,
            ):
                logger.warning(
                    "npc.reinvented name=%r field=%s expected=%r narrator=%r applied=%s turn=%d",
                    existing_name,
                    field,
                    e_val,
                    m_val,
                    applied,
                    turn_num,
                )
