"""REST endpoint for the chassis interior SVG.

GET /api/chassis/{instance_id}/interior -> image/svg+xml

Walks the configured genre-pack search paths to find the chassis
instance, looks up its chassis class, and renders the interior. The
snapshot is empty for the v1 — live session-bound rendering (PCs
visible at their real ``current_room``, NPCs from the live snapshot)
is a follow-on; for tonight the renderer's hardcoded NPC defaults
plus the chassis-default-room fallback for PCs are enough to feel
the feature at the table.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from sidequest.genre.loader import load_genre_pack
from sidequest.interior.render import render_interior_svg
from sidequest.server.rest import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.telemetry.spans.interior import emit_interior_render

logger = logging.getLogger(__name__)

interior_router = APIRouter()


def _find_chassis_instance(search_paths: list[Path], instance_id: str):
    """Return (chassis_class, chassis_instance_config, genre_slug, world_slug)
    or (None, None, None, None) if no instance with this id is authored.
    """
    for sp in search_paths:
        if not (sp.exists() and sp.is_dir()):
            continue
        for genre_dir in sorted(sp.iterdir()):
            if not genre_dir.is_dir():
                continue
            try:
                pack = load_genre_pack(genre_dir)
            except Exception as exc:
                logger.warning(
                    "interior: skipping pack %s (load failed: %s)",
                    genre_dir.name,
                    exc,
                )
                continue
            if pack.chassis_classes is None:
                continue
            for world_slug, world in pack.worlds.items():
                for inst_cfg in world.chassis_instances:
                    if inst_cfg.id == instance_id:
                        chassis_class = next(
                            (
                                c
                                for c in pack.chassis_classes.classes
                                if c.id == inst_cfg.chassis_class_id
                            ),
                            None,
                        )
                        return chassis_class, inst_cfg, genre_dir.name, world_slug
    return None, None, None, None


class _EmptySnapshot:
    """Stand-in for the live game snapshot when the endpoint is hit
    outside of a session (e.g., direct curl). Matches the duck shape
    the renderer reads."""

    characters: list = []
    npcs: list = []


@interior_router.get("/api/chassis/{instance_id}/interior")
def get_chassis_interior(instance_id: str, request: Request):
    search_paths: list[Path] = getattr(
        request.app.state,
        "genre_pack_search_paths",
        DEFAULT_GENRE_PACK_SEARCH_PATHS,
    )
    chassis_class, chassis_inst, _genre_slug, _world_slug = _find_chassis_instance(
        search_paths, instance_id
    )
    if chassis_class is None or chassis_inst is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"chassis instance {instance_id!r} not found in any genre pack on the search path"
            ),
        )

    # The runtime ChassisInstance carries the same id/name/class_id the
    # renderer reads; we can pass the YAML config directly because the
    # renderer only touches those three attributes.
    class _InstView:
        pass

    inst_view = _InstView()
    inst_view.id = chassis_inst.id
    inst_view.name = chassis_inst.name
    inst_view.class_id = chassis_inst.chassis_class_id

    # The default crew NPCs (kestrel_captain etc.) are hardcoded in
    # the renderer's KESTREL_NPC_DEFAULT_ROOM table. To make them
    # visible against an empty snapshot, synthesize lightweight NPC
    # actor stubs for each crew_npcs entry. When this endpoint is
    # later wired to the live session, the real snapshot.npcs takes
    # over and this synthesis is bypassed.
    class _StubActor:
        def __init__(self, name: str):
            self.core = type("Core", (), {"name": name})()
            self.current_room = None

    snapshot = _EmptySnapshot()
    snapshot.npcs = [_StubActor(npc_id) for npc_id in chassis_inst.crew_npcs]

    svg = render_interior_svg(chassis_class, inst_view, snapshot)

    emit_interior_render(
        chassis_instance_id=instance_id,
        actor_count=len(snapshot.npcs),
        tracked_pcs=0,
        tracked_npcs=len(snapshot.npcs),
        output_size_bytes=len(svg.encode("utf-8")),
    )
    return Response(content=svg, media_type="image/svg+xml")
