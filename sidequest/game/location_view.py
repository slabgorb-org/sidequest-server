"""Read-time merge of authored manifest + promotions + encounter overlays.

Story 54-7 / ADR-109 §5.5. Pure functions: ``authored`` and
``authored_description`` come from the loader; ``snapshot`` carries the
live ``StructuredEncounter`` (with its optional ``location_overlay``);
``store`` is the SQLite layer that owns ``location_promotions``.

Authored YAML is never mutated. Promotions accumulate in
``location_promotions``. Encounter overlays are encounter-scoped — they
exist while ``snapshot.encounter`` is live and bound to the region, and
vanish when the encounter resolves.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from sidequest.game.location_resolver import _build_effective_manifest
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
)

if TYPE_CHECKING:
    from sidequest.game.persistence import SqliteStore


def active_overlays_for(snapshot: object, *, region_id: str) -> list[EncounterLocationOverlay]:
    """Return every active encounter overlay bound to ``region_id``.

    V1 has at most one active encounter (``snapshot.encounter``), so this
    is at most a one-element list. The list shape is the seam for future
    multi-encounter support — keeps the read path uniform.
    """
    enc = getattr(snapshot, "encounter", None)
    if enc is None or enc.resolved:
        return []
    overlay = getattr(enc, "location_overlay", None)
    if overlay is None:
        return []
    if overlay.bound_room_id != region_id:
        return []
    return [overlay]


def get_location_manifest(
    *,
    region_id: str,
    authored: Iterable[LocationEntity],
    snapshot: object,
    store: SqliteStore,
    save_id: str = "default",
) -> list[LocationEntity]:
    """Effective manifest for ``region_id``: authored + overlays + promotions.

    Matches the resolver's effective-manifest order so the UI and the
    resolver agree on what's "in the room" at any moment.
    """
    overlays = active_overlays_for(snapshot, region_id=region_id)
    promotions = store.list_location_promotions(save_id=save_id, region_id=region_id)
    merged = _build_effective_manifest(authored=authored, promotions=promotions, overlays=overlays)
    return [entity for entity, _ in merged]


def get_location_prose(
    *,
    region_id: str,
    authored_description: str,
    snapshot: object,
) -> str:
    """Effective prose for ``region_id``: base description + overlay suffixes.

    Suffixes joined by a blank line so the UI can render them as separate
    paragraphs without parsing. Empty suffixes are dropped. When the
    authored base is empty, the suffix-only string is returned without a
    leading separator (no orphan double-newline).
    """
    overlays = active_overlays_for(snapshot, region_id=region_id)
    suffixes = [o.prose_suffix for o in overlays if o.prose_suffix]
    if not suffixes:
        return authored_description
    joined_suffixes = "\n\n".join(suffixes)
    if not authored_description:
        return joined_suffixes
    return authored_description + "\n\n" + joined_suffixes
