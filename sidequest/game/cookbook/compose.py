"""compose_room_prose — deterministic per-region prose + manifest.

Story 55-1 / ADR-109 §5.2. Pure function consumed by
``assemble_region``. The dressing pool feeds ``flavor_only`` entities;
the per-region special rooms feed ``real_object`` entities with
``binding.kind = location_feature`` and affordances seeded from the
special's ``mechanic`` id.

All entities carry ``provenance="cookbook"`` — this is the seam the
ADR-100 KnownFacts / Story 54-6 promotion paths use to tell authored
content from procedurally composed content.

The RNG must be seeded by the caller from
``(campaign_seed, expansion_id, room_id)`` so re-materialization of the
same region produces identical output. ``assemble_region`` derives this
seed from its own ``region_rng`` plus the ``room_id`` so the
deterministic chain is end-to-end.

The function refuses to fabricate prose: a ``LookDef`` with an empty
dressing pool raises ``ValueError`` loudly (No Silent Fallbacks).
``validate_bundle`` is the upstream guard that should catch this at
load time; the runtime guard here is a final safety net that names the
offending ``LookDef`` so dev sees WHICH bundle entry needs content.
"""

from __future__ import annotations

import random
import re

# Imports for reference URL attachment — loaded at call time to keep import
# surface minimal. Declared here for pyright to resolve them.
from sidequest.foundation.reference_anchors import build_lore_url
from sidequest.game.cookbook.models import (
    GeneratedRoomDescription,
    LookDef,
    SpecialRoom,
)
from sidequest.protocol.models import (
    LocationEntity,
    LocationEntityBinding,
)
from sidequest.telemetry.spans.reference import (
    reference_url_attached_span,
    reference_url_skipped_span,
)

# v1 dressing sample size: 2 lines per room minimum, 3 maximum. Tuned per
# spec §8 ("Cookbook dressing pool size matters. Author 8-12 dressing
# lines per look minimum; assembler samples 2-3 per room").
DRESSING_PICK_MIN = 2
DRESSING_PICK_MAX = 3

_ID_TRIM_RE = re.compile(r"[^a-z0-9]+")


def _id_from_text(text: str) -> str:
    """Stable id derived from a dressing line or special id. Lower-snake-ish.

    Truncated to a reasonable length so DB joins remain ergonomic. Empty
    output (all-punctuation input) falls back to ``"flavor"`` so the
    entity id is never an empty string (LocationEntity.id requires
    min_length=1).
    """
    base = _ID_TRIM_RE.sub("_", text.lower()).strip("_")
    return base[:48] or "flavor"


def _attach_location_reference_url(
    *,
    pack_id: str | None,
    world_slug: str | None,
    label: str,
) -> str | None:
    """Build a lore-page URL for a LocationEntity and emit the matching span.

    Returns the constructed URL when pack_id + world_slug are both non-empty,
    otherwise returns None after emitting a skipped span. Callers pass the
    result directly into the LocationEntity constructor.

    Per the Task 9 decision doc: no per-world locations registry is consulted.
    The lore page's bad-anchor banner (Task 4) handles stale labels loudly.
    A registry-tightening skip path is deferred until the loader exposes
    locations.yaml as a runtime list.
    """
    if pack_id and world_slug:
        url = build_lore_url(pack_id, world_slug, "location", label)
        with reference_url_attached_span(
            kind="location",
            pack=pack_id,
            world=world_slug,
            keys=(label,),
        ):
            pass
        return url
    with reference_url_skipped_span(
        kind="location",
        pack=pack_id or "",
        world=world_slug,
        keys=(label,),
        reason="no_pack_or_world_in_scope",
    ):
        pass
    return None


def compose_room_prose(
    *,
    rng: random.Random,
    look_def: LookDef,
    special_rooms: list[SpecialRoom],
    room_id: str,
    pack_id: str | None = None,
    world_slug: str | None = None,
) -> GeneratedRoomDescription:
    """Compose deterministic prose + manifest for one materialized region.

    See module docstring for contract notes. Raises ``ValueError`` when
    the ``LookDef`` has no dressing pool — that is an upstream bundle bug
    that must surface loudly (CLAUDE.md No Silent Fallbacks). The error
    message names the offending look id and the target room id so the
    dev sees BOTH what needs content authored AND where it was being
    materialized when the failure surfaced.

    ``pack_id`` / ``world_slug``: when both are non-empty, each constructed
    ``LocationEntity`` receives a ``reference_url`` pointing at
    ``/reference/lore/<pack>/<world>#location-<slug>``. When either is
    absent (test fixtures, older callers without world context) the entities
    are constructed with ``reference_url=None`` and a
    ``sidequest.reference.url_skipped`` span is emitted for observability.
    """
    if not look_def.dressing:
        raise ValueError(
            f"compose_room_prose: LookDef {look_def.id!r} has empty dressing "
            f"pool; cannot compose prose for room {room_id!r}. "
            "validate_bundle should have caught this at load time."
        )

    pool = list(look_def.dressing)
    pick_n = min(len(pool), rng.randint(DRESSING_PICK_MIN, DRESSING_PICK_MAX))
    # Sample without replacement so the same line never appears twice in
    # one room's prose.
    chosen_lines = rng.sample(pool, k=pick_n)

    # Build prose: dressing lines first (the base scene), then any
    # special-room telegraph lines. This ordering matches the spec's
    # "base then special" hint flow — the player reads the room before
    # the bait drops.
    paragraphs: list[str] = list(chosen_lines)
    for special in special_rooms:
        if special.telegraph:
            paragraphs.append(special.telegraph)
    description = "\n\n".join(paragraphs)

    entities: list[LocationEntity] = []
    seen_ids: set[str] = set()

    # flavor_only entities from chosen dressing.
    for line in chosen_lines:
        entity_id = _id_from_text(line)
        if entity_id in seen_ids:
            continue
        seen_ids.add(entity_id)
        reference_url = _attach_location_reference_url(
            pack_id=pack_id,
            world_slug=world_slug,
            label=line,
        )
        entities.append(
            LocationEntity(
                id=entity_id,
                label=line,
                tier="flavor_only",
                provenance="cookbook",
                reference_url=reference_url,
            )
        )

    # real_object entities from attached specials.
    for special in special_rooms:
        entity_id = _id_from_text(special.id)
        if entity_id in seen_ids:
            continue
        seen_ids.add(entity_id)
        label = special.telegraph or special.id
        reference_url = _attach_location_reference_url(
            pack_id=pack_id,
            world_slug=world_slug,
            label=label,
        )
        entities.append(
            LocationEntity(
                id=entity_id,
                label=label,
                tier="real_object",
                binding=LocationEntityBinding(
                    kind="location_feature",
                    ref=special.id,
                ),
                affordances=[special.mechanic] if special.mechanic else [],
                provenance="cookbook",
                reference_url=reference_url,
            )
        )

    return GeneratedRoomDescription(
        room_id=room_id,
        description=description,
        entities=entities,
    )
