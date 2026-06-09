"""Mutation catalog models — structural validation only.

Content invariants (exactly 10 positives per category, the faithful AWN
d100 table) are enforced by the sidequest-content pack validator, not
here (spec P2-4). This module guarantees what RESOLUTION needs: unique
slug-shaped ids, a gapless/non-overlapping d100 partition, and the -2
attribute-penalty floor (AWN p.18).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CATEGORIES = ("structure", "sense", "hybrid", "cognition", "pseudo_psychic", "exotic")

_ID_RE = re.compile(r"^[a-z_]+/[a-z0-9_]+$")


class MutationAttack(BaseModel):
    """Natural-weapon block — resolves via the existing CWN attack stack."""

    model_config = {"extra": "forbid"}

    skill: str
    damage: str
    shock: str | None = None
    trauma_die: str | None = None
    trauma_rating: str | None = None


class SaveVs(BaseModel):
    """Save clause — same shape innate_v1 codified (stat None = auto-apply)."""

    model_config = {"extra": "forbid"}

    stat: str | None = None
    effect: str = "negates"


class PositiveMutationDef(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    name: str
    category: Literal[
        "structure", "sense", "hybrid", "cognition", "pseudo_psychic", "exotic"
    ]
    effect: str
    strain_cost: int = Field(default=0, ge=0)
    usage: Literal["at_will", "per_scene", "per_day"] = "at_will"
    uses_per_period: int = Field(default=1, ge=1)
    save: SaveVs | None = None
    attack: MutationAttack | None = None
    modifiers: dict[str, int] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def id_slug_shape(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"mutation id {v!r} must match <category>/<snake_case>")
        return v

    @model_validator(mode="after")
    def id_prefix_matches_category(self) -> PositiveMutationDef:
        prefix = self.id.split("/", 1)[0]
        if prefix != self.category:
            raise ValueError(
                f"id prefix {prefix!r} must equal category {self.category!r} (id={self.id!r})"
            )
        return self


class NegativeMutationDef(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    name: str
    roll_range: tuple[int, int]
    effect: str
    attr_penalties: dict[str, int] = Field(default_factory=dict)
    modifiers: dict[str, int] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def id_slug_shape(cls, v: str) -> str:
        if not _ID_RE.match(v) or not v.startswith("negative/"):
            raise ValueError(f"negative mutation id {v!r} must match negative/<snake_case>")
        return v

    @field_validator("attr_penalties")
    @classmethod
    def penalty_floor(cls, v: dict[str, int]) -> dict[str, int]:
        for attr, pen in v.items():
            if pen < -2 or pen > 0:
                raise ValueError(
                    f"attr penalty {attr}={pen} out of range; AWN floors penalties at -2 (p.18)"
                )
        return v

    @model_validator(mode="after")
    def range_ordered(self) -> NegativeMutationDef:
        lo, hi = self.roll_range
        if not (1 <= lo <= hi <= 100):
            raise ValueError(f"roll_range {self.roll_range} must satisfy 1 <= lo <= hi <= 100")
        return self


class StigmaTables(BaseModel):
    """d6 body-part x d6 nature x d12 flavor (AWN p.17)."""

    model_config = {"extra": "forbid"}

    body_part: list[str]
    nature: list[str]
    flavor: list[str]

    @model_validator(mode="after")
    def table_sizes(self) -> StigmaTables:
        if len(self.body_part) != 6 or len(self.nature) != 6 or len(self.flavor) != 12:
            raise ValueError(
                f"stigma tables must be d6/d6/d12 — got "
                f"{len(self.body_part)}/{len(self.nature)}/{len(self.flavor)}"
            )
        return self


class MpEconomy(BaseModel):
    """The AWN MP economy numbers (p.16) — data, not code."""

    model_config = {"extra": "forbid"}

    mutant_classes: list[str]
    base_mp: int = 2
    per_negative_mp: int = 2
    max_negatives: int = 3
    concealable_stigma_cost: int = 1
    spend_random_positive: int = 1
    spend_pick_positive: int = 3
    spend_same_category: int = 3
    chargen_negatives_rolled: int = 1


class MutationCatalog(BaseModel):
    model_config = {"extra": "forbid"}

    mp_economy: MpEconomy
    stigma: StigmaTables
    negatives: list[NegativeMutationDef]
    positives: list[PositiveMutationDef]
    narrator_register: str = ""

    @model_validator(mode="after")
    def unique_ids(self) -> MutationCatalog:
        ids = [m.id for m in self.negatives] + [m.id for m in self.positives]
        seen: set[str] = set()
        dupes = {i for i in ids if i in seen or seen.add(i)}  # type: ignore[func-returns-value]
        if dupes:
            raise ValueError(f"duplicate mutation ids: {sorted(dupes)}")
        return self

    @model_validator(mode="after")
    def d100_partition(self) -> MutationCatalog:
        ranges = sorted(m.roll_range for m in self.negatives)
        cursor = 1
        for lo, hi in ranges:
            if lo != cursor:
                raise ValueError(
                    f"negative d100 table must partition 1-100 exactly; "
                    f"expected next range to start at {cursor}, got {lo}"
                )
            cursor = hi + 1
        if cursor != 101:
            raise ValueError(
                f"negative d100 table must partition 1-100 exactly; coverage ends at {cursor - 1}"
            )
        return self

    def positive_by_id(self, mutation_id: str) -> PositiveMutationDef:
        for m in self.positives:
            if m.id == mutation_id:
                return m
        raise KeyError(
            f"positive mutation {mutation_id!r} not in catalog; "
            f"known: {sorted(m.id for m in self.positives)}"
        )

    def negative_for_roll(self, roll: int) -> NegativeMutationDef:
        for m in self.negatives:
            lo, hi = m.roll_range
            if lo <= roll <= hi:
                return m
        raise ValueError(f"d100 roll {roll} matched no negative range (validator should prevent)")
