"""Character progression types from progression.yaml.

Port of sidequest-genre/src/models/progression.rs.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, Field, model_validator

from sidequest.genre.models.advancement import AdvancementEffect


class Ability(BaseModel):
    """An ability within an affinity tier.

    Can be either a simple string description or a full struct. Mirrors the
    Rust `AbilityRepr` untagged enum — when nested inside `list[Ability]`,
    the before-validator coerces a plain string into `{"name": s}`.
    """

    model_config = {"extra": "forbid"}

    name: str
    experience: str = ""
    limits: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"name": data}
        return data


class AffinityTier(BaseModel):
    """A single tier within an affinity."""

    model_config = {"extra": "forbid"}

    name: str
    description: str
    abilities: list[Ability] = Field(default_factory=list)
    mechanical_effects: list[AdvancementEffect] | None = None


class AffinityUnlocks(BaseModel):
    """Tier unlocks for an affinity (generic per-tier ``ProgressionUnlock`` shape).

    Two authored conventions exist:
    - Numbered: ``tier_0, tier_1, tier_2, tier_3`` (elemental_harmony)
    - Named: ``novice, journeyman, expert, master`` (spaghetti_western)

    Both forms are accepted. The named-tier attributes (``tier_0..tier_3``
    and ``novice/journeyman/expert/master``) are exposed for typed access;
    all tiers (however keyed) are also available via ``.tiers`` as an
    ordered dict keyed by the YAML tier name. Other packs with non-magical
    classes (low_fantasy, pulp_noir) can reuse the named convention without
    changes here — the dynamic ``.tiers`` dict captures whatever the pack
    authored. No consumer currently dispatches by tier name — TODO: wire
    at level-up. See ``docs/content-drift-triage.md`` for the spaghetti_western
    triage that introduced the named convention.
    """

    model_config = {"extra": "allow"}

    tier_0: AffinityTier | None = None
    tier_1: AffinityTier | None = None
    tier_2: AffinityTier | None = None
    tier_3: AffinityTier | None = None
    novice: AffinityTier | None = None
    journeyman: AffinityTier | None = None
    expert: AffinityTier | None = None
    master: AffinityTier | None = None
    tiers: dict[str, AffinityTier] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _collect_tiers(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out: dict[str, Any] = {"tiers": {}}
        for key, value in data.items():
            if key == "tiers":
                continue
            if key in _NAMED_TIER_KEYS:
                out[key] = value
            out["tiers"][key] = value
        return out


# Named tier keys that get exposed as typed attributes on ``AffinityUnlocks``.
# Numbered (``tier_0..tier_3``) and named (``novice..master``) coexist so
# packs can pick whichever convention fits their progression vibe. Generic
# enough for cross-pack reuse — non-magical packs (low_fantasy, pulp_noir)
# can adopt the named convention without changes here.
_NAMED_TIER_KEYS = frozenset(
    (
        "tier_0",
        "tier_1",
        "tier_2",
        "tier_3",
        "novice",
        "journeyman",
        "expert",
        "master",
    )
)


class Affinity(BaseModel):
    """A skill/affinity tree.

    ``sub_paths`` is elemental_harmony-specific authored content describing
    branching specializations within an affinity. Rust silently dropped it;
    accepted here as pass-through until a consumer wires it.
    """

    model_config = {"extra": "forbid"}

    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    tier_thresholds: list[int] = Field(default_factory=list)
    unlocks: AffinityUnlocks | None = None
    sub_paths: list[dict[str, Any]] = Field(default_factory=list)


class ItemEvolution(BaseModel):
    """Item evolution thresholds.

    Two authored naming conventions exist:
    - mutant_wasteland: ``naming_threshold`` / ``power_up_threshold`` (float 0–1)
    - elemental_harmony: ``name_threshold`` / ``power_bump_threshold`` (int count)

    Accept either via aliases. Values are stored as float for a common type;
    int thresholds widen without loss.
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    naming_threshold: float = Field(
        default=0.0,
        validation_alias=AliasChoices("naming_threshold", "name_threshold"),
    )
    power_up_threshold: float = Field(
        default=0.0,
        validation_alias=AliasChoices("power_up_threshold", "power_bump_threshold"),
    )


class LevelBonuses(BaseModel):
    """Per-level bonuses. Two authored shapes exist in content:

    - Fixed struct (mutant_wasteland): ``{stat_points: 1, hp_bonus: "class_based"}``
    - Per-level narrative dict (space_opera, elemental_harmony):
      ``{"1": "Starting crew…", "2": "…", …}``

    Rust's struct silently discarded the dict form; the port accepts both and
    exposes the per-level narrative via ``per_level_notes``. Consumers choose
    which form they read based on whether a genre authored fixed bonuses or
    per-level flavor.
    """

    model_config = {"extra": "forbid"}

    stat_points: int = 0
    hp_bonus: str = ""
    per_level_notes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_dict_form(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        keys = list(data.keys())
        if not keys:
            return data
        # Dict-form: every key is a level number (e.g., "1", "2", 3). Convert
        # to the struct-form by moving the dict under per_level_notes.
        if all(isinstance(k, (int, str)) and str(k).lstrip("-").isdigit() for k in keys):
            return {"per_level_notes": {str(k): v for k, v in data.items()}}
        return data


class WealthTier(BaseModel):
    """A wealth tier with optional currency cap.

    The currency is genre-specific — ``max_gold`` for medieval packs,
    ``max_credits`` for sci-fi. Accept either alias into the same field.
    ``description`` is authored flavor (space_opera) that Rust dropped.
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    max_gold: int | None = Field(
        default=None,
        validation_alias=AliasChoices("max_gold", "max_credits"),
    )
    label: str
    description: str = ""


class ProgressionConfig(BaseModel):
    """Character progression configuration.

    ``synergies`` is elemental_harmony-specific — combinations of affinities
    that produce emergent effects. Rust silently dropped it; accepted here
    as pass-through until a consumer wires it.
    """

    model_config = {"extra": "forbid"}

    affinities: list[Affinity] = Field(default_factory=list)
    milestone_categories: list[str] = Field(default_factory=list)
    milestones_per_level: int = 0
    max_level: int = 0
    item_evolution: ItemEvolution | None = None
    level_bonuses: LevelBonuses | None = None
    wealth_tiers: list[WealthTier] = Field(default_factory=list)
    synergies: list[dict[str, Any]] = Field(default_factory=list)
