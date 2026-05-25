"""Advancement effect types (Story 39-5 / ADR-078).

Port of sidequest-genre/src/models/advancement.rs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# RecoveryTrigger
# ---------------------------------------------------------------------------


class RecoveryTriggerOnResolution(BaseModel):
    """Restore edge when the encounter resolves."""

    model_config = {"extra": "forbid"}

    kind: Literal["on_resolution"]


class RecoveryTriggerOnAllyRescue(BaseModel):
    """An ally spending an action to shore up the creature."""

    model_config = {"extra": "forbid"}

    kind: Literal["on_ally_rescue"]


class RecoveryTriggerOnBeatSuccess(BaseModel):
    """A specific authored beat landing."""

    model_config = {"extra": "forbid"}

    kind: Literal["on_beat_success"]
    beat_id: str
    amount: int
    while_strained: bool = False


RecoveryTrigger = Annotated[
    RecoveryTriggerOnResolution | RecoveryTriggerOnAllyRescue | RecoveryTriggerOnBeatSuccess,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# LoreRevealScope
# ---------------------------------------------------------------------------


class LoreRevealScope(StrEnum):
    """Scope of a Lore-revealing advancement effect."""

    threshold_crossings = "threshold_crossings"
    encounter_resolution = "encounter_resolution"
    session_summary = "session_summary"


# ---------------------------------------------------------------------------
# AdvancementEffect variants
# ---------------------------------------------------------------------------


class AdvancementEffectHpMaxBonus(BaseModel):
    """Raise core.hp.max by amount on grant (ADR-114: was AdvancementEffectEdgeMaxBonus)."""

    model_config = {"extra": "forbid"}

    type: Literal["edge_max_bonus"]
    amount: int


class AdvancementEffectHpRecovery(BaseModel):
    """Add a new RecoveryTrigger to the creature's pool (ADR-114: was AdvancementEffectEdgeRecovery)."""

    model_config = {"extra": "forbid"}

    type: Literal["edge_recovery"]
    trigger: RecoveryTrigger
    amount: int


class AdvancementEffectBeatDiscount(BaseModel):
    """Reduce the edge_delta of a specific beat."""

    model_config = {"extra": "forbid"}

    type: Literal["beat_discount"]
    beat_id: str
    edge_delta_mod: int
    resource_mod: dict[str, int] | None = None


class AdvancementEffectLeverageBonus(BaseModel):
    """Increase the target_edge_delta of a specific beat."""

    model_config = {"extra": "forbid"}

    type: Literal["leverage_bonus"]
    beat_id: str
    target_edge_delta_mod: int


class AdvancementEffectLoreRevealBonus(BaseModel):
    """Broaden the scope of Lore reveals."""

    model_config = {"extra": "forbid"}

    type: Literal["lore_reveal_bonus"]
    scope: LoreRevealScope


AdvancementEffect = Annotated[
    AdvancementEffectHpMaxBonus
    | AdvancementEffectHpRecovery
    | AdvancementEffectBeatDiscount
    | AdvancementEffectLeverageBonus
    | AdvancementEffectLoreRevealBonus,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# AdvancementTier / AdvancementTree
# ---------------------------------------------------------------------------


class AdvancementTier(BaseModel):
    """A single authored advancement tier."""

    model_config = {"extra": "forbid"}

    id: str
    required_milestone: str
    class_gates: list[str] = Field(default_factory=list)
    effects: list[AdvancementEffect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_non_blank(self) -> AdvancementTier:
        if not self.id.strip():
            raise ValueError("AdvancementTier.id must not be blank")
        if not self.required_milestone.strip():
            raise ValueError("AdvancementTier.required_milestone must not be blank")
        return self


class AdvancementTree(BaseModel):
    """A genre's authored advancement tiers."""

    model_config = {"extra": "forbid"}

    tiers: list[AdvancementTier] = Field(default_factory=list)
