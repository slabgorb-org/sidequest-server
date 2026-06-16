"""Layered genre-pack resolution — base → genre → world → culture merge.

Replaces the Rust #[derive(Layered)] proc-macro with a single runtime base
class that reads field-level merge strategies from pydantic Field metadata.

Port of:
  sidequest-genre/src/resolver/merge.rs   — MergeStrategy enum + helpers
  sidequest-genre/src/resolver/load.rs    — LayeredMerge trait
  sidequest-genre/src/resolver/resolved.rs — Resolved<T> wrapper
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel

from sidequest.protocol.provenance import ContributionKind, MergeStep, Provenance, Tier


class MergeStrategy(StrEnum):
    """Per-field merge strategy, declared via Field(json_schema_extra={"merge": ...}).

    Maps directly to the Rust MergeStrategy enum in resolver/merge.rs.
    """

    REPLACE = "replace"
    """Deeper tier's value wins outright when present.

    Matches Rust: Replace variant. The proc-macro emits `other.#ident` for this strategy,
    meaning the deeper tier's value unconditionally replaces the shallower tier's.
    """

    APPEND = "append"
    """Deeper tier's list concatenates onto base's.

    Matches Rust: Append variant + apply_append() helper. Result is base + deeper
    (shallower items first).
    """

    DEEP_MERGE = "deep_merge"
    """Struct-walked merge — recurses into nested LayeredMerge instances.

    Matches Rust: DeepMerge variant. Only valid when both values are LayeredMerge
    instances; raises TypeError otherwise.
    """

    CULTURE_FINAL = "culture_final"
    """Semantic signal that this field should only be set by the Culture tier.

    IMPORTANT: The merge logic is identical to REPLACE — no runtime enforcement
    that only the Culture tier sets this field. This matches the Rust proc-macro
    behavior exactly: both Replace and CultureFinal emit `other.#ident`. The
    distinction is documentation-only intent, not a runtime guarantee.
    """


def _apply_strategy(strategy: str, self_val: Any, other_val: Any) -> Any:
    """Apply a merge strategy to produce the merged value.

    Args:
        strategy: One of the MergeStrategy string values.
        self_val: The shallower (base) tier's value.
        other_val: The deeper tier's value.

    Returns:
        The merged value.

    Raises:
        TypeError: If deep_merge is used on non-LayeredMerge values.
        ValueError: If the strategy string is not recognized.
    """
    if strategy in (MergeStrategy.REPLACE.value, MergeStrategy.CULTURE_FINAL.value):
        return other_val
    if strategy == MergeStrategy.APPEND.value:
        return list(self_val) + list(other_val)
    if strategy == MergeStrategy.DEEP_MERGE.value:
        if isinstance(self_val, LayeredMerge) and isinstance(other_val, LayeredMerge):
            return self_val.merge(other_val)
        raise TypeError(
            f"deep_merge requires both values to be LayeredMerge instances; "
            f"got {type(self_val).__name__} and {type(other_val).__name__}"
        )
    raise ValueError(f"Unknown merge strategy: {strategy!r}")


class LayeredMerge(BaseModel):
    """Base class for pydantic models participating in base → genre → world → culture layering.

    Port of the Rust LayeredMerge trait (resolver/load.rs). In Rust this was a
    proc-macro trait; here it's a runtime pydantic base class that reads merge
    strategies from field metadata at merge time.

    Field merge behavior is declared via Field metadata:

        name: str = Field(default="", json_schema_extra={"merge": "replace"})
        tags: list[str] = Field(default_factory=list, json_schema_extra={"merge": "append"})

    Every field MUST have a "merge" key in json_schema_extra. The wiring test
    for each concrete subclass verifies this (no silent defaults).
    """

    def merge(self, other: Self) -> Self:
        """Merge `other` (deeper tier) into `self` (shallower tier).

        Walks all fields, reads each field's "merge" strategy from
        json_schema_extra, and dispatches to _apply_strategy. Fields
        with no declared strategy default to "replace" but concrete
        subclasses are expected to declare all strategies explicitly
        (enforced by per-type wiring tests).

        Args:
            other: The deeper-tier instance. Must be the same type as self.

        Returns:
            A new instance of the same type with merged field values.
        """
        merged: dict[str, Any] = {}
        for field_name, field_info in type(self).model_fields.items():
            extra = field_info.json_schema_extra or {}
            raw_strategy = extra.get("merge", "replace") if isinstance(extra, dict) else "replace"
            strategy = str(raw_strategy)
            self_val = getattr(self, field_name)
            other_val = getattr(other, field_name)
            merged[field_name] = _apply_strategy(strategy, self_val, other_val)
        return type(self)(**merged)


T = TypeVar("T")


class Resolved(BaseModel, Generic[T]):  # noqa: UP046 — pydantic 2.x BaseModel + PEP 695 syntax has known caveats
    """A resolved content value paired with its full provenance.

    Port of Rust Resolved<T> (resolver/resolved.rs). Generic over the value
    type; in practice T is a LayeredMerge subclass like ArchetypeResolved.

    The provenance types (Tier, MergeStep, Provenance) live in
    sidequest.protocol.provenance to mirror the Rust pattern where they live
    in sidequest-protocol so they can ride on wire payloads.
    """

    model_config = {"arbitrary_types_allowed": True}

    value: Any
    """The resolved value."""
    provenance: Provenance
    """Where the value came from and how it was assembled."""


class ResolutionContext(BaseModel):
    """The genre / world / culture context for a resolution walk.

    Port of Rust ResolutionContext (resolver/load.rs). Carries the three
    contextual keys needed to locate tier files on disk.
    """

    genre: str
    """Genre code (e.g. "heavy_metal", "caverns_and_claudes")."""
    world: str | None = None
    """World name (e.g. "evropi"). Required for World and Culture tiers."""
    culture: str | None = None
    """Culture name (e.g. "thornwall"). Required for Culture tier only."""


def _load_tier(path: Path, type_: type[Any]) -> Any:
    """Load and parse a YAML tier file.

    Args:
        path: Absolute path to the YAML file.
        type_: The pydantic model class to parse into.

    Returns:
        A parsed instance of type_.

    Raises:
        GenreLoadError: If the file cannot be read.
        SchemaValidationError: If the YAML cannot be parsed into type_.
    """
    import yaml

    from sidequest.genre.error import GenreLoadError, SchemaValidationError

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GenreLoadError(path=path, detail=str(e)) from e

    try:
        raw = yaml.safe_load(text)
        return type_.model_validate(raw)
    except Exception as e:
        raise SchemaValidationError(
            message=f"parsing {path}: {e}",
        ) from e


class Resolver[T]:
    """Loads tier files and applies the Layered merge walk, recording provenance.

    Port of Rust Resolver<T> (resolver/load.rs). Walks Global → Genre →
    World → Culture, merging at each tier using the LayeredMerge protocol.

    The resolver is generic over T (must be a LayeredMerge subclass for
    resolve_merged). The root is the genre packs root directory.
    """

    def __init__(self, root: Path, type_: type[T]) -> None:
        self._root = root
        self._type = type_

    def resolve(self, axis: str, ctx: ResolutionContext) -> Resolved[T]:
        """Load the World-tier file for `axis` under
        {root}/{genre}/worlds/{world}/{axis}.yaml.

        Single-file World-tier load. Use resolve_merged for the full
        Global → Genre → World → Culture walk.

        Args:
            axis: Axis filename stem (e.g. "archetype").
            ctx: Resolution context. world must be set.

        Returns:
            Resolved[T] with World-tier provenance.

        Raises:
            GenreValidationError: If world is not set in ctx.
            GenreLoadError: If the file cannot be read.
            SchemaValidationError: If the YAML cannot be parsed.
        """
        from sidequest.genre.error import GenreValidationError

        if ctx.world is None:
            raise GenreValidationError(message="world is required for this axis")

        path = self._root / ctx.genre / "worlds" / ctx.world / f"{axis}.yaml"
        value = _load_tier(path, self._type)

        step = MergeStep(
            tier=Tier.world,
            file=str(path),
            span=None,
            contribution=ContributionKind.initial,
        )
        provenance = Provenance(
            source_tier=Tier.world,
            source_file=str(path),
            source_span=None,
            merge_trail=[step],
        )
        return Resolved(value=value, provenance=provenance)

    def resolve_merged(self, axis: str, field_path: str, ctx: ResolutionContext) -> Resolved[T]:
        """Resolve a field path across Global → Genre → World → Culture.

        Port of Rust Resolver<T>::resolve_merged. Merges tier files using
        the LayeredMerge.merge() protocol at each tier that provides a file.

        Args:
            axis: Semantic axis name for OTEL (e.g. "archetype").
            field_path: On-disk file stem (e.g. "archetype"). May differ
                from axis once content is reorganized.
            ctx: Resolution context.

        Returns:
            Resolved[T] with full merge-trail provenance.

        Raises:
            GenreValidationError: If no tier provides the field.
            GenreLoadError: If a tier file cannot be read.
            SchemaValidationError: If a tier file cannot be parsed.
        """
        from sidequest.genre.error import GenreValidationError

        trail: list[MergeStep] = []
        current: T | None = None
        final_tier = Tier.global_
        final_file = ""

        # Global tier
        global_path = self._root / f"{field_path}.yaml"
        if global_path.exists():
            val = _load_tier(global_path, self._type)
            contribution = ContributionKind.initial
            current = val
            final_tier = Tier.global_
            final_file = str(global_path)
            trail.append(
                MergeStep(
                    tier=Tier.global_,
                    file=str(global_path),
                    span=None,
                    contribution=contribution,
                )
            )

        # Genre tier
        genre_path = self._root / ctx.genre / f"{field_path}.yaml"
        if genre_path.exists():
            val = _load_tier(genre_path, self._type)
            contribution = (
                ContributionKind.merged if current is not None else ContributionKind.initial
            )
            if current is not None:
                assert isinstance(current, LayeredMerge)
                assert isinstance(val, LayeredMerge)
                current = current.merge(val)  # type: ignore[assignment]
            else:
                current = val
            final_tier = Tier.genre
            final_file = str(genre_path)
            trail.append(
                MergeStep(
                    tier=Tier.genre,
                    file=str(genre_path),
                    span=None,
                    contribution=contribution,
                )
            )

        # World tier
        if ctx.world is not None:
            world_path = self._root / ctx.genre / "worlds" / ctx.world / f"{field_path}.yaml"
            if world_path.exists():
                val = _load_tier(world_path, self._type)
                contribution = (
                    ContributionKind.merged if current is not None else ContributionKind.initial
                )
                if current is not None:
                    assert isinstance(current, LayeredMerge)
                    assert isinstance(val, LayeredMerge)
                    current = current.merge(val)  # type: ignore[assignment]
                else:
                    current = val
                final_tier = Tier.world
                final_file = str(world_path)
                trail.append(
                    MergeStep(
                        tier=Tier.world,
                        file=str(world_path),
                        span=None,
                        contribution=contribution,
                    )
                )

        # Culture tier
        if ctx.world is not None and ctx.culture is not None:
            culture_path = (
                self._root
                / ctx.genre
                / "worlds"
                / ctx.world
                / "cultures"
                / ctx.culture
                / f"{field_path}.yaml"
            )
            if culture_path.exists():
                val = _load_tier(culture_path, self._type)
                contribution = (
                    ContributionKind.merged if current is not None else ContributionKind.initial
                )
                if current is not None:
                    assert isinstance(current, LayeredMerge)
                    assert isinstance(val, LayeredMerge)
                    current = current.merge(val)  # type: ignore[assignment]
                else:
                    current = val
                final_tier = Tier.culture
                final_file = str(culture_path)
                trail.append(
                    MergeStep(
                        tier=Tier.culture,
                        file=str(culture_path),
                        span=None,
                        contribution=contribution,
                    )
                )

        if current is None:
            raise GenreValidationError(message=f"no tier supplied field '{field_path}'")

        provenance = Provenance(
            source_tier=final_tier,
            source_file=final_file,
            source_span=None,
            merge_trail=trail,
        )
        return Resolved(value=current, provenance=provenance)
