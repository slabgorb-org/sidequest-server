"""Per-room creature binding resolver — Story 107-2 (ADR-059).

The Monster Manual injection seam (:mod:`monster_manual_inject`) materializes
the whole Available encounter pool with no per-room filter, so a dungeon room
fields a flat pool the narrator can improvise around ("the creature of animal
musk", combat playtest 2026-06-13). This module reads a room's *structured*
``encounter_creatures`` binding — a top-level list of world-bestiary ids on the
room YAML — and returns the bound ids so the injection seam can surface the
room's AUTHORED opponent under its real name.

Contract (TEA-defined, ratified by Keith's 2026-06-13 "proceed fixture-driven"
ruling because the live per-room key is owned by 107-1):

- ``resolve_room_creatures(pack, world_slug, room_id) -> list[str]`` reads the
  room's ``encounter_creatures`` and returns the bound bestiary ids. A room with
  no binding (or no room file) is a legitimate non-combat room — return ``[]``.
- A binding that references an unknown bestiary id is an authoring error: raise
  :class:`RoomCreatureBindingError` (No Silent Fallbacks / AC5 — an unresolved
  binding fails LOUD, never a silent empty pool — the 87-4 bug shape).
- Resolving a non-empty binding emits ``monster_manual.room_bound`` (AC5
  lie-detector) naming the room and the bound creatures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sidequest.telemetry.spans import Span
from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_ROOM_BOUND


class RoomCreatureBindingError(Exception):
    """A room's ``encounter_creatures`` binding cannot be resolved.

    Raised when a declared binding references a bestiary id that does not exist
    in the world's effective bestiary — an author-time error surfaced loud
    rather than degraded to a silent empty pool (No Silent Fallbacks, AC5).
    """


def resolve_room_creatures(pack: Any, world_slug: str, room_id: str) -> list[str]:
    """Resolve a room's ``encounter_creatures`` binding to bestiary ids.

    Reads ``{pack.source_dir}/worlds/{world_slug}/rooms/{room_id}.yaml`` and
    returns its ``encounter_creatures`` list. Returns ``[]`` for a room with no
    binding or no room file (a legitimate non-combat region). Raises
    :class:`RoomCreatureBindingError` when a bound id is not a real bestiary
    entry. Emits :data:`SPAN_MONSTER_MANUAL_ROOM_BOUND` for a resolved binding.
    """
    source_dir = getattr(pack, "source_dir", None)
    if source_dir is None:
        raise RoomCreatureBindingError(
            f"genre pack for world {world_slug!r} has no source_dir; cannot resolve "
            f"room binding for {room_id!r}"
        )

    room_path = Path(source_dir) / "worlds" / world_slug / "rooms" / f"{room_id}.yaml"
    if not room_path.is_file():
        # No room file for this region id — no per-room binding declared. The
        # common case for a procedural megadungeon region without an authored
        # room file; not a fallback, just an absent binding.
        return []

    data = yaml.safe_load(room_path.read_text(encoding="utf-8"))
    raw = data.get("encounter_creatures") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    bound = [cid for cid in raw if isinstance(cid, str) and cid.strip()]
    if not bound:
        return []

    bestiary, _ = pack.effective_bestiary(world_slug)
    valid_ids = {entry.id for entry in bestiary.entries}
    dangling = [cid for cid in bound if cid not in valid_ids]
    if dangling:
        raise RoomCreatureBindingError(
            f"room {room_id!r} (world {world_slug!r}) binds unknown bestiary ids "
            f"{dangling}; every encounter_creatures id must resolve to a real "
            f"bestiary entry (No Silent Fallbacks)"
        )

    with Span.open(
        SPAN_MONSTER_MANUAL_ROOM_BOUND,
        {
            "room_id": room_id,
            "world_slug": world_slug,
            "bound_creatures": list(bound),
            "bound_count": len(bound),
        },
    ):
        pass

    return bound
