"""Inventory and economy types from inventory.yaml.

Port of sidequest-genre/src/models/inventory.rs.
"""

from __future__ import annotations

import random
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.protocol.dice import DieSides

_DICE_RE = re.compile(r"^(?P<count>\d+)d(?P<faces>\d+)$")

# Licenses that permit verbatim reproduction of an SRD mechanical envelope
# (ADR-145 D4). The Without Number line is "wn-free"; nothing else permits
# verbatim reuse. Single source of truth for the verbatim-license invariant,
# enforced structurally in ItemProvenance and (defense in depth) in the
# extraction tool.
_VERBATIM_LICENSES = frozenset({"wn-free"})

# Parity die (the tabletop "no d2 in the bag" move): a d2 is realized by
# throwing a real, renderable die and reading its parity — even → 1, odd → 2.
# The 3D overlay (ADR-074/075) only meshes d4/d6/d8/d10/d12/d20/d100, so a
# literal d2 cannot animate; the backing d6 does. This is the single source of
# truth for the mapping, shared by ``DamageSpec.roll`` (server-side) and the
# overlay damage path (``dispatch/damage_roll.py``).
_PARITY_FACES = 2
PARITY_BACKING_SIDES = DieSides.D6


def parity_value(face: int) -> int:
    """Map a backing-die face to its d2 value: even → 1, odd → 2."""
    return 1 if face % 2 == 0 else 2


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

    # CWN lethality (spec 2026-05-28). All default to "off" so non-CWN content
    # validates unchanged. trauma_die: weapon's Trauma Die rolled vs the victim's
    # Trauma Target; on a Traumatic Hit total damage is multiplied by trauma_rating.
    # trauma_target overrides the victim's default Trauma Target when the weapon
    # itself sets the bar (rare; usually None → cfg default).
    #
    # CWN "Shock X/AC Y" (two decoupled numbers):
    #   shock    — the chip amount X applied on a MISS.
    #   shock_ac — the Melee-AC ceiling Y. Shock only fires when the target's
    #              Melee AC <= shock_ac.
    # A v1 simplification collapsed X and Y into the single `shock` value, which
    # meant a katana with shock=2 only ever chipped vs AC<=2 targets — Shock
    # never fired against real opponents. They are now separate fields.
    trauma_die: str | None = None
    trauma_rating: int = Field(default=1, ge=1)
    trauma_target: int | None = Field(default=None, ge=2)
    shock: int = Field(default=0, ge=0)
    shock_ac: int | None = Field(default=None, ge=1)

    @field_validator("trauma_die")
    @classmethod
    def _valid_trauma_die(cls, v: str | None) -> str | None:
        if v is None:
            return v
        m = _DICE_RE.match(v.strip())
        if not m:
            raise ValueError(f"trauma_die {v!r} is not NdM notation")
        if DieSides.from_wire(int(m["faces"])) is DieSides.Unknown:
            raise ValueError(f"trauma_die {v!r} uses unsupported face count")
        return v

    @field_validator("dice")
    @classmethod
    def _valid_dice(cls, v: str) -> str:
        m = _DICE_RE.match(v.strip())
        if not m:
            raise ValueError(f"damage dice {v!r} is not NdM notation")
        count, faces = int(m["count"]), int(m["faces"])
        if count < 1:
            raise ValueError(f"damage dice {v!r} needs at least 1 die")
        # d2 is a parity die — legal even though it has no overlay mesh; it is
        # realized by a backing d6 read for parity (see PARITY_BACKING_SIDES).
        if faces == _PARITY_FACES:
            return v
        if DieSides.from_wire(faces) is DieSides.Unknown:
            raise ValueError(f"damage dice {v!r} uses unsupported face count d{faces}")
        return v

    @property
    def is_parity_die(self) -> bool:
        """True when ``dice`` is an Nd2 parity die (backed by a real d6)."""
        m = _DICE_RE.match(self.dice.strip())
        return m is not None and int(m["faces"]) == _PARITY_FACES

    @model_validator(mode="after")
    def _shock_requires_ceiling(self) -> DamageSpec:
        # No Silent Fallbacks: a shock weapon without an AC ceiling can never
        # fire (the "Shock X/AC Y" rule needs both numbers). Fail at load
        # rather than silently never-chipping.
        if self.shock > 0 and self.shock_ac is None:
            raise ValueError(
                "shock>0 requires shock_ac (the 'Shock X/AC Y' ceiling); "
                "a shock weapon without a ceiling is a content error"
            )
        return self

    def roll(self, rng: random.Random) -> int:
        """Roll this damage to a concrete total (sum of N d<faces> + bonus).

        Server-side dice for cases where the result isn't a client physics
        throw (e.g. NPC/ship-gunnery damage). ``dice`` is validated NdM at
        construction, so the parse here is safe.

        A parity die (Nd2) is rolled as N backing d6 reads mapped even→1/odd→2,
        matching the overlay path so the two never diverge."""
        m = _DICE_RE.match(self.dice.strip())
        count, faces = int(m["count"]), int(m["faces"])  # type: ignore[union-attr]
        if faces == _PARITY_FACES:
            backing = PARITY_BACKING_SIDES.faces()
            assert backing is not None  # PARITY_BACKING_SIDES is never Unknown
            return sum(parity_value(rng.randint(1, backing)) for _ in range(count)) + self.bonus
        return sum(rng.randint(1, faces) for _ in range(count)) + self.bonus


class ItemProvenance(BaseModel):
    """Where a catalog item's mechanics came from (ADR-145 D2).

    Strict, first-class data — not a YAML comment — so the extraction tool
    (114-3) and the licensing audit can read it. ``mode`` describes the
    *mechanical envelope* only; presentation (``name``/``description``) is
    always freely reskinnable regardless of mode (ADR-145 D1).
    """

    model_config = {"extra": "forbid"}

    mode: Literal["verbatim", "derived", "bespoke"]
    srd: str | None = None  # "wwn" | "cwn" | "swn" | "awn"; None iff bespoke
    srd_ref: str | None = None  # SRD section/table, e.g. "WWN SRD §3.0.1 Armor"
    # "wn-free" = Without Number SRD free-use terms (all four WN SRDs);
    # "ccby" = Fate Core; "none"/"na" = no verbatim permission.
    license: Literal["wn-free", "ccby", "none", "na"] = "na"
    extracted_by: str | None = None  # extraction-tool version stamp; None for hand-authored bespoke

    @model_validator(mode="after")
    def _verbatim_requires_permitting_license(self) -> ItemProvenance:
        # No Silent Fallbacks (ADR-145 D4): a verbatim record may only claim a
        # license that permits verbatim reuse. Enforced structurally here so NO
        # construction path — not just the extraction tool — can mint a
        # self-inconsistent verbatim item. Mirrors DamageSpec._shock_requires_ceiling.
        if self.mode == "verbatim" and self.license not in _VERBATIM_LICENSES:
            raise ValueError(
                f"provenance mode='verbatim' requires a license permitting verbatim reuse "
                f"(one of {sorted(_VERBATIM_LICENSES)}); got license={self.license!r} (ADR-145 D4)"
            )
        return self


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
    armor_class: int | None = (
        None  # armor: SWN ascending AC the attack rolls against (distinct from mitigation soak)
    )
    heal_amount: str | None = (
        None  # consumable: HP restored on use, NdM[+B] (e.g. "1d6+2"); applied at consume (Story 106-4)
    )
    # ADR-145 D2 schema delta. None-defaulted so existing melee/armor items
    # validate unchanged; populated by SRD extraction (114-3).
    provenance: ItemProvenance | None = None  # where the mechanics came from
    tech_level: int | None = None  # SWN/AWN/CWN TL tag
    range_band: str | None = None  # ranged: "thrown" | "pistol" | "rifle" | ... (SRD bands)
    magazine: int | None = None  # ranged: shots per reload


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
