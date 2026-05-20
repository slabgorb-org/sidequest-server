"""Per-connection WebSocket session handler.

Extracted from ``session_handler.py``: the ``WebSocketSessionHandler`` class
lives here so the lifecycle/dispatch surface can evolve without churning the
helpers (``_State``, ``_SessionData``, ``_KIND_TO_MESSAGE_CLS``, etc.) that
remain re-exported from ``session_handler``. Tracer name is preserved
(``sidequest.server.session_handler``) so OTEL consumers do not see a
span-source rename.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from sidequest.handlers.base import MessageHandler
    from sidequest.protocol.models import LocationEntity
    from sidequest.server.session_room import RoomRegistry, SessionRoom

from sidequest.agents.claude_client import ClaudeClient, LlmClient
from sidequest.agents.orchestrator import TurnContext
from sidequest.audio.library_backend import LibraryBackend
from sidequest.daemon_client import (
    DaemonClient,
    DaemonRequestError,
    DaemonUnavailableError,
    render_enabled,
)
from sidequest.game.archetype_apply import apply_archetype_resolved
from sidequest.game.builder import (
    BuilderError,
    CharacterBuilder,
    NoQualifyingClassesError,
    PoolValueNotPresentError,
    StoryInput,
    UnfilledArrangementError,
)
from sidequest.game.character import Character
from sidequest.game.chassis import (
    init_chassis_registry,
    rebind_chassis_bonds_to_character,
)
from sidequest.game.event_log import EventLog
from sidequest.game.lore_seeding import (
    seed_lore_from_char_creation,
    seed_world_lore,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection_filter import ProjectionFilter
from sidequest.game.region_init import RegionInitError, init_region_location
from sidequest.game.room_movement import (
    RoomGraphInitError,
    init_room_graph_location,
    process_session_open,
)
from sidequest.game.session import (
    GameSnapshot,
    NarrativeEntry,
)
from sidequest.game.shared_world_delta import (
    build_shared_world_delta,
)
from sidequest.game.world_materialization import (
    CampaignMaturity,
    HistoryParseError,
    materialize_from_genre_pack,
    parse_history_chapters,
    preload_authored_npcs,
    recompute_arc_history,
    should_recompute_arc,
)
from sidequest.genre.archetype.shim import resolve_archetype
from sidequest.genre.error import GenreValidationError
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.world import NavigationMode
from sidequest.protocol import GameMessage
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    AudioCueMessage,
    AudioCuePayload,
    ChapterMarkerMessage,
    ChapterMarkerPayload,
    CharacterCreationMessage,
    CharacterCreationPayload,
    ConfrontationMessage,
    ConfrontationPayload,
    ImageMessage,
    ImagePayload,
    NarrationEndMessage,
    NarrationEndPayload,
    NarrationMessage,
    NarrationPayload,
    NarrationSegmentPayload,
    RenderQueuedMessage,
    RenderQueuedPayload,
    ScrapbookEntryPayload,
    SecretNotePayload,
    SessionEventPayload,
    TacticalGridMessage,
    TacticalGridPayload,
    TurnStatusMessage,
    TurnStatusPayload,
)
from sidequest.protocol.models import (
    Footnote,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server import views
from sidequest.server.audio_cue import build_audio_cue_payload
from sidequest.server.dispatch.chargen_loadout import apply_starting_loadout
from sidequest.server.dispatch.chargen_summary import render_confirmation_summary
from sidequest.server.dispatch.opening import (
    OpeningResolutionError,
    _resolve_opening_post_chargen,
    build_directive,
    record_opening_played,
)
from sidequest.server.dispatch.scenario_bind import bind_scenario
from sidequest.server.magic_init import init_magic_state_for_session
from sidequest.server.narration_apply import (
    _apply_narration_result_to_snapshot,
    _handshake_resolved_tropes,
)
from sidequest.server.session_handler import (
    _AUDIO_INTERPRETER,
    _build_pc_descriptor,
    _hash_snapshot,
    _SessionData,
    _shared_world_delta_to_state_delta,
    _State,
)
from sidequest.server.session_helpers import (
    _build_turn_context,
    _error_msg,
    _render_url_from_path,
    _resolve_acting_character_name,
    _resolve_location_display,
    _world_history_value,
    build_secret_note_events,
)
from sidequest.server.utils import slugify_player_name as _slugify_player_name
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.spans import (
    SPAN_CHARGEN_ARCHETYPE_GATE_BLOCKED,
    SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED,
    audio_backend_disabled_span,
    audio_backend_enabled_span,
    audio_dispatched_span,
    audio_skipped_span,
    encounter_momentum_broadcast_span,
    orchestrator_process_action_span,
    round_invariant_span,
    turn_span,
)
from sidequest.telemetry.turn_record import PatchSummary, TurnRecord
from sidequest.telemetry.validator import Validator
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

# Preserve the original tracer name so OTEL span sources do not rename when
# this class moved out of session_handler.py. Phase-3 plan principle.
tracer = trace.get_tracer("sidequest.server.session_handler")


def _populate_opening_directive_on_chargen_complete(
    session_data: _SessionData,
    snapshot: GameSnapshot,
    pack: GenrePack,
    world_slug: str,
    mode: object,
) -> None:
    """Resolve and stash an Opening directive at chargen-completion time.

    Called from the ``is_first_commit`` branch of
    :meth:`WebSocketSessionHandler._handle_character_creation` after
    authored NPCs have been pre-loaded but before persistence and the
    first narrator turn fires. Picks one Opening from the world's bank
    via :func:`_resolve_opening_post_chargen`, builds a directive via
    :func:`build_directive`, and stashes both the seed and directive on
    ``session_data`` so :meth:`_run_opening_turn_narration` can consume
    them on the very next call.

    Side effects:
    - sets ``session_data.opening_seed`` to the chosen Opening's
      first_turn_invitation
    - sets ``session_data.opening_directive`` to the rendered directive
    - sets ``session_data._resolved_opening_id`` for the played-span

    No-ops gracefully when:
    - ``opening_directive`` is already populated (idempotency for
      double-confirmation guards)
    - the snapshot has no characters (defensive — chargen should have
      appended one already)
    - the world has no openings authored (Validator 7 should have
      caught this at world load)
    - resolution fails (Validators 7+8 should make this unreachable)

    The MP-joiner case is handled by the *caller*: this helper is only
    invoked from the ``is_first_commit`` branch, so a peer joining a
    snapshot that already has characters never reaches this code path.

    See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md``
    §2.4 + §2.6.
    """
    if getattr(session_data, "opening_directive", None) is not None:
        return  # already populated — idempotent (no event: silent on replay)

    # Loud-fail bail-out paths (playtest 2026-05-03 — opening narration
    # skips Kestrel beat). Each ``return`` previously was a silent
    # ``# defensive`` bail; the GM panel had no signal that the canned
    # opening was even attempted. Now every skip emits an
    # ``opening.skipped`` watcher event so Sebastien sees the lie-
    # detector fire when the narrator improvises in place of the
    # canned opening. Severity=warning because the resolver running and
    # finding no match is the exact bug that surfaces as "PCs land in
    # Vaskov Centrum customs instead of the Kestrel galley".
    _genre_slug = getattr(session_data, "genre_slug", "")
    _world_slug = getattr(session_data, "world_slug", world_slug)

    def _emit_skip(reason: str, **extra: object) -> None:
        _watcher_publish(
            "opening.skipped",
            {
                "reason": reason,
                "genre": _genre_slug,
                "world": _world_slug,
                "characters_committed": len(snapshot.characters),
                **extra,
            },
            component="opening_hook",
            severity="warning",
        )

    if not snapshot.characters:
        _emit_skip("empty_snapshot")
        return

    pc = snapshot.characters[0]
    pc_background = getattr(pc, "background", "") or ""

    world = pack.worlds.get(world_slug)
    if world is None or not world.openings:
        _emit_skip(
            "world_or_openings_missing",
            world_present=world is not None,
            opening_bank_size=len(getattr(world, "openings", []) or []) if world else 0,
        )
        return

    mode_str = mode.value if hasattr(mode, "value") else str(mode)
    try:
        opening = _resolve_opening_post_chargen(
            world.openings,
            mode=mode_str,
            player_count=len(snapshot.characters),
            pc_background=pc_background,
            world_slug=world_slug,
        )
    except OpeningResolutionError as exc:
        # The active cause of the Kestrel-skip bug: ``mp_galley_jumprest``
        # declares ``min_players: 2`` and the resolver runs at first
        # commit when only 1 PC is seated. The deferral gate
        # (``_should_fire_opening_narration``) catches this and waits
        # for the second commit; the skip event tells Sebastien
        # *why* the resolver gave up.
        _emit_skip(
            "resolution_failed",
            mode=mode_str,
            player_count=len(snapshot.characters),
            pc_background=pc_background,
            opening_bank_size=len(world.openings),
            error=str(exc),
        )
        return

    # Chassis lookup — World may not store chassis_instances directly
    # (loader populates them as a sibling structure for validators).
    # Use getattr to fall through gracefully when the field is absent;
    # location-anchored Openings won't need it anyway.
    chassis = None
    authored_crew: list = []
    bond_tier: str = "neutral"
    if opening.setting.chassis_instance is not None:
        chassis_instances = getattr(world, "chassis_instances", []) or []
        chassis = next(
            (c for c in chassis_instances if c.id == opening.setting.chassis_instance),
            None,
        )
        if chassis is not None:
            npc_by_id = {n.id: n for n in world.authored_npcs}
            authored_crew = [npc_by_id[i] for i in chassis.crew_npcs if i in npc_by_id]
            for seed in chassis.bond_seeds:
                if seed.character_role == "player_character":
                    bond_tier = seed.bond_tier_chassis
                    break

    present_npcs: list = []
    if opening.setting.chassis_instance is None:
        npc_by_id = {n.id: n for n in world.authored_npcs}
        present_npcs = [npc_by_id[i] for i in opening.setting.present_npcs if i in npc_by_id]

    per_pc_beat = None
    pc_drive = getattr(pc, "drive", "") or ""
    for beat in opening.per_pc_beats:
        applies = beat.applies_to
        if applies.get("background") == pc_background:
            per_pc_beat = beat
            break
        if applies.get("drive") == pc_drive:
            per_pc_beat = beat
            break

    magic_register = getattr(world, "magic_register", "") or ""

    pc_name_parts = pc.core.name.split() if hasattr(pc, "core") else [""]
    directive = build_directive(
        opening=opening,
        chassis=chassis,
        authored_crew=authored_crew,
        magic_register=magic_register,
        bond_tier_for_pc=bond_tier,
        per_pc_beat=per_pc_beat,
        pc_first_name=getattr(pc, "first_name", None) or pc_name_parts[0],
        pc_last_name=getattr(pc, "last_name", "") or "",
        pc_nickname=getattr(pc, "nickname", "") or "",
        present_npcs=present_npcs,
    )

    session_data.opening_seed = opening.first_turn_invitation
    session_data.opening_directive = directive
    session_data._resolved_opening_id = opening.id

    # sq-playtest 2026-05-09 [OBS] projection.party_zone_absent_with_characters:
    # ``party_location()`` returned None at game start because no seated PC
    # had a ``character_locations`` entry — the perception rewriter then
    # fell back to "you can't identify them" mode and the narrator had to
    # call PCs *"another figure — armed, by the silhouette"* instead of by
    # name. Bootstrap every seated PC's location to the resolved Opening's
    # ``setting.location_label`` so ``visible_to()`` / ``in_same_zone()``
    # return true on turn 1. Idempotent: only writes when an entry is
    # absent. Both PCs land on the same string, so consensus matches.
    _bootstrap_character_locations_from_opening(snapshot, opening)


def _bootstrap_character_locations_from_opening(snapshot: GameSnapshot, opening: object) -> None:
    """Write the opening's ``setting.location_label`` to every seated PC
    without a ``character_locations`` entry.

    Idempotent — preserves any prior entry (turn-1 narration apply or a
    resumed save's last-known location). Only fills the *empty* slots that
    cause ``party_location()`` to return None at game start.

    Emits ``snapshot.character_locations_bootstrapped`` (watcher event +
    span event) so the GM panel can verify the chargen-complete bootstrap
    fired and which seats were populated.
    """
    location = getattr(getattr(opening, "setting", None), "location_label", None)
    if not location:
        return
    seated = [name for name in snapshot.player_seats.values() if name]
    bootstrapped: list[str] = []
    for name in seated:
        if name not in snapshot.character_locations:
            snapshot.character_locations[name] = location
            bootstrapped.append(name)
    if bootstrapped:
        opening_id = getattr(opening, "id", "") or ""
        _watcher_publish(
            "snapshot.character_locations_bootstrapped",
            {
                "source": "opening.setting.location_label",
                "opening_id": opening_id,
                "bootstrapped_pcs": bootstrapped,
                "bootstrapped_count": len(bootstrapped),
                "seated_count": len(seated),
            },
            component="opening_hook",
            severity="info",
        )
        trace.get_current_span().add_event(
            "snapshot.character_locations_bootstrapped",
            {
                "event": "snapshot.character_locations_bootstrapped",
                "source": "opening.setting.location_label",
                "opening_id": opening_id,
                "bootstrapped_pcs": ",".join(bootstrapped),
                "bootstrapped_count": len(bootstrapped),
                "seated_count": len(seated),
            },
        )


def _should_fire_opening_narration(session_data: object, room: object) -> bool:
    """Decide whether to run opening narration on this chargen.complete.

    Playtest 2026-05-03: in MP, the canned ``mp_galley_jumprest`` opening
    declares ``min_players: 2`` but the populator runs at FIRST commit
    when only 1 PC is seated. Resolution fails → no directive → narrator
    improvises Vaskov Centrum customs instead of the Kestrel galley. The
    second committer hits ``mp_joiner_opening_suppressed_at_consume``
    and gets joiner-orientation anchored on the (improvised) host
    location — the canned MP opening is never used.

    Fix: the populator is now called on every commit (idempotent — bails
    if directive already set). This gate decides whether to *fire* the
    opening narration based on whether the party is complete enough
    that the canned opening can resolve. Solo paths fire on first
    commit (preserves prior behavior). MP first committers DEFER until
    the last committer can re-run the populator with the full party
    seated.

    Returns ``True`` when:
      - There is no MP room (solo headless path).
      - Room reports ``non_abandoned_player_count <= 1`` (solo via room).
      - The directive is already populated (resolver succeeded —
        either because we're solo or because this is the last
        committer and the party is complete).
      - All non-abandoned seats have committed chargen
        (``len(characters) >= non_abandoned_player_count``) — fire as a
        belt-and-suspenders fallback even if the populator hasn't
        managed to set the directive (e.g. world has no openings — the
        narrator's "I look around" fallback still beats silence).

    Returns ``False`` only in the explicit MP-defer case: room exists,
    >1 seat expected, party not complete, and no directive populated.
    """
    if room is None:
        return True
    try:
        seat_count = int(room.non_abandoned_player_count())
    except Exception:  # noqa: BLE001 — fail open to current behavior on contract drift
        return True
    if seat_count <= 1:
        return True
    if getattr(session_data, "opening_directive", None) is not None:
        return True
    snapshot = getattr(session_data, "snapshot", None)
    chars = getattr(snapshot, "characters", []) if snapshot is not None else []
    return len(chars) >= seat_count


def _maybe_build_runtime_cavern_payload(
    *,
    sd: _SessionData,
    room_id: str,
) -> TacticalGridPayload | None:
    """Build a TacticalGridPayload from a persisted runtime cavern mask.

    Story 52-4 (ADR-096 + ADR-106). When ``_maybe_emit_tactical_grid``
    finds no static room YAML and the world has a procedural dungeon
    (``sd.dungeon_store`` is wired by the Beneath Sünden path), look up
    the persisted mask BLOB for ``room_id`` and:

    1. Emit a ``.cavern.png`` sidecar at the local-renders mount via
       ``emit_runtime_cavern_png`` (fires the
       ``dungeon.render.cavern_mask_to_png`` OTEL span).
    2. Synthesise a minimal ``TacticalGridPayload`` carrying the
       decoded ASCII mask + resolved cavern image URL + cell size.
       Cellular params are unknown at this point (the mask is the
       truth; the originating generation params live in
       ``GenerationReport``, not in the persisted mask BLOB).
       ``DerivedRoomData`` is computed from the mask: floor_count is
       the ASCII '.' count; exits and pois are empty here — the
       procedural region's exits live at the region-graph level, not
       the cavern-mask level (52-5 / future work owns surfacing them).
    3. Return the payload so the caller emits a normal
       ``TACTICAL_GRID`` message. The UI's existing TacticalGridRenderer
       consumes it identically to the static path (AC5 contract: "the
       PNG sidecar integrates with the existing TacticalGridRenderer").

    Returns ``None`` when:
      * ``sd.dungeon_store`` is absent (no procedural dungeon wired —
        applies to every non-Beneath-Sünden world right now);
      * no mask is persisted for ``room_id`` (the room_id is not a
        materialised region — could be a settlement_id or a typo);
      * the local-renders output directory is not configured
        (``SIDEQUEST_OUTPUT_DIR`` env var unset at turn time —
        ``server.app`` startup also logs ``render_assets.no_output_dir``
        loudly when it cannot resolve a daemon-output handshake; this
        per-turn check is the safety net and also emits its own
        ``tactical_grid.runtime_render_skipped`` watcher event so the
        GM panel sees the per-turn skip without depending on
        startup-time correlation).

    No silent fallbacks: a corrupt mask BLOB or PIL failure propagates
    as a ``ValueError`` / ``OSError`` from ``emit_runtime_cavern_png``
    and is caught by the outer ``_maybe_emit_tactical_grid``'s generic
    ``except Exception`` handler (logged at warning level).
    """
    import base64 as _base64
    import os as _os

    from sidequest.game.room_file_loader import emit_runtime_cavern_png
    from sidequest.protocol.models import DerivedRoomData
    from sidequest.server.asset_urls import resolve_asset_url

    # The Decision-N gate shape (``<var> = getattr(sd, "dungeon_store", None)``
    # followed by ``if <var> is not None:``) is grep-asserted by
    # tests/dungeon/test_setpiece_attach_wiring.py — keep the positive guard
    # so the structural lint binding both this helper and the existing
    # resolve_complications_for_resolved_tropes site continues to pass.
    dungeon_store = getattr(sd, "dungeon_store", None)
    if dungeon_store is not None:
        try:
            masks = dungeon_store.load_masks()
        except Exception as exc:  # noqa: BLE001 — must not crash a turn
            logger.warning(
                "tactical_grid.runtime_mask_load_failed genre=%s world=%s room_id=%s error=%s",
                sd.genre_slug,
                sd.world_slug,
                room_id,
                exc,
            )
            return None
        mask_dict = masks.get(room_id)
        if mask_dict is None:
            return None  # room_id is not a materialised region — defer to static-not-found path

        output_dir_env = _os.environ.get("SIDEQUEST_OUTPUT_DIR")
        if not output_dir_env:
            # No silent fallback: a missing output dir is an
            # operator-visible config gap. ``server.app`` also logs
            # this loudly at startup; the per-turn warning + watcher
            # event is the safety net per CLAUDE.md No-Silent-Fallbacks.
            logger.warning(
                "tactical_grid.runtime_render_skipped reason=no_output_dir "
                "genre=%s world=%s room_id=%s",
                sd.genre_slug,
                sd.world_slug,
                room_id,
            )
            _watcher_publish(
                "tactical_grid.runtime_render_skipped",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "room_id": room_id,
                    "reason": "no_output_dir",
                },
                component="cavern_renderer",
                severity="warning",
            )
            return None
        output_root = Path(output_dir_env)

        save_path = getattr(sd.store, "_path", None)
        save_id = save_path.stem if isinstance(save_path, Path) else "in_memory"

        relative = f"artifacts/dungeon/{save_id}/regions/{room_id}.cavern.png"
        output_path = output_root / relative
        emit_runtime_cavern_png(
            mask_dict=mask_dict,
            output_path=output_path,
            region_id=room_id,
        )

        cavern_image_url = resolve_asset_url(relative)

        # Decode the mask ASCII for the UI overlay (independent of the PNG
        # — the UI uses the mask string for cell-stepped math, not pixel-
        # peeping).
        mask_text = _base64.b64decode(mask_dict["mask_bytes_b64"]).decode("ascii")
        floor_count = mask_text.count(".")
        block = mask_dict["block"]

        return TacticalGridPayload(
            room_id=room_id,
            room_name=room_id,  # procedural rooms have no authored name — region_id IS the name
            room_type="cavern",
            mask=mask_text,
            cavern_image_url=cavern_image_url,
            cell_size=block["cell_width"],
            cellular=None,  # originating generation params not persisted in the mask BLOB
            derived=DerivedRoomData(
                floor_count=floor_count,
                exits={},  # procedural exits live at the region-graph level, not the mask
                pois=[],
            ),
            tokens=[],
            initiative=None,
            entities=[],
        )

    return None


def _maybe_emit_tactical_grid(
    handler: object,
    *,
    sd: _SessionData,
    snapshot: GameSnapshot,
    actor: str | None,
    emit_fn: object,
    room_id_override: str | None = None,
) -> None:
    """Emit a TACTICAL_GRID message when the player enters a room-graph room.

    ADR-096 Task 20b. Wires ``load_room_payload`` into the room-enter dispatch
    path so the UI's Automapper receives live cavern/settlement data.

    Called from two sites:
    1. Narrator location-change branch (narration turn loop) — ``actor`` is the
       acting character, room_id comes from ``snapshot.character_locations``.
    2. Chargen room-graph init — ``room_id_override`` is the entrance room id
       returned by ``init_room_graph_location``.

    OTEL: emits ``tactical_grid.emitted`` on success,
    ``tactical_grid.room_not_found`` when the room YAML is absent (non-fatal —
    many worlds use room_graph without per-room YAMLs), and
    ``tactical_grid.load_failed`` on unexpected loader errors.
    """
    from sidequest.game.room_file_loader import RoomNotFoundError, load_room_payload
    from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader

    world = sd.genre_pack.worlds.get(sd.world_slug)
    if world is None:
        return

    if room_id_override is not None:
        room_id = room_id_override
    else:
        # Read the post-apply location for this actor.
        room_id = snapshot.character_locations.get(actor or "") if actor else None
    if not room_id:
        return

    try:
        loader = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS)
        world_dir = loader.find(sd.genre_slug) / "worlds" / sd.world_slug
    except Exception as exc:  # noqa: BLE001 — non-fatal; world dir lookup must not crash a turn
        logger.warning(
            "tactical_grid.world_dir_lookup_failed genre=%s world=%s error=%s",
            sd.genre_slug,
            sd.world_slug,
            exc,
        )
        return

    # Story 52-5: discriminator on tactical_grid.emitted so the GM panel
    # (Sebastien) can distinguish runtime-generated cavern PNGs from
    # statically authored ones without correlating sibling OTEL spans.
    source = "static"
    try:
        payload = load_room_payload(world_dir, room_id, genre_slug=sd.genre_slug)
    except RoomNotFoundError:
        # Story 52-4: no static YAML — try the runtime path. If the
        # world has a procedural dungeon (Beneath Sünden via ADR-106)
        # and the room_id is a region_id with a persisted mask, emit
        # the runtime cavern PNG sidecar and synthesise a
        # TacticalGridPayload from the mask. Falls through (return)
        # if the world has no procedural dungeon or no mask exists for
        # this room_id — the existing static-path absence is non-fatal.
        runtime_payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id)
        if runtime_payload is not None:
            payload = runtime_payload
            source = "runtime"
        else:
            # Per CLAUDE.md no-silent-fallback: log at debug so the
            # absence IS visible to Keith/Sebastien at low verbosity,
            # but not loud enough to alarm on every non-YAML room.
            logger.debug(
                "tactical_grid.room_not_found genre=%s world=%s room_id=%s",
                sd.genre_slug,
                sd.world_slug,
                room_id,
            )
            _watcher_publish(
                "tactical_grid.room_not_found",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "room_id": room_id,
                },
                component="cavern_renderer",
            )
            return
    except FileNotFoundError as exc:
        # Mask .txt or .cavern.png missing — authoring error, log loud.
        logger.warning(
            "tactical_grid.load_failed genre=%s world=%s room_id=%s error=%s",
            sd.genre_slug,
            sd.world_slug,
            room_id,
            exc,
        )
        _watcher_publish(
            "tactical_grid.load_failed",
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "room_id": room_id,
                "error": str(exc),
            },
            component="cavern_renderer",
            severity="warning",
        )
        return
    except Exception as exc:  # noqa: BLE001 — must not crash a turn
        logger.warning(
            "tactical_grid.load_failed genre=%s world=%s room_id=%s error=%s",
            sd.genre_slug,
            sd.world_slug,
            room_id,
            exc,
        )
        _watcher_publish(
            "tactical_grid.load_failed",
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "room_id": room_id,
                "error": str(exc),
            },
            component="cavern_renderer",
            severity="warning",
        )
        return

    # Tokens and initiative are populated from game state.
    # TODO: populate tokens from snapshot encounter/party at room_id when
    # the token placement system (ADR-096 Phase 3) lands. For now, empty
    # list is correct — the plan task description explicitly notes this is
    # out of scope for Phase E.
    tactical_msg = TacticalGridMessage(
        payload=payload,
        player_id=getattr(sd, "player_id", ""),
    )
    _watcher_publish(
        "tactical_grid.emitted",
        {
            "genre": sd.genre_slug,
            "world": sd.world_slug,
            "room_id": room_id,
            "room_type": payload.room_type,
            "room_name": payload.room_name,
            "source": source,
        },
        component="cavern_renderer",
    )
    logger.info(
        "tactical_grid.emitted genre=%s world=%s room_id=%s room_type=%s",
        sd.genre_slug,
        sd.world_slug,
        room_id,
        payload.room_type,
    )
    # Dispatch via the shared-world emit function (broadcasts to all connected
    # sockets). The emit_fn is the closure from the turn dispatch loop; it
    # handles both the room.broadcast path and the outbound-list fallback for
    # test fixtures without a real SessionRoom.
    emit_fn(tactical_msg, "TACTICAL_GRID")  # type: ignore[operator]


def _maybe_emit_location_description(
    handler: object,
    *,
    sd: _SessionData,
    snapshot: GameSnapshot,
    actor: str | None,
    emit_fn: object,
    room_id_override: str | None = None,
) -> None:
    """Emit a LOCATION_DESCRIPTION message when the party's current_room changes.

    Story 54-2 / ADR-109. Mirrors the ``_maybe_emit_tactical_grid`` wiring:

    1. Narrator location-change branch (same call sites as tactical grid).
    2. Session-resume dispatch (``room_id_override`` is the resumed room).

    Two source paths:
    - Per-room YAML via ``load_room_payload`` (room_graph worlds).
    - Cartography region (region-mode worlds) — fallback when no room YAML.

    Missing room id is silent because no-room is not an error state.
    Missing manifest source fires the ``location_description.no_source``
    watcher event so the absence is observable on the GM panel — per
    CLAUDE.md OTEL Observability Principle.

    The ``overlays`` payload field is always emitted as ``[]`` in this
    story — overlay population is owned by Story 54-7
    (``LOCATION_OVERLAY_CHANGED``).
    """
    from sidequest.game.room_file_loader import RoomNotFoundError, load_room_payload
    from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.protocol.models import LocationDescriptionPayload

    world = sd.genre_pack.worlds.get(sd.world_slug)
    if world is None:
        return

    if room_id_override is not None:
        room_id = room_id_override
    else:
        room_id = snapshot.character_locations.get(actor or "") if actor else None
    if not room_id:
        return

    prose: str = ""
    terrain: str | None = None
    entities: list[LocationEntity] = []
    sourced = False

    # Path 1: per-room YAML via load_room_payload.
    try:
        loader = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS)
        world_dir = loader.find(sd.genre_slug) / "worlds" / sd.world_slug
    except Exception as exc:  # noqa: BLE001 — non-fatal; world dir lookup must not crash a turn
        logger.warning(
            "location_description.world_dir_lookup_failed genre=%s world=%s error=%s",
            sd.genre_slug,
            sd.world_slug,
            exc,
        )
        _watcher_publish(
            "location_description.world_dir_lookup_failed",
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "error": str(exc),
            },
            component="location",
            severity="warning",
        )
        return

    try:
        room_payload = load_room_payload(world_dir, room_id, genre_slug=sd.genre_slug)
        prose = room_payload.settlement_description or ""
        terrain = room_payload.room_type
        entities = room_payload.entities
        sourced = True
    except RoomNotFoundError:
        # Fall back to cartography region lookup (region-mode worlds).
        cartography = getattr(world, "cartography", None)
        region = (
            cartography.regions.get(room_id)
            if cartography is not None and hasattr(cartography, "regions")
            else None
        )
        if region is not None:
            prose = getattr(region, "description", "") or ""
            terrain = getattr(region, "terrain", None)
            entities = getattr(region, "entities", [])
            sourced = True
    except Exception as exc:  # noqa: BLE001 — must not crash a turn
        logger.warning(
            "location_description.load_failed genre=%s world=%s room=%s error=%s",
            sd.genre_slug,
            sd.world_slug,
            room_id,
            exc,
        )
        _watcher_publish(
            "location_description.load_failed",
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "room_id": room_id,
                "error": str(exc),
            },
            component="location",
            severity="warning",
        )
        return

    if not sourced:
        # Neither path produced a manifest source — make the absence
        # observable on the GM panel rather than silently emitting an
        # empty payload.
        _watcher_publish(
            "location_description.no_source",
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "room_id": room_id,
            },
            component="location",
        )
        return

    payload = LocationDescriptionPayload(
        region_id=room_id,
        prose=prose,
        terrain=terrain,
        entities=entities,
        overlays=[],  # Story 54-7 populates this.
    )
    msg = LocationDescriptionMessage(
        payload=payload,
        player_id=getattr(sd, "player_id", ""),
    )
    _watcher_publish(
        "location_description.emitted",
        {
            "genre": sd.genre_slug,
            "world": sd.world_slug,
            "room_id": room_id,
            "entity_count": len(entities),
            "prose_chars": len(prose),
        },
        component="location",
    )
    logger.info(
        "location_description.emitted genre=%s world=%s room=%s entities=%d",
        sd.genre_slug,
        sd.world_slug,
        room_id,
        len(entities),
    )
    emit_fn(msg, "LOCATION_DESCRIPTION")  # type: ignore[operator]


def _maybe_emit_dungeon_map(
    handler: object,
    *,
    sd: _SessionData,
    snapshot: GameSnapshot,
    emit_fn: object,
) -> None:
    """Emit a DUNGEON_MAP frame for a beneath_sunden session (BETTER fix
    seam 3). Projects the live region graph (discovered regions only —
    fog of war) to the UI Map tab in the ``MapState``/``ExploredLocation``
    shape so the MapWidget's Automapper region-graph path renders it with
    no adapter. Cures the 2026-05-17 "No map data yet" defect: the
    materialized dungeon was never projected to the UI after ADR-019
    MAP_UPDATE was deleted in the port (this is the NEW ADR-055 message).

    Called every narration turn (idempotent — the UI just replaces its
    MapState). Clean no-op for every other world. OTEL: emits
    ``dungeon.map_emitted`` on success / ``dungeon.map_skipped`` (with a
    reason) otherwise, so the GM panel sees the UI seam engaged — never a
    silent skip. A live turn never hard-fails on a dungeon defect.

    Lazy imports: ``sidequest.dungeon`` depends on game models (the
    frontier-hook lazy-import precedent)."""
    from sidequest.dungeon.persistence import DatabaseError, DungeonStore
    from sidequest.dungeon.region_projection import applies_to
    from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
    from sidequest.dungeon.themes import load_theme_palette
    from sidequest.genre.loader import (
        DEFAULT_GENRE_PACK_SEARCH_PATHS,
        GenreLoader,
    )
    from sidequest.protocol.messages import (
        DungeonMapExit,
        DungeonMapLocation,
        DungeonMapMessage,
        DungeonMapPayload,
    )

    if not applies_to(sd.genre_slug, sd.world_slug):
        return  # the per-turn dungeon.region_projection span already
        # records the other-world no-op; a second event here is noise.

    store = DungeonStore(sd.store.connection())
    try:
        graph = store.load_map(entrance_id=ENTRANCE_ID)
    except DatabaseError as exc:
        _watcher_publish(
            "dungeon.map_skipped",
            {"world": sd.world_slug, "reason": f"no_schema: {exc}"},
            component="dungeon",
            severity="warning",
        )
        logger.warning("dungeon.map_skipped no schema: %s", exc)
        return
    if not graph.nodes:
        _watcher_publish(
            "dungeon.map_skipped",
            {"world": sd.world_slug, "reason": "empty_map"},
            component="dungeon",
            severity="warning",
        )
        logger.warning("dungeon.map_skipped empty dungeon_map")
        return

    loader = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS)
    world_dir = loader.find(sd.genre_slug) / "worlds" / sd.world_slug
    palette = load_theme_palette(world_dir.parent.parent)

    current_region = snapshot.current_region or ""
    discovered = [r for r in snapshot.discovered_regions if r in graph.nodes]
    # Fog of war: never leak undiscovered regions. If discovered_regions
    # is somehow empty but a current_region is bound, at least show that.
    if not discovered and current_region in graph.nodes:
        discovered = [current_region]

    explored: list[DungeonMapLocation] = []
    for rid in discovered:
        node = graph.nodes[rid]
        try:
            display = palette.get(node.theme).display_name
        except KeyError:
            display = rid  # fail-soft label; the span/log below is loud
        room_exits = [
            DungeonMapExit(target=(e.b if e.a == rid else e.a), exit_type=e.kind)
            for e in graph.edges
            if rid in (e.a, e.b) and not e.hidden  # secrets stay off the map
        ]
        explored.append(
            DungeonMapLocation(
                id=rid,
                name=display,
                type="region",
                connections=[x.target for x in room_exits],
                room_exits=room_exits,
                room_type="entrance" if rid == ENTRANCE_ID else "normal",
                is_current_room=(rid == current_region),
            )
        )

    payload = DungeonMapPayload(
        current_location=current_region,
        region=current_region,
        explored=explored,
    )
    msg = DungeonMapMessage(payload=payload, player_id=getattr(sd, "player_id", ""))
    _watcher_publish(
        "dungeon.map_emitted",
        {
            "world": sd.world_slug,
            "current_region": current_region,
            "discovered_regions": len(explored),
            "total_regions": len(graph.nodes),
        },
        component="dungeon",
    )
    logger.info(
        "dungeon.map_emitted current=%s discovered=%d/%d",
        current_region,
        len(explored),
        len(graph.nodes),
    )
    emit_fn(msg, "DUNGEON_MAP")  # type: ignore[operator]


class WebSocketSessionHandler:
    """Per-connection session: state machine + dispatch.

    Created fresh per WebSocket connection by the /ws endpoint factory.
    Not shared across connections (Phase 1 is single-player).
    """

    def __init__(
        self,
        *,
        claude_client_factory: Callable[[], LlmClient] | None = None,
        genre_pack_search_paths: list[Path] | None = None,
        save_dir: Path,
        validator: Validator | None = None,
    ) -> None:
        self._client_factory: Callable[[], LlmClient] = (
            claude_client_factory if claude_client_factory is not None else ClaudeClient
        )
        self._search_paths: list[Path] = (
            genre_pack_search_paths
            if genre_pack_search_paths is not None
            else DEFAULT_GENRE_PACK_SEARCH_PATHS
        )
        self._save_dir = save_dir
        self._validator: Validator | None = validator
        self._state = _State.AwaitingConnect
        self._session_data: _SessionData | None = None
        # Room context fields — populated by attach_room_context() during the
        # WebSocket lifecycle (ws_endpoint). Absent here means the handler is
        # being driven outside that lifecycle (e.g. unit tests that exercise
        # non-slug-connect code paths). The slug-connect branch rejects this
        # loudly rather than silently skipping room wiring.
        self._room_registry: RoomRegistry | None = None
        self._socket_id: str | None = None
        self._out_queue: asyncio.Queue[object] | None = None
        self._room: SessionRoom | None = None
        # EventLog + projection filter are bound in the slug-connect branch.
        # The legacy genre/world connect path leaves them None; _emit_event
        # falls back to a plain message without seq in that case. This is a
        # real production code path (not a test-only skip), documented below.
        self._event_log: EventLog | None = None
        self._projection_filter: ProjectionFilter | None = None
        self._projection_cache: ProjectionCache | None = None

    # ------------------------------------------------------------------
    # Room context (MP-02 Task 2)
    # ------------------------------------------------------------------

    def attach_room_context(
        self,
        *,
        registry: RoomRegistry,
        socket_id: str,
        out_queue: asyncio.Queue[object],
    ) -> None:
        """Attach the process-wide RoomRegistry, socket_id, and per-socket outbound queue.

        Called by ws_endpoint immediately after accept(). _room is assigned in
        the slug-connect branch when a room is joined. out_queue is the
        asyncio.Queue that the writer task in ws_endpoint drains.

        All three fields are required. The slug-connect branch fails loudly if
        this method was not called — there is no silent test-only path.
        """
        self._room_registry = registry
        self._socket_id = socket_id
        self._out_queue = out_queue

    def current_room(self) -> SessionRoom | None:
        """Return the room this handler is currently registered in, or None."""
        return self._room

    # ------------------------------------------------------------------
    # EventLog fan-out helper (MP-03 Task 3)
    # ------------------------------------------------------------------

    def _emit_event(
        self, kind: str, payload_model: object, *, author_player_id: str | None = None
    ) -> object:
        """Persist + fan-out an event. Delegates to ``emitters.emit_event``.

        Phase 1 of session_handler decomposition (see
        docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).

        ``author_player_id`` (ADR-105 Track A): when set, the emit is a
        shared merged-MP turn whose driving handler is the last-submitter,
        NOT the sole author. The emitter is then projected like any
        recipient instead of receiving the solo Invariant-3 raw bypass.
        ``None`` (solo / legacy) preserves the byte-identical raw-bypass
        delegate call — keeps the documented emitter-skip + lazy_fill
        invariant intact.
        """
        from sidequest.server import emitters

        if author_player_id is None:
            return emitters.emit_event(self, kind, payload_model)
        return emitters.emit_event(self, kind, payload_model, author_player_id=author_player_id)

    def _dispatch_pending_magic_frames(self, snapshot: GameSnapshot) -> None:
        """Phase 5 (Story 47-3): drain pending magic-confrontation queues.

        ``apply_magic_working`` and ``_resolve_magic_confrontation_if_applicable``
        populate ``snapshot.pending_magic_auto_fires`` (CONFRONTATION
        starts) and ``snapshot.pending_magic_confrontation_outcome``
        (the resolved branch + mandatory_outputs). Both are dispatched
        here as outbound WebSocket frames; both fields are reset
        afterwards so the next turn starts clean.

        Frames are emitted via ``self._emit_event(...)`` rather than a
        direct broadcast so they participate in the EventLog (durable
        replay) and any per-recipient projection. Errors during
        dispatch propagate — a broken outbound queue is loud per
        CLAUDE.md.
        """
        # CONFRONTATION starts (one per auto-fire). The payload shape
        # already matches ``ConfrontationPayload``; emit_event will
        # dispatch it. Pop-as-you-go (round 2 fix) so a malformed
        # entry's ValidationError doesn't strand previously-emitted or
        # subsequent entries in the queue forever — pre-fix the
        # post-loop ``= []`` was the only drain path, so a raise on
        # entry N left entries 0..end stuck and re-fired the valid
        # entries 0..N-1 every dispatch tick.
        if snapshot.pending_magic_auto_fires:
            from sidequest.protocol.messages import ConfrontationPayload

            queue = snapshot.pending_magic_auto_fires
            while queue:
                raw = queue.pop(0)
                try:
                    payload = ConfrontationPayload(**raw)
                except Exception:
                    # Surface the malformed entry to the GM panel + log
                    # loud, then continue draining the queue. The bad
                    # entry is dropped (already popped) — fail loud, do
                    # not silently re-emit the next tick.
                    logger.error(
                        "magic.dispatch_payload_invalid kind=CONFRONTATION raw=%r",
                        raw,
                    )
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "magic_state",
                            "op": "dispatch_payload_invalid",
                            "kind": "CONFRONTATION",
                            "raw": raw,
                        },
                        component="magic",
                        severity="error",
                    )
                    continue
                self._emit_event("CONFRONTATION", payload)

        # CONFRONTATION_OUTCOME (the reveal panel — Decision #9 calls
        # this "explicit panel callout at outcome time, ALWAYS shown").
        # Field is reset BEFORE emit so a ValidationError doesn't leave
        # the bad payload stranded for the next dispatch tick.
        if snapshot.pending_magic_confrontation_outcome is not None:
            from sidequest.protocol.messages import ConfrontationOutcomePayload

            raw_outcome = snapshot.pending_magic_confrontation_outcome
            snapshot.pending_magic_confrontation_outcome = None
            try:
                outcome_payload = ConfrontationOutcomePayload(**raw_outcome)
            except Exception:
                logger.error(
                    "magic.dispatch_payload_invalid kind=CONFRONTATION_OUTCOME raw=%r",
                    raw_outcome,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "magic_state",
                        "op": "dispatch_payload_invalid",
                        "kind": "CONFRONTATION_OUTCOME",
                        "raw": raw_outcome,
                    },
                    component="magic",
                    severity="error",
                )
            else:
                self._emit_event("CONFRONTATION_OUTCOME", outcome_payload)

    # ------------------------------------------------------------------
    # Scrapbook entry emission (pingpong 2026-04-26 [S3-REGRESSION])
    # ------------------------------------------------------------------

    def _emit_scrapbook_entry(
        self,
        *,
        sd: _SessionData,
        snapshot: GameSnapshot,
        result: object,
        render_status: str = "rendered",
    ) -> None:
        """Persist + emit a scrapbook entry. Delegates to ``emitters.emit_scrapbook_entry``.

        Phase 1 of session_handler decomposition (see
        docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).

        ``render_status`` carries the unified Story 45-30 + Story 45-31
        discriminator: ``"rendered"`` (happy path), ``"skipped_policy"``
        (45-30 — trigger policy returned NONE_POLICY), ``"failed"``
        (daemon errored synchronously), ``"unavailable"`` (45-31 — daemon
        mirror reports UNRESPONSIVE; the dispatcher will skip the
        round-trip and the UI shows the placeholder badge live, no
        replay JOIN). Daemon-unavailable wins over policy decisions —
        when there's no render coming either way, the user-facing reason
        is "the daemon is down."
        """
        from sidequest.server import emitters

        emitters.emit_scrapbook_entry(
            self,
            sd=sd,
            snapshot=snapshot,
            result=result,
            render_status=render_status,
        )

    def _persist_scrapbook_entry(self, payload: ScrapbookEntryPayload) -> None:
        """Insert a scrapbook row. Delegates to ``emitters.persist_scrapbook_entry``.

        Phase 1 of session_handler decomposition (see
        docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).
        """
        from sidequest.server import emitters

        emitters.persist_scrapbook_entry(self, payload)

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    @property
    def session_data(self) -> _SessionData | None:
        """Public read accessor for session state (used by tests and GM panel)."""
        return self._session_data

    async def handle_message(self, msg: GameMessage) -> list[object]:
        """Dispatch an inbound message; return list of outbound protocol message objects.

        Looks the message type up in the per-class ``_MESSAGE_HANDLERS`` registry
        (built once at first dispatch) and forwards to the corresponding
        first-class handler under :mod:`sidequest.handlers`. The thin
        ``_handle_X`` methods on this class remain as a test-friendly
        public API so test suites can drive a single message type without
        going through the WebSocket protocol layer.
        """
        msg_type: str = msg.type  # type: ignore[attr-defined]

        handler = type(self)._message_handler_for(msg_type)
        if handler is None:
            logger.warning(
                "session.unhandled_message_type type=%s state=%s",
                msg_type,
                self._state.name,
            )
            return [_error_msg(f"Unsupported message type in Phase 1: {msg_type}")]
        return await handler.handle(self, msg)

    @classmethod
    def _message_handler_for(cls, msg_type: str) -> MessageHandler | None:
        """Lazy-built registry of message-type → first-class handler singleton.

        Built on first call to avoid importing the handler modules at
        ``WebSocketSessionHandler`` class-definition time, which would
        eagerly drag in their transitive imports (and create a circular
        reference, since the handler modules import this class for
        type-checking). The registry is cached on the class so subsequent
        dispatches are a single dict lookup.
        """
        registry = getattr(cls, "_MESSAGE_HANDLERS", None)
        if registry is None:
            from sidequest.handlers.action_reveal import HANDLER as ACTION_REVEAL_HANDLER
            from sidequest.handlers.character_creation import HANDLER as CHARACTER_CREATION_HANDLER
            from sidequest.handlers.dice_throw import HANDLER as DICE_THROW_HANDLER
            from sidequest.handlers.journal_request import HANDLER as JOURNAL_REQUEST_HANDLER
            from sidequest.handlers.orbital_intent import HANDLER as ORBITAL_INTENT_HANDLER
            from sidequest.handlers.player_action import HANDLER as PLAYER_ACTION_HANDLER
            from sidequest.handlers.player_seat import HANDLER as PLAYER_SEAT_HANDLER
            from sidequest.handlers.session_event import HANDLER as SESSION_EVENT_HANDLER
            from sidequest.handlers.yield_action import HANDLER as YIELD_HANDLER

            registry = {
                "SESSION_EVENT": SESSION_EVENT_HANDLER,
                "PLAYER_ACTION": PLAYER_ACTION_HANDLER,
                "CHARACTER_CREATION": CHARACTER_CREATION_HANDLER,
                "PLAYER_SEAT": PLAYER_SEAT_HANDLER,
                "DICE_THROW": DICE_THROW_HANDLER,
                "YIELD": YIELD_HANDLER,
                "ORBITAL_INTENT": ORBITAL_INTENT_HANDLER,
                "ACTION_REVEAL": ACTION_REVEAL_HANDLER,
                "JOURNAL_REQUEST": JOURNAL_REQUEST_HANDLER,
            }
            cls._MESSAGE_HANDLERS = registry
        return registry.get(msg_type)

    async def cleanup(self) -> None:
        """Called on disconnect — persist current state if in Playing."""
        if self._session_data is not None:
            # Cancel any in-flight embed worker first so it cannot write
            # to an orphaned in-memory lore_store after store.close().
            # CancelledError is a BaseException in Python 3.8+, so it
            # escapes every `except Exception` in the worker; we await
            # and swallow it here so disconnect never raises to the
            # WebSocket layer. Non-cancel exceptions (a real worker bug
            # that happened to surface during cancellation) are logged
            # at warning with exc_info — cleanup still proceeds.
            embed_task = self._session_data.embed_task
            if embed_task is not None and not embed_task.done():
                embed_task.cancel()
                try:
                    await embed_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 — cleanup must proceed
                    logger.warning(
                        "session.embed_task_cleanup_error type=%s error=%s",
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
            # Beneath Sünden Plan 7 (§8): drain the live dungeon look-ahead
            # worker before the final save so the observer is gone before
            # state is persisted. Null-safe by construction — lookahead_handle
            # is None for non-beneath_sunden sessions and for sessions that
            # disconnected before attach. Mirrors embed_task.cancel(): it
            # unregisters/drains only and must NOT close the store — the room
            # owns the store lifecycle. Function-local import avoids any
            # import cycle through the dungeon package.
            from sidequest.dungeon.session_integration import (
                detach_dungeon_from_session,
            )

            await detach_dungeon_from_session(self._session_data.lookahead_handle)
            try:
                # ADR-037 Python port: room owns the canonical snapshot,
                # so a plain room.save() persists it once for every
                # session that disconnects. Legacy non-slug path falls
                # back to the per-session store.
                if self._room is not None:
                    self._room.save()
                else:
                    self._session_data.store.save(self._session_data.snapshot)
                logger.info(
                    "session.disconnect_save genre=%s world=%s player=%s "
                    "char_count=%d seat_count=%d",
                    self._session_data.genre_slug,
                    self._session_data.world_slug,
                    self._session_data.player_name,
                    len(self._session_data.snapshot.characters),
                    len(self._session_data.snapshot.player_seats),
                )
            except Exception as exc:
                logger.error("session.disconnect_save_failed error=%s", exc)

            # Story 45-31: post-session render diagnostic. Writes a
            # JSON snapshot of the render worker's lifetime so the
            # next Felix-style 13-minute silence can be diagnosed
            # without reproducing the crash. Best-effort — diagnostic
            # write must never raise back to the WebSocket layer.
            try:
                from datetime import UTC
                from datetime import datetime as _dt

                from sidequest.daemon_client.state_mirror import get_mirror
                from sidequest.server.render_diagnostics import (
                    write_session_diagnostic,
                )

                room_slug_for_diag: str | None = None
                if self._room is not None:
                    room_slug_for_diag = getattr(self._room, "slug", None)
                if not room_slug_for_diag:
                    # Legacy/no-slug path — namespace by genre+player so
                    # the diagnostic file remains greppable.
                    room_slug_for_diag = (
                        (
                            f"{self._session_data.genre_slug or 'unknown'}-"
                            f"{self._session_data.player_id or 'unknown'}"
                        )
                        .replace("/", "-")
                        .replace("\\", "-")
                    )

                _mirror = get_mirror()
                snapshot_payload = {
                    "heartbeat_history": [
                        {
                            "queue": q,
                            "state": _mirror.state(q).value,
                            "queue_depth": _mirror.queue_depth(q),
                        }
                        for q in ("image", "embed")
                    ],
                    "enqueue_count": int(self._session_data.render_enqueue_count),
                    "backpressure_warn_count": int(
                        self._session_data.render_backpressure_warn_count
                    ),
                    "unresponsive_windows": [
                        {
                            "count": int(self._session_data.render_unresponsive_window_count),
                        }
                    ]
                    if self._session_data.render_unresponsive_window_count
                    else [],
                    "last_successful_render_id": (self._session_data.last_successful_render_id),
                    "last_successful_render_ts": (self._session_data.last_successful_render_ts_iso),
                    "last_heartbeat_ts": _mirror.last_heartbeat_ts(),
                }
                write_session_diagnostic(
                    room_slug=room_slug_for_diag,
                    session_end_iso=_dt.now(UTC).isoformat(),
                    snapshot=snapshot_payload,
                )
            except Exception as _diag_exc:  # noqa: BLE001 — diagnostic must never crash teardown
                logger.warning("render.session_diagnostic_failed err=%s", _diag_exc)
            finally:
                # ADR-037 Python port: when the session is bound to a room,
                # the room owns the SqliteStore lifecycle — every WS session
                # bound to the slug shares the same store reference, so
                # closing it from one session's cleanup() leaves
                # ``room.save()`` operating on a closed connection from any
                # other session's perspective and produces
                # ``session.disconnect_save_failed error=Cannot operate on
                # a closed database`` (playtest 2026-04-25 [BUG-LOW]). The
                # room's store is closed via ``room.close_store()`` at room
                # teardown — not from per-session cleanup.
                #
                # Legacy non-slug path (no room) still closes its
                # per-session store here — it is owned by the session.
                if self._room is None:
                    with contextlib.suppress(Exception):
                        self._session_data.store.close()

    # ------------------------------------------------------------------
    # PLAYER_SEAT dispatch (MP-02 Task 5)
    # ------------------------------------------------------------------

    async def _handle_player_seat(self, msg: GameMessage) -> list[object]:
        """Handle PLAYER_SEAT — delegates to ``sidequest.handlers.player_seat.HANDLER``."""
        from sidequest.handlers.player_seat import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # DICE_THROW dispatch (story 34 port — restored for 2026-04-24 playtest)
    # ------------------------------------------------------------------

    async def _handle_dice_throw(self, msg: GameMessage) -> list[object]:
        """Handle DICE_THROW — delegates to ``sidequest.handlers.dice_throw.HANDLER``."""
        from sidequest.handlers.dice_throw import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # YIELD dispatch (dual-track momentum Phase 3)
    # ------------------------------------------------------------------

    async def _handle_yield(self, msg: GameMessage) -> list[object]:
        """Handle YIELD — delegates to ``sidequest.handlers.yield_action.HANDLER``."""
        from sidequest.handlers.yield_action import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # SESSION_EVENT dispatch
    # ------------------------------------------------------------------

    async def _handle_session_event(self, msg: GameMessage) -> list[object]:
        """Handle SESSION_EVENT — delegates to ``sidequest.handlers.session_event.HANDLER``."""
        from sidequest.handlers.session_event import HANDLER

        return await HANDLER.handle(self, msg)

    async def _handle_connect(
        self,
        payload: SessionEventPayload,
        player_id: str,
    ) -> list[object]:
        """Handle connect sub-event — delegates to ``sidequest.handlers.connect.HANDLER``."""
        from sidequest.handlers.connect import HANDLER

        return await HANDLER.handle(self, payload, player_id)

    # ------------------------------------------------------------------
    # CHARACTER_CREATION dispatch
    # ------------------------------------------------------------------

    async def _handle_character_creation(self, msg: GameMessage) -> list[object]:
        """Handle CHARACTER_CREATION — delegates to ``sidequest.handlers.character_creation.HANDLER``."""
        from sidequest.handlers.character_creation import HANDLER

        return await HANDLER.handle(self, msg)

    # ---- phase=scene ----------------------------------------------------
    def _chargen_scene(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        choice_str = payload.choice if payload.choice is not None else "1"

        resolved_index: int | None
        try:
            # 1-based numeric index (Rust: saturating_sub(1))
            n = int(choice_str)
            resolved_index = max(0, n - 1)
        except ValueError:
            # Label-match (case-insensitive) against the current scene's
            # choices. Only applies when we're in InProgress; AwaitingFollowup
            # has no choice list and will fall through to freeform below.
            resolved_index = None
            if builder.is_in_progress():
                current = builder.current_scene()
                for i, c in enumerate(current.choices):
                    if c.label.casefold() == choice_str.casefold():
                        resolved_index = i
                        break

        span.add_event(
            "character_creation.scene",
            {
                "phase": "scene",
                "choice_raw": choice_str,
                "resolved_index": str(resolved_index),
                "player_id": player_id,
            },
        )

        if resolved_index is not None:
            try:
                builder.apply_choice(resolved_index)
            except BuilderError as exc:
                return [_error_msg(f"Invalid choice: {exc!r}")]
        else:
            try:
                builder.apply_freeform(choice_str)
            except BuilderError as exc:
                return [_error_msg(f"Invalid freeform input: {exc!r}")]

        return self._next_message(builder, sd, player_id)

    # ---- phase=continue -------------------------------------------------
    def _chargen_continue(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        logger.info("chargen.continue player_id=%s", player_id)
        span.add_event(
            "character_creation.continue",
            {"phase": "continue", "player_id": player_id},
        )
        try:
            builder.apply_auto_advance()
        except BuilderError as exc:
            return [_error_msg(f"Cannot continue from current scene: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_assign ------------------------------------------
    def _chargen_arrange_assign(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        if payload.stat is None or payload.value is None:
            return [_error_msg("arrange_assign requires stat and value fields")]
        span.add_event(
            "character_creation.arrange_assign",
            {
                "stat": payload.stat,
                "value": payload.value,
                "player_id": player_id,
            },
        )
        try:
            builder.assign_stat(stat_name=payload.stat, value=payload.value)
        except PoolValueNotPresentError as exc:
            return [_error_msg(f"arrange_assign rejected: {exc!r}")]
        except BuilderError as exc:
            return [_error_msg(f"arrange_assign failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_clear -------------------------------------------
    def _chargen_arrange_clear(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        if payload.stat is None:
            return [_error_msg("arrange_clear requires stat field")]
        span.add_event(
            "character_creation.arrange_clear",
            {"stat": payload.stat, "player_id": player_id},
        )
        try:
            builder.clear_stat(stat_name=payload.stat)
        except BuilderError as exc:
            return [_error_msg(f"arrange_clear failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_confirm -----------------------------------------
    def _chargen_arrange_confirm(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.arrange_confirm",
            {"player_id": player_id},
        )
        try:
            builder.apply_arrangement_confirm()
        except UnfilledArrangementError as exc:
            return [_error_msg(f"arrange_confirm rejected: {exc!r}")]
        except NoQualifyingClassesError as exc:
            return [_error_msg(f"arrange_confirm has no qualifying class: {exc!r}")]
        except BuilderError as exc:
            return [_error_msg(f"arrange_confirm failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_reject ------------------------------------------
    def _chargen_arrange_reject(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.arrange_reject",
            {"player_id": player_id},
        )
        try:
            builder.apply_arrangement_reject()
        except BuilderError as exc:
            return [_error_msg(f"arrange_reject failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=story_autogen -------------------------------------------
    def _chargen_story_autogen(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        seed = payload.seed if payload.seed is not None else random.randint(0, 2**31 - 1)
        span.add_event(
            "character_creation.story_autogen",
            {"seed": seed, "player_id": player_id},
        )
        try:
            autogen_result = builder.autogen_backstory(seed=seed)
        except BuilderError as exc:
            return [_error_msg(f"story_autogen failed: {exc!r}")]
        # Render the next scene message and inject autogen_result into the
        # payload. Builder state remains in the_story (autogen is rerollable;
        # we don't commit it to the builder).
        msgs = self._next_message(builder, sd, player_id)
        if msgs and isinstance(msgs[0], CharacterCreationMessage):
            msgs[0].payload.autogen_result = autogen_result
        return msgs

    # ---- phase=story_confirm -------------------------------------------
    def _chargen_story_confirm(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.story_confirm",
            {
                "pronouns_present": bool(payload.pronouns),
                "background_present": bool(payload.background),
                "description_present": bool(payload.description),
                "player_id": player_id,
            },
        )
        story = StoryInput(
            pronouns=payload.pronouns or "",
            background=payload.background or "",
            description=payload.description or "",
        )
        try:
            builder.apply_response(story)
        except UnfilledArrangementError as exc:
            return [_error_msg(f"story_confirm rejected: {exc!r}")]
        except BuilderError as exc:
            return [_error_msg(f"story_confirm failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- archetype resolution helper (Story 2.3 Slice A) ----------------
    def _resolve_character_archetype(
        self,
        character: Character,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> None:
        """Resolve a raw ``jungian/rpg_role`` pair through the archetype shim.

        The builder encodes accumulated archetype hints as
        ``f"{jungian}/{rpg_role}"`` on ``character.resolved_archetype`` (see
        ``builder.py:1585-1590``). This helper detects that raw form, runs
        the four-tier resolve (base → constraints → world funnels), and
        replaces the raw pair with the resolved display name via
        ``apply_archetype_resolved`` — keeping ``archetype_provenance`` in
        lockstep.

        Resolution failures emit a ``character_creation.archetype_resolution_failed``
        span event and leave the raw pair in place. The downstream
        archetype-resolution gate in ``_chargen_confirmation`` (Story
        45-6, ``_gate_archetype_resolution``) detects the partial state
        and rejects the commit with a typed ERROR frame
        (``code="chargen_archetype_unresolved"``); this helper is
        no-op-on-failure intentionally so the gate can decide. Missing
        axis data on the pack (no ``base_archetypes`` or
        ``archetype_constraints``) also silently no-ops here — the
        gate then routes to ``OK_NO_AXES`` (pass) if the builder
        produced no pair, or ``raw_pair_unresolved`` (block) if the
        builder produced a pair the resolver couldn't run.

        Rust parity: ``connect.rs:1644-1737``.
        """
        raw = character.resolved_archetype
        if raw is None or "/" not in raw:
            return

        jungian, rpg_role = raw.split("/", 1)
        pack = sd.genre_pack
        if pack.base_archetypes is None or pack.archetype_constraints is None:
            return

        world = pack.worlds.get(sd.world_slug)
        funnels = world.archetype_funnels if world is not None else None

        try:
            resolution = resolve_archetype(
                jungian=jungian,
                rpg_role=rpg_role,
                base=pack.base_archetypes,
                constraints=pack.archetype_constraints,
                funnels=funnels,
                genre=sd.genre_slug,
                world=sd.world_slug,
            )
        except GenreValidationError as exc:
            span.add_event(
                "character_creation.archetype_resolution_failed",
                {
                    "event": "archetype.resolution_failed",
                    "error": str(exc),
                    "jungian": jungian,
                    "rpg_role": rpg_role,
                    "player_id": player_id,
                },
            )
            logger.warning(
                "chargen.archetype_resolution_failed jungian=%s rpg_role=%s error=%s",
                jungian,
                rpg_role,
                exc,
            )
            return

        apply_archetype_resolved(character, resolution)
        span.add_event(
            "character_creation.archetype_resolved",
            {
                "event": "archetype.resolved",
                "jungian": jungian,
                "rpg_role": rpg_role,
                "resolved_name": resolution.resolved.name,
                "source": resolution.source.value,
                "source_tier": resolution.provenance.source_tier.value,
                "weight": resolution.weight.value,
                "faction": resolution.resolved.faction or "none",
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        )

    # ---- archetype-resolution gate (Story 45-6) -------------------------
    def _gate_archetype_resolution(
        self,
        character: Character,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> tuple[bool, str | None]:
        """Inspect the post-resolve character state and decide whether
        chargen is allowed to ship.

        The gate distinguishes three states:

        - **OK_RESOLVED** — ``apply_archetype_resolved`` ran and stamped
          ``archetype_provenance``. The discriminator keys on
          ``character.archetype_provenance is not None``, NOT on the
          shape of ``resolved_archetype`` — that survives display
          names that legitimately contain ``"/"``. Pass.
        - **OK_NO_AXES** — ``resolved_archetype is None`` AND the pack
          opted out of the archetype system
          (``base_archetypes is None and archetype_constraints is None``).
          The builder didn't form a pair and the resolver had nothing
          to resolve. Pass.
        - **BLOCKED_PARTIAL** — anything else. Three failure modes
          (Story 45-6 / playtest 3 ``pumblestone`` corpus):

          1. ``raw_pair_unresolved`` — a literal ``"jungian/rpg_role"``
             string is still on the character AND the pack lacks axes.
             ``_resolve_character_archetype`` short-circuited at the
             pack-axes check (line 579) before calling
             ``resolve_archetype``; the raw pair stayed.
          2. ``missing_axes_with_pack_axes`` — pack declares axes but
             the chargen scenes accumulated at most one hint, so the
             builder set ``resolved_archetype = None``. This is the
             ``pumblestone`` failure: chargen scenes malformed.
          3. ``resolver_raised`` — pack has axes AND a raw pair is
             still on the character. Pure shape inference: the
             pack-lacks-axes short-circuit at line 579 would have
             returned before calling ``resolve_archetype``, so a raw
             pair with pack-axes-set can only mean the resolver was
             called and raised — the catch-block at line 595 caught
             ``GenreValidationError`` and returned, leaving the raw
             pair.

        The evaluator span fires on every chargen-confirm; the blocked
        span fires only on BLOCKED_PARTIAL. Both go to the GM panel via
        ``SPAN_ROUTES`` (Sebastien's lie-detector — CLAUDE.md OTEL
        Observability Principle). On the blocked branch a
        ``logger.warning()`` entry is also emitted to the structured
        server log so ops debugging is independent of the OTEL pipeline
        (python.md rule 4).

        Returns ``(is_blocked, block_reason)`` — ``block_reason`` is
        one of ``"raw_pair_unresolved"``,
        ``"missing_axes_with_pack_axes"``, ``"resolver_raised"``, or
        ``None`` on a pass.
        """
        pack = sd.genre_pack
        pack_has_axes = pack.base_archetypes is not None and pack.archetype_constraints is not None
        ra = character.resolved_archetype
        provenance_set = character.archetype_provenance is not None
        # ``had_both_hints`` is the only granularity the gate has access
        # to: the builder writes ``resolved_archetype = f"{j}/{r}"`` iff
        # both hints were set, else ``None``. After the resolver runs,
        # ``apply_archetype_resolved`` may overwrite the raw pair with a
        # display name — but ``apply_archetype_resolved`` only fires
        # when both hints fed the resolver in the first place. So:
        # ``ra is not None`` ⇒ both hints; ``ra is None`` ⇒ at most one
        # hint. The gate cannot tell *which* hint was missing without
        # threading the builder accumulator through, which would be
        # invasive — the OTEL signal is intentionally per-pair, not
        # per-axis.
        had_both_hints = ra is not None

        # Decide. The discriminator keys on ``provenance_set`` (the
        # OK_RESOLVED signal) rather than ``"/"`` in ``ra`` — the latter
        # would misclassify display names that legitimately contain
        # ``"/"`` (no validator on ``ArchetypeResolved.name`` forbids
        # it). ``apply_archetype_resolved`` writes both
        # ``resolved_archetype`` and ``archetype_provenance`` together,
        # so ``provenance_set`` is the durable lockstep signal.
        gate_state: str
        block_reason: str | None
        if provenance_set:
            # OK_RESOLVED — resolver succeeded and stamped provenance.
            gate_state = "ok_resolved"
            block_reason = None
        elif ra is None and not pack_has_axes:
            # OK_NO_AXES — pack opted out of the archetype system.
            gate_state = "ok_no_axes"
            block_reason = None
        elif ra is None:
            # ra is None AND pack_has_axes — pumblestone case (chargen
            # scenes malformed; the builder didn't form a pair).
            block_reason = "missing_axes_with_pack_axes"
            gate_state = "blocked_partial"
        else:
            # Raw pair on the character (no provenance ⇒ resolver did
            # not run successfully). With pack-axes-set the resolver
            # was called and raised; without pack-axes it short-
            # circuited at line 579.
            block_reason = "resolver_raised" if pack_has_axes else "raw_pair_unresolved"
            gate_state = "blocked_partial"

        # Evaluator span — fires on every chargen-confirm. ``state``
        # carries the decision so the GM panel sees the choice on every
        # path, including the success branches (negative confirmation
        # that the gate ran).
        with tracer.start_as_current_span(
            SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED,
            attributes={
                "state": gate_state,
                "resolved_archetype": ra if ra is not None else "",
                "pack_has_axes": pack_has_axes,
                "had_both_hints": had_both_hints,
                "provenance_set": provenance_set,
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        ):
            pass

        if block_reason is None:
            return False, None

        # python.md rule 4: error paths MUST log to the structured
        # server log surface. The OTEL span is independent (it goes to
        # the watcher dashboard / GM panel); this WARNING entry lands
        # in journald / file logs so ops debugging works without the
        # OTEL pipeline.
        logger.warning(
            "chargen.archetype_gate_blocked player_id=%s block_reason=%s "
            "genre=%s world=%s pack_has_axes=%s resolved_archetype=%s",
            player_id,
            block_reason,
            sd.genre_slug,
            sd.world_slug,
            pack_has_axes,
            ra if ra is not None else "<none>",
        )

        # Blocked span — fires only on BLOCKED_PARTIAL. This is the
        # explicit lie-detector entry that says "a chargen would have
        # shipped broken; the gate caught it." The legacy
        # ``character_creation.archetype_resolution_failed`` event is
        # the inner-resolver event; the blocked span is the outer-gate
        # event.
        with tracer.start_as_current_span(
            SPAN_CHARGEN_ARCHETYPE_GATE_BLOCKED,
            attributes={
                "state": "blocked_partial",
                "block_reason": block_reason,
                "resolved_archetype": ra if ra is not None else "",
                "pack_has_axes": pack_has_axes,
                "had_both_hints": had_both_hints,
                "provenance_set": provenance_set,
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        ) as gate_span:
            # Also record on the parent span for correlation in the
            # existing ``character_creation.*`` event stream.
            span.add_event(
                "character_creation.archetype_gate_blocked",
                {
                    "event": "archetype_gate_blocked",
                    "block_reason": block_reason,
                    "resolved_archetype": ra if ra is not None else "",
                    "pack_has_axes": pack_has_axes,
                    "player_id": player_id,
                },
            )
            # Mirror onto the gate span as well so ReadableSpan
            # consumers (test exporter) see the same payload either
            # way.
            gate_span.add_event(
                "character_creation.archetype_gate_blocked",
                {"block_reason": block_reason},
            )

        return True, block_reason

    # ---- phase=confirmation (commit) ------------------------------------
    async def _chargen_confirmation(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        # Name resolution: scene > lobby > "Player". Do NOT fall back to
        # payload.choice — that's the UI button index (e.g. "1"), not a
        # name (Rust comment at connect.rs:1607).
        name_from_scene = builder.character_name()
        char_name = name_from_scene or sd.player_name or "Player"
        source = "name_scene" if name_from_scene is not None else "player_name_fallback"
        span.add_event(
            "character_creation.name_resolved",
            {
                "event": "name_resolved",
                "char_name": char_name,
                "source": source,
                "player_id": player_id,
            },
        )

        try:
            character = builder.build(char_name)
        except BuilderError as exc:
            return [_error_msg(f"Character build failed: {exc!r}")]

        # ADR-014 / ADR-078: emit edge.current/.max instead of `hp` — the
        # field formerly labelled `hp` was already pulling from edge, so the
        # name was misleading the OTEL dashboard. `schema=adr-014` lets us
        # find the rename in audits.
        span.add_event(
            "character_creation.character_built",
            {
                "event": "character_built",
                "schema": "adr-014",
                "name": character.core.name,
                "class": character.char_class,
                "race": character.race,
                "edge_current": character.core.edge.current,
                "edge_max": character.core.edge.max,
                "player_id": player_id,
            },
        )

        # Archetype resolution (Story 2.3 Slice A). The builder writes
        # a raw "jungian/rpg_role" pair into ``resolved_archetype`` when
        # both axis hints were accumulated during chargen. Resolve it
        # through the four-tier shim (base → constraints → world funnels)
        # and replace the raw pair with the resolved display name, also
        # stamping ``archetype_provenance`` so the GM panel can show the
        # source tier. Rust parity: connect.rs:1644-1737.
        self._resolve_character_archetype(character, sd, player_id, span)

        # Story 45-6: archetype-resolution gate. After resolution runs
        # (or silently no-ops via one of the three early-return branches
        # at lines 574, 579, 595 of ``_resolve_character_archetype``),
        # this gate inspects the post-state and rejects partial
        # commits — the ``pumblestone`` regression from Playtest 3
        # evrópí. See ``_gate_archetype_resolution`` docstring for the
        # three pass / fail paths.
        is_blocked, block_reason = self._gate_archetype_resolution(character, sd, player_id, span)
        if is_blocked:
            return [
                _error_msg(
                    "Character creation incomplete: archetype resolution "
                    f"failed ({block_reason}). Please re-run chargen.",
                    code="chargen_archetype_unresolved",
                )
            ]

        # Starting equipment loadout (Story 2.3 Slice A). The builder only
        # holds item_hints; the class-specific loadout from inventory.yaml
        # is wired in here. Rust parity: connect.rs:1745-1864.
        # Story 45-12: pass session identity so the dedup-evaluated /
        # dedup-fired spans carry genre/world/player_id for GM-panel
        # attribution. Snapshot slugs are populated at connect time.
        apply_starting_loadout(
            character,
            sd.genre_pack.inventory,
            genre=sd.snapshot.genre_slug,
            world=sd.snapshot.world_slug,
            player_id=player_id,
        )

        # MP: peer may have already committed on the same slug. The
        # ADR-037 Python port has every WS session on a slug share the
        # room's canonical ``GameSnapshot`` reference, so an in-memory
        # check is the authoritative one. Disk reload here used to
        # paper over the snapshot-orphaning bug (sd.snapshot reassignment
        # below) by re-fetching the peer's persisted state, but the
        # peer's ``room.save()`` could only write the stale (empty)
        # bound snapshot — so the disk-read returned no characters and
        # the second player wrongly took the first-commit branch. With
        # the canonical snapshot mutated in place (``replace_with``
        # below), in-memory ``sd.snapshot.characters`` is always live.
        existing_chars: list = list(sd.snapshot.characters)
        is_first_commit = not existing_chars

        if is_first_commit:
            # World materialization (Story 2.3 Slice C / Rust connect.rs:1892).
            # Parse failure → empty-snapshot fallback (must not hard-fail mid-
            # commit when the character is already built).
            history_value = _world_history_value(sd.genre_pack, sd.world_slug)
            try:
                # Story 45-19: parse once at chargen and cache the typed
                # chapter list on _SessionData so the per-turn arc-recompute
                # tick doesn't re-parse history.yaml on every interaction.
                # Same input drives ``materialize_from_genre_pack`` below;
                # both calls share the same HistoryParseError fate.
                sd.cached_history_chapters = parse_history_chapters(history_value)
                materialized = materialize_from_genre_pack(
                    history_value,
                    CampaignMaturity.Fresh,
                    sd.genre_slug,
                    sd.world_slug,
                )
            except HistoryParseError as exc:
                # Loud failure (CLAUDE.md "No Silent Fallbacks"): log at
                # ERROR and emit an OTEL span event so the GM panel /
                # watcher dashboard surfaces the malformed shipping
                # content. We retain the empty-snapshot fallback because
                # the character has already been chargen-built and
                # hard-failing here would orphan the commit; but the
                # error is no longer invisible.
                logger.error(
                    "world_materialization.parse_failed genre=%s world=%s error=%s",
                    sd.genre_slug,
                    sd.world_slug,
                    exc,
                    exc_info=True,
                )
                span.add_event(
                    "history.parse_failed",
                    {
                        "event": "history.parse_failed",
                        "genre": sd.genre_slug,
                        "world": sd.world_slug,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                        "exception_repr": repr(exc),
                    },
                )
                materialized = GameSnapshot(genre_slug=sd.genre_slug, world_slug=sd.world_slug)
                # Parse failed: cache an empty chapter list so the
                # arc-recompute tick is a graceful no-op rather than
                # propagating the parse error per turn.
                sd.cached_history_chapters = []
            # Canned-openings Phase 3 (Task 13): pre-load authored NPCs
            # before the chargen character is attached. Gate inside
            # ``preload_authored_npcs`` checks
            # ``state.characters == [] AND turn_manager.interaction == 0``;
            # the materialized snapshot satisfies both at this seam (the
            # PC is appended on the next line). Resumed sessions never
            # reach this branch — ``is_first_commit`` is False.
            world_for_authored = sd.genre_pack.worlds.get(sd.world_slug)
            if world_for_authored is not None:
                preload_authored_npcs(materialized, list(world_for_authored.authored_npcs))
            # Discard the "Adventurer" placeholder the fresh chapter may
            # author — the chargen-built character owns that slot.
            materialized.characters = [character]
            # Wave 2B (story 45-48): the chapter's location was previously
            # written to the materialized snapshot's ``location`` field;
            # that field is gone. Backfill the now-attached PC's
            # per-character entry from the latest chapter that authored a
            # ``location`` so chargen-confirmation lands the player at the
            # scene the chapter described. Room-graph worlds overwrite this
            # below via ``init_room_graph_location`` (entrance room id),
            # which is intentional — the chapter's free-text location is
            # the fallback for non-room-graph worlds.
            for ch in reversed(materialized.world_history):
                if ch.location:
                    materialized.character_locations[character.core.name] = ch.location
                    break
            # Mutate the canonical room snapshot in place rather than
            # reassigning ``sd.snapshot``. Reassignment orphans the
            # ``room._snapshot`` reference: ``room.save()`` then
            # persists the stale pre-chargen snapshot, the next
            # connecting peer loads an empty save, treats themselves
            # as first-commit, materializes their own world, and the
            # slug ends up running two parallel solo games. Mutating
            # in place keeps every existing ``sd.snapshot`` /
            # ``room.snapshot`` reference live and pointing at the
            # same authoritative object — including any peer session
            # already bound to this slug.
            sd.snapshot.replace_with(materialized)

            # S1 (2026-05-04 split-brain cleanup): magic_state MUST be
            # initialized before init_chassis_registry runs, because the
            # chassis loader now writes confrontations directly into
            # snapshot.magic_state.confrontations (the canonical home).
            # The legacy world_confrontations stash is gone — see design
            # spec 2026-05-04-snapshot-split-brain-cleanup-design.md S1.
            #
            # Magic Phase 4: instantiate MagicState on the canonical
            # snapshot for worlds that ship a magic.yaml pair (genre +
            # world). This is the production hook that pairs Phase 1's
            # loader with Phase 2's snapshot field — without it,
            # snapshot.magic_state stays None and the LedgerPanel never
            # surfaces bars even though the engine can apply workings
            # correctly. ``add_character`` instantiates per-character
            # bars (sanity / notice / vitality on Coyote Star) keyed
            # to the actor name the narrator emits in magic_working.
            init_magic_state_for_session(
                snapshot=sd.snapshot,
                genre_pack_source_dir=sd.genre_pack.source_dir,
                world_slug=sd.world_slug,
                character_id=character.core.name,
                character_class=character.char_class,
            )

            init_chassis_registry(sd.snapshot, sd.genre_pack)
            # Story 47-6: bond_seeds in rigs.yaml use the
            # ``"player_character"`` placeholder. Rewrite every chassis's
            # bond_ledger to the real chargen character id so
            # process_room_entry can find the bond on later transitions.
            rebind_chassis_bonds_to_character(sd.snapshot, character.core.name)
            # Story 47-6: evaluate room-entry eligibility for the starting
            # interior_room so a session that opens INTO the galley fires
            # the_tea_brew on turn 1 instead of waiting for a later
            # narrator-driven location update.
            process_session_open(
                sd.snapshot,
                character_id=character.core.name,
                current_turn=sd.snapshot.turn_manager.interaction,
            )
            span.add_event(
                "character_creation.world_materialized",
                {
                    "event": "world_materialized",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "chapters_applied": len(materialized.world_history),
                    "maturity": materialized.campaign_maturity,
                    "trigger": "new_player_chargen",
                    "player_id": player_id,
                },
            )

            # Scenario binding (Slice D / connect.rs:1948). No-op without
            # scenarios; sets active_scenario on the session for later
            # pressure / scene-budget / accusation consumers.
            bind_result = bind_scenario(
                sd.genre_pack,
                sd.snapshot,
                genre_slug=sd.genre_slug,
                world_slug=sd.world_slug,
            )
            if bind_result is not None:
                _, active_pack = bind_result
                sd.active_scenario = active_pack

            world = sd.genre_pack.worlds.get(sd.world_slug)

            # Region init (Story 37-31). Runs for every world with
            # cartography. Init errors are pack-authoring bugs — log loud,
            # don't hard-fail the confirmation frame.
            if world is not None:
                try:
                    region_id = init_region_location(sd.snapshot, world.cartography)
                    span.add_event(
                        "region.initialized",
                        {
                            "event": "region.initialized",
                            "region": region_id,
                            "mode": world.cartography.navigation_mode.value,
                            "source": "starting_region",
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                        },
                    )
                    logger.info(
                        "region.init genre=%s world=%s region=%s discovered_regions=%d",
                        sd.genre_slug,
                        sd.world_slug,
                        region_id,
                        len(sd.snapshot.discovered_regions),
                    )
                except RegionInitError as exc:
                    logger.error(
                        "region.init_failed genre=%s world=%s error=%s",
                        sd.genre_slug,
                        sd.world_slug,
                        exc,
                    )
                    span.add_event(
                        "region.init_failed",
                        {
                            "event": "region.init_failed",
                            "mode": world.cartography.navigation_mode.value,
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                            "error": str(exc),
                        },
                    )

            if (
                world is not None
                and world.cartography.navigation_mode == NavigationMode.room_graph
                and world.cartography.rooms
            ):
                try:
                    entrance_id = init_room_graph_location(
                        sd.snapshot, list(world.cartography.rooms)
                    )
                    span.add_event(
                        "location.initialized",
                        {
                            "event": "location.initialized",
                            "location": entrance_id,
                            "mode": "room_graph",
                            "source": "entrance_room",
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                        },
                    )
                    logger.info(
                        "room_graph.init genre=%s world=%s entrance=%s discovered_rooms=%d",
                        sd.genre_slug,
                        sd.world_slug,
                        entrance_id,
                        len(sd.snapshot.discovered_rooms),
                    )

                    # ADR-096 Task 20b: emit TACTICAL_GRID for the entrance room so
                    # the UI Automapper has grid data from session start, not only on
                    # the first narrator-driven location change.
                    def _chargen_emit_tactical_grid(msg: object, _kind: str) -> None:
                        out.append(msg)

                    _maybe_emit_tactical_grid(
                        self,
                        sd=sd,
                        snapshot=sd.snapshot,
                        actor=None,
                        emit_fn=_chargen_emit_tactical_grid,
                        room_id_override=entrance_id,
                    )
                    # Story 54-2 / ADR-109: emit LOCATION_DESCRIPTION on the
                    # chargen / session-resume init path. room_id_override
                    # carries the entrance room so a resumed client gets the
                    # manifest snapshot without polling.
                    _maybe_emit_location_description(
                        self,
                        sd=sd,
                        snapshot=sd.snapshot,
                        actor=None,
                        emit_fn=_chargen_emit_tactical_grid,
                        room_id_override=entrance_id,
                    )
                except RoomGraphInitError as exc:
                    logger.error(
                        "room_graph.init_failed genre=%s world=%s error=%s",
                        sd.genre_slug,
                        sd.world_slug,
                        exc,
                    )
                    span.add_event(
                        "location.init_failed",
                        {
                            "event": "location.init_failed",
                            "mode": "room_graph",
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                            "error": str(exc),
                        },
                    )
        else:
            # MP second commit. ADR-037 Python port: sd.snapshot is the
            # canonical room snapshot (already populated by the first
            # committer's bind_world); just append our PC if not already
            # present. No reload from store — the in-memory snapshot is
            # authoritative.
            existing_names = {c.core.name for c in sd.snapshot.characters}
            if character.core.name not in existing_names:
                sd.snapshot.characters.append(character)
            # Pingpong 2026-05-07 ("magic.init only fires for the host"):
            # the host's chargen-complete branch (above) calls
            # ``init_magic_state_for_session`` to register the actor in
            # the magic ledger AND emit the ``magic.init`` OTEL span;
            # MP joiners never reached that call site. Result: late
            # joiners (Donut, Katia) had no actor row in
            # ``snapshot.magic_state.ledger``, so any narrator-emitted
            # working against them raised ``unknown actor; call
            # add_character first`` (the same shape as the 2026-04-30
            # bug fixed in magic_init.py — the idempotence is now
            # required at TWO seams, not one). Mirror the call here so
            # every committer gets ``add_character`` + the
            # observable ``magic.init`` span. Idempotent on
            # ``snapshot.magic_state`` per the existing
            # first_commit-vs-reuse branch.
            init_magic_state_for_session(
                snapshot=sd.snapshot,
                genre_pack_source_dir=sd.genre_pack.source_dir,
                world_slug=sd.world_slug,
                character_id=character.core.name,
                character_class=character.char_class,
            )
            # Re-bind active_scenario on this socket from whatever the
            # peer wrote — its presence on sd.active_scenario is what
            # downstream pressure / accusation code keys off.
            if sd.active_scenario is None:
                bind_result = bind_scenario(
                    sd.genre_pack,
                    sd.snapshot,
                    genre_slug=sd.genre_slug,
                    world_slug=sd.world_slug,
                )
                if bind_result is not None:
                    _, active_pack = bind_result
                    sd.active_scenario = active_pack
            span.add_event(
                "character_creation.mp_world_reused",
                {
                    "event": "mp_world_reused",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "existing_pc_count": len(existing_chars),
                    "appended_pc": character.core.name,
                    "total_pc_count": len(sd.snapshot.characters),
                    "player_id": player_id,
                },
            )
            logger.info(
                "session.mp_second_commit genre=%s world=%s existing_pcs=%d new_pc=%s total=%d",
                sd.genre_slug,
                sd.world_slug,
                len(existing_chars),
                character.core.name,
                len(sd.snapshot.characters),
            )

        # Pingpong 2026-04-30 ("Lore RAG returns empty_query_or_store
        # for all 4 PCs every turn — no genre lore reaching narration"):
        # the genre pack's ``Lore`` corpus and the world's ``WorldLore``
        # were never seeded into the per-session lore store — only
        # chargen-choice fragments via ``seed_lore_from_char_creation``.
        # Result: every ``lore_embedding.retrieve`` came back with
        # ``store_size=0 outcome=empty_query_or_store`` and the narrator
        # composed every turn with zero hits from the genre lore corpus.
        # Pure wiring fix: ``seed_lore_from_genre_pack`` already existed
        # (sidequest/game/lore_seeding.py:46) and was unit-tested but
        # had zero production callers — exactly the
        # "Don't Reinvent — Wire Up What Exists" gap CLAUDE.md warns
        # about. Added a sibling ``seed_lore_from_world`` to cover the
        # world-level lore.yaml (overrides the genre pack's defaults
        # for that specific world; e.g. ``coyote_star`` has its own
        # history/geography/factions distinct from ``space_opera``'s).
        # Both run BEFORE ``seed_lore_from_char_creation`` so the
        # genre/world fragments land first; chargen choices layer on
        # top with ``Character`` category so they're scoped distinctly
        # in the LoreStore index. Idempotent: re-seeding on a reconnect
        # silently skips duplicate ids (``DuplicateLoreId`` guard).
        # OTEL lie-detector: per the user's pingpong note request, expose
        # ``lore.store_loaded count=N world=X`` so the GM panel can
        # distinguish "lore is empty by design for this scenario" from
        # "lore was supposed to load and didn't" — both pre-fix manifest
        # as ``outcome=empty_query_or_store`` at retrieve time. Sebastien's
        # State / Subsystems tabs read this watcher event. Shared with the
        # slug-resume connect path via ``seed_world_lore`` so a resumed
        # save's lore_store is re-seeded with the SAME deterministic
        # genre+world fragments and emits the SAME ``lore_store_loaded``
        # event (the resume gap commit 72750db exposed).
        def _emit_lore_store_loaded(
            *,
            genre_fragments_added: int,
            world_fragments_added: int,
            total_fragments: int,
            total_tokens: int,
        ) -> None:
            logger.info(
                "lore.store_loaded genre=%s world=%s genre_fragments=%d "
                "world_fragments=%d total=%d total_tokens=%d",
                sd.genre_slug,
                sd.world_slug,
                genre_fragments_added,
                world_fragments_added,
                total_fragments,
                total_tokens,
            )
            _watcher_publish(
                "lore_store_loaded",
                {
                    "genre_slug": sd.genre_slug,
                    "world_slug": sd.world_slug,
                    "genre_fragments_added": genre_fragments_added,
                    "world_fragments_added": world_fragments_added,
                    "total_fragments": total_fragments,
                    "total_tokens": total_tokens,
                    "player_id": player_id,
                },
                component="rag",
            )

        genre_lore_added, world_lore_added = seed_world_lore(
            sd.lore_store,
            sd.genre_pack,
            sd.world_slug,
            emit=_emit_lore_store_loaded,
        )

        # Lore seeding (Slice F / connect.rs:2196). Must run BEFORE
        # clearing the builder — the seeder reads scene choices to
        # build Character-category lore fragments for narrator RAG.
        lore_added = seed_lore_from_char_creation(sd.lore_store, list(builder.scenes()))
        span.add_event(
            "lore.char_creation_seeded",
            {
                "event": "char_creation_lore_seeded",
                "fragments_added": lore_added,
                "total_fragments": len(sd.lore_store),
                "total_tokens": sd.lore_store.total_tokens(),
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        )
        logger.info(
            "rag.character_creation_lore_seeded count=%d total=%d",
            lore_added,
            len(sd.lore_store),
        )
        _watcher_publish(
            "lore_retrieval",
            {
                "reason": "character_creation_seed",
                "fragments_added": lore_added,
                "total_fragments": len(sd.lore_store),
                "total_tokens": sd.lore_store.total_tokens(),
                "genre_slug": sd.genre_slug,
                "world_slug": sd.world_slug,
                "player_id": player_id,
            },
            component="rag",
        )

        # NPC registry reset removed in story 45-52. The pre-Wave-2A clear
        # targeted the legacy ``npc_registry`` (chargen-tier name extractions
        # — player's own name, lobby filler). With the registry dropped and
        # the post-Wave-2A pool channel intentionally persistent across
        # chargen (world-authored cast pre-seeds; recurring narrator-cited
        # NPCs survive the seam), there is nothing to clear here.

        # MP per-player chargen binding (playtest 2026-04-25). Maps
        # player_id → character_name so slug-resume routes new players
        # to chargen instead of auto-claiming an existing PC.
        if sd.player_id and character.core.name:
            sd.snapshot.player_seats[sd.player_id] = character.core.name
            # Wave 2B (story 45-48) + sq-playtest 2026-05-09 [OBS]: seed
            # the joiner's per-character location from another seated PC.
            # The original branch used strict ``party_location()`` consensus
            # (all seated PCs agree on the same location), which fails the
            # moment a new player commits because *their* slot is still
            # empty — so MP commits 2..N never inherited and the
            # post-chargen window had no consensus at all. Loose
            # inheritance: take any other seated PC's known location. The
            # chargen-complete bootstrap from
            # ``_bootstrap_character_locations_from_opening`` ensures the
            # FIRST committer has a location to inherit.
            if character.core.name not in sd.snapshot.character_locations:
                inherited_from: str | None = None
                inherited_loc: str | None = None
                for seated_name in sd.snapshot.player_seats.values():
                    if not seated_name or seated_name == character.core.name:
                        continue
                    candidate = sd.snapshot.character_locations.get(seated_name)
                    if candidate:
                        inherited_from = seated_name
                        inherited_loc = candidate
                        break
                if inherited_loc:
                    sd.snapshot.character_locations[character.core.name] = inherited_loc
                    span.add_event(
                        "snapshot.character_location_inherited",
                        {
                            "event": "snapshot.character_location_inherited",
                            "joiner": character.core.name,
                            "inherited_from": inherited_from or "",
                        },
                    )
            span.add_event(
                "session.player_seat_bound",
                {
                    "event": "session.player_seat_bound",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": sd.player_id,
                    "character_name": character.core.name,
                    "seat_count": len(sd.snapshot.player_seats),
                },
            )
            logger.info(
                "session.player_seat_bound player_id=%s character=%s seat_count=%d",
                sd.player_id,
                character.core.name,
                len(sd.snapshot.player_seats),
            )
            _watcher_publish(
                "session_player_seat_bound",
                {
                    "genre_slug": sd.genre_slug,
                    "world_slug": sd.world_slug,
                    "player_id": sd.player_id,
                    "character_name": character.core.name,
                    "seat_count": len(sd.snapshot.player_seats),
                    "seated_player_ids": list(sd.snapshot.player_seats.keys()),
                },
                component="session",
            )

        # Persist (Slice G / connect.rs:2174). Snapshot save makes the
        # next slug-resume hit has_character=True; failure must not
        # strand the player mid-commit (log loud, continue).
        try:
            # ADR-037 Python port: route through the canonical room save
            # so concurrent chargen-commits from peers are serialized by
            # the room lock. Fallback for legacy non-slug paths.
            if self._room is not None:
                self._room.save()
            else:
                sd.store.save(sd.snapshot)
            span.add_event(
                "session.persisted_at_chargen_complete",
                {
                    "event": "session.persisted",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player": sd.player_name,
                    "turn": sd.snapshot.turn_manager.interaction,
                },
            )
            logger.info(
                "session.persisted_at_chargen_complete genre=%s world=%s player=%s",
                sd.genre_slug,
                sd.world_slug,
                sd.player_name,
            )
        except Exception as exc:
            # Don't strand mid-commit — log + OTEL event, then proceed.
            # On next reconnect the save will be absent so chargen repeats.
            logger.error(
                "session.persist_failed_at_chargen_complete genre=%s world=%s error=%s",
                sd.genre_slug,
                sd.world_slug,
                exc,
            )
            span.add_event(
                "session.persist_failed_at_chargen_complete",
                {
                    "event": "session.persist_failed",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player": sd.player_name,
                    "error": str(exc),
                },
            )

        # Flip to Playing atomically with persistence (Slice G /
        # connect.rs:2183). Pre-fix, a disconnect between confirm and
        # first action lost the save-state flag.
        self._state = _State.Playing

        # Story 45-2: chargen committed → seat transitions CHARGEN → PLAYING
        # so the turn barrier counts this peer. No-op if no room (solo
        # path with no MP room) or if the peer was already PLAYING (safe
        # under double-confirmation).
        if self._room is not None:
            self._room.transition_to_playing(player_id)

        sd.builder = None
        # ADR-014 / ADR-078: HP was removed in favor of EdgePool (composure).
        # Log surface-level mechanical state as edge=current/max so playtest
        # logs match the actual schema instead of leaking a stale `hp=N` field.
        # `schema=adr-014` is grep-able so future regressions (re-introduction
        # of an `hp` integer on CreatureCore) are auditable.
        logger.info(
            "chargen.complete schema=adr-014 char_name=%s class=%s race=%s edge=%d/%d",
            character.core.name,
            character.char_class,
            character.race,
            character.core.edge.current,
            character.core.edge.max,
        )

        payload = CharacterCreationPayload(
            phase="complete",
            total_scenes=builder.total_scenes(),
            character=character.model_dump(mode="json"),
        )
        out: list[object] = [CharacterCreationMessage(payload=payload, player_id=player_id)]

        # PARTY_STATUS snapshot (Slice H / connect.rs:2533). Lands the
        # Character tab populated at session-start. MP: also broadcast
        # to peers so they see the new arrival without waiting for their
        # own turn-end refresh.
        try:
            party_status_msg = views.build_session_start_party_status(
                self, sd, character, player_id
            )
            out.append(party_status_msg)
            from sidequest.game.persistence import (  # noqa: PLC0415 — break import cycle
                GameMode as _GameMode,
            )

            if (
                self._room is not None
                and sd.mode == _GameMode.MULTIPLAYER
                and self._socket_id is not None
            ):
                self._room.broadcast(party_status_msg, exclude_socket_id=self._socket_id)
            span.add_event(
                "session.start.character_snapshot_emitted",
                {
                    "event": "session.start.character_snapshot_emitted",
                    "player_id": player_id,
                    "character_name": character.core.name,
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "sheet_class": character.char_class,
                    "inventory_count": len(
                        [
                            i
                            for i in character.core.inventory.items
                            if str(i.get("state", "Carried")) == "Carried"
                        ]
                    ),
                },
            )
        except Exception as exc:
            # Snapshot frame is UI convenience — log loud, don't block.
            logger.error(
                "session.start.character_snapshot_failed player=%s error=%s",
                sd.player_name,
                exc,
            )
            span.add_event(
                "session.start.character_snapshot_failed",
                {
                    "event": "session.start.character_snapshot_failed",
                    "error": str(exc),
                    "player_id": player_id,
                },
            )

        # Canned-openings Phase 4 (Task 19): resolve + stash the
        # opening directive at chargen-completion.
        #
        # Playtest 2026-05-03 [BUG] Opening narration skips Kestrel
        # beat: previously gated on ``is_first_commit``, which fired
        # the resolver when only 1 PC was seated in MP — the canned
        # ``mp_galley_jumprest`` opening declares ``min_players: 2``
        # so resolution always failed and the narrator improvised
        # Vaskov Centrum customs. Now the populator runs on EVERY
        # commit (idempotent — bails when ``opening_directive`` is
        # already set, emits ``opening.skipped`` watcher events
        # otherwise), so the second committer's call succeeds with
        # the full party seated.
        _populate_opening_directive_on_chargen_complete(
            session_data=sd,
            snapshot=sd.snapshot,
            pack=sd.genre_pack,
            world_slug=sd.world_slug,
            mode=sd.mode,
        )

        # Opening-turn bootstrap (Slice H / connect.rs:2270). Fires
        # narrator with opening_seed + opening_directive (Early zone),
        # consumed once so subsequent turns run directive-free.
        #
        # Deferral gate (playtest 2026-05-03): in MP, skip the opening
        # narration on first commit so the canned MP opening can
        # resolve at second commit with full party context. The last
        # committer's narration is broadcast to peers below so the
        # first committer still sees the opening.
        if _should_fire_opening_narration(sd, self._room):
            opening_messages = await self._run_opening_turn_narration(sd, player_id, span)
            out.extend(opening_messages)
            # MP: broadcast opening narration to peers so the earlier
            # committer (whose chargen.complete was deferred) sees the
            # canned opening when it finally fires here. Solo path
            # (room is None or seat_count <= 1) skips the broadcast.
            from sidequest.game.persistence import (  # noqa: PLC0415 — break import cycle
                GameMode as _GameMode2,
            )

            if (
                self._room is not None
                and sd.mode == _GameMode2.MULTIPLAYER
                and self._socket_id is not None
                and opening_messages
            ):
                for _msg in opening_messages:
                    self._room.broadcast(_msg, exclude_socket_id=self._socket_id)
                _watcher_publish(
                    "opening.broadcast_to_peers",
                    {
                        "genre": sd.genre_slug,
                        "world": sd.world_slug,
                        "player_id": player_id,
                        "message_count": len(opening_messages),
                    },
                    component="opening_hook",
                    severity="info",
                )
        else:
            # Defer: don't fire opening narration yet — the next
            # committer will populate the directive and fire it for
            # everyone via the broadcast path above.
            seat_count = self._room.non_abandoned_player_count() if self._room is not None else 0
            _watcher_publish(
                "opening.deferred_party_incomplete",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": player_id,
                    "characters_committed": len(sd.snapshot.characters),
                    "non_abandoned_seats": seat_count,
                },
                component="opening_hook",
                severity="info",
            )
            logger.info(
                "opening.deferred_party_incomplete genre=%s world=%s player=%s "
                "committed=%d expected=%d",
                sd.genre_slug,
                sd.world_slug,
                sd.player_name,
                len(sd.snapshot.characters),
                seat_count,
            )
        return out

    # ---- helper: next scene message OR confirmation summary -------------
    def _next_message(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
    ) -> list[object]:
        """After apply_choice / apply_freeform / apply_auto_advance, emit the
        appropriate next frame: either the next scene message, or the
        confirmation summary if the builder has transitioned to Confirmation.

        Rust parity: ``dispatch::chargen_summary::render_confirmation_summary``
        takes the pack + lobby_name directly because the builder does not
        own them.
        """
        if builder.is_confirmation():
            return [render_confirmation_summary(builder, sd.genre_pack, sd.player_name, player_id)]
        return [builder.to_scene_message(player_id)]

    # ------------------------------------------------------------------
    # PLAYER_ACTION dispatch
    # ------------------------------------------------------------------

    async def _handle_player_action(self, msg: GameMessage) -> list[object]:
        """Handle PLAYER_ACTION — delegates to ``sidequest.handlers.player_action.HANDLER``."""
        from sidequest.handlers.player_action import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # Narration execution — shared between player_action and opening turn
    # ------------------------------------------------------------------

    async def _execute_narration_turn(
        self,
        sd: _SessionData,
        action: str,
        turn_context: TurnContext,
        *,
        is_opening_turn: bool = False,
    ) -> list[object]:
        """Run one narration turn: orchestrator call, snapshot mutation,
        persistence, NARRATION + NARRATION_END message build.

        Shared by :meth:`_handle_player_action` and
        :meth:`_run_opening_turn_narration` (Story 2.3 Slice H). The
        caller owns TurnContext construction so each entrypoint can
        set per-turn fields (opening_directive on turn 0, pending
        trope beats on subsequent turns) without leaking responsibility.

        ``is_opening_turn`` (Story 45-5 / ADR-051): the chargen-confirmation
        narration sets the scene at round=1 / interaction=1 rather than
        completing a player-narrator exchange. Skipping
        ``record_interaction()`` for this turn keeps both counters at
        their fresh ``materialize_from_genre_pack`` defaults so post-
        chargen state is exactly ``(round=1, interaction=1)``. The
        narrative_log row this turn writes uses the pre-bump
        ``interaction=1``, so the 45-11 ``round_invariant`` (round ==
        MAX(narrative_log.round_number)) still holds. The first
        PLAYER_ACTION turn is the first real exchange and advances both
        counters in lockstep.
        """
        snapshot = sd.snapshot
        snapshot_before_hash = _hash_snapshot(snapshot)
        # Reuse a pre-built PhaseTimings from the calling handler when
        # one is attached — handler-entry construction lets pre-narrator
        # phases (lore_retrieval, mp_barrier_wait, turn_context_build)
        # land in the same `phase_durations_ms` dict the dashboard reads.
        # When the caller didn't attach one (test fixtures, legacy paths),
        # fall back to constructing here so the existing in-turn phases
        # still record.
        if isinstance(turn_context.phase_timings, PhaseTimings) and (
            turn_context.phase_timings is not PhaseTimings.NULL
        ):
            timings = turn_context.phase_timings
        else:
            timings = PhaseTimings(action_received_monotonic=time.monotonic())
            turn_context.phase_timings = timings
        submitted = False
        # Story 45-20: capture trope-status baseline BEFORE any apply step
        # mutates statuses. The handshake fires post-record_interaction and
        # diffs this baseline against the live snapshot to detect any trope
        # whose status flipped to "resolved" — chapter promotion (today),
        # narrator extraction or engine tick (future). Capturing late
        # would mask the diff.
        trope_status_baseline: dict[str, str] = {t.id: t.status for t in snapshot.active_tropes}
        # Capture the watcher→OTLP synthetic-span counter at turn start so the
        # finally-block can log the per-turn delta. With this in the server
        # log a `grep turn.bridge_diagnostic /tmp/sidequest-server.log` reveals
        # whether the bridge minted any spans for this turn — Jaeger-empty
        # turns now have a hard, grep-able truth-value rather than a "did the
        # bridge fire?" guessing game (playtest 2026-04-30 #Jaeger-bridge).
        from sidequest.telemetry.watcher_hub import synthetic_spans_count  # noqa: PLC0415

        bridge_minted_at_start = synthetic_spans_count()
        try:
            with turn_span(
                turn_id=snapshot.turn_manager.interaction,
                player_id=sd.player_id,
                agent_name="narrator",
                genre=sd.genre_slug,
                world=sd.world_slug,
                action_len=len(action),
            ):
                # LocalDM is dormant on the live turn path as of 2026-04-28
                # (docs/superpowers/specs/2026-04-28-localdm-offline-only-design.md).
                # turn_context.dispatch_package stays None; build_narrator_prompt's
                # is-None guards skip redaction and the dispatch bank.

                # Monster Manual injection (ADR-059, port of Rust
                # dispatch/mod.rs:643-681). Materialize Manual NPCs and
                # encounter creatures into snapshot.npcs BEFORE the
                # narrator runs so the gaslighting doctrine path
                # delivers them as world truth — never as appended
                # "available list" text.  TurnContext was already built
                # in the handler from the pre-injection snapshot, so
                # refresh its ``npcs`` reference from the post-patch
                # snapshot before dispatch.
                from sidequest.server.dispatch import monster_manual_inject

                manual = monster_manual_inject.ensure_loaded(sd)
                if manual is not None:
                    mm_location = (
                        turn_context.current_location
                        if isinstance(turn_context.current_location, str)
                        else ""
                    )
                    mm_injected = monster_manual_inject.inject(
                        sd,
                        snapshot,
                        current_location=mm_location,
                        in_combat=bool(turn_context.in_combat),
                    )
                    # Honest plain-text signal (CLAUDE.md OTEL principle).
                    # monster_manual.injected is an OTEL-only span; a GM
                    # reading server.log otherwise has zero proof the
                    # Manual materialized vs. the narrator improvising
                    # creatures (Pattern 5). Logs the actual patch count,
                    # not a presence boolean.
                    logger.info(
                        "monster_manual.injected genre=%s world=%s "
                        "player_id=%s turn=%s in_combat=%s patches=%d",
                        sd.genre_slug,
                        sd.world_slug,
                        sd.player_id,
                        turn_context.turn_number,
                        bool(turn_context.in_combat),
                        mm_injected,
                    )
                    turn_context.npcs = list(snapshot.npcs)
                    # _build_turn_context (session_helpers.py) snapshots
                    # monster_manual off sd.monster_manual at the *caller*,
                    # which runs BEFORE ensure_loaded() above lazily
                    # populates it. Without this refresh the orchestrator
                    # gets context.monster_manual=None on every session's
                    # turn 1 and every MP turn where the acting player's
                    # per-player _SessionData has not yet narrated —
                    # lookup_monster goes dead and the context_wired OTEL
                    # flag logs a false negative (playtest 2026-05-17).
                    turn_context.monster_manual = sd.monster_manual

                with orchestrator_process_action_span(action_len=len(action)):
                    result = await sd.orchestrator.run_narration_turn(
                        action, turn_context, room=self._room
                    )

                logger.info(
                    "session.narration_complete genre=%s world=%s degraded=%s duration_ms=%s",
                    sd.genre_slug,
                    sd.world_slug,
                    result.is_degraded,
                    result.agent_duration_ms,
                )

                # Capture encounter state BEFORE applying the narration result so we
                # can detect transitions (live→resolved) after the dispatch below and
                # emit the corresponding state_transition / resolve events.
                prior_encounter = snapshot.encounter
                prior_live = prior_encounter is not None and not prior_encounter.resolved
                prior_type = prior_encounter.encounter_type if prior_encounter else None

                # Unified dispatch — passes the pack so encounter instantiation /
                # beat application / resolution happen in one place (emits the
                # Story-3.4 OTEL spans the GM panel reads).
                #
                # ADR-074 dice integration — read the most recent dice outcome
                # stashed by the DICE_THROW handler (if any) and classify it as
                # success/failure for the beat application. Uses getattr so this
                # stays forward-compatible with the in-flight ``pending_roll_outcome``
                # field on ``_SessionData`` that OQ-2 is landing in parallel —
                # when the field is absent the call is a no-op.
                with timings.phase("state_apply"):
                    dice_outcome = getattr(sd, "pending_roll_outcome", None)
                    dice_failed: bool | None = None
                    if dice_outcome is not None:
                        outcome_name = getattr(dice_outcome, "name", None) or str(dice_outcome)
                        dice_failed = outcome_name in ("Fail", "CritFail")
                    dice_actor: str | None = getattr(sd, "pending_roll_actor", None)
                    opposed_player_d20: int | None = getattr(
                        sd,
                        "pending_opposed_player_d20",
                        None,
                    )
                    opposed_player_beat_id: str | None = getattr(
                        sd,
                        "pending_opposed_player_beat_id",
                        None,
                    )
                    if sd._room is None:
                        # Slug-connect branch always sets _room; this is
                        # a programming-error path. Surface as a hard error.
                        raise RuntimeError(
                            "_apply_narration_result_to_snapshot: sd._room "
                            "is None — slug-connect wiring missing"
                        )
                    # Story 45-30: capture pre-apply state so the render
                    # trigger policy classifier can detect SCENE_CHANGE
                    # (location differs from prior turn) and ENCOUNTER_RESOLVED
                    # (encounter transitioned from unresolved to resolved this
                    # turn). Wave 2B (story 45-48): the comparison is now
                    # per-character — capture the acting PC's current
                    # location, not the removed party-level field.
                    _acting_for_render_trigger = _resolve_acting_character_name(sd, sd._room)
                    snapshot_location_before_apply = snapshot.party_location(
                        perspective=_acting_for_render_trigger
                    )
                    encounter_unresolved_before = (
                        snapshot.encounter is not None and not snapshot.encounter.resolved
                    )
                    _apply_narration_result_to_snapshot(
                        snapshot,
                        result,
                        sd.player_name,
                        room=sd._room,
                        pack=sd.genre_pack,
                        dice_failed=dice_failed,
                        dice_actor=dice_actor,
                        opposed_player_d20=opposed_player_d20,
                        opposed_player_beat_id=opposed_player_beat_id,
                        opposed_player_actor=dice_actor,
                        acting_character_name=_resolve_acting_character_name(sd, sd._room),
                    )
                    encounter_resolved_this_turn = encounter_unresolved_before and (
                        snapshot.encounter is None or snapshot.encounter.resolved
                    )
                    # Monster Manual lifecycle post-apply (ADR-059, port of
                    # Rust dispatch/mod.rs:1671-1695). Dormant-on-scene-change
                    # runs FIRST so a location change clears Active anchors
                    # before this turn's narration is scanned for new
                    # activations — otherwise the post-change party_location
                    # would re-activate NPCs that should have aged out with
                    # the scene.  Save() runs last so the mutated lifecycle
                    # is persisted before the broader snapshot persist.
                    if sd.monster_manual is not None:
                        post_apply_location = snapshot.party_location(
                            perspective=_acting_for_render_trigger
                        )
                        if (
                            snapshot_location_before_apply
                            and post_apply_location
                            and snapshot_location_before_apply != post_apply_location
                        ):
                            monster_manual_inject.mark_all_dormant(sd.monster_manual)
                        monster_manual_inject.mark_active_from_narration(
                            sd.monster_manual,
                            getattr(result, "narration", "") or "",
                            post_apply_location or "",
                        )
                        sd.monster_manual.save()
                    # Phase 5 (Story 47-3): drain magic-confrontation
                    # outbound queues. ``narration_apply.apply_magic_working``
                    # populates ``pending_magic_auto_fires`` (one CONFRONTATION
                    # payload per auto-fire); ``_resolve_magic_confrontation_if_applicable``
                    # populates ``pending_magic_confrontation_outcome``. Both
                    # are dispatched as outbound WebSocket frames here so the
                    # UI overlay mounts and the reveal panel surfaces in
                    # production gameplay (not just in test harnesses).
                    self._dispatch_pending_magic_frames(snapshot)
                    # Consume the pending outcome — one turn per roll.
                    if dice_outcome is not None and hasattr(sd, "pending_roll_outcome"):
                        sd.pending_roll_outcome = None
                    if hasattr(sd, "pending_roll_actor"):
                        sd.pending_roll_actor = None
                    if hasattr(sd, "pending_opposed_player_d20"):
                        sd.pending_opposed_player_d20 = None
                    if hasattr(sd, "pending_opposed_player_beat_id"):
                        sd.pending_opposed_player_beat_id = None
                    # Story 45-5 / ADR-051: chargen is round 0; gameplay
                    # starts at round 1. The opening narration is the
                    # round-1 scene-set, not a player-narrator exchange,
                    # so it does not bump either counter. The first
                    # PLAYER_ACTION turn is the first real exchange.
                    if not is_opening_turn:
                        # Capture the round *before* incrementing so the
                        # taunt-expiry OTEL span labels it correctly as
                        # "the round that just ended" (Task 6).
                        _prior_round = snapshot.turn_manager.round
                        snapshot.turn_manager.record_interaction()

                        # Story 2026-05-10 — taunt decay tick (Task 6).
                        # Runs on every round-advance so the 1-round taunt
                        # duration is enforced mechanically, not left to
                        # narrator improvisation. Only fires when an encounter
                        # is active and unresolved — no-op outside combat.
                        if snapshot.encounter is not None and not snapshot.encounter.resolved:
                            from sidequest.game.taunt_tick import (  # noqa: PLC0415
                                tick_taunt_round_advance,
                            )

                            tick_taunt_round_advance(
                                snapshot.encounter,
                                prior_round=_prior_round,
                            )

                    # Story 45-19: arc-recompute tick. Closes Felix's
                    # Playtest 3 bug where world_history froze at turn 30.
                    # The predicate is consulted with the post-bump
                    # interaction so cadence boundaries align with the
                    # interaction count the GM panel surfaces. Empty
                    # cached_history_chapters (pack with no history.yaml,
                    # or parse-failed chargen fallback) is a graceful
                    # no-op inside ``recompute_arc_history`` — the tick
                    # span still fires so the panel sees the empty case.
                    #
                    # Story 45-23: when the recompute reports newly-
                    # promoted chapters, seed each chapter's narrative
                    # log + lore strings into the durable narrative_log
                    # and the RAG-retrievable lore store. Closes Felix's
                    # writeback gap (71 turns, narrative_log + lore_store
                    # empty of arc-sourced content). Per-chapter
                    # ``arc_embedding_seed`` span carries the seeded
                    # counts so the GM panel can chart Lane B throughput.
                    if should_recompute_arc(snapshot.turn_manager.interaction):
                        added_chapters = recompute_arc_history(snapshot, sd.cached_history_chapters)
                        if added_chapters:
                            from sidequest.game.lore_seeding import (  # noqa: PLC0415
                                seed_lore_from_arc_promotion,
                            )
                            from sidequest.telemetry.spans import (  # noqa: PLC0415
                                SPAN_WORLD_HISTORY_ARC_EMBEDDING_SEED,
                                Span,
                            )

                            for chapter in added_chapters:
                                # One seed-call per promoted chapter so
                                # the OTEL span attributes can attribute
                                # the counts to the specific chapter id
                                # — the GM panel filters by chapter to
                                # diagnose which content got seeded.
                                seed_result = seed_lore_from_arc_promotion(
                                    snapshot,
                                    sd.store,
                                    sd.lore_store,
                                    [chapter],
                                )
                                with Span.open(
                                    SPAN_WORLD_HISTORY_ARC_EMBEDDING_SEED,
                                    {
                                        "chapter_id": getattr(chapter, "id", ""),
                                        "narrative_entries_appended": (
                                            seed_result.narrative_entries_appended
                                        ),
                                        "lore_fragments_minted": (
                                            seed_result.lore_fragments_minted
                                        ),
                                        "lore_fragments_skipped_duplicate": (
                                            seed_result.lore_fragments_skipped_duplicate
                                        ),
                                        "content_bytes_seeded": (seed_result.content_bytes_seeded),
                                        "interaction": (snapshot.turn_manager.interaction),
                                    },
                                ):
                                    pass

                    # Story 45-27: trope progression tick. Advances passive
                    # progression, fires staggered beats, gates new
                    # activations through cap + cooldown. Wired here so
                    # ``now_turn`` (interaction) is the post-bump value
                    # and so any engine-driven resolution flows into the
                    # 45-20 handshake's diff below — the handshake's
                    # baseline was captured at the top of this method
                    # before any apply step, so a fresh resolved status
                    # set by the tick is visible to the diff.
                    from sidequest.game.trope_tick import tick_tropes  # noqa: PLC0415

                    tick_tropes(
                        snapshot,
                        sd.genre_pack,
                        now_turn=snapshot.turn_manager.interaction,
                        days_advanced=result.days_advanced,  # Story 50-4 — Pass A2 time skip
                    )

                    # Story 45-20: trope-resolution handshake. Diffs the
                    # baseline captured at the top of this method against
                    # the post-recompute snapshot to detect any trope that
                    # flipped to "resolved" this turn (chapter-promotion
                    # path today; engine/narrator-extraction future).
                    # Writes the durable record (quest_log entry +
                    # active_stakes marker) and emits the handshake span
                    # so the GM panel sees the path engaged. Idempotent
                    # re-detect (already-resolved last turn) emits a span
                    # with active_stakes_appended=False but does not
                    # rewrite — the lie-detector signal Sebastien needs.
                    _handshake_resolved_tropes(
                        snapshot,
                        trope_status_baseline,
                        player_name=sd.player_name,
                        source="chapter_promotion",
                    )

                    # Plan 6 Task 5 — complication-ledger resolution
                    # subscription. Consumes the same resolved-trope diff
                    # the 45-20 handshake already computed above (reuse-
                    # first, Decision M). Calls
                    # resolve_complications_for_resolved_tropes which calls
                    # store.resolve_thread() (Plan 5's span emitter) for
                    # each open trope-thread whose ref_id matches a
                    # just-resolved trope. Quest-thread resolution is
                    # Plan 7's (Decision O).
                    #
                    # DECISION N (honest deferral, no runtime noise):
                    # No-op is correct iff no dungeon was materialized. Only
                    # Plan 7 both materializes set-pieces (creating ledger
                    # threads via attach_set_piece) AND wires
                    # sd.dungeon_store — the two land together, so
                    # store-absent ⟺ no open dungeon threads exist.
                    # Resolution wiring activates the moment Plan 7 sets
                    # sd.dungeon_store. The loud seam is the mandatory
                    # wiring test's structural tripwire
                    # (test_setpiece_attach_wiring.py), NOT a per-turn log:
                    # the trope engine is global, so a per-turn warning
                    # would fire on ~100% of pre-Plan-7 turns and be pure
                    # ignorable noise. No warning, no log here — the no-op
                    # is provably correct.
                    _dungeon_store = getattr(sd, "dungeon_store", None)
                    if _dungeon_store is not None:
                        from sidequest.dungeon.setpiece_attach import (  # noqa: PLC0415
                            resolve_complications_for_resolved_tropes,
                        )

                        _resolved_this_turn = [
                            t.id
                            for t in snapshot.active_tropes
                            if t.status == "resolved"
                            and trope_status_baseline.get(t.id) != "resolved"
                        ]
                        resolve_complications_for_resolved_tropes(
                            resolved_trope_ids=_resolved_this_turn,
                            store=_dungeon_store,
                        )

                    now_encounter = snapshot.encounter
                    now_live = now_encounter is not None and not now_encounter.resolved

                    from sidequest.server.dispatch.encounter_lifecycle import (
                        _is_combat_category,
                        apply_resource_patches,
                        award_turn_xp,
                    )

                    in_combat_now = (
                        snapshot.encounter is not None
                        and not snapshot.encounter.resolved
                        and _is_combat_category(sd.genre_pack, snapshot.encounter.encounter_type)
                    )
                    award_turn_xp(snapshot, in_combat=in_combat_now)

                    try:
                        crossed_thresholds = apply_resource_patches(
                            snapshot,
                            affinity_progress=result.affinity_progress or [],
                            lore_store=sd.lore_store,
                            turn=snapshot.turn_manager.interaction,
                        )
                    except Exception as exc:  # noqa: BLE001 — LLM typos must not kill the turn
                        logger.warning(
                            "resource.patch_failed error=%s — skipping threshold mint for this turn",
                            exc,
                        )
                        crossed_thresholds = []
                for t in crossed_thresholds:
                    logger.info(
                        "resource.threshold_crossed event_id=%s at=%s",
                        t.event_id,
                        t.at,
                    )

                with timings.phase("persistence"):
                    try:
                        # ADR-037 Python port: room owns the canonical snapshot, so a
                        # plain room.save() is sufficient — there is no per-session
                        # divergence to merge. Falls back to sd.store.save when the
                        # legacy non-slug path didn't bind a room.
                        if self._room is not None:
                            self._room.save()
                        else:
                            sd.store.save(snapshot)
                        # Story 45-22: log the player's turn before the narrator
                        # response so the narrative_log shows both sources.
                        # Felix's Playtest 3 had 71 entries all author='narrator'
                        # because the player append site was missing — Sebastien
                        # could not distinguish player input from narrator
                        # inference on the GM panel. Skipped on the opening
                        # turn (no real player input — chargen-confirmation
                        # seeds the action programmatically).
                        if not is_opening_turn:
                            acting_name = _resolve_acting_character_name(
                                sd,
                                self._room,
                            )
                            player_entry = NarrativeEntry(
                                timestamp=0,
                                round=snapshot.turn_manager.interaction,
                                author="player",
                                content=action,
                                tags=[],
                                speaker=acting_name,
                            )
                            sd.store.append_narrative(player_entry)
                        narrative_entry = NarrativeEntry(
                            timestamp=0,
                            round=snapshot.turn_manager.interaction,
                            author="narrator",
                            content=result.narration,
                            tags=[],
                        )
                        sd.store.append_narrative(narrative_entry)
                        logger.info(
                            "session.persisted turn=%d player=%s char_count=%d seat_count=%d",
                            snapshot.turn_manager.interaction,
                            sd.player_name,
                            len(snapshot.characters),
                            len(snapshot.player_seats),
                        )
                    except Exception as exc:
                        logger.error("session.persist_failed error=%s", exc)

                # Story 45-11 — turn_manager.round invariant lie-detector.
                # Felix's Playtest 3 ended round=65 / max(narrative_log)=72
                # with nothing watching the divergence. Emit on EVERY tick
                # (whether or not the invariant holds) so the GM panel can
                # tell "engaged + clean" apart from "not engaged at all".
                # Read MAX(round_number) from the durable narrative_log,
                # not whatever in-memory mirror the snapshot carries —
                # the SQL value is the ground truth Felix's save proved
                # the snapshot can drift from.
                try:
                    max_narrative_round = int(sd.store.max_narrative_round())
                except Exception as exc:  # noqa: BLE001 — telemetry must never crash a turn
                    logger.warning(
                        "round_invariant.max_lookup_failed error=%s",
                        exc,
                    )
                    max_narrative_round = 0
                with round_invariant_span(
                    round=snapshot.turn_manager.round,
                    interaction=snapshot.turn_manager.interaction,
                    max_narrative_round=max_narrative_round,
                ):
                    # Span attributes are set by the helper; the body is
                    # intentionally empty — this is a point-in-time emit,
                    # not a wrapping span around downstream work.
                    pass

                # Story 37-33: embed newly-seeded / pending lore fragments in the
                # background so the *next* turn's RAG retrieval can find them.
                # Spawns a fire-and-forget task — the narration turn returns to
                # the player immediately; embeds populate during the human's
                # reading time.
                self._dispatch_embed_worker(sd)

                narration_text = result.narration or "(The world holds its breath...)"
                try:
                    narration_nbs = NonBlankString(narration_text)
                except Exception:
                    narration_nbs = NonBlankString("The world holds its breath...")

                # Forward extracted footnotes into the NarrationPayload so the UI
                # Knowledge journal fills. Narrator produces them every turn
                # (see `game_patch.extracted footnotes=N` in the server log) and
                # the UI's useStateMirror was already wired to consume them — the
                # session handler was the only missing link. Coerce raw dicts
                # from the extraction into typed Footnote models, skipping any
                # that fail validation rather than crashing the turn.
                forwarded_footnotes: list[Footnote] = []
                fact_ids_minted_this_turn = 0
                for fn in result.footnotes or []:
                    if not isinstance(fn, dict):
                        continue
                    try:
                        footnote = Footnote(**fn)
                    except Exception as exc:  # noqa: BLE001 — drop-and-log is safer than a mid-turn crash
                        logger.warning(
                            "state.footnote_coerce_failed error=%s payload=%r",
                            exc,
                            fn,
                        )
                        continue
                    # ADR-100 Seam C: every Footnote that reaches the UI MUST
                    # carry a stable fact_id. The narrator prompt asks for one,
                    # but narrators don't always comply (especially for new
                    # facts where the prompt is permissive). Without this
                    # defensive mint, the UI's strict drop policy
                    # (useStateMirror.ts:198, "footnote missing fact_id;
                    # skipping") silently swallows load-bearing world facts —
                    # the exact consistency leak ADR-100 exists to close
                    # (sq-playtest 2026-05-15: 6 dropped facts in one turn).
                    #
                    # Mint a deterministic hash-based id so a re-narration of
                    # the same fact in a later turn collides on the client's
                    # seenFactIds dedupe rather than re-entering the journal
                    # with a fresh UUID. Narrator-supplied fact_ids are
                    # preserved untouched — scenario clue_intake matches them
                    # against ClueNode.id (genre-authored), so replacing them
                    # would break Seam A.
                    if footnote.fact_id is None:
                        cat_str = (
                            footnote.category.value
                            if hasattr(footnote.category, "value")
                            else str(footnote.category)
                        )
                        digest_input = f"{footnote.summary}|{cat_str}|{footnote.is_new}"
                        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
                        footnote = footnote.model_copy(update={"fact_id": f"fn-{digest[:16]}"})
                        fact_ids_minted_this_turn += 1
                    forwarded_footnotes.append(footnote)
                if fact_ids_minted_this_turn > 0:
                    _watcher_publish(
                        "state.footnote_fact_id_minted",
                        {
                            "count": fact_ids_minted_this_turn,
                            "player_id": sd.player_id,
                            "turn_number": snapshot.turn_manager.interaction,
                            "reason": "narrator_omitted_fact_id",
                        },
                        component="footnotes",
                    )
                    logger.info(
                        "state.footnote_fact_id_minted count=%d player=%s turn=%d",
                        fact_ids_minted_this_turn,
                        sd.player_name,
                        snapshot.turn_manager.interaction,
                    )
                logger.info(
                    "state.footnotes_forwarded count=%d player=%s",
                    len(forwarded_footnotes),
                    sd.player_name,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "footnotes",
                        "count": len(forwarded_footnotes),
                        "player_id": sd.player_id,
                        "turn_number": snapshot.turn_manager.interaction,
                        "summaries": [fn.summary for fn in forwarded_footnotes][:10],
                    },
                    component="footnotes",
                )
                # Story 50-5 / ADR-100 seams A + B: feed extracted footnotes
                # to the scenario clue graph. Footnotes whose fact_id matches
                # a ClueNode advance the scenario (SPAN_SCENARIO_ADVANCE) and
                # mint a Discovered KnownFact on the acting player's
                # character. No-op when no scenario is bound or when no
                # fact_id matches.
                from sidequest.server.dispatch.scenario_clue_intake import (  # noqa: PLC0415
                    consume_clue_footnotes,
                )

                consume_clue_footnotes(
                    snapshot,
                    forwarded_footnotes,
                    active_character_name=snapshot.player_seats.get(sd.player_id, sd.player_name),
                )
                # Story 50-8 / ADR-053 AC-5: the AccusationEvaluator dispatch
                # sibling. Imported here so the GM panel and tests can verify
                # the narration-response path can reach the evaluator on demand.
                # Per-turn invocation is deferred until a player-action
                # accusation trigger lands (NarrationPayload sidecar / footnote
                # subtype TBD); the import makes the dispatch module a live
                # consumer of the production code path rather than dead code.
                from sidequest.server.dispatch.scenario_accusation import (  # noqa: F401, PLC0415
                    consume_accusation_request,
                )

                # Story 49-8: visibility classifier produces the v2
                # sidecar that drives per-recipient routing AND 2nd-person
                # POV swap downstream in emitters.emit_event. Pure
                # post-narration work — no narrator subprocess calls.
                #
                # The classifier prefers result.action_rewrite.named when
                # the narrator emitted it (ADR-039), falling back to a
                # first-sentence scan of the prose. Returns atmospheric
                # (anchor_pc=None, pov_strategy="atmospheric") when no PC
                # name surfaces — that path broadcasts the canonical
                # prose unchanged.
                from sidequest.server.visibility_classifier import (  # noqa: PLC0415
                    classify_narration_visibility,
                )

                _connected_player_ids = (
                    self._room.connected_player_ids()
                    if self._room is not None
                    and callable(getattr(self._room, "connected_player_ids", None))
                    else []
                )
                _player_id_to_character: dict[str, str] = {}
                if self._room is not None:
                    _slot_lookup = getattr(self._room, "slot_to_player_id", None)
                    if callable(_slot_lookup):
                        try:
                            _player_id_to_character = {
                                pid: slot for slot, pid in _slot_lookup().items()
                            }
                        except Exception:  # noqa: BLE001 — classifier must never crash a turn
                            _player_id_to_character = {}
                try:
                    _visibility_sidecar = classify_narration_visibility(
                        result=result,
                        snapshot=snapshot,
                        connected_player_ids=_connected_player_ids,
                        player_id_to_character=_player_id_to_character,
                    )
                except ValueError:
                    # Empty narration — the classifier fails loud; we
                    # mirror the older behavior of emitting the canonical
                    # prose unchanged so a degraded turn still surfaces
                    # to the UI rather than crashing the hot path.
                    _visibility_sidecar = None

                narration_payload = NarrationPayload(
                    text=narration_nbs,
                    state_delta=None,
                    footnotes=forwarded_footnotes,
                    visibility_sidecar=_visibility_sidecar,
                )
                # MP-03 Task 3: route through EventLog + ProjectionFilter before send.
                #
                # ADR-105 Track A: a shared merged-MP narration (>1
                # connected player) has no sole author — the driving
                # handler is merely the last submitter. Thread its stable
                # _session_data.player_id as author_player_id so the
                # driver is projected/POV-swapped like every recipient
                # (one projection.filter.decide + perception_rewrite +
                # second_person_swap per DISTINCT player) rather than
                # receiving the solo Invariant-3 raw bypass — the
                # confirmed firewall+POV breach. Solo (<=1 connected)
                # passes None and keeps the raw-bypass + lazy_fill
                # invariant byte-identical.
                _mp_author = (
                    self._session_data.player_id
                    if self._session_data is not None and len(_connected_player_ids) > 1
                    else None
                )
                with timings.phase("broadcast"):
                    narration_msg = self._emit_event(
                        "NARRATION", narration_payload, author_player_id=_mp_author
                    )

                    # ADR-105 B3: emit each narrator-partitioned private
                    # prose segment as its own NARRATION_SEGMENT, routed
                    # by _visibility.visible_to to the single owning PC
                    # (GM implicit via the GM CoreInvariant). The shared
                    # NARRATION text above is public-safe by the amended
                    # output contract; these carry the withheld
                    # perception. The visibility-gated CoreInvariant (B1)
                    # structurally firewalls non-recipients;
                    # author_player_id (Track A) gives the owning PC a
                    # real per-recipient projection pass. Default is zero
                    # segments — fully-public turns add nothing here.
                    _private_segments = getattr(result, "private_prose_segments", []) or []
                    if _private_segments:
                        _seat_to_player = {
                            name: pid for pid, name in snapshot.player_seats.items() if name
                        }
                        _seg_turn_id = (
                            f"{sd.genre_slug}:{sd.world_slug}:"
                            f"{sd.player_id}:{snapshot.turn_manager.interaction}"
                        )
                        for _seg in _private_segments:
                            _seg_text = (_seg.get("text") or "").strip()
                            if not _seg_text:
                                continue
                            _seg_anchor = (_seg.get("anchor_pc") or "").strip() or None
                            _owner_pid = _seat_to_player.get(_seg_anchor) if _seg_anchor else None
                            if _owner_pid is None:
                                # Fail loud, never leak: an unresolvable
                                # owner means we cannot safely route this
                                # private prose. Dropping loses the prose
                                # but routing-to-all would be the exact
                                # breach ADR-105 closes. Surface it for
                                # the GM panel rather than swallow it.
                                logger.warning(
                                    "narration.segment_unroutable "
                                    "anchor_pc=%r seated=%r — DROPPED (no leak)",
                                    _seg_anchor,
                                    list(_seat_to_player),
                                )
                                _watcher_publish(
                                    "state_transition",
                                    {
                                        "field": "narration.segment_routed",
                                        "anchor_pc": _seg_anchor or "",
                                        "visible_to": "",
                                        "recipient_count": 0,
                                        "withheld_from_count": len(_connected_player_ids),
                                        "routed": False,
                                    },
                                    component="projection",
                                    severity="warning",
                                )
                                continue
                            # author_player_id=_owner_pid makes the owner
                            # the emitter: every OTHER connected player is
                            # a peer recipient and the visibility-gated
                            # CoreInvariant (B1) excludes them at fan-out
                            # (no queue frame); the owner's frame is
                            # projected + perception-rewritten + (B4)
                            # POV-swapped and RETURNED. emit_event's peer
                            # fan-out never delivers to the emitter, so the
                            # owner's own segment must be pushed to the
                            # owner's CURRENT socket explicitly (the
                            # owner is not necessarily the turn driver —
                            # the segment's owner is whichever PC the
                            # narrator anchored the private prose to).
                            _seg_msg = self._emit_event(
                                "NARRATION_SEGMENT",
                                NarrationSegmentPayload(
                                    text=_seg_text,
                                    anchor_pc=_seg_anchor,
                                    turn_id=_seg_turn_id,
                                    # ADR-105 B4: per-segment POV. Each
                                    # segment is single-PC by construction,
                                    # so carrying anchor_pc + pc_anchored
                                    # in _visibility lets the existing
                                    # _apply_pov_swap (Track A having fixed
                                    # per-recipient binding) rewrite it to
                                    # 2nd-person for the owner. No
                                    # multi-antecedent blob: the swap
                                    # operates on one PC's private prose.
                                    visibility_sidecar={
                                        "visible_to": [_owner_pid],
                                        "fidelity": {},
                                        "anchor_pc": _seg_anchor,
                                        "pov_strategy": "pc_anchored",
                                    },
                                ),
                                author_player_id=_owner_pid,
                            )
                            # Deliver the owner's projected+swapped frame
                            # to the owner's live socket. Lookup at
                            # delivery time so a reconnected owner's NEW
                            # socket queue gets it (mirrors the
                            # CONFRONTATION dispatcher-socket fix). On the
                            # EventLog regardless (replay / GM lie-detector
                            # / reconnect lazy_fill) — a missing live
                            # socket is not a leak, just a deferred read.
                            _seg_room = self._room
                            if _seg_msg is not None and _seg_room is not None:
                                _seg_sock_fn = getattr(_seg_room, "socket_for_player", None)
                                _seg_q_fn = getattr(_seg_room, "queue_for_socket", None)
                                if callable(_seg_sock_fn) and callable(_seg_q_fn):
                                    _seg_sock = _seg_sock_fn(_owner_pid)
                                    _seg_q = _seg_q_fn(_seg_sock) if _seg_sock is not None else None
                                    if _seg_q is not None:
                                        _seg_q.put_nowait(_seg_msg)
                            _watcher_publish(
                                "state_transition",
                                {
                                    "field": "narration.segment_routed",
                                    "anchor_pc": _seg_anchor,
                                    "visible_to": _owner_pid,
                                    "recipient_count": 1,
                                    "withheld_from_count": max(len(_connected_player_ids) - 1, 0),
                                    "routed": True,
                                },
                                component="projection",
                            )

                # Pingpong 2026-04-26 [S3-REGRESSION]: emit a SCRAPBOOK_ENTRY for
                # every narration turn so the UI gallery has metadata to merge with
                # the IMAGE that lands later from the daemon. Pure reuse — no new
                # LLM calls. Fields come from the orchestrator result and the
                # snapshot the narrator just stamped.
                with timings.phase("dispatch_post"):
                    # Story 45-31 first: consult the daemon-state mirror so
                    # the dispatcher below can skip its round-trip when the
                    # daemon is UNRESPONSIVE. ``sd.render_unavailable_pending``
                    # is the shared flag the dispatcher reads.
                    from sidequest.daemon_client.state_mirror import (
                        get_mirror as _get_mirror,
                    )

                    _hb_mirror = _get_mirror()
                    sd.render_unavailable_pending = (
                        _hb_mirror.last_heartbeat_ts() is not None and _hb_mirror.is_unresponsive()
                    )

                    # Story 45-30: classify the render trigger reason once
                    # so the same value lands in both the SCRAPBOOK_ENTRY
                    # render_status discriminator and the dispatcher below.
                    # ``classify_trigger`` is pure so the two call sites
                    # converge without coordination.
                    from sidequest.server.render_trigger import (
                        RenderTriggerReason,
                        classify_trigger,
                    )

                    _trigger_reason = classify_trigger(
                        result,
                        snapshot_location_before=snapshot_location_before_apply,
                        encounter_resolved_this_turn=encounter_resolved_this_turn,
                    )

                    # Unified render_status (Story 45-30 + 45-31) — daemon-
                    # unavailable wins over policy decisions because no
                    # render is coming either way; the user-facing reason
                    # is "the daemon is down." Then policy decides
                    # skipped_policy vs rendered.
                    if sd.render_unavailable_pending:
                        _render_status = "unavailable"
                    elif _trigger_reason is RenderTriggerReason.NONE_POLICY:
                        _render_status = "skipped_policy"
                    else:
                        _render_status = "rendered"

                    try:
                        self._emit_scrapbook_entry(
                            sd=sd,
                            snapshot=snapshot,
                            result=result,
                            render_status=_render_status,
                        )
                    except Exception as exc:  # noqa: BLE001 — scrapbook must never crash a turn
                        logger.warning(
                            "scrapbook.emit_failed turn=%d error=%s",
                            snapshot.turn_manager.interaction,
                            exc,
                        )

                    # Group G Task 6: route prompt-redacted dispatches as SECRET_NOTE
                    # events. Task 5's ``redact_dispatch_package`` stripped these from the
                    # narrator prompt and parked them on ``result.secret_routes``; here we
                    # reify each one as its own event so the same ProjectionFilter /
                    # visibility_tag rule (Task 3) delivers it only to the recipients in
                    # its ``_visibility.visible_to``. Only SubsystemDispatch entries route;
                    # see ``build_secret_note_events`` for the skip rules.
                    if result.secret_routes:
                        for _envelope in build_secret_note_events(
                            result.secret_routes,
                            turn_id=f"{sd.genre_slug}:{sd.world_slug}:{sd.player_id}:{snapshot.turn_manager.interaction}",
                        ):
                            import json as _json

                            _payload_data = _json.loads(_envelope.payload_json)
                            self._emit_event(
                                "SECRET_NOTE",
                                SecretNotePayload(
                                    turn_id=_payload_data["turn_id"],
                                    idempotency_key=_payload_data["idempotency_key"],
                                    subsystem=_payload_data["subsystem"],
                                    params=_payload_data.get("params", {}),
                                    visibility_sidecar=_payload_data["_visibility"],
                                ),
                            )

                    # Story 3.4 Task 11: emit CONFRONTATION when encounter state transitions.
                    # OTEL visibility: add event to current span so the GM panel (Sebastien-
                    # tier mechanical visibility) can see the dispatch decision end-to-end.
                    # Pingpong 2026-04-26 S2-BUG: confrontations were PRIVATE to the
                    # acting player — peers froze on the prior shared beat (no NPC
                    # card, no narration, no buttons). Root cause: the message was
                    # built directly via ``ConfrontationMessage(...)`` and only
                    # appended to the actor's ``outbound`` list, never broadcast.
                    # Fix: route through ``self._emit_event("CONFRONTATION", ...)``
                    # so the canonical EventLog + ProjectionFilter fan-out path
                    # delivers the same per-player frame to every connected peer
                    # (mirrors how NARRATION is emitted at the line above). The
                    # kind is already registered in ``_KIND_TO_MESSAGE_CLS``.
                    # Independent of the multi-target parse-failure (#5) — that
                    # lives in local_dm and degrades the dispatch package, but
                    # the missing broadcast here is the sole cause of peer freeze.
                    confrontation_msg: object | None = None
                    confrontation_payload: ConfrontationPayload | None = None
                    confrontation_event_attrs: dict[str, object] | None = None
                    if now_live and now_encounter is not None:
                        from sidequest.server.dispatch.confrontation import (
                            build_confrontation_payload,
                            find_confrontation_def,
                        )

                        cdef = find_confrontation_def(
                            sd.genre_pack.rules.confrontations if sd.genre_pack.rules else [],
                            now_encounter.encounter_type,
                        )
                        # No silent fallback: an active encounter whose type is not in the
                        # pack is a pack-data bug. Task 10 raises in the same case during
                        # beat-apply; the dispatch path matches.
                        if cdef is None:
                            raise ValueError(
                                f"active encounter type {now_encounter.encounter_type!r} "
                                f"not in pack confrontations (genre={sd.genre_slug!r})"
                            )
                        # Canonical (full-union) payload — persisted to
                        # EventLog via ``_emit_event`` below for replay
                        # parity, and also delivered to legacy stub-room
                        # test fixtures that lack the full SessionRoom
                        # API. ``recipient_pc=None`` is explicit: this
                        # call deliberately does NOT project per-PC —
                        # the per-recipient overlay loop below
                        # overwrites each connected socket's
                        # Confrontation tab with class-filtered beats
                        # (Story 49-7).
                        payload_dict = build_confrontation_payload(
                            encounter=now_encounter,
                            cdef=cdef,
                            genre_slug=sd.genre_slug,
                            recipient_pc=None,
                        )
                        confrontation_payload = ConfrontationPayload(**payload_dict)
                        confrontation_event_attrs = {
                            "active": True,
                            "encounter_type": now_encounter.encounter_type,
                            "genre_slug": sd.genre_slug,
                        }
                    elif prior_live and not now_live:
                        from sidequest.server.dispatch.confrontation import (
                            build_clear_confrontation_payload,
                        )

                        assert prior_type is not None  # guaranteed by prior_live=True
                        payload_dict = build_clear_confrontation_payload(
                            encounter_type=prior_type,
                            genre_slug=sd.genre_slug,
                        )
                        confrontation_payload = ConfrontationPayload(**payload_dict)
                        confrontation_event_attrs = {
                            "active": False,
                            "encounter_type": prior_type,
                            "genre_slug": sd.genre_slug,
                        }

                    if confrontation_payload is not None:
                        # Story 45-3: lie-detector for the post-narration
                        # CONFRONTATION emit. The narrator just opened or
                        # advanced an encounter; the dial is about to move
                        # on screen. Sebastien (mechanical-first player)
                        # needs a span confirming the engine emitted
                        # post-mutation momentum here, not just that the
                        # narrator's prose happened to mention combat.
                        # Only fires on the live branch (active emit
                        # carrying real metric values) — the clear-payload
                        # branch broadcasts active=false with empty
                        # metrics, so there is no post-mutation momentum
                        # to audit.
                        if now_live and now_encounter is not None:
                            with encounter_momentum_broadcast_span(
                                encounter_type=now_encounter.encounter_type,
                                player_metric_after=now_encounter.player_metric.current,
                                opponent_metric_after=now_encounter.opponent_metric.current,
                                source="narration_apply",
                                beat_id=None,
                            ):
                                confrontation_msg = self._emit_event(
                                    "CONFRONTATION",
                                    confrontation_payload,
                                )
                        else:
                            confrontation_msg = self._emit_event(
                                "CONFRONTATION",
                                confrontation_payload,
                            )

                        # Story 49-7: per-PC beat projection overlay. The
                        # canonical ``_emit_event`` call above persists the
                        # full-union payload to EventLog (replay-safe) and
                        # fans it to peers via the ProjectionFilter — but
                        # every recipient sees the same 16-button beat list
                        # regardless of class (the 2026-05-12 caverns_sunden
                        # playtest bug: Fighter saw Backstab/Cast Spell/Turn
                        # Undead). We follow the canonical emit with a per-
                        # recipient direct-queue overlay that delivers a
                        # class-filtered CONFRONTATION to each connected
                        # socket. UI renders whichever arrives last for a
                        # given encounter — filtered wins. EventLog retains
                        # the canonical row for audit; reconnect rebuilds
                        # via the slug-resume bootstrap which is itself per-
                        # PC filtered (handlers/connect.py:1110).
                        # Track whether the per-PC overlay loop below
                        # successfully queued a filtered CONFRONTATION to
                        # the dispatcher's socket. The canonical
                        # dispatcher push at the "Pingpong 2026-04-30
                        # follow-on" block below (around line 3360) must
                        # skip the dispatcher when the overlay already
                        # covered them — otherwise the unfiltered canonical
                        # lands AFTER the filtered overlay in the
                        # dispatcher's queue and the UI's last-message-wins
                        # render reverts the dispatcher's Confrontation tab
                        # to the full 16-button union (pingpong 2026-05-12
                        # 17:48: position-#3 PC always broke because
                        # sd.player_id rotates to the last-narrated PC in
                        # merged-dispatch order).
                        _dispatcher_overlay_delivered = False
                        if now_live and now_encounter is not None and self._room is not None:
                            from sidequest.server.dispatch.confrontation import (
                                resolve_recipient_pc,
                            )

                            socket_for_player_fn = getattr(self._room, "socket_for_player", None)
                            queue_for_socket_fn = getattr(self._room, "queue_for_socket", None)
                            connected_player_ids_fn = getattr(
                                self._room, "connected_player_ids", None
                            )
                            if (
                                callable(socket_for_player_fn)
                                and callable(queue_for_socket_fn)
                                and callable(connected_player_ids_fn)
                            ):
                                for _pid in connected_player_ids_fn():
                                    _recipient_pc, _recipient_actor = resolve_recipient_pc(
                                        snapshot=sd.snapshot,
                                        genre_pack=sd.genre_pack,
                                        player_id=_pid,
                                    )
                                    if _recipient_pc is None:
                                        continue
                                    _per_pc_dict = build_confrontation_payload(
                                        encounter=now_encounter,
                                        cdef=cdef,
                                        genre_slug=sd.genre_slug,
                                        recipient_pc=_recipient_pc,
                                        recipient_actor_name=_recipient_actor,
                                    )
                                    _per_pc_msg = ConfrontationMessage(
                                        payload=ConfrontationPayload(**_per_pc_dict),
                                        player_id="server",
                                    )
                                    _socket_id = socket_for_player_fn(_pid)
                                    if _socket_id is None:
                                        continue
                                    _q = queue_for_socket_fn(_socket_id)
                                    if _q is None:
                                        continue
                                    _q.put_nowait(_per_pc_msg)
                                    if _pid == sd.player_id:
                                        _dispatcher_overlay_delivered = True
                        assert confrontation_event_attrs is not None
                        trace.get_current_span().add_event(
                            "confrontation.dispatched",
                            confrontation_event_attrs,
                        )
                        # OTEL lie-detector hook (per CLAUDE.md OTEL principle): the
                        # GM panel needs evidence the broadcast actually reached
                        # peers — not just that the actor saw a confrontation card.
                        # Without this, a regression to the pre-fix behavior (frame
                        # appended only to actor's outbound) is invisible to the
                        # watcher dashboard.
                        peer_player_ids: list[str] = []
                        room_slug: str = ""
                        if self._room is not None:
                            # Some unit-test fixtures (e.g. _StubRoom in
                            # test_dice_throw_wiring) provide a minimal Room shim
                            # that lacks ``connected_player_ids`` / ``slug``. The
                            # OTEL hook is best-effort logging — never crash the
                            # turn for missing observability metadata.
                            import contextlib  # noqa: PLC0415 — local import keeps hot path lean

                            with contextlib.suppress(AttributeError):
                                peer_player_ids = [
                                    pid
                                    for pid in self._room.connected_player_ids()
                                    if pid != sd.player_id
                                ]
                            room_slug = getattr(self._room, "slug", "") or ""
                        logger.info(
                            "confrontation.peer_projection_broadcast slug=%s acting=%s "
                            "encounter_type=%s active=%s peers=%s",
                            room_slug,
                            sd.player_id,
                            confrontation_event_attrs["encounter_type"],
                            confrontation_event_attrs["active"],
                            peer_player_ids,
                        )
                        _watcher_publish(
                            "confrontation_peer_projection_broadcast",
                            {
                                "slug": room_slug,
                                "acting_player_id": sd.player_id,
                                "encounter_type": confrontation_event_attrs["encounter_type"],
                                "active": confrontation_event_attrs["active"],
                                "peers": peer_player_ids,
                            },
                            component="confrontation",
                        )

                with timings.phase("broadcast"):
                    # MP merged-dispatch: shared-world frames need to reach
                    # every connected socket in the room. NARRATION rides the
                    # _emit_event/EventLog path (durable, replayed on
                    # reconnect). The four shared-world envelopes built below
                    # — NARRATION_END / CHAPTER_MARKER / PARTY_STATUS /
                    # AUDIO_CUE — are NOT durable: they're built once at
                    # turn-end and never persisted. Two pingpong cycles on
                    # 2026-04-30 caught both halves of the broadcast bug:
                    #   (1) commit 4b90250 — peers missed all four because
                    #       they were only appended to ``outbound`` (which
                    #       ships to the dispatcher socket alone). Fix added
                    #       a peer-broadcast helper.
                    #   (2) follow-on bug — the dispatcher's *own* socket
                    #       missed the four envelopes whenever the dispatcher
                    #       reconnected mid-narration (browser refresh during
                    #       the 30-60s Claude await). The peer-broadcast
                    #       excluded the dispatcher's pre-await socket_id;
                    #       outbound.append delivered to the now-cancelled
                    #       writer task on the old socket; the new socket
                    #       got nothing because the envelopes aren't in
                    #       EventLog to replay. Last-submitter froze every
                    #       turn (Reproduced 3× in pingpong).
                    #
                    # Fix: emit shared-world frames via a single broadcast to
                    # every CURRENT socket in the room (exclude_socket_id=
                    # None). Picks up reconnected sockets that registered
                    # before the broadcast fires; the original socket either
                    # detached (clean) or is still attached (gets the frame
                    # like everyone else). Replaces both outbound.append and
                    # the peer-only broadcast — single delivery path, no
                    # double-send risk.
                    #
                    # Legacy non-slug path (self._room is None — only legacy
                    # genre/world connect tests reach this) still falls back
                    # to outbound.append so test fixtures without a room
                    # registry continue to work.
                    _has_room = self._room is not None

                    # OTEL lie-detector: emit one watcher event per shared-
                    # world frame that records every recipient socket_id
                    # plus the resolved player_id. The GM panel can verify
                    # all 4 sockets received NARRATION_END after the merged
                    # dispatch — the only way to catch silent regressions
                    # of this exact bug going forward.
                    def _emit_shared_world_frame(msg: object, frame_kind: str) -> None:
                        if not _has_room:
                            outbound.append(msg)
                            return
                        room = self._room
                        assert room is not None  # noqa: S101 — narrowed by _has_room
                        room.broadcast(msg, exclude_socket_id=None)
                        # OTEL lie-detector: emit one watcher event per
                        # shared-world frame keyed by recipient player_ids
                        # so the GM panel can verify the dispatcher AND every
                        # peer received the frame after the merged dispatch
                        # — the only way to catch silent regressions of the
                        # last-submitter-stuck bug going forward. Wrapped in
                        # try/except: the broadcast above is the load-bearing
                        # call; OTEL must never crash a turn (and in tests a
                        # stub Room may not expose `connected_player_ids`).
                        try:
                            recipients_method = getattr(room, "connected_player_ids", None)
                            recipient_player_ids = (
                                recipients_method() if callable(recipients_method) else []
                            )
                            slug_attr = getattr(room, "slug", "")
                            _watcher_publish(
                                "shared_world_frame_broadcast",
                                {
                                    "frame_kind": frame_kind,
                                    "slug": slug_attr,
                                    "recipient_count": len(recipient_player_ids),
                                    "recipient_player_ids": recipient_player_ids,
                                    "dispatcher_player_id": sd.player_id,
                                },
                                component="multiplayer",
                            )
                        except Exception as exc:  # noqa: BLE001 — telemetry must never crash a turn
                            logger.warning(
                                "shared_world_frame.watcher_publish_failed kind=%s error=%s",
                                frame_kind,
                                exc,
                            )

                    outbound: list[object] = [narration_msg]
                    if confrontation_msg is not None:
                        # Pingpong 2026-04-30 follow-on (sibling of f0b40c7):
                        # CONFRONTATION goes through ``_emit_event`` for
                        # EventLog persistence + projection-filter peer
                        # fan-out. The dispatcher's copy was previously
                        # added to ``outbound`` (closure-captured,
                        # delivered to the dispatcher's PRE-await socket
                        # queue). When the dispatcher's WS cycles during
                        # the 30-60s Claude await — browser refresh /
                        # network blip — the pre-await writer task is
                        # already cancelled; ``outbound.append`` lands on
                        # a dead queue and the encounter dial never
                        # activates on the dispatcher's tab. Reconnect
                        # replay can backfill via EventLog + lazy_fill,
                        # but the race is wide (CONFRONTATION fires AFTER
                        # the dispatcher's reconnect handler has finished
                        # its own replay), so the dispatcher freezes on
                        # the prior turn even though the EventLog has the
                        # row. Fix matches f0b40c7's shape: deliver to
                        # the dispatcher's CURRENT socket via
                        # room.queue_for_socket(socket_for_player(...))
                        # — the lookup runs at delivery time, so a
                        # reconnected dispatcher's NEW socket queue gets
                        # the frame. Peer delivery is unchanged
                        # (``_emit_event`` peer fan-out already covered
                        # them) — no double-delivery hazard. Falls back
                        # to ``outbound.append`` when the room is None
                        # (legacy non-slug test fixtures).
                        # Stub rooms in older test fixtures (_StubRoom in
                        # dice-throw wiring tests) may not expose the
                        # full SessionRoom API — fall back to outbound
                        # so those tests continue to exercise the
                        # actor-receives-via-return-value contract.
                        socket_for_player = (
                            getattr(
                                self._room,
                                "socket_for_player",
                                None,
                            )
                            if _has_room
                            else None
                        )
                        queue_for_socket = (
                            getattr(
                                self._room,
                                "queue_for_socket",
                                None,
                            )
                            if _has_room
                            else None
                        )
                        if _has_room and callable(socket_for_player) and callable(queue_for_socket):
                            room = self._room
                            assert room is not None  # noqa: S101 — narrowed by _has_room
                            dispatcher_socket = socket_for_player(sd.player_id)
                            dispatcher_queue = (
                                queue_for_socket(dispatcher_socket)
                                if dispatcher_socket is not None
                                else None
                            )
                            # Pingpong 2026-05-12 17:48 (trailing-PC regression
                            # of 49-7): only push the unfiltered canonical to
                            # the dispatcher when the per-PC overlay above did
                            # NOT already queue a filtered frame for them. The
                            # overlay covers the dispatcher whenever
                            # ``sd.player_id`` is connected AND seated to a PC
                            # the genre pack can resolve a class for. When that
                            # holds (the typical MP playtest case — every PC is
                            # seated), the overlay's class-filtered frame
                            # already sits in the dispatcher's queue; pushing
                            # the unfiltered canonical here lands AFTER it and
                            # the UI's last-message-wins render snaps back to
                            # the full 16-button union. When the overlay
                            # skipped the dispatcher (no PC seat / no class
                            # resolution / clear branch / stub-room test
                            # fixture without socket helpers), the canonical
                            # remains the dispatcher's sole delivery path.
                            if dispatcher_queue is not None and not _dispatcher_overlay_delivered:
                                dispatcher_queue.put_nowait(confrontation_msg)
                            # OTEL lie-detector: per-recipient confrontation
                            # delivery for the GM panel. Mirrors the
                            # ``shared_world_frame_broadcast`` watcher used by
                            # NARRATION_END / CHAPTER_MARKER / PARTY_STATUS /
                            # AUDIO_CUE so Sebastien's panel can spot a silent
                            # regression of this exact bug — frame_kind names
                            # the variant so confrontation events filter
                            # cleanly out of the broader frame stream.
                            # ``dispatcher_delivery_path`` distinguishes
                            # ``per_pc_overlay`` (the seated-PC branch the
                            # 17:48 fix protects) from ``canonical_push``
                            # (the legacy fallback for unseated dispatchers
                            # / stub-room test fixtures) so a future
                            # regression of either branch is visible from
                            # the dashboard alone.
                            try:
                                slug_attr = getattr(room, "slug", "")
                                connected = (
                                    room.connected_player_ids()
                                    if callable(getattr(room, "connected_player_ids", None))
                                    else []
                                )
                                _watcher_publish(
                                    "shared_world_frame_broadcast",
                                    {
                                        "frame_kind": "confrontation_projection",
                                        "slug": slug_attr,
                                        "recipient_count": len(connected),
                                        "recipient_player_ids": connected,
                                        "dispatcher_player_id": sd.player_id,
                                        "dispatcher_socket_attached": dispatcher_queue is not None,
                                        "dispatcher_delivery_path": (
                                            "per_pc_overlay"
                                            if _dispatcher_overlay_delivered
                                            else "canonical_push"
                                        ),
                                    },
                                    component="multiplayer",
                                )
                            except Exception as exc:  # noqa: BLE001 — telemetry must never crash a turn
                                logger.warning(
                                    "shared_world_frame.watcher_publish_failed "
                                    "kind=confrontation_projection error=%s",
                                    exc,
                                )
                        else:
                            # Legacy / stub-room fallback: append to
                            # outbound so test fixtures without a real
                            # SessionRoom API still see CONFRONTATION
                            # in the function return value (the
                            # contract pre-pingpong-2026-04-30).
                            outbound.append(confrontation_msg)
                    # CHAPTER_MARKER — the UI's ``useRunningHeader`` hook derives the
                    # running-header chapter title from this frame. When the narrator
                    # emits a location in game_patch, the new location is already on
                    # ``snapshot.character_locations[acting_character]`` (applied in
                    # ``_apply_narration_result_to_snapshot``). Emit one frame per
                    # location change so the header updates in lock-step with
                    # narration. Without this the header stays blank since the UI
                    # never saw the server's ``state.location_update`` log line.
                    # Pingpong 2026-04-24 — "Location not rendered in the header on
                    # resume" — fix is symmetric (slug-resume bootstrap also emits
                    # CHAPTER_MARKER; see the slug-connect block).
                    if result.location:
                        chapter_marker_msg = ChapterMarkerMessage(
                            payload=ChapterMarkerPayload(
                                title=None,
                                location=_resolve_location_display(
                                    sd.genre_pack,
                                    sd.world_slug,
                                    snapshot.party_location(perspective=_acting_for_render_trigger),
                                ),
                            ),
                            player_id=sd.player_id,
                        )
                        _emit_shared_world_frame(chapter_marker_msg, "CHAPTER_MARKER")
                        # ADR-096 Task 20b: emit TACTICAL_GRID when the world
                        # uses room_graph navigation and the new location has
                        # a room YAML file on disk. This closes the wiring
                        # gap: load_room_payload is now reachable from gameplay.
                        _maybe_emit_tactical_grid(
                            self,
                            sd=sd,
                            snapshot=snapshot,
                            actor=_acting_for_render_trigger,
                            emit_fn=_emit_shared_world_frame,
                        )
                        # Story 54-2 / ADR-109: emit LOCATION_DESCRIPTION on
                        # the same room-change branch. Carries the typed
                        # entity manifest + base prose so the UI Location
                        # tab stays in sync with the party's current room.
                        _maybe_emit_location_description(
                            self,
                            sd=sd,
                            snapshot=snapshot,
                            actor=_acting_for_render_trigger,
                            emit_fn=_emit_shared_world_frame,
                        )
                    # Beneath Sünden BETTER fix (seam 3): project the live
                    # region graph to the UI Map tab every turn (NOT gated
                    # on result.location — region moves arrive via the
                    # current_region patch, not always a location change;
                    # unconditional per-turn also covers turn 1 + resume,
                    # curing "No map data yet"). Idempotent; clean no-op
                    # for every non-beneath_sunden world.
                    _maybe_emit_dungeon_map(
                        self,
                        sd=sd,
                        snapshot=snapshot,
                        emit_fn=_emit_shared_world_frame,
                    )
                    # Story 45-1 — sealed-letter shared-world handshake.
                    # Build the canonical delta from the post-resolution
                    # snapshot and ride it on NARRATION_END so peers see
                    # ground-truth location/encounter/party formation
                    # (playtest 3 fix: stops narrator fabricating
                    # "collapsed corridor" between Orin and Blutka).
                    handshake_delta = build_shared_world_delta(
                        snapshot,
                        room=self._room,
                    )
                    # Magic Phase 4: ride the post-resolution magic_state on the
                    # NARRATION_END handshake. Sent every turn (not gated on the
                    # internal StateDelta.magic flag) — the UI is stateless on
                    # this payload and an unchanged dict is cheap; gating would
                    # silently desync the ledger after a reconnect.
                    magic_state_dict = (
                        snapshot.magic_state.model_dump(mode="json")
                        if snapshot.magic_state is not None
                        else None
                    )
                    narration_end_msg = NarrationEndMessage(
                        type="NARRATION_END",  # type: ignore[arg-type]
                        payload=NarrationEndPayload(
                            state_delta=_shared_world_delta_to_state_delta(
                                handshake_delta,
                                magic_state=magic_state_dict,
                            ),
                        ),
                        player_id=sd.player_id,
                    )
                    _emit_shared_world_frame(narration_end_msg, "NARRATION_END")

                    # MP turn-ownership clear (ADR-036 sealed-letter pacing). Pair with
                    # the TURN_STATUS{active} broadcast at action receipt — peers' banner
                    # tone="peer" stays stuck without this clear. exclude_socket_id=None
                    # so every socket (including the actor) gets the resolved signal;
                    # the UI clears activePlayerName on status="resolved".
                    if self._room is not None and sd.player_name:
                        try:
                            acting_name = _resolve_acting_character_name(sd, self._room)
                            turn_resolved_msg = TurnStatusMessage(
                                payload=TurnStatusPayload(
                                    player_name=NonBlankString(acting_name),
                                    status="resolved",
                                ),
                                player_id=sd.player_id or "",
                            )
                            self._room.broadcast(turn_resolved_msg, exclude_socket_id=None)
                            logger.info(
                                "session.turn_status_resolved player=%s player_id=%s slug=%s",
                                acting_name,
                                sd.player_id,
                                self._room.slug,
                            )
                            _watcher_publish(
                                "turn_status",
                                {
                                    "status": "resolved",
                                    "player_name": acting_name,
                                    "player_id": sd.player_id,
                                    "slug": self._room.slug,
                                },
                                component="session",
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "session.turn_status_resolved_broadcast_failed error=%s",
                                exc,
                            )

                    # Refresh PARTY_STATUS so `current_location` and any HP/inventory
                    # mutations landed by the narration apply propagate to the client
                    # header / CharacterSheet. Previously PARTY_STATUS
                    # was emitted exactly once at chargen-end (before the opening
                    # turn), which froze the location at its pre-opening value
                    # (typically empty). Playtest 2026-04-22.
                    if snapshot.characters:
                        try:
                            # MP: resolve "self" by sd.player_id, not snapshot.characters[0].
                            # The turn-end refresh fires per-socket; if the requesting
                            # socket isn't characters[0] (any non-first-committed
                            # player in MP), passing characters[0] mis-tags that PC's
                            # data with the requesting socket's player_id and the UI
                            # renders the wrong PC as "(YOU)" — playtest 2026-04-25
                            # "Tab 2 sees Laverne (YOU)".
                            self_char = (
                                views.resolve_self_character(self, sd) or snapshot.characters[0]
                            )
                            party_status = views.build_session_start_party_status(
                                self, sd, self_char, sd.player_id
                            )
                            # MP merged-dispatch: every connected socket needs the
                            # post-narration party refresh (location/HP/inventory).
                            # The dispatcher-built payload is safe to broadcast as-is —
                            # each peer's UI resolves "(YOU)" via the seat_map-tagged
                            # player_id of the member whose ``name`` matches its
                            # connectedPlayerName, not via the dispatcher's player_id
                            # field. Pre-fix-2 (peer-only broadcast), peers got it
                            # but the dispatcher's reconnected socket missed it
                            # (pingpong 2026-04-30 follow-on); single broadcast
                            # path fixes both halves.
                            _emit_shared_world_frame(party_status, "PARTY_STATUS")
                            # Wave 2B (story 45-48): the log/event location is
                            # the actor's own current scene — there is no
                            # party-frame ``snapshot.location`` anymore.
                            _ps_log_loc = (
                                snapshot.party_location(perspective=self_char.core.name) or ""
                            )
                            logger.info(
                                "state.party_status_emitted reason=turn_end location=%r turn=%d "
                                "self_char=%s",
                                _ps_log_loc,
                                snapshot.turn_manager.interaction,
                                self_char.core.name,
                            )
                            _watcher_publish(
                                "state_transition",
                                {
                                    "field": "party_status",
                                    "reason": "turn_end",
                                    "location": _ps_log_loc,
                                    "turn_number": snapshot.turn_manager.interaction,
                                    "player_id": sd.player_id,
                                },
                                component="party_status",
                            )
                        except Exception as exc:  # noqa: BLE001 — party refresh must never crash a turn
                            logger.warning("state.party_status_refresh_failed error=%s", exc)

                # Visual-scene render dispatch. Fire-and-forget: the RENDER_QUEUED
                # message ships with the NARRATION payload; the async render task
                # posts an IMAGE message onto the per-connection outbound queue
                # when the daemon replies. Short-circuits without any socket work
                # when: render flag off, no visual scene, daemon socket missing,
                # or outbound queue unavailable (test configurations that don't
                # attach room context).
                render_queued = self._maybe_dispatch_render(
                    sd,
                    result,
                    encounter_resolved_this_turn=encounter_resolved_this_turn,
                    snapshot_location_before=snapshot_location_before_apply,
                    acting_character_name=_acting_for_render_trigger,
                )
                if render_queued is not None:
                    outbound.append(render_queued)

                # Audio DJ dispatch. Synchronous: AUDIO_CUE (or nothing) ships
                # with this turn's outbound frames. No placeholder + later message
                # dance — the DJ is a local filesystem lookup. MP: same cue plays
                # for every player at the table — broadcast to peers so the music
                # bed transitions in lock-step with the shared narration.
                audio_cue = self._maybe_dispatch_audio(sd, result)
                if audio_cue is not None:
                    _emit_shared_world_frame(audio_cue, "AUDIO_CUE")

                # turn_complete is now emitted by the validator (per ADR-089 §6.7).
                # The TurnRecord assembled below is the single source of truth.

                # --- TurnRecord assembly + validator submit ---
                # Wrapped in try/except: the validator must NEVER crash the hot path.
                if self._validator is not None:
                    try:
                        _patch_summaries: list[PatchSummary] = []
                        if result.location:
                            _patch_summaries.append(
                                PatchSummary(patch_type="location", fields_changed=["location"])
                            )
                        if result.quest_updates:
                            _patch_summaries.append(
                                PatchSummary(
                                    patch_type="quest",
                                    fields_changed=list(result.quest_updates),
                                )
                            )
                        if result.lore_established:
                            _patch_summaries.append(
                                PatchSummary(patch_type="lore", fields_changed=["lore_established"])
                            )
                        if result.npcs_present:
                            _patch_summaries.append(
                                PatchSummary(
                                    patch_type="npc_pool",
                                    fields_changed=[n.name for n in result.npcs_present],
                                )
                            )
                        if result.items_gained or result.items_lost:
                            _patch_summaries.append(
                                PatchSummary(patch_type="inventory", fields_changed=[])
                            )

                        _beats_fired: list[tuple[str, float]] = []
                        for beat in result.beat_selections or []:
                            _beats_fired.append(
                                (
                                    getattr(beat, "trope_id", None)
                                    or getattr(beat, "beat_id", None)
                                    or "unknown",
                                    float(getattr(beat, "threshold", 0.0) or 0.0),
                                )
                            )

                        timings.mark_done()
                        record = TurnRecord(
                            turn_id=snapshot.turn_manager.interaction,
                            timestamp=datetime.now(UTC),
                            player_id=sd.player_id,
                            player_input=action,
                            classified_intent="unknown",  # TODO: tighten when LocalDM exposes intent
                            agent_name=result.agent_name or "narrator",
                            narration=result.narration or "",
                            patches_applied=_patch_summaries,
                            snapshot_before_hash=snapshot_before_hash,
                            snapshot_after=snapshot,
                            delta=None,  # TODO: tighten when StateDelta is wired
                            beats_fired=_beats_fired,
                            extraction_tier=1,  # TODO: map result.prompt_tier to int tier
                            token_count_in=result.token_count_in or 0,
                            token_count_out=result.token_count_out or 0,
                            agent_duration_ms=result.agent_duration_ms or 0,
                            is_degraded=result.is_degraded,
                            phase_durations_ms=timings.to_dict(),
                            phase_call_counts=timings.phase_call_counts,
                            total_duration_ms=timings.total_ms,
                            footnotes_count=len(result.footnotes or []),
                        )
                        await self._validator.submit(record)
                        submitted = True
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("turn_record.assemble_failed: %s", exc)

                # Per-turn `game_state_snapshot` for the dashboard State tab
                # (playtest 2026-04-30 #1C). Pre-fix this event was published
                # only at session connect / chargen confirmation — after the
                # initial fire the State tab read "Waiting for
                # GameStateSnapshot event..." forever. Per ADR-031 the watcher
                # is supposed to tick every turn so the GM panel can verify
                # state advancement; this publish closes that gap. Wrapped in
                # try/except so a serialization issue cannot crash the hot
                # turn path.
                try:
                    _watcher_publish(
                        "game_state_snapshot",
                        {
                            "reason": "turn",
                            "genre_slug": sd.genre_slug,
                            "world_slug": sd.world_slug,
                            "player_name": sd.player_name,
                            "player_id": sd.player_id,
                            "turn_number": snapshot.turn_manager.interaction,
                            # Full snapshot dump so the dashboard's State
                            # panel can render characters / NPCs / inventory
                            # / known facts / regions etc. Pre-fix the
                            # event payload was a thin summary
                            # (counts only) and the State panel could
                            # not draw any of its rich UI even when the
                            # event did fire at connect.
                            "snapshot": snapshot.model_dump(mode="json"),
                            # Back-compat summary fields the connect-time
                            # publishes have always exposed. Wave 2B (story
                            # 45-48): use the per-actor location for the
                            # dashboard's "current_location" — the dashboard
                            # is per-player.
                            "current_location": (
                                snapshot.party_location(
                                    perspective=snapshot.player_seats.get(sd.player_id, "")
                                )
                                or snapshot.party_location()
                                or ""
                            ),
                            "discovered_regions": list(snapshot.discovered_regions),
                            "npc_pool_count": len(snapshot.npc_pool),
                            "quest_log_count": len(snapshot.quest_log),
                            "lore_established_count": len(snapshot.lore_established),
                            "character_count": len(snapshot.characters),
                        },
                        component="game",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "game_state_snapshot.publish_failed turn=%d error=%s",
                        snapshot.turn_manager.interaction,
                        exc,
                    )

                return outbound
        finally:
            # Capture timings even when the turn raised — phase data is the
            # diagnostic signal we most need on failure paths.
            import contextlib  # noqa: PLC0415 — local import keeps module import lean

            with contextlib.suppress(Exception):  # finally must never re-raise
                timings.mark_done()

            # Per-turn watcher→OTLP bridge diagnostic + flush. Two birds:
            # (1) prove the bridge fired during this turn — a non-zero
            # ``minted`` value is hard evidence that publish_event saw the
            # ``SIDEQUEST_WATCHER_AS_SPANS`` flag and minted synthetic spans,
            # closing the "is the bridge live during gameplay?" question that
            # the resume-only Jaeger output kept open. (2) force a tracer
            # flush so the BatchSpanProcessor (default 2 s schedule) doesn't
            # hide turn-level spans from a live Jaeger viewer for the next
            # batch window. Both actions are wrapped in suppress() because
            # diagnostics must NEVER fail a turn.
            with contextlib.suppress(Exception):
                minted = synthetic_spans_count() - bridge_minted_at_start
                logger.info(
                    "turn.bridge_diagnostic minted=%d turn=%d player=%s genre=%s world=%s",
                    minted,
                    snapshot.turn_manager.interaction,
                    sd.player_id,
                    sd.genre_slug,
                    sd.world_slug,
                )
            with contextlib.suppress(Exception):
                provider = trace.get_tracer_provider()
                # ``force_flush`` is on the SDK ``TracerProvider``; the proxy
                # provider used in tests / pre-init paths doesn't have it. A
                # hasattr check avoids importing the SDK class here just to
                # isinstance-check it.
                flush = getattr(provider, "force_flush", None)
                if callable(flush):
                    flush(timeout_millis=200)

            if not submitted and self._validator is not None:
                try:
                    degraded_record = TurnRecord(
                        turn_id=snapshot.turn_manager.interaction,
                        timestamp=datetime.now(UTC),
                        player_id=sd.player_id,
                        player_input=action,
                        classified_intent="unknown",
                        agent_name="narrator",
                        narration="",
                        patches_applied=[],
                        snapshot_before_hash=snapshot_before_hash,
                        snapshot_after=snapshot,
                        delta=None,
                        beats_fired=[],
                        extraction_tier=0,
                        token_count_in=0,
                        token_count_out=0,
                        agent_duration_ms=0,
                        is_degraded=True,
                        phase_durations_ms=timings.to_dict(),
                        phase_call_counts=timings.phase_call_counts,
                        total_duration_ms=timings.total_ms,
                    )
                    await self._validator.submit(degraded_record)
                except Exception:  # noqa: BLE001
                    logger.exception("turn_record.degraded_submit_failed")

    async def _run_opening_turn_narration(
        self,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        """Fire the opening narration turn at the end of chargen.

        Consumes ``sd.opening_seed`` + ``sd.opening_directive`` exactly
        once. The seed becomes the first "action" string; the directive
        is injected into the narrator's Early zone for this turn only.
        Both fields are zeroed on the session after the turn runs so
        the next PLAYER_ACTION turn sees a fresh context.

        When no opening hook was resolved at connect time (pack has no
        openings) the Rust dispatcher substitutes a generic
        "I look around and take in my surroundings." Match that — the
        narrator still fires so the player lands in the world rather
        than at a blank UI.

        Rust parity: connect.rs:2270-2529.
        """
        # Consume-time MP-joiner suppression (playtest 2026-04-26
        # [S2-BUG] coyote_star regression). The connect-time guard in
        # ``_handle_connect`` only fires when the joiner connects AFTER
        # the host has completed chargen — checking
        # ``len(snapshot.characters) > 0`` at connect-time. In the more
        # common race scenario (both players in lobby together, both
        # walking chargen at the same time) the joiner's ``sd.opening_
        # seed/directive`` get populated at connect because the snapshot
        # was empty, and only the timing of chargen-completion decides
        # who's first vs. second. This guard catches the second
        # committer at consume-time: by the time we get here, the joiner's
        # PC is already in ``sd.snapshot.characters`` (appended in the
        # second-commit branch around line 2725), so the test is "more
        # than just me" → at least one peer character is present →
        # suppress the cold-open and fall back to the generic continuation
        # action so the persistent narrator (ADR-067) treats this as
        # scene continuation, not a fresh in-medias-res open.
        if sd.opening_seed is not None and len(sd.snapshot.characters) > 1:
            _watcher_publish(
                "mp_joiner_opening_suppressed_at_consume",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": player_id,
                    "player_name": sd.player_name,
                    "character_count": len(sd.snapshot.characters),
                    "had_seed": True,
                    "had_directive": sd.opening_directive is not None,
                },
                component="opening_hook",
                severity="info",
            )
            logger.info(
                "session.mp_joiner_opening_suppressed_at_consume "
                "genre=%s world=%s player=%s character_count=%d",
                sd.genre_slug,
                sd.world_slug,
                sd.player_name,
                len(sd.snapshot.characters),
            )
            span.add_event(
                "mp_joiner_opening_suppressed_at_consume",
                {
                    "event": "mp_joiner_opening_suppressed_at_consume",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": player_id,
                    "character_count": len(sd.snapshot.characters),
                },
            )
            sd.opening_seed = None
            sd.opening_directive = None

        # Playtest 2026-04-29 BUG-LOW: when MP joiner-orientation fires (the
        # suppression branch above just zeroed the seed), the previous
        # fallback "I look around and take in my surroundings." gave the
        # narrator a generic, unattributed action — and the resulting
        # narration treated the host PC as if THEY had performed it
        # ("Laverne is in the pilot's couch, hands flat on her thighs..."),
        # because the narrator had no anchor for whose POV the orientation
        # belonged to. Naming the joining PC explicitly fixes the POV
        # attribution; the SOUL.md Agency strengthening (sibling fix in
        # this playtest cycle) keeps the narrator from inventing dialogue
        # for either PC. We resolve the joining PC's character name from
        # the snapshot — it was just appended in the second-commit branch,
        # so the joiner is the LAST entry in ``snapshot.characters``.
        joiner_orientation = sd.opening_seed is None and len(sd.snapshot.characters) > 1
        if joiner_orientation:
            # Per-PC POV anchoring works for the 2P "P2 joins P1's
            # in-progress game" case the original fix targeted, but
            # produces jarring single-PC framing for fresh 3+ player
            # openings (playtest 2026-04-30 BUG-LOW: opening narration
            # anchored on Linus, the last/4th committer, when the four
            # PCs all completed chargen together — Sebastien-class
            # incoherence between "you have the floor" per-tab banners
            # and a single-PC POV opening). For 3+ PCs use omniscient
            # framing that introduces the party as a group rather than
            # picking one arbitrary anchor; preserve the per-PC anchor
            # for the original 2P case (one incumbent + one joiner).
            if len(sd.snapshot.characters) >= 3:
                action = (
                    "The party has assembled in the scene — describe the "
                    "shared surroundings in a brief grounding paragraph "
                    "that establishes the location and mood for everyone "
                    "present. Use omniscient/scene-wide framing, not any "
                    "single character's point of view. Do not generate "
                    "dialogue, decisions, or new actions for any PC."
                )
                source_tier = "mp_party_orientation"
            else:
                joiner_char_name = (
                    sd.snapshot.characters[-1].core.name
                    if sd.snapshot.characters
                    else (sd.player_name or "the new arrival")
                )
                # Playtest 2026-05-02 [BUG-LOW]: joiner-orientation drifted
                # off the established scene (host on the Kestrel cockpit;
                # joiner improvised at Vaskov Centrum East Freight Stair).
                # The chargen confirmation epilogue promises "the crew is
                # the crew" — both PCs aboard the same chassis at session
                # start — and the canned MP opening (mp_galley_jumprest)
                # anchors the host aboard the Kestrel. Anchor the joiner
                # explicitly to the location the host's prior turn already
                # established so the narrator does not invent a new place
                # for the second PC. Falls back to "the same scene the
                # other player(s) are in" when no seated PC has a known
                # location yet (degenerate path, but defensible).
                #
                # Wave 2B (story 45-48): pick the location from any
                # already-seated non-joiner PC's per-character entry.
                # ``party_location()`` would return None here because the
                # just-chargen'd joiner doesn't have an entry yet.
                host_location = ""
                for _seated_char in sd.snapshot.player_seats.values():
                    if _seated_char and _seated_char != joiner_char_name:
                        _here = sd.snapshot.character_locations.get(_seated_char)
                        if _here:
                            host_location = _here.strip()
                            break
                where_clause = (
                    f"into the location the prior turn established ({host_location!r})"
                    if host_location
                    else "into the same scene the other player(s) are already in"
                )
                action = (
                    f"{joiner_char_name} steps into the scene and orients to "
                    f"the surroundings — describe their arrival {where_clause} "
                    "from their point of view in a brief grounding paragraph. "
                    "Do NOT relocate them to a new location. Do NOT generate "
                    "dialogue, decisions, or new actions for any other PC "
                    "already present."
                )
                source_tier = "mp_joiner_orientation"
                # OTEL: surface the anchor decision so the GM panel can
                # verify the joiner's prompt actually carried the host's
                # location (CLAUDE.md OTEL principle — Sebastien's
                # lie-detector). Mirror the watcher_publish payload as a
                # span.add_event so OTLP exporters (Jaeger / in-memory
                # test exporter) see it without needing
                # SIDEQUEST_WATCHER_AS_SPANS=1.
                anchor_kind = "host_location" if host_location else "fallback_same_scene"
                _watcher_publish(
                    "mp_joiner_orientation_anchored",
                    {
                        "genre": sd.genre_slug,
                        "world": sd.world_slug,
                        "joiner_char_name": joiner_char_name,
                        "host_location": host_location or None,
                        "anchor_kind": anchor_kind,
                    },
                    component="opening_hook",
                    severity="info",
                )
                span.add_event(
                    "mp_joiner_orientation_anchored",
                    {
                        "event": "mp_joiner_orientation_anchored",
                        "genre": sd.genre_slug,
                        "world": sd.world_slug,
                        "joiner_char_name": joiner_char_name,
                        "host_location": host_location or "",
                        "anchor_kind": anchor_kind,
                    },
                )
        else:
            action = sd.opening_seed or "I look around and take in my surroundings."
            source_tier = "world_or_genre_hook" if sd.opening_seed else "fallback"

        # Cold-open delivery (playtest 2026-04-25 [P2]). The opening seed
        # is in-medias-res prose the world author wrote for the player to
        # READ — not narrator prompt-context. Previously it was passed only
        # as `action='...'` and the narrator silently consumed it: long
        # hooks got truncated (player saw "the iron door grinds inward" but
        # never the kidnapping setup), short hooks survived expansion by
        # accident. The contract was broken either way.
        #
        # Fix: emit the seed directly to the player as a NARRATION message
        # BEFORE running the narrator. The narrator's first turn still
        # receives the seed as `action` and continues from where the hook
        # ends — so what the player sees is hook + continuation as a
        # single coherent opening beat, instead of the hook being a ghost
        # in the prompt. Suppressed when the pack has no opening hook
        # (the fallback "I look around…" is the player's implicit action,
        # not authored cold-open prose).
        cold_open_messages: list[object] = []
        if sd.opening_seed:
            cold_open_messages.append(
                NarrationMessage(
                    payload=NarrationPayload(text=NonBlankString(sd.opening_seed)),
                )
            )
            _watcher_publish(
                "cold_open_emitted",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "seed_len": len(sd.opening_seed),
                },
                component="opening_hook",
                severity="info",
            )

        lore_context = await self._retrieve_lore_for_turn(sd, action)
        turn_context = _build_turn_context(
            sd,
            opening_directive=sd.opening_directive,
            lore_context=lore_context,
            room=self._room,
        )

        span.add_event(
            "opening_turn.dispatched",
            {
                "event": "opening_turn.dispatched",
                "has_directive": sd.opening_directive is not None,
                "seed_source": source_tier,
                "action_len": len(action),
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "cold_open_emitted": bool(cold_open_messages),
            },
        )

        narrator_messages = await self._execute_narration_turn(
            sd,
            action,
            turn_context,
            is_opening_turn=True,
        )
        messages = cold_open_messages + list(narrator_messages)

        # Canned-openings Phase 4 (Task 19): emit opening.played span at
        # consumption so the GM panel can verify the canned opening
        # actually reached the narrator's first turn rather than being
        # silently dropped. Only fires when a directive was actually
        # rendered (skips MP-joiner / no-opening / fallback paths).
        if sd.opening_directive is not None:
            record_opening_played(
                opening_id=getattr(sd, "_resolved_opening_id", None) or "<unknown>",
                turn_id=sd.snapshot.turn_manager.interaction,
            )

        # Consume once — Rust uses `opening_directive.take()`; subsequent
        # turns must run directive-free. Same for the seed: it's a
        # one-shot bootstrap action, not a recurring input.
        sd.opening_seed = None
        sd.opening_directive = None

        return messages

    # ------------------------------------------------------------------
    # Audio DJ backend construction
    # ------------------------------------------------------------------

    def _build_audio_backend(
        self,
        genre_slug: str,
        genre_pack: GenrePack,
    ) -> LibraryBackend | None:
        """Construct the per-session LibraryBackend, or None when the
        genre pack has no resolvable on-disk audio directory.

        Emits a watcher event when audio is disabled so the GM panel
        can tell whether a silent turn is because the narration had
        no cues or because audio is off entirely."""
        try:
            pack_dir = GenreLoader().find(genre_slug)
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash connect
            # Span emission replaces the prior direct ``_watcher_publish`` —
            # ``WatcherSpanProcessor`` re-emits via
            # ``SPAN_ROUTES[SPAN_AUDIO_BACKEND_DISABLED]``.
            with audio_backend_disabled_span(
                reason="pack_dir_missing",
                genre=genre_slug,
            ):
                logger.warning(
                    "audio.backend_skipped reason=pack_dir_missing genre=%s error=%s",
                    genre_slug,
                    exc,
                )
            return None

        audio_cfg = genre_pack.audio
        if not audio_cfg.mood_tracks and not audio_cfg.themes and not audio_cfg.sfx_library:
            with audio_backend_disabled_span(
                reason="empty_config",
                genre=genre_slug,
            ):
                logger.info(
                    "audio.backend_skipped reason=empty_config genre=%s",
                    genre_slug,
                )
            return None

        with audio_backend_enabled_span(
            genre=genre_slug,
            mood_count=len(audio_cfg.mood_tracks) + len(audio_cfg.themes),
            sfx_count=len(audio_cfg.sfx_library),
        ):
            logger.info(
                "audio.backend_ready genre=%s pack_dir=%s",
                genre_slug,
                pack_dir,
            )
        return LibraryBackend(audio_cfg, base_path=pack_dir)

    # ------------------------------------------------------------------
    # Visual-scene render dispatch
    # ------------------------------------------------------------------

    def _maybe_dispatch_render(
        self,
        sd: _SessionData,
        result: object,
        *,
        encounter_resolved_this_turn: bool = False,
        snapshot_location_before: str | None = None,
        acting_character_name: str | None = None,
    ) -> RenderQueuedMessage | None:
        """Fire a render request at the media daemon if the trigger policy
        classifies this turn as eligible (Story 45-30).

        Returns a ``RenderQueuedMessage`` to append to the turn's outbound
        frames, or ``None`` when nothing was dispatched (policy chose
        none_policy, feature flag off, daemon offline, or no outbound queue).

        The actual daemon round-trip runs on a background task; the IMAGE
        reply lands on ``self._out_queue`` whenever the render completes.
        Failures are swallowed with OTEL spans + watcher events so that no
        render error ever crashes a turn.

        Args:
            encounter_resolved_this_turn: ``True`` when an active encounter
                transitioned to ``resolved`` on this turn. Threaded from
                the ``narration_apply`` seam — the caller compares pre- and
                post-apply state to derive this signal. Default ``False``
                lets test fixtures and the throttled-by-other-gates code
                path call without rewiring.
            snapshot_location_before: The acting PC's location BEFORE
                ``_apply_narration_result_to_snapshot`` mutated it (Wave
                2B uses ``snapshot.party_location(perspective=acting)``).
                The production caller captures this; tests that call
                directly may omit it (defaults to the party consensus
                accessor, which is correct when the test never applies
                narration). The classifier needs the pre-apply value to
                detect SCENE_CHANGE.
        """
        from sidequest.agents.orchestrator import NarrationTurnResult
        from sidequest.server.render_trigger import (
            RenderTriggerReason,
            classify_trigger,
        )

        if not isinstance(result, NarrationTurnResult):
            return None

        visual = result.visual_scene
        had_visual_scene = visual is not None
        subject_present = had_visual_scene and bool(getattr(visual, "subject", "").strip())

        # Policy gate (Story 45-30) — classify the trigger reason from the
        # structured signals already on NarrationTurnResult plus the
        # out-of-band ``encounter_resolved_this_turn`` boolean. The
        # narrator's ``visual_scene`` block is NOT a signal; pre-story
        # behaviour gated on it and let banter turns render while named-
        # NPC introductions did not.
        location_before = (
            snapshot_location_before
            if snapshot_location_before is not None
            else sd.snapshot.party_location(perspective=sd.player_name)
        )
        reason = classify_trigger(
            result,
            snapshot_location_before=location_before,
            encounter_resolved_this_turn=encounter_resolved_this_turn,
        )

        turn_number = sd.snapshot.turn_manager.interaction

        # NONE_POLICY: emit both render.trigger (with eligible=False,
        # queued=False) AND the focused render.policy_skip event. Per
        # CLAUDE.md OTEL Observability Principle, silence is the bug —
        # the GM panel needs the negative confirmation that the policy
        # ran on this turn. Pattern matches the existing render
        # throttle_decision watcher events: event_type=state_transition,
        # field=render, op=<route key>. The SPAN_ROUTES entry for
        # render.trigger is a static registry check (asserts the route
        # is declared); the actual emission happens here so it works
        # without an OTEL span being opened.
        if reason is RenderTriggerReason.NONE_POLICY:
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "trigger",
                    "reason": reason.value,
                    "eligible": False,
                    "queued": False,
                    "turn_number": turn_number,
                    "player_id": sd.player_id,
                    "had_visual_scene": had_visual_scene,
                    "subject_present": subject_present,
                },
                component="render",
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "policy_skip",
                    "reason": reason.value,
                    "turn_number": turn_number,
                    "player_id": sd.player_id,
                    # Distinguishes "narrator didn't even try" from
                    # "narrator emitted a subject but no policy match".
                    "narrator_emitted_subject": subject_present,
                },
                component="render",
            )
            return None

        # Eligible — emit the trigger event before any downstream gate so
        # the GM panel sees the policy decision even when the feature
        # flag / daemon / queue refuses below.
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "trigger",
                "reason": reason.value,
                "eligible": True,
                # ``queued`` reflects whether dispatch will actually proceed.
                # We set True optimistically here; if a downstream gate
                # refuses synchronously, that path emits its own watcher
                # event and we don't retroactively edit this one.
                "queued": True,
                "turn_number": turn_number,
                "player_id": sd.player_id,
                "had_visual_scene": had_visual_scene,
                "subject_present": subject_present,
            },
            component="render",
        )

        # Eligible turns still need a visual_scene to actually compose a
        # prompt — the policy says "the narrative weight earned a render"
        # but the prompt-building code requires a subject string. When
        # the narrator didn't emit one (narrative weight present but
        # subject missing), we cannot dispatch — log loudly and return.
        if not subject_present:
            logger.warning(
                "render.eligible_no_subject reason=%s turn=%d — "
                "policy fired but narrator emitted no visual_scene subject",
                reason.value,
                turn_number,
            )
            return None

        if not render_enabled():
            logger.info("render.skipped reason=feature_flag_disabled")
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "skipped",
                    "reason": "feature_flag_disabled",
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
            )
            return None
        client = DaemonClient()
        # Story 45-31: the turn pipeline already consulted the
        # daemon-state mirror immediately before the scrapbook emit
        # and stamped ``sd.render_unavailable_pending`` accordingly.
        # The SCRAPBOOK_ENTRY for this turn already carries
        # ``render_status="unavailable"`` (live broadcast + DB row in
        # one shot — no duplicate row, no separate replay JOIN).
        # All this branch needs to do is: emit the watcher event so
        # the GM panel sees the substitution, increment counters, and
        # return None to skip the daemon round-trip.
        if sd.render_unavailable_pending:
            from sidequest.daemon_client.state_mirror import get_mirror as _get_mirror

            _mirror = _get_mirror()
            sd.render_unresponsive_window_count += 1
            logger.warning(
                "render.unavailable reason=heartbeat_lost last_ts=%s turn=%d",
                _mirror.last_heartbeat_ts(),
                sd.snapshot.turn_manager.interaction,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "unavailable",
                    "reason": "heartbeat_lost",
                    "last_heartbeat_ts": _mirror.last_heartbeat_ts(),
                    "turn_number": sd.snapshot.turn_manager.interaction,
                    "player_id": sd.player_id,
                },
                component="render",
                severity="warning",
            )
            return None
        if not client.is_available():
            logger.warning(
                "render.skipped reason=daemon_unavailable socket=%s",
                client.socket_path,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "skipped",
                    "reason": "daemon_unavailable",
                    "socket": str(client.socket_path),
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
                severity="warning",
            )
            return None
        if self._out_queue is None:
            # Test configurations that don't attach room context can't
            # receive async IMAGE frames. Skip loudly so we don't fire
            # a render whose result has nowhere to land.
            logger.warning("render.skipped reason=no_outbound_queue")
            return None

        # ADR-050 image pacing throttle. Consult BEFORE allocating a
        # render_id or touching the daemon — suppressed renders should
        # leave no trace beyond the OTEL decision event.
        throttle_decision = sd.image_pacing_throttle.should_render()
        provisional_render_id = uuid.uuid4().hex[:12]
        if not throttle_decision.allowed:
            logger.info(
                "render.throttled render_id=%s reason=%s remaining=%ds",
                provisional_render_id,
                throttle_decision.reason,
                throttle_decision.cooldown_remaining_seconds,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "throttle_decision",
                    "decision": "suppress",
                    "reason": throttle_decision.reason,
                    "render_id": provisional_render_id,
                    "cooldown_remaining_seconds": (throttle_decision.cooldown_remaining_seconds),
                    "cooldown_seconds": sd.image_pacing_throttle.cooldown_seconds,
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
            )
            return None
        # Allowed — emit the allow decision so the GM panel can see both
        # branches in the OTEL stream (lie-detector requirement per
        # CLAUDE.md OTEL Observability Principle).
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "throttle_decision",
                "decision": "allow",
                "reason": throttle_decision.reason,
                "render_id": provisional_render_id,
                "cooldown_seconds": sd.image_pacing_throttle.cooldown_seconds,
                "turn_number": sd.snapshot.turn_manager.interaction,
            },
            component="render",
        )

        render_id = provisional_render_id
        tier = (visual.tier or "scene_illustration").strip() or "scene_illustration"

        # The location is free-form narrator prose (e.g. "The Kestrel —
        # Galley, Mid-Coast", "Engine Bay"), not a `where:<slug>`
        # PlaceCatalog ref. The daemon's PromptComposer `_resolve_location`
        # accepts an empty location (transient setting, subject prose
        # carries it) and rejects anything else with ValueError →
        # COMPOSE_FAILED. Sanitize once, centrally, before the per-tier
        # branches: only true `where:<slug>` refs survive. Free-form prose
        # is dropped to "" with a loud watcher event so the GM panel can
        # see the contract gap (per CLAUDE.md "no silent fallbacks" + OTEL
        # observability principle). Wave 2B (story 45-48): the source is
        # the acting PC's per-character location; party-frame consensus
        # is the fallback when the caller didn't thread an actor.
        raw_location = (
            sd.snapshot.party_location(perspective=acting_character_name)
            if acting_character_name
            else sd.snapshot.party_location()
        ) or ""
        raw_location = raw_location.strip()
        sanitized_location = raw_location if raw_location.startswith("where:") else ""
        if raw_location and not sanitized_location:
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "location_dropped",
                    "reason": "free_form_prose_no_catalog_ref",
                    "raw_location": raw_location[:120],
                    "tier": tier,
                    "render_id": render_id,
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
                severity="info",
            )

        # R2 migration Task 20: propagate the session id into the daemon's
        # render params so the artifact upload key
        # ``artifacts/<world>/<session>/<kind>/<sha>.<ext>`` carries the
        # real session segment. The daemon's zimage worker reads
        # ``params["session_id"]`` and falls back to the literal
        # ``"unknown"`` when missing — defeating per-session bucketing,
        # save-aware sweeping, and operational forensics. The slug-connect
        # path (the only production render-eligible path) always populates
        # ``sd.game_slug``; ``sd._room.slug`` is the same value and used
        # as the source of truth so we don't depend on an optional field.
        # Per "No Silent Fallbacks" we refuse to dispatch with a missing
        # session id rather than papering over it with a placeholder —
        # the dispatch-eligible code path is always inside an active room.
        if self._room is not None:
            session_id = self._room.slug
        elif sd.game_slug is not None:
            session_id = sd.game_slug
        else:
            raise RuntimeError(
                "render dispatch fired without a bound session id "
                "(no room and no sd.game_slug) — slug-connect path "
                "should have populated this. Refusing to dispatch with "
                "a fallback that would corrupt R2 artifact keying."
            )
        params: dict[str, object] = {
            "tier": tier,
            "subject": visual.subject,
            "mood": visual.mood or "",
            "tags": list(visual.tags or []),
            "location": sanitized_location,
            "narration": result.narration,
            "genre": sd.genre_slug,
            # Catalog-injected compose wiring (slice 1): the daemon scopes
            # CharacterCatalog / PlaceCatalog / StyleCatalog by (genre, world).
            # Without this field the daemon's compose conditional is dead and
            # every render falls through to the prose-subject prompt path.
            #
            # Bug #2a (playtest 2026-04-26) reinforced the same constraint:
            # the daemon's PromptComposer gate at
            # sidequest-daemon/sidequest_daemon/media/daemon.py:453 short-
            # circuits when ``params["world"]`` is absent, falling back to a
            # raw subject+mood+tags prompt with no genre/world style. That
            # silent fallback is why grimvault renders looked generic.
            # Sending ``world`` engages the explicit-recipes pipeline so the
            # world-scoped ``visual_style.yaml::positive_suffix`` actually
            # lands in the ART_SENSIBILITY.WORLD slot.
            "world": sd.world_slug,
            # R2 migration Task 20 — see preamble above.
            "session_id": session_id,
        }
        # Portrait initials overlay (story 37-30 AC-4): the daemon's
        # portrait composer needs the character's display name to draw
        # the initials card. Other tiers ignore the field.
        if tier == "portrait":
            params["subject_name"] = sd.player_name
            # Catalog-injected compose, slice 2: emit a structured `pc:<slug>`
            # ref so the daemon's PromptComposer routes the portrait through
            # the catalog instead of falling through to the prose-subject
            # path. When the snapshot has a Character to project, we ship a
            # descriptor blob alongside; the daemon's `_get_composer` calls
            # `CharacterCatalog.add_pc` from it.
            pc_slug = _slugify_player_name(sd.player_name)
            params["characters"] = [f"pc:{pc_slug}"]
            descriptor = _build_pc_descriptor(sd, pc_slug)
            if descriptor is not None:
                params["pc_descriptor"] = descriptor
        elif tier == "scene_illustration":
            pc_slug = _slugify_player_name(sd.player_name)
            # Match daemon's `build_cue_from_params` read key. The portrait
            # branch above sets `characters`; this branch was previously
            # setting `participants`, which the daemon never reads — so the
            # PC ref never reached the composer's casting layer. The on-
            # the-wire field is `characters` for both tiers; the daemon
            # routes to portrait/illustration recipes by tier.
            params["characters"] = [f"pc:{pc_slug}"]
            descriptor = _build_pc_descriptor(sd, pc_slug)
            if descriptor is not None:
                params["pc_descriptor"] = descriptor

        # Story 37-30 — record the (room_slug, player_id) mapping at
        # dispatch so the completion handler can route the IMAGE through
        # the live RoomRegistry queue instead of a closure-captured one
        # that may have gone stale across a reconnect.
        room_slug = self._room.slug if self._room is not None else None
        player_id = sd.player_id
        # Playtest 2026-05-02: capture the dispatch-time turn_id so the
        # render-completed handler can backfill the matching
        # scrapbook_entries row's image_url (the live broadcast is
        # ephemeral; replay-on-reload misses every IMAGE without this).
        dispatch_turn_id = int(sd.snapshot.turn_manager.interaction)

        logger.info(
            "render.dispatched render_id=%s tier=%s subject=%r",
            render_id,
            tier,
            visual.subject[:80],
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "dispatched",
                "render_id": render_id,
                "tier": tier,
                "subject": visual.subject[:120],
                "turn_number": sd.snapshot.turn_manager.interaction,
                "player_id": player_id,
                "room_slug": room_slug or "",
                # Bug #2a lie-detector: surface the genre/world routing the
                # daemon will see. If ``world`` is empty here, the daemon's
                # PromptComposer gate will short-circuit and the render will
                # silently fall back to a styleless prompt.
                "genre": sd.genre_slug,
                "world": sd.world_slug,
            },
            component="render",
        )

        # Story 45-31: backpressure check — orthogonal to ADR-050 throttle.
        # The throttle gates time-since-last-dispatch; backpressure gates
        # concurrent in-flight depth so a daemon already swamped with
        # renders gets a loud warn (and counter increment) before the
        # 4th render piles in. Default threshold = 3; warn-mode lets
        # the request through, conservative-tunable reject-mode is left
        # for a follow-up.
        sd.render_enqueue_count += 1
        in_flight_after = sd.render_in_flight + 1
        backpressure_threshold = 3
        if in_flight_after > backpressure_threshold:
            sd.render_backpressure_warn_count += 1
            logger.warning(
                "render.enqueue.backpressure render_id=%s queue_depth=%d threshold=%d",
                render_id,
                in_flight_after,
                backpressure_threshold,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "enqueue.backpressure",
                    "decision": "warn",
                    "queue_depth": in_flight_after,
                    "threshold": backpressure_threshold,
                    "render_id": render_id,
                    "turn_number": sd.snapshot.turn_manager.interaction,
                    "player_id": sd.player_id,
                },
                component="render",
                severity="warning",
            )
        # Increment the in-flight counter; ``_run_render`` decrements
        # it in the background task's finally block.
        sd.render_in_flight = in_flight_after

        # Capture the legacy out_queue only as a fallback for the
        # pre-room-context test/legacy path. When `_room` is set (the
        # production slug-connect path) the completion handler looks the
        # queue up via the registry instead.
        legacy_queue = self._out_queue if room_slug is None else None
        asyncio.create_task(
            self._run_render(
                client,
                params,
                render_id,
                room_slug,
                player_id,
                legacy_queue,
                dispatch_turn_id,
                sd,
            )
        )
        # ADR-050 — record the dispatch *after* the task is created so the
        # cooldown only starts ticking on actually-dispatched renders.
        # ``force_render`` callers intentionally skip this call to leave the
        # cadence untouched; this code path is the organic dispatch only.
        sd.image_pacing_throttle.record_render()

        return RenderQueuedMessage(
            type=MessageType.RENDER_QUEUED,  # type: ignore[arg-type]
            payload=RenderQueuedPayload(render_id=render_id),
            player_id=player_id,
        )

    # ------------------------------------------------------------------
    # Audio DJ dispatch — runs after NARRATION, ships AUDIO_CUE alongside.
    # Synchronous filesystem lookup; no daemon round-trip, no placeholder
    # message. See docs/superpowers/specs/2026-04-23-audio-dj-wiring-design.md
    # ------------------------------------------------------------------

    def _maybe_dispatch_audio(
        self,
        sd: _SessionData,
        result: object,
    ) -> AudioCueMessage | None:
        """Run the DJ: interpret narration → resolve tracks → return an
        AudioCueMessage, or None if any precondition fails. Best-effort;
        exceptions are caught and logged so audio never crashes a turn."""
        from sidequest.agents.orchestrator import NarrationTurnResult

        if not isinstance(result, NarrationTurnResult):
            return None
        if sd.audio_backend is None:
            self._audio_skip(sd, "no_audio_config")
            return None
        narration = (result.narration or "").strip()
        if not narration:
            self._audio_skip(sd, "no_narration")
            return None

        try:
            # Keep the span open across interpret + payload build so its
            # attributes can carry the *final* DJ decision (mood/track/
            # sfx). Playtest 2026-04-24 "sidequest.audio.dispatch span has
            # zero attributes — blind OTEL" — the prior impl opened the
            # span with no attributes and the GM panel couldn't tell why
            # the client was firing "Unable to decode audio data".
            with tracer.start_as_current_span("sidequest.audio.dispatch") as span:
                span.set_attribute("genre", sd.genre_slug)
                span.set_attribute("turn_number", sd.snapshot.turn_manager.interaction)
                cues = _AUDIO_INTERPRETER.interpret(
                    narration,
                    sd.audio_backend._config,  # type: ignore[attr-defined]
                )
                payload = build_audio_cue_payload(
                    cues,
                    audio_backend=sd.audio_backend,
                    genre_slug=sd.genre_slug,
                )
                # Emit the resolved cue shape so the GM panel can correlate
                # a turn's dispatch with the client-side decode errors.
                span.set_attribute("mood", payload.mood or "")
                span.set_attribute("music_track", payload.music_track or "")
                span.set_attribute("sfx_count", len(payload.sfx_triggers))
                if payload.sfx_triggers:
                    # Spans accept list attributes; truncate to keep the
                    # trace payload bounded even with long SFX batches.
                    span.set_attribute(
                        "sfx_triggers",
                        list(payload.sfx_triggers[:16]),
                    )
                span.set_attribute(
                    "reason",
                    "empty_cues"
                    if payload.mood is None and not payload.sfx_triggers
                    else "dispatched",
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash a turn
            logger.warning("audio.dispatch_failed error=%s", exc)
            self._audio_skip(sd, "error", extra={"error": type(exc).__name__})
            return None

        if payload.mood is None and not payload.sfx_triggers:
            self._audio_skip(sd, "empty_cues")
            return None

        self._audio_dispatched(sd, payload)
        return AudioCueMessage(
            payload=payload,
            player_id=sd.player_id,
        )

    def _audio_skip(
        self,
        sd: _SessionData,
        reason: str,
        *,
        extra: dict[str, object] | None = None,
    ) -> None:
        # Span emission replaces the prior direct ``_watcher_publish`` —
        # ``WatcherSpanProcessor`` re-emits via
        # ``SPAN_ROUTES[SPAN_AUDIO_SKIPPED]``. ``extra`` is JSON-encoded
        # because OTEL drops dict attribute values; the route extract
        # returns the JSON string for dashboard parity.
        with audio_skipped_span(
            reason=reason,
            turn_number=sd.snapshot.turn_manager.interaction,
            extra=extra,
        ):
            pass

    def _audio_dispatched(
        self,
        sd: _SessionData,
        payload: AudioCuePayload,
    ) -> None:
        # Span emission replaces the prior direct ``_watcher_publish`` —
        # ``WatcherSpanProcessor`` re-emits via
        # ``SPAN_ROUTES[SPAN_AUDIO_DISPATCHED]``.
        with audio_dispatched_span(
            turn_number=sd.snapshot.turn_manager.interaction,
            mood=payload.mood or "",
            music_track=payload.music_track or "",
            sfx_count=len(payload.sfx_triggers),
        ):
            pass

    # ------------------------------------------------------------------
    # Lore embedding — RAG retrieval (pre-turn) + worker dispatch (post-turn)
    # ------------------------------------------------------------------

    async def _retrieve_lore_for_turn(self, sd: _SessionData, action: str) -> str | None:
        """Pre-turn lore RAG retrieval. Delegates to ``lore_embed.retrieve_for_turn``.

        Phase 3 of session_handler decomposition (see
        docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).
        """
        from sidequest.server.dispatch import lore_embed

        return await lore_embed.retrieve_for_turn(self, sd, action)

    def _dispatch_embed_worker(self, sd: _SessionData) -> None:
        """Post-turn embed worker dispatch. Delegates to ``lore_embed.dispatch_worker``.

        Phase 3 of session_handler decomposition (see
        docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).
        """
        from sidequest.server.dispatch import lore_embed

        lore_embed.dispatch_worker(self, sd)

    async def _run_embed_worker(
        self, sd: _SessionData, pending_count: int, turn_number: int
    ) -> None:
        """Background embed worker. Delegates to ``lore_embed.run_worker``.

        Phase 3 of session_handler decomposition (see
        docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).
        """
        from sidequest.server.dispatch import lore_embed

        await lore_embed.run_worker(self, sd, pending_count, turn_number)

    async def _run_render(
        self,
        client: DaemonClient,
        params: dict[str, object],
        render_id: str,
        room_slug: str | None,
        player_id: str,
        legacy_queue: asyncio.Queue[object] | None,
        dispatch_turn_id: int,
        sd: _SessionData | None = None,
    ) -> None:
        """Background render coroutine — waits for the daemon reply, then
        enqueues an IMAGE message or logs a failure. Never raises; any
        exception is caught and surfaced as an OTEL watcher event.

        Routing (story 37-30): when ``room_slug`` is set, the IMAGE is
        delivered to the *current* outbound queue looked up via the
        RoomRegistry — so a reconnect mid-render still gets its image.
        ``legacy_queue`` is the pre-room-context fallback for
        constructions that haven't joined a room (used by older tests
        and the deprecated genre/world connect path).

        ``sd`` is optional for backwards compatibility with legacy
        call sites; when provided, the in-flight render counter
        (story 45-31) is decremented in the finally block so the
        backpressure gate sees an accurate concurrent depth."""
        try:
            await self._run_render_inner(
                client,
                params,
                render_id,
                room_slug,
                player_id,
                legacy_queue,
                dispatch_turn_id,
                sd,
            )
        finally:
            if sd is not None:
                # Decrement is unconditional — a render that completed,
                # failed, or raised all release the in-flight slot.
                sd.render_in_flight = max(0, sd.render_in_flight - 1)

    async def _run_render_inner(
        self,
        client: DaemonClient,
        params: dict[str, object],
        render_id: str,
        room_slug: str | None,
        player_id: str,
        legacy_queue: asyncio.Queue[object] | None,
        dispatch_turn_id: int,
        sd: _SessionData | None = None,
    ) -> None:
        """Inner body of ``_run_render`` — does the actual daemon
        round-trip and IMAGE-frame fan-out. Split from ``_run_render``
        so the counter decrement (story 45-31) lives in a single
        finally block instead of being repeated at every return site.
        """
        try:
            reply = await client.render(params)
        except DaemonUnavailableError as exc:
            logger.warning("render.reply_unavailable render_id=%s error=%s", render_id, exc)
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "failed",
                    "render_id": render_id,
                    "reason": "daemon_unavailable",
                    "error": str(exc),
                },
                component="render",
                severity="warning",
            )
            return
        except DaemonRequestError as exc:
            logger.warning(
                "render.reply_error render_id=%s code=%s error=%s",
                render_id,
                exc.code,
                exc.message,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "failed",
                    "render_id": render_id,
                    "reason": "daemon_error",
                    "code": exc.code,
                    "error": exc.message,
                },
                component="render",
                severity="error",
            )
            return
        except Exception as exc:  # noqa: BLE001 — background task must never crash the loop
            logger.exception("render.reply_exception render_id=%s", render_id)
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "failed",
                    "render_id": render_id,
                    "reason": "exception",
                    "error": type(exc).__name__,
                },
                component="render",
                severity="error",
            )
            return

        image_url = str(reply.get("image_url") or "")
        # R2 migration (Task 11): when the daemon uploaded the artifact
        # to R2 (Task 13+) the reply carries an ``r2_key`` field. Prefer
        # that path through the asset_urls seam — it returns the CDN
        # URL the UI should fetch. When ``r2_key`` is absent we're on
        # the legacy local-tmpdir flow, which still needs the
        # self-healing render mount described below.
        #
        # Self-healing render mount (S4-BUG, legacy path): if the
        # daemon restarted mid-session its tmp dir changed;
        # ensure_render_mount appends the new dir to the live
        # StaticFiles mount so /renders/* keeps serving without a
        # server restart. Falls back to the legacy env-based rewriter
        # so single-root paths (and unit tests that don't wire app
        # singleton) continue to work.
        from sidequest.server.render_mounts import (
            ensure_render_mount,
            get_active_app,
            resolve_artifact_url,
        )

        r2_key = reply.get("r2_key")
        if r2_key:
            served_url = resolve_artifact_url(str(r2_key)) or ""
        else:
            active_app = get_active_app()
            healed: str | None = (
                ensure_render_mount(active_app, image_url)
                if active_app is not None and image_url
                else None
            )
            served_url = healed if healed is not None else _render_url_from_path(image_url)
        width = int(reply.get("width") or 0) or None
        height = int(reply.get("height") or 0) or None
        elapsed = int(reply.get("elapsed_ms") or 0)

        msg = ImageMessage(
            type=MessageType.IMAGE,  # type: ignore[arg-type]
            payload=ImagePayload(
                url=served_url,
                render_id=render_id,
                tier=str(params.get("tier") or ""),
                width=width,
                height=height,
            ),
            player_id=player_id,
        )

        # Story 37-30 — resolve the live room at completion time, not at
        # dispatch. When the room is known, look up the *current*
        # SessionRoom via the RoomRegistry so reconnects mid-render still
        # land on live sockets.
        #
        # Bug #2b (playtest 2026-04-26): IMAGE used to land on a single
        # per-player queue (the originating actor's socket). Shared-world
        # scene imagery (POI/encounter/location/illustration) should be
        # shared across all connected players in the room — so peers see
        # the same image event. Switch to ``room.broadcast(msg)`` so
        # every attached outbound queue receives the IMAGE. The legacy
        # single-queue path remains for non-room-context tests and the
        # deprecated genre/world connect path.
        recipients_count = 0
        broadcast_used = False
        if room_slug is not None:
            registry = self._room_registry
            room = registry.get(room_slug) if registry is not None else None
            if room is None:
                # No live room — surface as session_not_found so the GM
                # panel sees the drop instead of it being silent.
                logger.warning(
                    "render.session_not_found render_id=%s room=%s player=%s reason=room_missing",
                    render_id,
                    room_slug,
                    player_id,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "session_not_found",
                        "render_id": render_id,
                        "room_slug": room_slug,
                        "player_id": player_id,
                        "tier": str(params.get("tier") or ""),
                        "url": served_url,
                        "reason": "room_missing",
                    },
                    component="render",
                    severity="warning",
                )
                return
            # Broadcast to every connected socket in the room. We do NOT
            # exclude the originating player — they need the IMAGE too,
            # mirroring the SCRAPBOOK_ENTRY/_emit_event fan-out pattern.
            #
            # Pingpong 2026-04-30 "Scrapbook only on first-connected
            # player": the OTEL `recipients_count` was being computed
            # from `connected_player_ids()` (the `_connected` map size)
            # but the broadcast itself iterates `_outbound_queues`. When
            # those diverge — typically because a peer's WebSocket
            # closed without their `detach_outbound` call running yet,
            # leaving them in `_connected` but not in `_outbound_queues`
            # — the broadcast log over-reports recipients while peers
            # silently miss the IMAGE. Switched to using the broadcast
            # return value (the list of (socket_id, player_id) pairs
            # actually queued onto) so the GM panel sees ground truth
            # instead of a synthesized count. Also surfaces per-recipient
            # detail so the dashboard's "scrapbook.image_received" lie-
            # detector has the receive-side player_id list to diff
            # against.
            try:
                delivered_recipients = room.broadcast(msg, exclude_socket_id=None)
            except Exception as exc:  # noqa: BLE001 — broadcast failure must surface
                logger.warning(
                    "render.broadcast_failed render_id=%s error=%s",
                    render_id,
                    exc,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "broadcast_failed",
                        "render_id": render_id,
                        "room_slug": room_slug,
                        "player_id": player_id,
                        "url": served_url,
                        "error": type(exc).__name__,
                    },
                    component="render",
                    severity="error",
                )
                return
            broadcast_used = True
            # Lie-detector: ground-truth recipient count from the
            # broadcast itself, plus the connect-map count for the
            # divergence check. If these differ, the GM panel surfaces
            # the gap directly instead of the prior over-report.
            recipients_count = len(delivered_recipients)
            connected_count = len(room.connected_player_ids())
            recipient_socket_ids = [sid for sid, _pid in delivered_recipients]
            recipient_player_ids = [pid for _sid, pid in delivered_recipients if pid is not None]
            try:
                _watcher_publish(
                    "scrapbook_image_broadcast",
                    {
                        "render_id": render_id,
                        "slug": room_slug,
                        "originating_player_id": player_id,
                        "url": served_url,
                        "tier": str(params.get("tier") or ""),
                        "recipients_count": recipients_count,
                        "connected_count": connected_count,
                        "queue_connect_divergence": connected_count != recipients_count,
                        "recipient_socket_ids": recipient_socket_ids,
                        "recipient_player_ids": recipient_player_ids,
                    },
                    component="render",
                    severity="warning" if connected_count != recipients_count else "info",
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must never crash a broadcast
                logger.warning(
                    "scrapbook_image_broadcast.watcher_publish_failed render_id=%s error=%s",
                    render_id,
                    exc,
                )
            if recipients_count == 0:
                logger.warning(
                    "render.broadcast_no_recipients render_id=%s room=%s",
                    render_id,
                    room_slug,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "session_not_found",
                        "render_id": render_id,
                        "room_slug": room_slug,
                        "player_id": player_id,
                        "tier": str(params.get("tier") or ""),
                        "url": served_url,
                        "reason": "no_connected_players",
                    },
                    component="render",
                    severity="warning",
                )
                return
        else:
            # Legacy / test path: no room context, fall back to the
            # single per-connection queue captured at dispatch.
            target_queue = legacy_queue
            if target_queue is None:
                logger.warning(
                    "render.session_not_found render_id=%s reason=no_queue",
                    render_id,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "session_not_found",
                        "render_id": render_id,
                        "player_id": player_id,
                        "tier": str(params.get("tier") or ""),
                        "url": served_url,
                        "reason": "no_outbound_queue",
                    },
                    component="render",
                    severity="warning",
                )
                return
            try:
                target_queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("render.outbound_queue_full render_id=%s", render_id)
                return
            recipients_count = 1

        # Playtest 2026-05-02: persist the URL into the matching
        # scrapbook_entries row so `slug_connect.replay` can JOIN it back
        # into the SCRAPBOOK_ENTRY payload on reconnect. The IMAGE
        # broadcast above is ephemeral; without this UPDATE every browser
        # reload turns 1/3 of the scrapbook into placeholder cards.
        from sidequest.server.emitters import update_scrapbook_image_url

        scrapbook_updated = update_scrapbook_image_url(
            self,
            dispatch_turn_id,
            served_url,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "scrapbook",
                "op": "image_url_backfilled",
                "render_id": render_id,
                "turn_id": dispatch_turn_id,
                "url": served_url,
                "row_updated": scrapbook_updated,
            },
            component="scrapbook",
        )

        logger.info(
            "render.completed render_id=%s url=%s elapsed_ms=%d recipients=%d broadcast=%s",
            render_id,
            served_url,
            elapsed,
            recipients_count,
            broadcast_used,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "completed",
                "render_id": render_id,
                "url": served_url,
                "elapsed_ms": elapsed,
                "player_id": player_id,
                "room_slug": room_slug or "",
                # Bug #2b lie-detector: surface whether the IMAGE was
                # delivered as a shared-world broadcast or via the
                # legacy single-queue path, plus the recipient count.
                # Without this, "image only reached one player" was an
                # invisible regression (playtest 2026-04-26).
                "broadcast": broadcast_used,
                "recipients": recipients_count,
            },
            component="render",
        )
        # Story 45-31: stamp the per-session diagnostic counters with
        # the most-recent successful render so the post-session
        # snapshot can quote the last image the player actually saw.
        if sd is not None:
            from datetime import UTC
            from datetime import datetime as _dt

            sd.last_successful_render_id = render_id
            sd.last_successful_render_ts_iso = _dt.now(UTC).isoformat()
