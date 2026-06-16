"""Vessel-tag parser and rig-pool binder (Story 53-2, Epic 53 Road Warrior).

Content-side vessel items encode mechanical pool state as colon-separated tag
strings on the inventory item dict::

    {
        "id": "rig_tier_1_prospect",
        "tags": ["vessel", "rig", "tier-1",
                 "composure:4", "composure_max:4",
                 "speed:3", "armor:0", "mount_slots:1"],
        ...
    }

This module:

  - parses those tags into a typed :class:`VesselTags` value,
  - finds the first ``vessel``-tagged item in an inventory list,
  - binds a :class:`~sidequest.game.rig_composure_pool.RigComposurePool` to a
    :class:`~sidequest.game.creature_core.CreatureCore` whose inventory carries
    a vessel item,
  - walks a :class:`~sidequest.game.session.GameSnapshot` and binds rig pools
    on every character.

Per CLAUDE.md "No Silent Fallbacks": every malformed shape raises
:class:`InvalidVesselTagsError`; the binder never falls back to a default
pool or skips a malformed vessel.

Story 53-2 ships composure-only parsing — ``speed:N``, ``armor:N``,
``fuel_capacity:N``, ``mount_slots:N`` are read by other subsystems (display,
later mechanics) and are intentionally ignored here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from sidequest.game.rig_composure_pool import RigComposurePool

if TYPE_CHECKING:
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.session import GameSnapshot


_VESSEL_TAG = "vessel"
_COMPOSURE_KEY = "composure"
_COMPOSURE_MAX_KEY = "composure_max"


class InvalidVesselTagsError(ValueError):
    """A vessel inventory item failed parser validation.

    Carries the offending item's id so the chargen flow can surface
    *which* content entry needs fixing. Raised in preference to a bare
    :class:`ValueError` so callers can catch precisely without swallowing
    unrelated value errors.
    """

    def __init__(self, item_id: str, reason: str) -> None:
        self.item_id = item_id
        self.reason = reason
        super().__init__(f"vessel item {item_id!r}: {reason}")


class VesselTags(BaseModel):
    """Parsed composure values from a vessel inventory item's tag list."""

    model_config = {"extra": "forbid"}

    composure: int
    composure_max: int


def _parse_int_tag(value: str, *, key: str, item_id: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidVesselTagsError(
            item_id, f"tag {key!r} value is not an integer: {value!r}"
        ) from exc


def parse_vessel_tags(item: dict) -> VesselTags:
    """Extract :class:`VesselTags` from an inventory item dict.

    Fails loud on every malformed shape — see :class:`InvalidVesselTagsError`.
    """
    raw_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise InvalidVesselTagsError(
            str(raw_id or "<unknown>"),
            "inventory item has no id",
        )
    item_id = raw_id

    tags = item.get("tags")
    if not isinstance(tags, list):
        raise InvalidVesselTagsError(
            item_id,
            f"tags must be a list, got {type(tags).__name__}",
        )

    if _VESSEL_TAG not in tags:
        raise InvalidVesselTagsError(
            item_id,
            f"not a vessel item (no {_VESSEL_TAG!r} tag)",
        )

    composure: int | None = None
    composure_max: int | None = None

    for tag in tags:
        if not isinstance(tag, str) or ":" not in tag:
            continue
        key, _, raw_value = tag.partition(":")
        if key == _COMPOSURE_KEY:
            if composure is not None:
                raise InvalidVesselTagsError(item_id, f"duplicate {_COMPOSURE_KEY!r} tag")
            composure = _parse_int_tag(raw_value, key=_COMPOSURE_KEY, item_id=item_id)
        elif key == _COMPOSURE_MAX_KEY:
            if composure_max is not None:
                raise InvalidVesselTagsError(item_id, f"duplicate {_COMPOSURE_MAX_KEY!r} tag")
            composure_max = _parse_int_tag(raw_value, key=_COMPOSURE_MAX_KEY, item_id=item_id)

    if composure is None:
        raise InvalidVesselTagsError(item_id, f"missing {_COMPOSURE_KEY!r}:N tag")
    if composure_max is None:
        raise InvalidVesselTagsError(item_id, f"missing {_COMPOSURE_MAX_KEY!r}:N tag")
    if composure_max <= 0:
        raise InvalidVesselTagsError(
            item_id, f"{_COMPOSURE_MAX_KEY} must be > 0, got {composure_max}"
        )
    if composure < 0:
        raise InvalidVesselTagsError(item_id, f"{_COMPOSURE_KEY} must be >= 0, got {composure}")
    if composure > composure_max:
        raise InvalidVesselTagsError(
            item_id,
            f"{_COMPOSURE_KEY} ({composure}) exceeds {_COMPOSURE_MAX_KEY} ({composure_max})",
        )

    return VesselTags(composure=composure, composure_max=composure_max)


def find_vessel_item(items: Iterable[dict]) -> dict | None:
    """Return the first inventory item carrying the ``vessel`` tag, or None.

    "First wins" per Story 53-2's documented assumption — the
    ``starting_equipment`` table in road_warrior allots exactly one rig
    per character; salvage scenarios with multi-rig are a separate story.
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags")
        if isinstance(tags, list) and _VESSEL_TAG in tags:
            return item
    return None


def bind_rig_pool_from_inventory(
    core: CreatureCore, *, character_id: str
) -> RigComposurePool | None:
    """Scan ``core.inventory.items`` for a vessel; bind a pool to ``core``.

    Idempotent: if ``core.rig_pool`` is already set (e.g. a reloaded save),
    returns ``None`` without mutating — the live (possibly-damaged) pool
    is preserved.

    Returns the bound pool on first-time binding, or ``None`` when there
    was no vessel item to bind (or the pool was already present).
    """
    if not character_id or not character_id.strip():
        raise ValueError("character_id cannot be blank")

    if core.rig_pool is not None:
        return None

    vessel = find_vessel_item(core.inventory.items)
    if vessel is None:
        return None

    tags = parse_vessel_tags(vessel)
    pool = RigComposurePool(
        current=tags.composure,
        max=tags.composure_max,
        base_max=tags.composure_max,
        character_id=character_id,
        chassis_id=vessel["id"],
    )
    core.rig_pool = pool
    return pool


def bind_rig_pools(snapshot: GameSnapshot) -> None:
    """Walk ``snapshot.characters`` and bind a rig pool wherever possible.

    Uses ``character.core.name`` as the ``character_id`` — same convention
    as :func:`sidequest.game.chassis.rebind_chassis_bonds_to_character`.

    Hard no-op on an empty character list. Propagates
    :class:`InvalidVesselTagsError` loudly so the chargen / session-start
    handler can surface the content bug.
    """
    for character in snapshot.characters:
        bind_rig_pool_from_inventory(character.core, character_id=character.core.name)


__all__ = [
    "InvalidVesselTagsError",
    "VesselTags",
    "bind_rig_pool_from_inventory",
    "bind_rig_pools",
    "find_vessel_item",
    "parse_vessel_tags",
]
