"""Inventory and economy types from inventory.yaml.

Port of sidequest-genre/src/models/inventory.rs.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from sidequest.protocol.dice import DieSides

_DICE_RE = re.compile(r"^(?P<count>\d+)d(?P<faces>\d+)$")


class CurrencyConfig(BaseModel):
    """Currency system definition.

    ``abbreviation``, ``description``, and ``secondary`` are authored flavor
    fields that the Rust engine silently dropped. Accepted here as
    pass-through so content isn't lossy and future consumers can read them.
    """

    model_config = {"extra": "forbid"}

    name: str
    denominations: Any = None  # accepts list[str] or dict[str, float]
    abbreviation: str | None = None
    description: str | None = None
    secondary: Any = None  # some packs declare a secondary currency (dict or string)


class DamageSpec(BaseModel):
    """Weapon damage descriptor (ADR-114 §3). SWN-native dice (1d6…2d12) so the
    value is concrete and feeds the ADR-074/075 dice overlay directly."""

    model_config = {"extra": "forbid"}

    dice: str  # "NdM" — M must be a supported DieSides face count
    bonus: int = 0
    armor_piercing: int = Field(default=0, ge=0)  # AP: reduces target Armor before subtraction

    @field_validator("dice")
    @classmethod
    def _valid_dice(cls, v: str) -> str:
        m = _DICE_RE.match(v.strip())
        if not m:
            raise ValueError(f"damage dice {v!r} is not NdM notation")
        count, faces = int(m["count"]), int(m["faces"])
        if count < 1:
            raise ValueError(f"damage dice {v!r} needs at least 1 die")
        if DieSides.from_wire(faces) is DieSides.Unknown:
            raise ValueError(f"damage dice {v!r} uses unsupported face count d{faces}")
        return v


class CatalogItem(BaseModel):
    """A single item in the genre pack's item catalog."""

    model_config = {"extra": "forbid"}

    id: str
    name: str
    description: str
    category: str
    value: int = 0
    weight: float = 0.0
    rarity: str = ""
    power_level: int = 0
    tags: list[str] = Field(default_factory=list)
    lore: str = ""
    narrative_weight: Any = None  # accepts string or numeric
    resource_ticks: int | None = None
    damage: DamageSpec | None = None  # weapons
    mitigation: int | None = None  # armor: flat damage reduction (SWN soak)


class CarryMode(StrEnum):
    """Whether inventory limits are enforced by item count or total weight."""

    # Note: using 'item_count' as the enum name because 'count' conflicts with str.count().
    # The YAML value is "count" (matching the Rust snake_case rename).
    item_count = "count"
    weight = "weight"


class InventoryPhilosophy(BaseModel):
    """Inventory philosophy configuration.

    ``notes`` is authored prose (space_opera) that Rust dropped. Accepted as
    pass-through.
    """

    model_config = {"extra": "forbid"}

    carry_limit: int | None = None
    carry_mode: CarryMode = CarryMode.item_count
    weight_limit: float | None = None
    restricted_categories: list[str] = Field(default_factory=list)
    progression_gates: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class InventoryConfig(BaseModel):
    """Complete inventory configuration from inventory.yaml."""

    model_config = {"extra": "forbid"}

    currency: CurrencyConfig | None = None
    item_catalog: list[CatalogItem] = Field(default_factory=list)
    starting_equipment: dict[str, list[str]] = Field(default_factory=dict)
    starting_gold: dict[str, int] = Field(default_factory=dict)
    philosophy: InventoryPhilosophy | None = None
