"""SiteArchetype — a genre's site catalog entry (interior algorithm, size, grid
dims). Content, not engine code (Jade doctrine): a new archetype is YAML, never
a server change. Loaded from the genre-root ``site_archetypes.yaml`` (Track B,
plan task 10) and consumed by bounded materialization (task 11) + the site
single-writer (task 12)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SiteArchetype(BaseModel):
    """A per-genre site type: which interior generator to run, how many rooms,
    and the tactical grid dimensions. Distinct from ``NpcArchetype`` (chargen);
    the ``site_archetypes.yaml`` filename avoids the ``archetypes.yaml``
    collision."""

    model_config = {"extra": "allow"}

    archetype_id: str
    interior_algorithm: str
    room_count_min: int = Field(ge=1)
    room_count_max: int = Field(ge=1)
    grid_width: int = Field(ge=5)
    grid_height: int = Field(ge=5)
    cell_scale_feet: int = Field(ge=1, default=5)
    room_vocabulary: list[str] = Field(default_factory=list)
    feature_palette: list[str] = Field(default_factory=list)

    @field_validator("interior_algorithm")
    @classmethod
    def _known_algorithm(cls, v: str) -> str:
        # Lazy import: this model is imported by GenrePack, which is pulled in
        # during game.persistence's module init; importing ``sidequest.dungeon``
        # at module top would run dungeon/__init__ -> materializer ->
        # dungeon.persistence -> game.persistence (mid-init) and close a cycle.
        # Deferring to validation time (a SiteArchetype is only built well after
        # all modules load) breaks the genre->dungeon module-load edge.
        from sidequest.dungeon.interiors import ALGORITHMS

        # No Silent Fallbacks: an unknown generator is an authoring bug, loud.
        if v not in ALGORITHMS:
            raise ValueError(f"interior_algorithm {v!r} not in {sorted(ALGORITHMS)}")
        return v
