"""Tactical-grid, location-description, and dungeon-map emit helpers.

Extracted from ``websocket_session_handler`` (module-level free functions).
Each emits a typed WebSocket message off the live snapshot when the party
enters / changes rooms: TACTICAL_GRID (ADR-096), LOCATION_DESCRIPTION and
LOCATION_OVERLAY_CHANGED (ADR-109), and DUNGEON_MAP (ADR-055/§Q-map). The
``handler``/``emit_fn`` seam lets the turn-dispatch loop pass its broadcast
closure without these helpers knowing about the SessionRoom.

Heavy dependencies (room file loader, cartography, dungeon persistence,
theme palette) are imported lazily inside each function — both to avoid
import cycles (``sidequest.dungeon`` depends on game models) and to keep the
non-dungeon worlds' turn path import-light.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sidequest.protocol.messages import TacticalGridMessage, TacticalGridPayload
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.dungeon.region_graph.model import RegionGraph
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.game.session import GameSnapshot
    from sidequest.protocol.messages import DungeonMapPayload
    from sidequest.protocol.models import EncounterLocationOverlay, LocationEntity
    from sidequest.server.session_state import _SessionData

logger = logging.getLogger(__name__)


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

    from sidequest.foundation.asset_urls import resolve_asset_url
    from sidequest.game.room_file_loader import emit_runtime_cavern_png
    from sidequest.protocol.models import DerivedRoomData

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

        save_id = getattr(sd, "game_slug", "in_memory")

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
    from sidequest.server.session_state import session_world_dir

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
        world_dir = session_world_dir(sd)
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
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.protocol.models import LocationDescriptionPayload
    from sidequest.server.session_state import session_world_dir

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
    region_name: str | None = None
    sourced = False

    # Path 1: per-room YAML via load_room_payload.
    try:
        world_dir = session_world_dir(sd)
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
        region_name = room_payload.room_name or None
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
            region_name = getattr(region, "name", None) or None
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

    # Story 54-7: layer the active encounter overlay on top of the base
    # so a session-resume client sees the live overlay state without
    # waiting for a separate LOCATION_OVERLAY_CHANGED delta.
    from sidequest.game.location_view import (
        active_overlays_for,
        get_location_prose,
    )
    from sidequest.protocol.models import LocationDescriptionOverlaySummary

    effective_prose = get_location_prose(
        region_id=room_id,
        authored_description=prose,
        snapshot=snapshot,
    )
    active_overlays = active_overlays_for(snapshot, region_id=room_id)
    overlay_summaries: list[LocationDescriptionOverlaySummary] = []
    for overlay in active_overlays:
        enc = getattr(snapshot, "encounter", None)
        encounter_id_str = f"{enc.encounter_type}@{room_id}" if enc is not None else ""
        overlay_summaries.append(
            LocationDescriptionOverlaySummary(
                encounter_id=encounter_id_str,
                prose_suffix=overlay.prose_suffix,
                entity_delta_count=len(overlay.entity_delta),
            )
        )

    # Story 63-6: deep-link the region header into the /reference/lore wiki.
    # POI slugs from the world's history.yaml are the authoritative set of
    # regions that have a lore-page anchor (Story 63-8). Resolve to None when
    # the region has no anchor — no guessed/broken URL. Every decision emits a
    # reference-URL span so the GM panel sees location anchors fire (AC5 / OTEL).
    from sidequest.foundation.reference_anchors import reference_url_for_region
    from sidequest.server.reference_renderer import load_poi_image_slugs
    from sidequest.telemetry.spans.reference import (
        reference_url_attached_span,
        reference_url_failed_span,
        reference_url_skipped_span,
    )

    # Story 63-13 defense-in-depth: load_poi_image_slugs re-raises a malformed
    # history.yaml as ValueError, and this call sits OUTSIDE the sourcing guards
    # above — an unguarded raise here would crash a live room-change emit,
    # violating the "must not crash a turn" contract. Degrade to no anchor and
    # fire the FAILED span (loud, GM-panel-visible) — not a silent skip.
    try:
        poi_slugs = load_poi_image_slugs(world_dir)
    except Exception as exc:  # noqa: BLE001 — malformed manifest must not crash a turn
        logger.warning(
            "location_description.poi_manifest_load_failed genre=%s world=%s room=%s error=%s",
            sd.genre_slug,
            sd.world_slug,
            room_id,
            exc,
        )
        _watcher_publish(
            "location_description.poi_manifest_load_failed",
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "room_id": room_id,
                "error": str(exc),
            },
            component="location",
            severity="warning",
        )
        reference_url = None
        with reference_url_failed_span(
            kind="location",
            pack=sd.genre_slug,
            world=sd.world_slug,
            keys=(room_id,),
            reason="malformed_poi_manifest",
        ):
            pass
    else:
        reference_url = reference_url_for_region(
            pack=sd.genre_slug,
            world=sd.world_slug,
            region_id=room_id,
            known_location_slugs=poi_slugs,
        )
        if reference_url is not None:
            with reference_url_attached_span(
                kind="location",
                pack=sd.genre_slug,
                world=sd.world_slug,
                keys=(room_id,),
            ):
                pass
        else:
            with reference_url_skipped_span(
                kind="location",
                pack=sd.genre_slug,
                world=sd.world_slug,
                keys=(room_id,),
                reason="region_not_in_lore_poi_manifest",
            ):
                pass

    # POI landscape for the Location tab. Built from room_id VERBATIM — the
    # authored region slug IS the R2 object key (munchkin_country.png), so no
    # slugify (which would hyphenate to munchkin-country.png and miss the
    # underscore R2 key). The UI hides the image on a load error, so a region
    # with no rendered landscape degrades to text-only.
    from sidequest.foundation.asset_urls import resolve_asset_url

    poi_image_url = resolve_asset_url(
        f"genre_packs/{sd.genre_slug}/worlds/{sd.world_slug}/assets/poi/{room_id}.png"
    )

    payload = LocationDescriptionPayload(
        region_id=room_id,
        region_name=region_name,
        prose=effective_prose,
        terrain=terrain,
        entities=entities,
        overlays=overlay_summaries,
        reference_url=reference_url,
        poi_image_url=poi_image_url,
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
            "prose_chars": len(effective_prose),
            "overlay_count": len(overlay_summaries),
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


def _maybe_emit_location_overlay_changed(
    handler: object,
    *,
    sd: _SessionData,
    snapshot: GameSnapshot,
    transition: str,
    emit_fn: object,
    prior_overlay: EncounterLocationOverlay | None = None,
) -> None:
    """Emit LOCATION_OVERLAY_CHANGED on encounter overlay activate/deactivate.

    Story 54-7 / ADR-109 §5.5. ``transition`` is ``"activate"`` or
    ``"deactivate"``:

    - **activate** — called at the ``prior_live=False, now_live=True``
      edge in the narration turn loop. Reads the overlay off the live
      encounter; no-op when the encounter has no ``location_overlay``.
    - **deactivate** — called inside the ``encounter_resolved_this_turn``
      branch. ``prior_overlay`` is passed by the caller (the prior
      encounter has been replaced or cleared by this point). No-op when
      ``prior_overlay`` is ``None``.

    The payload carries the FULL post-transition overlay set — on
    activate that's one item, on deactivate that's an empty list. The UI
    replaces its overlay slice rather than reconciling diffs (54-9). The
    dedicated ``location.overlay.{activate,deactivate}`` OTEL spans
    (Story 54-8) carry the same fields through the ``SPAN_ROUTES``
    fan-out (``component="location"``, ``state_transition`` event), so
    the prior bare ``_watcher_publish("location_overlay_changed.emitted", ...)``
    is no longer needed and has been removed.
    """
    from contextlib import AbstractContextManager

    from sidequest.protocol.messages import LocationOverlayChangedMessage
    from sidequest.protocol.models import (
        LocationDescriptionOverlaySummary,
        LocationOverlayChangedPayload,
    )
    from sidequest.telemetry.spans import (
        location_overlay_activate_span,
        location_overlay_deactivate_span,
    )

    region_id: str
    overlay_summaries: list[LocationDescriptionOverlaySummary]
    span_cm: AbstractContextManager[object]

    if transition == "activate":
        enc = getattr(snapshot, "encounter", None)
        if enc is None or enc.resolved:
            return
        overlay = getattr(enc, "location_overlay", None)
        if overlay is None:
            return
        region_id = overlay.bound_room_id
        encounter_id_str = f"{enc.encounter_type}@{region_id}"
        overlay_summaries = [
            LocationDescriptionOverlaySummary(
                encounter_id=encounter_id_str,
                prose_suffix=overlay.prose_suffix,
                entity_delta_count=len(overlay.entity_delta),
            )
        ]
        span_cm = location_overlay_activate_span(
            region_id=region_id,
            encounter_id=encounter_id_str,
            delta_count=len(overlay.entity_delta),
            suffix_chars=len(overlay.prose_suffix),
        )
    elif transition == "deactivate":
        if prior_overlay is None:
            return
        region_id = prior_overlay.bound_room_id
        overlay_summaries = []
        # delta_count=0 reflects the post-transition state (the overlay
        # has just cleared). suffix_chars carries the prior suffix length
        # so the GM panel can still see what was just removed.
        span_cm = location_overlay_deactivate_span(
            region_id=region_id,
            encounter_id="",
            delta_count=0,
            suffix_chars=len(prior_overlay.prose_suffix),
        )
    else:
        raise ValueError(f"transition must be 'activate' or 'deactivate', got {transition!r}")

    payload = LocationOverlayChangedPayload(
        region_id=region_id,
        overlays=overlay_summaries,
    )
    msg = LocationOverlayChangedMessage(
        payload=payload,
        player_id=getattr(sd, "player_id", ""),
    )
    # 54-8: the dedicated span carries the same fields through the
    # SPAN_ROUTES fan-out (component='location', state_transition event),
    # so the bare _watcher_publish('location_overlay_changed.emitted', ...)
    # that 54-7 published is removed — the dual emit was redundant.
    with span_cm:
        logger.info(
            "location_overlay_changed.emitted region=%s transition=%s overlays=%d",
            region_id,
            transition,
            len(overlay_summaries),
        )
        emit_fn(msg, "LOCATION_OVERLAY_CHANGED")  # type: ignore[operator]


def _resolve_connection_pc_region(
    snapshot: GameSnapshot, player_id: str
) -> tuple[str | None, str | None]:
    """OP1 — resolve THIS connection's PC and its graph region.

    The connection's identity is ``sd.player_id``; ``snapshot.player_seats``
    maps ``player_id`` -> seated ``character.core.name`` (see
    ``GameSnapshot.player_seats``). The per-PC region is then
    ``region_for(perspective=<character_name>)`` — the SAME per-PC perspective
    accessor the rest of the code uses (mirrors ``character_locations`` /
    ``party_location``), NEVER the singular ``current_region``.

    Returns ``(pc_name, pc_region)``:
      - ``(None, None)`` — ``player_id`` maps to no seated character
        (spectator / GM-panel connection). Caller emits
        ``dungeon.map_skipped(no_pc_region)`` — per OP1, a connection with no
        seated PC gets the skip, NOT the consensus view, for v1.
      - ``(pc_name, None)`` — seated PC has no ``pc_regions`` entry. Caller
        also skips (loud) — NEVER falls back to ``current_region``.
      - ``(pc_name, region_id)`` — the per-connection YOU-ARE-HERE region.
    """
    pc_name = snapshot.player_seats.get(player_id) if player_id else None
    if not pc_name:
        return None, None
    return pc_name, snapshot.region_for(perspective=pc_name)


def _load_dungeon_map_context(
    sd: _SessionData,
) -> tuple[RegionGraph, ThemePalette, str] | None:
    """Load the live region graph + theme palette for the dungeon-map emit.

    The single content/IO seam (DungeonStore.load_map, GenreLoader,
    load_theme_palette). Returns ``(graph, palette, entrance_id)`` or
    ``None`` for a clean skip:
      - other-world no-op (``applies_to`` False) — silent (the per-turn
        ``dungeon.region_projection`` span already records it);
      - missing schema / empty map — emits ``dungeon.map_skipped`` (loud).

    Lazy imports: ``sidequest.dungeon`` depends on game models (the
    frontier-hook lazy-import precedent)."""
    from sidequest.dungeon.region_projection import applies_to
    from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
    from sidequest.dungeon.themes import load_theme_palette
    from sidequest.server.session_state import session_world_dir

    if not applies_to(sd.genre_slug, sd.world_slug):
        return None  # the per-turn dungeon.region_projection span already
        # records the other-world no-op; a second event here is noise.

    graph = sd.dungeon_repository.load_map(entrance_id=ENTRANCE_ID)
    if not graph.nodes:
        _watcher_publish(
            "dungeon.map_skipped",
            {"world": sd.world_slug, "reason": "empty_map"},
            component="dungeon",
            severity="warning",
        )
        logger.warning("dungeon.map_skipped empty dungeon_map")
        return None

    world_dir = session_world_dir(sd)
    # ADR-140 (story 113-1): themes/ is world-tier — resolve from the world dir,
    # not the genre-pack root (world_dir.parent.parent).
    palette = load_theme_palette(world_dir)
    return graph, palette, ENTRANCE_ID


def _build_dungeon_map_payload(
    *,
    graph: RegionGraph,
    palette: ThemePalette,
    pc_region: str,
    discovered_regions: list[str],
    entrance_id: str,
) -> DungeonMapPayload:
    """Build a ``DungeonMapPayload`` with a per-PC YOU-ARE-HERE marker.

    ``pc_region`` is THIS connection's PC region (§Q-map). ``discovered_regions``
    is the SHARED fog-of-war set — a region any PC entered is on the whole
    table's map; only the YOU-ARE-HERE marker (``is_current_room`` /
    ``current_location`` / ``region``) is per-PC."""
    from sidequest.dungeon.region_projection import assign_bearings
    from sidequest.protocol.messages import (
        DungeonMapExit,
        DungeonMapLocation,
        DungeonMapPayload,
    )

    nodes = graph.nodes
    edges = graph.edges

    discovered = [r for r in discovered_regions if r in nodes]
    # Fog of war: never leak undiscovered regions. If discovered_regions is
    # somehow empty but this PC's region is bound, at least show that.
    if not discovered and pc_region in nodes:
        discovered = [pc_region]

    explored: list[DungeonMapLocation] = []
    for rid in discovered:
        node = nodes[rid]
        try:
            display = palette.get(node.theme).display_name
        except KeyError:
            display = rid  # fail-soft label; the span/log below is loud
        # Same bearings the narrator names and the resolver matches — one
        # source (assign_bearings), so the map agrees with the prose.
        bearings = assign_bearings(graph, rid)
        room_exits = [
            DungeonMapExit(
                target=(target := (e.b if e.a == rid else e.a)),
                exit_type=e.kind,
                bearing=bearings.get(target, ""),
            )
            for e in edges
            if rid in (e.a, e.b) and not e.hidden  # secrets stay off the map
        ]
        explored.append(
            DungeonMapLocation(
                id=rid,
                name=display,
                type="region",
                connections=[x.target for x in room_exits],
                room_exits=room_exits,
                room_type="entrance" if rid == entrance_id else "normal",
                is_current_room=(rid == pc_region),
            )
        )

    return DungeonMapPayload(
        current_location=pc_region,
        region=pc_region,
        explored=explored,
    )


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

    Per-PC (Movement subsystem §Q-map / OP1): the YOU-ARE-HERE marker is
    THIS connection's PC region (``player_id`` -> seat -> PC ->
    ``region_for(perspective=pc)``), NOT the singular ``current_region``.
    A connection with no seated PC / no ``pc_regions`` entry emits
    ``dungeon.map_skipped(no_pc_region)`` — loud, NEVER a silent fall-through
    to the stale ``current_region``. ``discovered_regions`` stays SHARED."""
    from sidequest.protocol.messages import DungeonMapMessage

    player_id = getattr(sd, "player_id", "")

    # OP1 — resolve this connection's PC region BEFORE the content load so a
    # spectator / unseated connection skips loudly with no stale fallback.
    pc_name, pc_region = _resolve_connection_pc_region(snapshot, player_id)
    if not pc_region:
        _watcher_publish(
            "dungeon.map_skipped",
            {
                "world": sd.world_slug,
                "reason": "no_pc_region",
                "player_id": player_id,
                "pc_name": pc_name or "",
            },
            component="dungeon",
            severity="warning",
        )
        logger.warning(
            "dungeon.map_skipped no_pc_region player_id=%s pc_name=%s",
            player_id,
            pc_name,
        )
        return

    ctx = _load_dungeon_map_context(sd)
    if ctx is None:
        return  # other-world no-op or map_skipped already emitted by loader.
    graph, palette, entrance_id = ctx

    payload = _build_dungeon_map_payload(
        graph=graph,
        palette=palette,
        pc_region=pc_region,
        discovered_regions=list(snapshot.discovered_regions),
        entrance_id=entrance_id,
    )
    msg = DungeonMapMessage(payload=payload, player_id=player_id)
    _watcher_publish(
        "dungeon.map_emitted",
        {
            "world": sd.world_slug,
            "pc_name": pc_name,
            "pc_region": pc_region,
            "discovered_regions": len(payload.explored),
            "total_regions": len(graph.nodes),
        },
        component="dungeon",
    )
    logger.info(
        "dungeon.map_emitted pc=%s region=%s discovered=%d/%d",
        pc_name,
        pc_region,
        len(payload.explored),
        len(graph.nodes),
    )
    emit_fn(msg, "DUNGEON_MAP")  # type: ignore[operator]


def _maybe_emit_cartography_map(
    handler: object,
    *,
    sd: _SessionData,
    snapshot: GameSnapshot,
    emit_fn: object,
    acting_perspective: str | None = None,
) -> None:
    """Emit a MAP_UPDATE (cartography region graph) for a region-mode world.

    The region-mode sibling of :func:`_maybe_emit_dungeon_map` (which serves
    room_graph/procedural-dungeon worlds). Projects the world's authored
    cartography graph + visited-region overlay to the UI Map tab.

    EH-2 burning_peace playtest (2026-06-05): the Map tab read "No map data
    yet" for the entire opening of a region-mode world. Root cause — this
    emit was previously gated on ``_region_changed`` (only fired when the
    party MOVED to a different region), so turn 1 and intra-region moves
    (teahouse -> Hakone road, both in ``edo``) emitted nothing and the UI
    never received the graph. Now fires EVERY region-mode turn (idempotent —
    the UI just replaces its MapState), exactly like the dungeon-map,
    relationships, and quests projections that share this cadence. The single
    discovered region renders as a lone current-region node; adjacents come
    from the cartography ``adjacent`` lists.

    Clean no-op for non-region-mode worlds (the builder returns None off
    cartography / room_graph). OTEL: emits ``cartography.map_emitted`` on
    success / ``cartography.map_skipped`` (with a reason) otherwise, so the GM
    panel sees the Map-tab seam engaged — never a silent skip (CLAUDE.md OTEL
    principle; the prior inline emit carried no span at all)."""
    from sidequest.server.session_helpers import _build_cartography_map_message

    location = snapshot.current_region or snapshot.party_location(perspective=acting_perspective)
    msg = _build_cartography_map_message(
        getattr(sd, "genre_pack", None),
        getattr(sd, "world_slug", None),
        location,
        player_id=getattr(sd, "player_id", ""),
        discovered_regions=snapshot.discovered_regions,
    )
    if msg is None:
        # Off-region-mode (the common case) OR region-mode with no resolvable
        # location yet. Only the latter is worth a span — a non-region world is
        # not "skipping" anything. Distinguish on the world's navigation mode.
        world = getattr(getattr(sd, "genre_pack", None), "worlds", {}).get(
            getattr(sd, "world_slug", "") or ""
        )
        cart = getattr(world, "cartography", None)
        is_region_mode = (
            cart is not None and str(getattr(cart, "navigation_mode", "")) != "room_graph"
        )
        if is_region_mode:
            _watcher_publish(
                "cartography.map_skipped",
                {
                    "world": getattr(sd, "world_slug", ""),
                    "reason": "no_resolvable_location",
                    "current_region": snapshot.current_region or "",
                },
                component="location",
                severity="warning",
            )
        return

    _watcher_publish(
        "cartography.map_emitted",
        {
            "world": getattr(sd, "world_slug", ""),
            "current_location": location or "",
            "discovered_regions": len(msg.payload.explored),
            "total_regions": len(msg.payload.cartography.get("regions", {}))
            if msg.payload.cartography
            else 0,
        },
        component="location",
    )
    logger.info(
        "cartography.map_emitted world=%s location=%s discovered=%d",
        getattr(sd, "world_slug", ""),
        location,
        len(msg.payload.explored),
    )
    emit_fn(msg, "MAP_UPDATE")  # type: ignore[operator]
