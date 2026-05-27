"""Dogfight SWN shot resolution — pure positioning->resolution layer.

The maneuver cross-product (sealed-letter cells) sets each pilot's geometry in
``per_actor_state``. This module turns geometry into a to-hit modifier, and
(in later tasks) resolves SWN shots. No I/O — dice values are injected by the
caller, so every branch is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import GeometryModifiers


@dataclass
class GunSolution:
    """A pilot who got a shot this cell + the SWN params to resolve it.

    Pure data produced by the sealed-letter resolver; consumed by
    resolve_dogfight_shots (next task). No dice rolled yet."""

    shooter_role: str
    shooter_name: str
    target_role: str
    target_name: str
    attack: AttackRollParams
    weapon: DamageSpec
    weapon_name: str
    target_armor: int
    geometry_modifier: int


def resolve_geometry_modifier(per_actor_state: dict, geometry_modifiers: GeometryModifiers) -> int:
    """Sum the authored aspect + range modifiers for a shooter's current geometry.

    A ``target_aspect``/``target_range`` value that is absent from the runtime
    ``per_actor_state`` (or not present in the authored table) contributes 0 —
    geometry the engine hasn't seated yet is neutral, not an error. This is NOT
    a way to silently swallow authoring typos: a misspelled key in the authored
    ``GeometryModifiers`` table is rejected at pack-load time by pydantic
    ``extra="forbid"``, so by the time we read it here the table is trusted.
    """
    aspect = per_actor_state.get("target_aspect")
    rng = per_actor_state.get("target_range")
    mod = 0
    if aspect is not None:
        mod += geometry_modifiers.aspect.get(aspect, 0)
    if rng is not None:
        mod += geometry_modifiers.range.get(rng, 0)
    return mod


def effective_armor_after_ap(*, armor: int, armor_piercing: int) -> int:
    """SWN AP rule: AP reduces effective Armor before damage subtraction."""
    return max(0, armor - armor_piercing)
