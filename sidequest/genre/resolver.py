"""Field-level merge machinery for layered genre-pack models.

Provides ``LayeredMerge`` (a pydantic base class whose subclasses declare
per-field merge strategies via ``Field(json_schema_extra={"merge": ...})``)
and the ``MergeStrategy`` vocabulary it dispatches on.

History (Story 82-4 / ADR-121 narrowing): this module originally also
ported the Rust four-tier ``Resolver<T>`` walk (Global → Genre → World →
Culture via ``resolve_merged``), but that walk never gained a production
consumer and no genre pack ships its per-tier file layout. The production
archetype path is the two-tier shim (``sidequest.genre.archetype.shim``),
which merges different schemas per tier by pair-constrained lookup. The
dead walk (``Resolver``, ``ResolutionContext``, ``Resolved``,
``_load_tier``) was removed; ADR-121 now describes the shim as the
production reality. What remains here is the genuinely live machinery:
``ArchetypeResolved`` subclasses ``LayeredMerge``, and the provenance wire
types it pairs with live in ``sidequest.protocol.provenance``.

Port of:
  sidequest-genre/src/resolver/merge.rs   — MergeStrategy enum + helpers
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel


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
    """Base class for pydantic models that merge a deeper tier into a shallower one.

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
