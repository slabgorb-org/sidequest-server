"""ArchetypeResolved — the archetype value type on the Layered framework.

Port of sidequest-genre/src/archetype/resolved.rs.

Every field uses the `replace` merge strategy (as in the Rust source). The
comment in the Rust source notes a known Phase D refinement needed for
"missing in deeper YAML" semantics — that issue carries over to this port.
"""

from __future__ import annotations

from pydantic import Field

from sidequest.genre.resolver import LayeredMerge


class ArchetypeResolved(LayeredMerge):
    """Archetype value after four-tier resolution.

    Port of Rust ArchetypeResolved struct (archetype/resolved.rs).

    Every field uses replace merge strategy: a deeper tier's value
    overrides a shallower tier's. Fields absent from a deeper tier
    leave the shallower tier's value in place.

    NOTE: In pydantic with serde-default semantics, "missing in deeper
    YAML" resolves to the field's default value (empty string / None),
    which under replace semantics would clobber the shallower tier's
    value. The Rust source acknowledges this exact issue and defers it
    to Phase D. This port carries the same limitation.
    """

    name: str = Field(
        default="",
        json_schema_extra={"merge": "replace"},
        description="Display name shown to the player (e.g. 'Thornwall Mender').",
    )
    jungian: str = Field(
        default="",
        json_schema_extra={"merge": "replace"},
        description="Jungian axis identifier (e.g. 'sage').",
    )
    rpg_role: str = Field(
        default="",
        json_schema_extra={"merge": "replace"},
        description="RPG role axis identifier (e.g. 'healer').",
    )
    npc_role: str | None = Field(
        default=None,
        json_schema_extra={"merge": "replace"},
        description="NPC role identifier (e.g. 'mentor'). Only populated for NPCs.",
    )
    speech_pattern: str = Field(
        default="",
        json_schema_extra={"merge": "replace"},
        description="Speech pattern hint for the narrator. Typically genre-level flavor.",
    )
    lore: str = Field(
        default="",
        json_schema_extra={"merge": "replace"},
        description="Lore prose, typically authored at the world tier per funnel.",
    )
    faction: str | None = Field(
        default=None,
        json_schema_extra={"merge": "replace"},
        description="Faction name from a world-level funnel, if any.",
    )
    cultural_status: str | None = Field(
        default=None,
        json_schema_extra={"merge": "replace"},
        description="Cultural status marker from a world-level funnel, if any.",
    )
