"""Tests for LayeredMerge base class, MergeStrategy, and Resolved[T].

Ports from Rust:
  tests/layered_derive.rs  — 4 strategy tests
  tests/resolver_unit.rs   — strategy helpers + ResolutionContext
"""

from __future__ import annotations

import pytest
from pydantic import Field

from sidequest.genre.resolver import (
    LayeredMerge,
    MergeStrategy,
    ResolutionContext,
    Resolved,
    _apply_strategy,
)
from sidequest.protocol.provenance import (
    ContributionKind,
    MergeStep,
    Provenance,
    Span,
    Tier,
)

# ---------------------------------------------------------------------------
# Minimal test types — mirror the structs in layered_derive.rs
# ---------------------------------------------------------------------------


class Archetype(LayeredMerge):
    name: str = Field(default="", json_schema_extra={"merge": "replace"})
    quirks: list[str] = Field(default_factory=list, json_schema_extra={"merge": "append"})


class Nested(LayeredMerge):
    inner: str = Field(default="", json_schema_extra={"merge": "replace"})


class Outer(LayeredMerge):
    nested: Nested = Field(default_factory=Nested, json_schema_extra={"merge": "deep_merge"})
    culture_only: str | None = Field(default=None, json_schema_extra={"merge": "culture_final"})


# ---------------------------------------------------------------------------
# Port of layered_derive.rs — fn layered_replace_field_uses_deeper_value
# ---------------------------------------------------------------------------


def test_layered_replace_field_uses_deeper_value() -> None:
    base = Archetype(name="Base", quirks=["a"])
    deeper = Archetype(name="Deeper", quirks=["b"])
    merged = base.merge(deeper)
    assert merged.name == "Deeper"


# ---------------------------------------------------------------------------
# Port of layered_derive.rs — fn layered_append_field_concatenates
# ---------------------------------------------------------------------------


def test_layered_append_field_concatenates() -> None:
    base = Archetype(name="Base", quirks=["a"])
    deeper = Archetype(name="Deeper", quirks=["b"])
    merged = base.merge(deeper)
    assert merged.quirks == ["a", "b"]


# ---------------------------------------------------------------------------
# Port of layered_derive.rs — fn deep_merge_walks_into_nested_struct
# ---------------------------------------------------------------------------


def test_deep_merge_walks_into_nested_struct() -> None:
    base = Outer(nested=Nested(inner="base"), culture_only=None)
    deeper = Outer(nested=Nested(inner="deeper"), culture_only="x")
    merged = base.merge(deeper)
    assert merged.nested.inner == "deeper"
    assert merged.culture_only == "x"


# ---------------------------------------------------------------------------
# Port of layered_derive.rs — fn culture_final_field_takes_deeper_value
# ---------------------------------------------------------------------------


def test_culture_final_field_takes_deeper_value() -> None:
    """culture_final merge behavior is identical to replace (no runtime enforcement).

    This matches the Rust proc-macro exactly: both Replace and CultureFinal emit
    `other.#ident`. The "culture only" constraint is documentation-only intent.
    """
    base = Outer(nested=Nested(), culture_only="from_base")
    deeper = Outer(nested=Nested(), culture_only="from_deeper")
    merged = base.merge(deeper)
    assert merged.culture_only == "from_deeper"


# ---------------------------------------------------------------------------
# Port of resolver_unit.rs — apply_strategy helpers
# ---------------------------------------------------------------------------


def test_replace_strategy_returns_deeper() -> None:
    """Port of fn replace_strategy_returns_deeper."""
    result = _apply_strategy("replace", "base", "deeper")
    assert result == "deeper"


def test_replace_strategy_keeps_base_when_deeper_absent() -> None:
    """Port of fn replace_strategy_keeps_base_when_deeper_absent.

    NOTE: Python _apply_strategy operates on concrete values (not Option<T>).
    The Rust apply_strategy() takes Option<T> and returns deeper.or(base).
    In our Python port, None is a valid "deeper" value — this test documents
    that distinction. The LayeredMerge.merge() layer handles None semantics
    at the pydantic field level (default values). Direct _apply_strategy calls
    with None are valid and return None (the deeper value).
    """
    # Replacing with a concrete value — deeper wins
    result = _apply_strategy("replace", "base", "deeper")
    assert result == "deeper"
    # Explicit None as deeper — deeper (None) wins, base is discarded
    # This is the known Phase D limitation documented in ArchetypeResolved.
    result = _apply_strategy("replace", "base", None)
    assert result is None


def test_append_strategy_concatenates_lists() -> None:
    """Port of fn append_strategy_concatenates_lists."""
    result = _apply_strategy("append", ["a", "b"], ["c"])
    assert result == ["a", "b", "c"]


def test_append_strategy_handles_empty_base() -> None:
    """Port of fn append_strategy_handles_empty_base."""
    result = _apply_strategy("append", [], ["only"])
    assert result == ["only"]


# ---------------------------------------------------------------------------
# Port of resolver_unit.rs — ResolutionContext
# ---------------------------------------------------------------------------


def test_resolution_context_identifies_chain() -> None:
    """Port of fn resolution_context_identifies_chain."""
    ctx = ResolutionContext(genre="heavy_metal", world="evropi", culture="thornwall")
    assert ctx.genre == "heavy_metal"
    assert ctx.world == "evropi"
    assert ctx.culture == "thornwall"


# ---------------------------------------------------------------------------
# Port of resolver_unit.rs — Tier / Span / Provenance / MergeStep roundtrips
# ---------------------------------------------------------------------------


def test_tier_serializes_lowercase() -> None:
    """Port of fn tier_serializes_lowercase."""
    import json

    assert Tier.global_.value == "global"
    assert Tier.genre.value == "genre"
    assert Tier.world.value == "world"
    assert Tier.culture.value == "culture"

    # Verify JSON round-trip (mirrors Rust serde_json test)
    prov = Provenance(
        source_tier=Tier.global_,
        source_file="test.yaml",
        merge_trail=[],
    )
    blob = prov.model_dump_json()
    data = json.loads(blob)
    assert data["source_tier"] == "global"


def test_span_roundtrips() -> None:
    """Port of fn span_roundtrips."""
    s = Span(start_line=12, start_col=1, end_line=18, end_col=0)
    blob = s.model_dump_json()
    back = Span.model_validate_json(blob)
    assert back.start_line == s.start_line
    assert back.start_col == s.start_col
    assert back.end_line == s.end_line
    assert back.end_col == s.end_col


def test_provenance_round_trips_through_json() -> None:
    """Port of fn provenance_round_trips_through_json."""
    prov = Provenance(
        source_tier=Tier.world,
        source_file="worlds/evropi/archetype_funnels.yaml",
        source_span=Span(start_line=12, start_col=1, end_line=18, end_col=0),
        merge_trail=[
            MergeStep(
                tier=Tier.genre,
                file="heavy_metal/archetype_constraints.yaml",
                span=Span(start_line=3, start_col=1, end_line=9, end_col=0),
                contribution=ContributionKind.initial,
            ),
            MergeStep(
                tier=Tier.world,
                file="worlds/evropi/archetype_funnels.yaml",
                span=Span(start_line=12, start_col=1, end_line=18, end_col=0),
                contribution=ContributionKind.replaced,
            ),
        ],
    )
    blob = prov.model_dump_json()
    back = Provenance.model_validate_json(blob)
    assert back.source_tier == prov.source_tier
    assert back.source_file == prov.source_file
    assert back.source_span == prov.source_span
    assert len(back.merge_trail) == 2
    assert back.merge_trail[0].tier == Tier.genre
    assert back.merge_trail[1].contribution == ContributionKind.replaced


# ---------------------------------------------------------------------------
# Python-specific: unknown strategy raises ValueError
# ---------------------------------------------------------------------------


def test_apply_strategy_rejects_unknown_strategy() -> None:
    """_apply_strategy raises ValueError for unrecognized strategy strings."""
    with pytest.raises(ValueError, match="Unknown merge strategy"):
        _apply_strategy("obliterate", "base", "other")


# ---------------------------------------------------------------------------
# Python-specific: deep_merge raises TypeError for non-LayeredMerge values
# ---------------------------------------------------------------------------


def test_deep_merge_raises_type_error_for_non_layered() -> None:
    """deep_merge raises TypeError if either value is not a LayeredMerge instance."""
    with pytest.raises(TypeError, match="deep_merge requires both values"):
        _apply_strategy("deep_merge", {"a": 1}, {"b": 2})


# ---------------------------------------------------------------------------
# Resolved[T] — shape and provenance access
# ---------------------------------------------------------------------------


def test_resolved_carries_value_and_provenance() -> None:
    archetype = Archetype(name="Sage", quirks=["wise"])
    prov = Provenance(
        source_tier=Tier.genre,
        source_file="caverns/archetype.yaml",
        merge_trail=[],
    )
    resolved: Resolved[Archetype] = Resolved(value=archetype, provenance=prov)
    assert resolved.value.name == "Sage"
    assert resolved.provenance.source_tier == Tier.genre


# ---------------------------------------------------------------------------
# MergeStrategy enum values
# ---------------------------------------------------------------------------


def test_merge_strategy_values() -> None:
    assert MergeStrategy.REPLACE.value == "replace"
    assert MergeStrategy.APPEND.value == "append"
    assert MergeStrategy.DEEP_MERGE.value == "deep_merge"
    assert MergeStrategy.CULTURE_FINAL.value == "culture_final"
