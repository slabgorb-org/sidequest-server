"""CreatureCore — shared fields for Character and NPC.

Story 1-13: Extracted from Character and NPC via composition.

EdgePool is the composure currency (Epic 39). Replaces the old
hp/max_hp/ac fields. Stories 39-1 through 39-6 tune thresholds, recovery
triggers, and per-class base_max values.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.game.rig_composure_pool import RigComposurePool
from sidequest.game.status import Status, migrate_legacy_statuses

# Default placeholder base_max for EdgePool when per-class YAML isn't wired (story 39-3).
PLACEHOLDER_EDGE_BASE_MAX: int = 10


class RecoveryTrigger(str):
    """Recovery trigger values for EdgePool.

    Re-exported here from the genre layer. Using ``str`` constants rather
    than ``Enum`` to avoid import coupling.
    """

    OnResolution = "OnResolution"
    OnRest = "OnRest"
    OnSceneChange = "OnSceneChange"
    OnYield = "OnYield"


class EdgeThreshold(BaseModel):
    """A downward threshold on an EdgePool.

    P1-required: thresholds appear in JSON; must round-trip.
    """

    model_config = {"extra": "forbid"}

    at: int
    event_id: str
    narrator_hint: str


class EdgePool(BaseModel):
    """First-class composure pool (Epic 39, story 39-1).

    Replaces legacy hp/max_hp fields. ``current`` is clamped to
    ``[0, max]``.

    P1-required: narrator uses edge/max_edge for health-state framing.
    P2-deferred: recovery_triggers / thresholds (advancement/combat systems).
    """

    model_config = {"extra": "forbid"}

    current: int
    max: int
    base_max: int
    # P2-deferred: recovery trigger wiring (story 39-4/5/6 — combat/advancement)
    recovery_triggers: list[str] = Field(default_factory=list)
    # P2-deferred: threshold event emission (story 39-6 — advancement effects)
    thresholds: list[EdgeThreshold] = Field(default_factory=list)

    def apply_delta(self, delta: int) -> int:
        """Apply a composure delta. Returns new current value.

        Positive delta increases current (capped at max).
        Negative delta decreases current (floored at 0).
        """
        raw = self.current + delta
        self.current = max(0, min(self.max, raw))
        return self.current


def placeholder_edge_pool() -> EdgePool:
    """Build the default EdgePool used in constructors without YAML tuning."""
    return EdgePool(
        current=PLACEHOLDER_EDGE_BASE_MAX,
        max=PLACEHOLDER_EDGE_BASE_MAX,
        base_max=PLACEHOLDER_EDGE_BASE_MAX,
        recovery_triggers=[RecoveryTrigger.OnResolution],
        thresholds=[],
    )


def creature_edge_pool_from_hp(hp: int) -> EdgePool:
    """Translate a B/X-shaped creature HP value into an :class:`EdgePool`.

    Per ADR-078, runtime entities carry an ``EdgePool`` instead of raw HP.
    ``creatures.yaml`` is authored against the B/X content schema (an ``hp``
    integer per creature, e.g. ``1`` for a chalk_moth, ``30`` for a Patient
    Butcher), so the Monster Manual seeder ships the HP as-authored and the
    materializer translates it here. The pool is seeded full
    (``current == max == base_max``) with the same ``OnResolution`` recovery
    trigger ``placeholder_edge_pool`` uses; thresholds stay empty pending
    advancement-side wiring (ADR-081 deferred).

    Clamped at 1 because EdgePool requires a positive ceiling — a creature
    authored with ``hp: 0`` would otherwise be unrepresentable as a
    materialized actor.

    This is the SINGLE canonical HP→EdgePool translator. It was promoted
    from ``session._creature_edge_pool_from_hp`` (Beneath Sünden Plan 7
    Task 4) so the NPC-patch path AND the dungeon materializer's CR→Edge
    seam share one implementation. ``session._creature_edge_pool_from_hp``
    is now a thin back-compat re-export of this symbol — do NOT reintroduce
    a second copy of this body.
    """
    seed = max(1, hp)
    return EdgePool(
        current=seed,
        max=seed,
        base_max=seed,
        recovery_triggers=[RecoveryTrigger.OnResolution],
        thresholds=[],
    )


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


class EdgeConfigMissingClassError(KeyError):
    """Genre pack declared `edge_config` but omitted a `base_max_by_class`
    entry for the character's class.

    Raised loudly by ``edge_pool_from_config`` (Story 39-3) — silently
    reverting to the placeholder pool would hide content bugs. SOUL.md:
    fail loud at the boundary.

    Subclasses ``KeyError`` because this is conceptually a dict lookup
    miss; callers can still catch it as ``KeyError``.
    """

    def __init__(self, class_name: str) -> None:
        self.class_name = class_name
        super().__init__(f"edge_config.base_max_by_class missing entry for class '{class_name}'")


def edge_pool_from_config(edge_config: object, class_name: str, *, con_score: int) -> EdgePool:
    """Build a genre-authored EdgePool from an EdgeConfig and a class name.

    Resolves base_max from edge_config.base_max_by_class[class] (raises
    EdgeConfigMissingClassError when absent), applies a CON modifier
    ((con_score - 10) // 2) floored at 1, converts every
    EdgeThresholdDecl to an EdgeThreshold, and seeds recovery_triggers
    with OnResolution. The crossing-direction tag from YAML is
    informational — all EdgePool thresholds fire on downward crossings
    by construction.

    CON modifier (ADR-078 amendment 2026-05-10, story 39-10): retires the
    Story 39-4 hardcoded Fighter +2 stub and makes CON the universal
    Edge-seed modifier across all classes. A character is alive, so a
    Constitution that would zero out the pool is clamped to 1.

    The `edge_config` parameter is typed as `object` to avoid a circular
    import with sidequest.genre.models.rules (which imports from the
    genre layer). Duck-typing suffices: we rely on `base_max_by_class`
    and `thresholds` attributes, both typed in the genre EdgeConfig
    model.
    """
    base_max_by_class = getattr(edge_config, "base_max_by_class", {})
    if class_name not in base_max_by_class:
        raise EdgeConfigMissingClassError(class_name=class_name)
    class_base = base_max_by_class[class_name]
    con_modifier = (con_score - 10) // 2
    base_max = max(1, class_base + con_modifier)

    thresholds: list[EdgeThreshold] = []
    for decl in getattr(edge_config, "thresholds", []):
        thresholds.append(
            EdgeThreshold(
                at=decl.at,
                event_id=decl.event_id,
                narrator_hint=decl.narrator_hint,
            )
        )

    return EdgePool(
        current=base_max,
        max=base_max,
        base_max=base_max,
        recovery_triggers=[RecoveryTrigger.OnResolution],
        thresholds=thresholds,
    )


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
