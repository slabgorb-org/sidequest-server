"""MutationCatalog structural validators — synthetic fixtures only.

Per spec P2-4: content invariants (10 positives per category, the real
d100 table) belong to the PACK validator in sidequest-content. These
tests cover engine-structural rules with minimal synthetic catalogs.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)


def _stigma() -> StigmaTables:
    return StigmaTables(
        body_part=[f"part_{i}" for i in range(6)],
        nature=[f"nature_{i}" for i in range(6)],
        flavor=[f"flavor_{i}" for i in range(12)],
    )


def _negative(id: str = "negative/withered_arm", lo: int = 1, hi: int = 100) -> NegativeMutationDef:
    return NegativeMutationDef(id=id, name="Withered Arm", roll_range=(lo, hi), effect="arm weak")


def _positive(
    id: str = "structure/crushing_jaws", category: str = "structure"
) -> PositiveMutationDef:
    return PositiveMutationDef(id=id, name="Crushing Jaws", category=category, effect="bite hard")


def _catalog(**overrides) -> MutationCatalog:
    kwargs = dict(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=_stigma(),
        negatives=[_negative()],
        positives=[_positive()],
    )
    kwargs.update(overrides)
    return MutationCatalog(**kwargs)


def test_minimal_catalog_validates() -> None:
    cat = _catalog()
    assert cat.positive_by_id("structure/crushing_jaws").name == "Crushing Jaws"
    assert cat.negative_for_roll(1).id == "negative/withered_arm"
    assert cat.negative_for_roll(100).id == "negative/withered_arm"


def test_d100_gap_rejected() -> None:
    with pytest.raises(ValidationError, match="partition"):
        _catalog(negatives=[_negative(lo=1, hi=40), _negative(id="negative/frail", lo=42, hi=100)])


def test_d100_overlap_rejected() -> None:
    with pytest.raises(ValidationError, match="partition"):
        _catalog(negatives=[_negative(lo=1, hi=50), _negative(id="negative/frail", lo=50, hi=100)])


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        _catalog(positives=[_positive(), _positive()])


def test_id_must_be_category_slash_slug() -> None:
    with pytest.raises(ValidationError, match="id"):
        _positive(id="CrushingJaws")


def test_positive_id_prefix_must_match_category() -> None:
    with pytest.raises(ValidationError, match="category"):
        _catalog(positives=[_positive(id="sense/crushing_jaws", category="structure")])


def test_negative_attr_penalty_floor() -> None:
    with pytest.raises(ValidationError, match="-2"):
        NegativeMutationDef(
            id="negative/ruined_spine",
            name="Ruined Spine",
            roll_range=(1, 100),
            effect="bad",
            attr_penalties={"STR": -3},
        )


def test_unknown_positive_id_raises() -> None:
    with pytest.raises(KeyError, match="not in catalog"):
        _catalog().positive_by_id("exotic/wings")


def test_stigma_wrong_size_rejected() -> None:
    with pytest.raises(ValidationError, match="d6/d6/d12"):
        StigmaTables(
            body_part=["a"] * 5,
            nature=["b"] * 6,
            flavor=["c"] * 12,
        )
