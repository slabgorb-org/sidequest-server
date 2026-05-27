"""Dogfight SWN shot resolution — pure positioning->resolution layer.

The maneuver cross-product (sealed-letter cells) sets each pilot's geometry in
``per_actor_state``. This module turns geometry into a to-hit modifier, and
resolves SWN shots. No I/O — dice values are injected by the caller, so every
branch is unit-testable.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sidequest.game.hp_depletion import HpDepletionResult, check_hp_depletion
from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import GeometryModifiers
from sidequest.telemetry.spans.dogfight import (
    dogfight_shot_attempted_span,
    dogfight_shot_damage_span,
)


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


# ---------------------------------------------------------------------------
# Shot resolution
# ---------------------------------------------------------------------------


def _roll_damage_dice(spec: DamageSpec) -> int:
    """Roll a DamageSpec to an int. Single seam so tests monkeypatch one place."""
    return spec.roll(random.Random())


@dataclass(frozen=True)
class ShotResult:
    shooter_role: str
    shooter_name: str
    target_name: str
    d20_total: int
    target_ac: int
    hit: bool
    applied: int
    source: str  # player | npc


@dataclass(frozen=True)
class DogfightShotResolution:
    shots: list[ShotResult] = field(default_factory=list)
    depletion: HpDepletionResult | None = None


def resolve_dogfight_shots(
    *,
    encounter: Any,
    gun_solutions: list[GunSolution],
    d20_by_shooter: dict[str, int],
    edge_resolver: Callable[[str], Any | None],
) -> DogfightShotResolution:
    """Resolve every gun solution against PRE-shot HP, then run depletion.

    Two passes: pass 1 computes hit + damage for every shot (no HP mutation);
    pass 2 ablates HP and emits the damage span. This guarantees mutual
    solutions resolve against the same starting HP (mutual_destruction
    reachable). Fails loud (ValueError) on a gun solution with no injected d20,
    no resolvable target core, or no encounter actor seated for the shooter
    role — never a silent no-op.

    ``source`` ("player"|"npc") is DERIVED from the shooter's encounter actor
    ``side`` (not a caller-passed map), so the span source can never disagree
    with who is actually seated in the confrontation.
    """
    results: list[ShotResult] = []
    computed: list[tuple[GunSolution, ShotResult, int, Any]] = []

    # ---- pass 1: compute hits + damage, NO HP mutation ----
    for gs in gun_solutions:
        if gs.shooter_role not in d20_by_shooter:
            raise ValueError(
                f"gun solution for role={gs.shooter_role!r} has no d20 in "
                f"d20_by_shooter {sorted(d20_by_shooter)} — no silent skip"
            )
        target_core = edge_resolver(gs.target_name)
        if target_core is None:
            raise ValueError(
                f"gun solution target {gs.target_name!r} has no creature core "
                "(opponent not seated with stats) — fail loud per spec"
            )
        shooter_actor = next((a for a in encounter.actors if a.role == gs.shooter_role), None)
        if shooter_actor is None:
            raise ValueError(
                f"gun solution shooter role={gs.shooter_role!r} has no encounter "
                "actor seated — cannot derive source; fail loud per spec"
            )
        source = "player" if shooter_actor.side == "player" else "npc"
        d20 = int(d20_by_shooter[gs.shooter_role])
        total = d20 + gs.attack.modifier
        hit = total >= gs.attack.target_number
        with dogfight_shot_attempted_span(
            shooter=gs.shooter_name,
            target=gs.target_name,
            d20_total=total,
            target_ac=gs.attack.target_number,
            hit=hit,
            geometry_modifier=gs.geometry_modifier,
            source=source,
        ):
            pass
        applied = 0
        eff_armor = gs.target_armor
        if hit:
            raw = _roll_damage_dice(gs.weapon)
            eff_armor = effective_armor_after_ap(
                armor=gs.target_armor, armor_piercing=gs.weapon.armor_piercing
            )
            applied = max(0, raw - eff_armor)
        res = ShotResult(
            shooter_role=gs.shooter_role,
            shooter_name=gs.shooter_name,
            target_name=gs.target_name,
            d20_total=total,
            target_ac=gs.attack.target_number,
            hit=hit,
            applied=applied,
            source=source,
        )
        results.append(res)
        computed.append((gs, res, eff_armor, target_core))

    # ---- pass 2: ablate HP + emit damage span (target_hp_after is post-ablation) ----
    for gs, res, eff_armor, target_core in computed:
        if res.applied > 0:
            target_core.apply_hp_delta(-res.applied)
        if res.hit:
            with dogfight_shot_damage_span(
                shooter=res.shooter_name,
                target=res.target_name,
                dice=gs.weapon.dice,
                armor_piercing=gs.weapon.armor_piercing,
                armor_negated=gs.target_armor - eff_armor,
                applied=res.applied,
                target_hp_after=target_core.hp.current,
            ):
                pass

    depletion = check_hp_depletion(encounter, edge_resolver)
    return DogfightShotResolution(shots=results, depletion=depletion)
