"""Trope inheritance resolution.

Port of sidequest-genre/src/resolve.rs (161 LOC).

World-level tropes can `extends` genre-level abstract tropes. This module
resolves the inheritance chain, merging parent fields into child tropes,
and detects cycles and missing parents.

Python port supports multi-level chains with proper cycle detection,
matching the Rust implementation.
"""

from __future__ import annotations

from sidequest.genre.error import GenreCycleError, GenreMissingParentError, GenreValidationError
from sidequest.genre.models.tropes import TropeDefinition

# Maximum depth for trope inheritance chains.
# Prevents stack overflow from deeply nested (non-cyclic) extends chains (CWE-674).
MAX_INHERITANCE_DEPTH = 64


def _slugify(name: str) -> str:
    """Convert a name to a URL-safe slug for lookup (lowercase, spaces → hyphens).

    Port of Rust util::slugify.
    """
    return name.lower().replace(" ", "-")


def resolve_trope_inheritance(
    genre_tropes: list[TropeDefinition],
    world_tropes: list[TropeDefinition],
) -> list[TropeDefinition]:
    """Resolve trope inheritance by merging parent fields into child tropes.

    - Genre-level tropes act as the parent pool (looked up by slugified name).
    - World-level tropes with `extends` inherit missing fields from their parent.
    - Child fields override parent fields where both exist.
    - Only world tropes appear in the output; genre-level abstract tropes serve
      as parents but are not emitted directly.
    - Cycles in extends chains are detected and rejected.

    Port of Rust resolve_trope_inheritance().

    Raises:
        GenreMissingParentError: If a trope extends a parent that does not exist.
        GenreCycleError: If a cycle is detected in the extends chain.
        GenreValidationError: If the inheritance chain exceeds MAX_INHERITANCE_DEPTH.
    """
    # Build parent lookup: normalized name slug → trope definition
    parent_map: dict[str, TropeDefinition] = {}
    for trope in genre_tropes:
        slug = _slugify(trope.name)
        parent_map[slug] = trope
    # World tropes can also be parents (for multi-level chains)
    for trope in world_tropes:
        slug = _slugify(trope.name)
        parent_map[slug] = trope

    resolved: list[TropeDefinition] = []

    for trope in world_tropes:
        if trope.extends is not None:
            raw_parent_slug = trope.extends
            parent_slug = _slugify(raw_parent_slug)

            # Check for missing parent
            if parent_slug not in parent_map:
                raise GenreMissingParentError(
                    trope=trope.name,
                    parent=raw_parent_slug,
                )

            # Detect cycles (with depth limit)
            visited: set[str] = {_slugify(trope.name)}
            _detect_cycle(parent_slug, parent_map, visited, 0)

            # Merge: child overrides parent
            parent = parent_map[parent_slug]
            merged = _merge_trope(parent, trope)
            resolved.append(merged)
        else:
            # No extends — include as-is
            resolved.append(trope.model_copy())

    return resolved


def _detect_cycle(
    current_slug: str,
    parent_map: dict[str, TropeDefinition],
    visited: set[str],
    depth: int,
) -> None:
    """Detect cycles in the extends chain starting from current_slug.

    Also enforces a maximum depth to prevent stack overflow on deep non-cyclic chains.

    Raises:
        GenreCycleError: If a cycle is detected.
        GenreValidationError: If chain exceeds MAX_INHERITANCE_DEPTH.
    """
    if depth > MAX_INHERITANCE_DEPTH:
        raise GenreValidationError(
            message=f"trope inheritance chain exceeds maximum depth of {MAX_INHERITANCE_DEPTH}"
        )

    if current_slug in visited:
        raise GenreCycleError(trope=current_slug)

    visited.add(current_slug)

    trope = parent_map.get(current_slug)
    if trope is not None and trope.extends is not None:
        next_slug = _slugify(trope.extends)
        _detect_cycle(next_slug, parent_map, visited, depth + 1)


def _merge_trope(parent: TropeDefinition, child: TropeDefinition) -> TropeDefinition:
    """Merge a child trope with its parent: child fields override parent fields.

    Port of Rust merge_trope(). Uses model_validate with explicit dict to work
    around pydantic aliased-field pyright limitations.
    """
    data: dict[str, object] = {
        "id": child.id if child.id is not None else parent.id,
        "name": child.name,
        "description": child.description if child.description is not None else parent.description,
        # Child category overrides if non-empty, else inherit from parent
        "category": child.category if child.category else parent.category,
        "triggers": child.triggers if child.triggers else parent.triggers,
        "narrative_hints": child.narrative_hints
        if child.narrative_hints
        else parent.narrative_hints,
        "tension_level": (
            child.tension_level if child.tension_level is not None else parent.tension_level
        ),
        "resolution_hints": (
            child.resolution_hints
            if child.resolution_hints is not None
            else parent.resolution_hints
        ),
        "resolution_patterns": (
            child.resolution_patterns
            if child.resolution_patterns is not None
            else parent.resolution_patterns
        ),
        "tags": child.tags if child.tags else parent.tags,
        # Faction/zone-eligibility tag (epic-157, ADR-059 amendment). Same
        # child-overrides / empty-inherits semantics as tags/triggers: a world
        # trope that `extends` a parent and declares its own factions keeps them;
        # an untagged child inherits the parent's. Omitting this drops the tag to
        # [] (permissive) and the trope leaks across every zone — the Seam-3 gate
        # then can't scope it. (Surfaced by gulliver's three `extends` tropes,
        # story 157-5.)
        "factions": child.factions if child.factions else parent.factions,
        "escalation": child.escalation if child.escalation else parent.escalation,
        "passive_progression": (
            child.passive_progression
            if child.passive_progression is not None
            else parent.passive_progression
        ),
        # Resolved tropes are never abstract (use alias key for model_validate)
        "abstract": False,
        # Clear extends after resolution
        "extends": None,
    }
    return TropeDefinition.model_validate(data)
