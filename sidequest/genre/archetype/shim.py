"""Axis-lookup shim for archetype resolution.

Port of sidequest-genre/src/archetype/shim.rs (368 LOC).

This is the Phase 1 entry point for resolving `(jungian, rpg_role)` pairs
into a named archetype. It takes the same pre-loaded structures the legacy
archetype resolver took (base archetypes, genre constraints, optional world
funnels) and produces an ArchetypeResolution carrying the resolved
ArchetypeResolved value plus lookup metadata (source tier, pairing weight).

OTEL emission is omitted in this Python port — the Python daemon does not
run the OTEL span infrastructure. The resolution logic is verbatim.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from sidequest.genre.archetype.resolved import ArchetypeResolved
from sidequest.genre.error import GenreValidationError
from sidequest.genre.models.archetype_axes import BaseArchetypes
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints, PairingWeight
from sidequest.genre.models.archetype_funnels import ArchetypeFunnels
from sidequest.protocol.provenance import ContributionKind, MergeStep, Provenance, Tier


class ResolutionSource(StrEnum):
    """Which tier the archetype's display name came from.

    Port of Rust ResolutionSource enum (archetype/shim.rs).
    """

    world_funnel = "world_funnel"
    """Name came from a world-level funnel."""

    genre_fallback = "genre_fallback"
    """Name came from genre-level fallback."""


class ArchetypeResolution(BaseModel):
    """The full result of resolving an archetype pair.

    Port of Rust ArchetypeResolution struct (archetype/shim.rs).

    `resolved` is the archetype value. `source` and `weight` are lookup
    metadata describing where the resolution came from. `provenance` is
    the tier-annotated source record for GM-panel display.
    """

    model_config = {"arbitrary_types_allowed": True}

    resolved: ArchetypeResolved
    """The resolved archetype value (name, lore, faction, etc.)."""
    source: ResolutionSource
    """Tier that supplied the final display name."""
    weight: PairingWeight
    """Pairing-weight classification from the genre's constraint table."""
    provenance: Provenance
    """Tier + file + merge-trail of the final resolution, for GM-panel display."""


def resolve_archetype(
    jungian: str,
    rpg_role: str,
    base: BaseArchetypes,
    constraints: ArchetypeConstraints,
    funnels: ArchetypeFunnels | None,
    genre: str,
    world: str | None = None,
) -> ArchetypeResolution:
    """Resolve a (jungian, rpg_role) pair through the archetype inheritance chain.

    Mirrors the Rust resolve_archetype() behavior:
    1. Validate both axis values exist in base.
    2. Reject forbidden pairings (genre constraints + world funnels).
    3. Prefer a world-funnel match; otherwise fall back to the genre's
       configured fallback name.

    Port of Rust archetype::shim::resolve_archetype().

    Args:
        jungian: Jungian archetype id (e.g. "sage").
        rpg_role: RPG role id (e.g. "healer").
        base: Base archetype definitions (from archetypes_base.yaml).
        constraints: Genre-level archetype constraints.
        funnels: Optional world-level funnels.
        genre: Genre code string (for provenance labels).
        world: Optional world name (for provenance labels).

    Returns:
        ArchetypeResolution with resolved archetype and metadata.

    Raises:
        GenreValidationError: If axis id is unknown, or pairing is forbidden.
    """
    # Step 1: validate axis IDs.
    if not any(j.id == jungian for j in base.jungian):
        raise GenreValidationError(message=f"Unknown Jungian archetype: '{jungian}'")
    if not any(r.id == rpg_role for r in base.rpg_roles):
        raise GenreValidationError(message=f"Unknown RPG role: '{rpg_role}'")

    # Step 2: genre constraints.
    weight = constraints.pairing_weight(jungian, rpg_role)
    if weight is None:
        weight = PairingWeight.uncommon

    if weight == PairingWeight.forbidden:
        raise GenreValidationError(message=f"Forbidden pairing: [{jungian}, {rpg_role}]")

    # Step 2b: world-level forbidden.
    if funnels is not None and funnels.is_forbidden(jungian, rpg_role):
        raise GenreValidationError(message=f"World-forbidden pairing: [{jungian}, {rpg_role}]")

    # Step 3: world funnel lookup.
    if funnels is not None:
        funnel = funnels.resolve(jungian, rpg_role)
        if funnel is not None:
            resolved = ArchetypeResolved(
                name=funnel.name,
                jungian=jungian,
                rpg_role=rpg_role,
                npc_role=None,
                speech_pattern="",
                lore=funnel.lore,
                faction=funnel.faction,
                cultural_status=funnel.cultural_status,
            )
            source_file = f"{genre}/worlds/{world or '<unknown>'}/archetype_funnels.yaml"
            provenance = Provenance(
                source_tier=Tier.world,
                source_file=source_file,
                source_span=None,
                merge_trail=[
                    MergeStep(
                        tier=Tier.world,
                        file=source_file,
                        span=None,
                        contribution=ContributionKind.initial,
                    )
                ],
            )
            return ArchetypeResolution(
                resolved=resolved,
                source=ResolutionSource.world_funnel,
                weight=weight,
                provenance=provenance,
            )

    # Genre fallback
    resolved, source, provenance = _genre_fallback(jungian, rpg_role, constraints, genre)
    return ArchetypeResolution(
        resolved=resolved,
        source=source,
        weight=weight,
        provenance=provenance,
    )


def _genre_fallback(
    jungian: str,
    rpg_role: str,
    constraints: ArchetypeConstraints,
    genre: str,
) -> tuple[ArchetypeResolved, ResolutionSource, Provenance]:
    """Build a genre-level fallback resolution.

    Port of Rust genre_fallback() helper in archetype/shim.rs.
    """
    fallback_name = constraints.fallback_name(rpg_role)
    if fallback_name is None:
        fallback_name = rpg_role
    resolved = ArchetypeResolved(
        name=fallback_name,
        jungian=jungian,
        rpg_role=rpg_role,
        npc_role=None,
        speech_pattern="",
        lore="",
        faction=None,
        cultural_status=None,
    )
    source_file = f"{genre}/archetype_constraints.yaml"
    provenance = Provenance(
        source_tier=Tier.genre,
        source_file=source_file,
        source_span=None,
        merge_trail=[
            MergeStep(
                tier=Tier.genre,
                file=source_file,
                span=None,
                contribution=ContributionKind.initial,
            )
        ],
    )
    return resolved, ResolutionSource.genre_fallback, provenance
