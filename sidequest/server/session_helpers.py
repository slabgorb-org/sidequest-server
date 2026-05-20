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

from sidequest.agents.orchestrator import (
    RECENT_NARRATIVE_WINDOW_K,
    NpcMention,
    TurnContext,
)
from sidequest.game.builder import humanize_snake_case
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.session import (
    GameSnapshot,
    PartyPeer,
)
from sidequest.game.shared_world_delta import (
    build_shared_world_delta,
    merge_shared_delta_into_snapshot,
)
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.protocol.messages import (
    ErrorMessage,
    ErrorPayload,
    PlayerPresenceMessage,
    PlayerPresencePayload,
)
from sidequest.protocol.types import NonBlankString
from sidequest.telemetry.spans import (
    npc_auto_mint_skipped_span,
    npc_auto_minted_from_prose_span,
    npc_observation_gate_promoted_span,
    npc_observation_gate_purged_span,
    npc_recurring_presence_missed_span,
    npc_reinvented_span,
    orchestrator_notorious_party_gate_span,
    room_state_injected_span,
)

if TYPE_CHECKING:
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.session_room import SessionRoom

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
        for slot, pid in seat_map.items():
            if pid == sd.player_id and any(c.core.name == slot for c in snapshot.characters):
                return slot
    for char in snapshot.characters:
        if char.core.name == sd.player_name:
            return char.core.name
    return snapshot.characters[0].core.name


def _project_current_region(sd: _SessionData, snapshot: GameSnapshot) -> object | None:
    """Beneath Sünden BETTER fix (seam 1+2) — per-turn region projection.

    Re-derive the party's current region from the live ``DungeonStore``
    (SQLite is the single source of truth — never mirrored onto the
    persisted snapshot, which has a documented divergence disease). The
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
            span.set_attribute("outcome", "no_dungeon")
            span.set_attribute(
                "reason",
                f"other_world genre={sd.genre_slug!r} world={sd.world_slug!r}",
            )
            return None

        from sidequest.dungeon.persistence import DatabaseError, DungeonStore
        from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
        from sidequest.dungeon.themes import load_theme_palette
        from sidequest.genre.loader import (
            DEFAULT_GENRE_PACK_SEARCH_PATHS,
            GenreLoader,
        )

        store = DungeonStore(sd.store.connection())
        try:
            graph = store.load_map(entrance_id=ENTRANCE_ID)
        except DatabaseError as exc:
            span.set_attribute("outcome", "no_dungeon")
            span.set_attribute("reason", f"no_dungeon_schema: {exc}")
            logger.warning(
                "dungeon.region_projection no dungeon schema "
                "(genre=%s world=%s): %s",
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
            # Two faces of ONE disease: a fully materialized dungeon whose
            # current_region is not a real graph node — either blank (the
            # #314 entrance-bind seam never fired; OQ-1's 2026-05-17
            # verification proved a slug_resume connect returns before
            # connect.py's attach call site, so a RESUMED beneath_sunden
            # save has a materialized dungeon but a blank current_region
            # forever) OR a PHANTOM (narration title-parsing wrote a prose
            # name like 'ropefoot' into current_region — not a node id;
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
    lore_context: str | None = None,
    room: SessionRoom | None = None,
) -> TurnContext:
    """Assemble :class:`TurnContext` for one narration turn (Slice H).

    ``opening_directive`` is consumed turn 0 only (caller clears the
    session field). ``lore_context`` is the pre-rendered <lore> block.
    ``room`` provides the seat map so MP can identify the acting PC by
    player_id rather than guessing snapshot.characters[0].
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
    seat_map: dict[str, str] = seat_lookup_fn() if callable(seat_lookup_fn) else {}
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
    state_summary_payload = json.loads(snapshot.model_dump_json())
    # Story 49-1 — drop narrative_log from the Valley-zone state_summary
    # JSON dump. The last K=2 entries (57-1) now ride into the narrator prompt
    # via the Recency-zone ``recent_narrative_context`` section (see
    # orchestrator.build_narrator_prompt). Keeping the duplicate here
    # would put the same prose in two zones — high-attention Recency
    # AND decayed Valley — re-creating the attention-decay disease this
    # story exists to cure.
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
    # (``snapshot.party_location(perspective=char_name)``) — there is
    # no party-level snapshot.location anymore. The retrieved-container
    # payload also flows into ``state_summary_payload`` automatically
    # because line 312 already serializes the full ``snapshot`` (which
    # includes ``room_states``) into the narrator's <game_state> block.
    current_room_id = snapshot.party_location(perspective=char_name)
    if not current_room_id:
        # No silent fallback (CLAUDE.md): a turn without a canonical
        # location renders the room-state gate unreachable — the span
        # would fire with ``room_id=""`` and look indistinguishable from
        # a valid empty room. Log a warning so the GM panel can spot
        # the configuration gap. The span still fires below (so
        # Sebastien's lie-detector keeps its no-op case) but with
        # ``room_id=""`` AND a logged warning.
        logger.warning(
            "state.room_state_injected_unreachable reason=actor_location_empty interaction=%d",
            snapshot.turn_manager.interaction,
        )
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

    state_summary_json = json.dumps(state_summary_payload, indent=2)

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

    return TurnContext(
        in_combat=in_combat,
        in_chase=in_chase,
        in_encounter=in_encounter,
        encounter=encounter if in_encounter else None,
        confrontation_def=confrontation_def,
        available_confrontations=available_confrontations,
        encounter_summary=encounter_summary,
        state_summary=state_summary_json,
        narrator_verbosity="standard",
        narrator_vocabulary="literary",
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
        store=sd.store,
        lore_store=sd.lore_store,
        monster_manual=sd.monster_manual,
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
        party_peers=party_peers,
        opening_directive=opening_directive,
        world_context=sd.world_context,
        lore_context=lore_context,
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
        # Source the recency window from the durable narrative_log (SQLite)
        # rather than the in-memory snapshot mirror. sd.store.append_narrative
        # only writes to SQLite — the in-memory snapshot.narrative_log is
        # populated *only* by world_materialization (one-shot at startup) and
        # lore_seeding (chargen), never by the per-turn narrator append site
        # at websocket_session_handler.py:2832/2840. Reading from snapshot
        # made the recency injection emit turn_count=0/total_tokens=0 forever
        # (sq-playtest 2026-05-15 — story 49-1's safety-net was dormant).
        # SQLite is the same ground-truth source Story 45-11's round
        # invariant lie-detector relies on; aligning here closes the same
        # snapshot/SQLite divergence class.
        recent_narrative_log=sd.store.recent_narrative(RECENT_NARRATIVE_WINDOW_K),
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
        **extra_attrs,
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
        ):
            logger.info(
                "npc.auto_minted_from_prose name=%r role=%r pronouns=%r "
                "source=dialogue_extraction turn=%d",
                public_name,
                role_token,
                pronouns,
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
        _mint(public_name=public_name, role_token="", pronouns=pronouns)

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
        if pronouns is None:
            _emit_auto_mint_skip(
                public_name=public_name,
                role=role_token,
                reason=_SKIP_REASON_PRONOUNS_ROLE,
                turn_num=turn_num,
            )
            continue

        _mint(public_name=public_name, role_token=role_token, pronouns=pronouns)


def _apply_npc_observation_gate(
    *,
    snapshot: GameSnapshot,
    emitted_mentions: list[NpcMention],
    turn_num: int,
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

    survivors: list[NpcPoolMember] = []
    for member in snapshot.npc_pool:
        if not member.observation_pending:
            survivors.append(member)
            continue

        cf_name = member.name.casefold() if member.name else ""
        cf_role = (member.role or "").casefold()
        matched = (cf_name and cf_name in mention_names) or (cf_role and cf_role in mention_roles)

        if matched:
            member.observation_pending = False
            survivors.append(member)
            with npc_observation_gate_promoted_span(
                npc_name=member.name,
                role=member.role or "",
                turn_number=turn_num,
            ):
                logger.info(
                    "npc.observation_gate_promoted name=%r role=%r turn=%d",
                    member.name,
                    member.role,
                    turn_num,
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
) -> None:
    """Warn when narrator NPC mention disagrees with the canonical entry.

    Story 37-44. Empty fields on the mention = "no opinion"; only explicit
    disagreement triggers. Side-effect only (logger.warning + watcher).

    Wave 2A (story 45-47): refactored to take primitive fields rather than
    a typed registry entry, since callers may now hold either an ``Npc``
    or an ``NpcPoolMember``.
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
            ):
                logger.warning(
                    "npc.reinvented name=%r field=%s expected=%r narrator=%r turn=%d",
                    existing_name,
                    field,
                    e_val,
                    m_val,
                    turn_num,
                )
