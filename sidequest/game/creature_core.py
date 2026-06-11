"""CreatureCore — shared fields for Character and NPC.

Story 1-13: Extracted from Character and NPC via composition.

HpPool is the ablative hit-point pool (ADR-114) — personal vitality and the
damage track, reversing ADR-078's deletion of HP in favor of composure.
Authored B/X ``hp`` in content YAML seeds it directly via ``hp_pool_from_hp``
(creatures/NPCs) and ``hp_pool_from_config`` (chargen, class base + CON mod).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.game.rig_composure_pool import RigComposurePool
from sidequest.game.status import Status, migrate_legacy_statuses
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.wwn_magic import EffortPool, SpellcastingState


class HpPool(BaseModel):
    """First-class ablative hit-point pool (ADR-114 §1).

    Reintroduces personal vitality/damage tracking, reversing ADR-078's
    deletion of HP in favor of composure (:class:`EdgePool`). ``current``
    is clamped to ``[0, max]``. Mirrors ``EdgePool.apply_delta`` so callers
    re-pointed from edge→hp keep the same delta contract.
    """

    model_config = {"extra": "forbid"}

    current: int
    max: int
    base_max: int

    def apply_delta(self, delta: int) -> int:
        """Apply an HP delta. Returns new current value.

        Positive delta increases current (capped at max).
        Negative delta decreases current (floored at 0).
        """
        raw = self.current + delta
        self.current = max(0, min(self.max, raw))
        return self.current


def hp_pool_from_hp(hp: int) -> HpPool:
    """Seed an :class:`HpPool` from an authored HP value (ADR-114 §1).

    The SINGLE canonical HP seeder. Seeds the pool full
    (``current == max == base_max``). REPLACES
    ``creature_edge_pool_from_hp``, which re-interpreted authored HP as
    composure; under ADR-114 the same B/X ``hp`` integer in content YAML is
    once again personal vitality, not composure.

    Floored at 1 because a pool needs a positive ceiling — a creature
    authored with ``hp: 0`` must still be representable/alive as a
    materialized actor.
    """
    seed = max(1, hp)
    return HpPool(current=seed, max=seed, base_max=seed)


class HpConfigMissingClassError(KeyError):
    """Genre pack declared an HP config but omitted a base_max entry for the
    character's class. Fail loud at the boundary (SOUL.md: no silent fallbacks)."""

    def __init__(self, class_name: str) -> None:
        self.class_name = class_name
        super().__init__(f"hp base_max_by_class missing entry for class '{class_name}'")


def hp_pool_from_config(hp_config: object, class_name: str, *, con_score: int) -> HpPool:
    """Build a genre-authored HpPool from class base + CON modifier (ADR-114 §1,
    re-pointing ADR-078's 2026-05-10 CON-mod seed from Edge to HP).

    base_max = max(1, base_max_by_class[class_name] + floor((con_score - 10) / 2)).
    `hp_config` is typed `object` to avoid a circular import with the genre layer;
    duck-type `base_max_by_class`."""
    base_max_by_class = getattr(hp_config, "base_max_by_class", {})
    if class_name not in base_max_by_class:
        raise HpConfigMissingClassError(class_name=class_name)
    con_modifier = (con_score - 10) // 2
    base_max = max(1, base_max_by_class[class_name] + con_modifier)
    return HpPool(current=base_max, max=base_max, base_max=base_max)


class Inventory(BaseModel):
    """Character inventory ledger — append-only item history and gold.

    Phase 1 subset. Full item evolution (narrative_weight thresholds) is
    P2-deferred.
    """

    model_config = {"extra": "forbid"}

    items: list[dict] = Field(default_factory=list)
    gold: int = 0


class CreatureCore(BaseModel):
    """Shared fields for any creature (Character or NPC).

    Embedded via composition in both Character and Npc.

    P1-required: name, description, personality, level, hp, inventory, statuses.
    P2-deferred: acquired_advancements (advancement system, Epic 39-8).
    """

    model_config = {"extra": "forbid"}

    name: str
    description: str
    personality: str
    level: int = 1
    xp: int = 0
    inventory: Inventory = Field(default_factory=Inventory)
    statuses: list[Status] = Field(default_factory=list)
    hp: HpPool = Field(default_factory=lambda: HpPool(current=10, max=10, base_max=10))
    system_strain: SystemStrainPool | None = None
    effort: dict[str, EffortPool] = Field(default_factory=dict)
    spellcasting: SpellcastingState | None = None
    armor_class: int = 10  # SWN ascending AC; unarmored = 10. Seeded from content armor.
    # Generic trait hooks (story 103-2, build plan §D-B): world-tier stock
    # trait sets override Move / modify the Trauma Target for ANY creature —
    # no per-stock special cases. None/0 = the engine defaults stand.
    move: int | None = None  # meters per move action; None = ruleset default
    trauma_target_mod: int = 0  # delta to the trauma target rolled against this creature
    # Vessel-attached composure pool (Epic 53, story 53-2). None for any
    # character without a rig in inventory; populated by
    # ``sidequest.game.vessel_tags.bind_rig_pool_from_inventory`` at
    # chargen-loadout completion and round-tripped through the save file.
    rig_pool: RigComposurePool | None = None
    # P2-deferred: advancement tracking (epic 39-8, mechanical progression)
    acquired_advancements: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_statuses(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw = data.get("statuses")
        if raw is None:
            return data
        if isinstance(raw, list):
            data = {**data, "statuses": migrate_legacy_statuses(raw)}
        return data

    @field_validator("name")
    @classmethod
    def name_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be blank")
        return v

    @field_validator("description")
    @classmethod
    def description_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description cannot be blank")
        return v

    @field_validator("personality")
    @classmethod
    def personality_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("personality cannot be blank")
        return v

    def apply_hp_delta(self, delta: int) -> int:
        """Apply an HP delta and return the new current value."""
        return self.hp.apply_delta(delta)
