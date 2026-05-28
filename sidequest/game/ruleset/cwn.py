"""CwnRulesetModule — Cities Without Number resolution behind the seam.

CWN (Sine Nomine, CC0) shares SWN's resolution engine, so this subclasses
SwnRulesetModule and inherits attack/skill/save/initiative/damage verbatim.
The only core divergence is the CWN Luck saving throw: target = save_base -
(level - 1), unmodified by any attribute. NOT a fallback — selected explicitly
by `ruleset: cwn`.
"""

from __future__ import annotations

import random

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.lethality import LethalityResult
from sidequest.game.ruleset.resolution import CheckRollParams
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.game.system_strain import StrainResult
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import CwnConfig, SwnConfig
from sidequest.telemetry.spans.cwn import cwn_shock_applied_span, cwn_system_strain_delta_span, cwn_trauma_roll_span


class CwnRulesetModule(SwnRulesetModule):
    slug = "cwn"

    def save_params(self, *, stats, save, level, label, cfg) -> CheckRollParams:
        """CWN saves: three attribute saves inherited from SWN, plus Luck (no attribute)."""
        if save == "luck":
            return CheckRollParams(
                sides=20,
                count=1,
                modifier=0,
                difficulty=int(cfg.save_base) - (int(level) - 1),
                label=label,
            )
        return super().save_params(stats=stats, save=save, level=level, label=label, cfg=cfg)

    def resolve_shock(
        self,
        *,
        spec: DamageSpec,
        target_melee_ac: int,
        actor: str = "",
        _tracer: "trace.Tracer | None" = None,
    ) -> int:
        """CWN Shock: a melee weapon with shock>0 chips `shock` damage on a MISS
        when the target's Melee AC <= the weapon's Shock rating. v1 models the
        chip amount and the AC ceiling as the same content number (spec.shock).
        Returns the chip damage (0 when not applicable). Emits cwn.shock.applied
        only when damage is actually chipped."""
        if spec.shock <= 0 or target_melee_ac > spec.shock:
            return 0
        cwn_shock_applied_span(
            actor=actor, amount=spec.shock, melee_ac=target_melee_ac,
            shock_rating=spec.shock, _tracer=_tracer,
        )
        return spec.shock

    def resolve_trauma(
        self,
        *,
        spec: DamageSpec,
        base_total: int,
        cfg: SwnConfig | None,
        rng: random.Random,
        actor: str = "",
        _tracer: "trace.Tracer | None" = None,
    ) -> LethalityResult:
        """CWN Trauma: if the weapon has a Trauma Die, roll it; on a result that
        meets/exceeds the Trauma Target, multiply total damage by trauma_rating.

        Trauma Target = the weapon's override (spec.trauma_target) if set, else
        the genre default (cfg.trauma.default_trauma_target, 6 for unarmored).
        Emits cwn.trauma.roll on every CWN strike that has a trauma_die (the GM
        lie-detector sees both traumatic and non-traumatic rolls)."""
        if spec.trauma_die is None:
            return LethalityResult(
                base_total=base_total, final_total=base_total,
                traumatic=False, trauma_roll=0, trauma_target=0,
            )
        if not isinstance(cfg, CwnConfig):
            raise ValueError(
                f"resolve_trauma requires a CwnConfig; got {type(cfg).__name__!r}"
            )
        target = spec.trauma_target if spec.trauma_target is not None else cfg.trauma.default_trauma_target
        trauma_roll = DamageSpec(dice=spec.trauma_die).roll(rng)  # sum of the trauma dice (usually 1 die)
        traumatic = trauma_roll >= target
        final = base_total * spec.trauma_rating if traumatic else base_total
        cwn_trauma_roll_span(
            actor=actor, weapon_die=spec.trauma_die, roll=trauma_roll, target=target,
            traumatic=traumatic, rating=spec.trauma_rating, base=base_total, final=final,
            _tracer=_tracer,
        )
        return LethalityResult(
            base_total=base_total, final_total=final,
            traumatic=traumatic, trauma_roll=trauma_roll, trauma_target=target,
        )

    def apply_system_strain(
        self,
        *,
        core: CreatureCore,
        kind: str,
        amount: int,
        source: str,
        cfg: SwnConfig | None,
        _tracer: trace.Tracer | None = None,
    ) -> StrainResult:
        """Apply a System Strain change under CWN rules; emit cwn.system_strain.delta.

        kind: "temporary" | "first_aid" | "permanent" | "rest".
        Over-max temporary/permanent-install adds are REFUSED (applied=False, no change).
        Rest recovers toward (never below) the permanent floor. Fails loud if the
        character has no strain pool or kind is unknown.
        """
        pool = core.system_strain
        if pool is None:
            raise ValueError(
                f"{core.name!r} has no system_strain pool; cwn characters must seed one at chargen"
            )
        if not isinstance(cfg, CwnConfig):
            raise ValueError(
                f"apply_system_strain requires a CwnConfig; got {type(cfg).__name__!r}"
            )
        scfg = cfg.system_strain

        before = pool.current
        applied = True
        reason = ""
        requested: int = 0

        if kind == "first_aid":
            requested = scfg.first_aid_cost
            new_current = pool.current + requested
            if new_current > pool.max:
                applied = False
                reason = f"would exceed max ({pool.max})"
                new_current = pool.current
        elif kind == "temporary":
            requested = amount
            new_current = pool.current + amount
            if new_current > pool.max:
                applied = False
                reason = f"would exceed max ({pool.max})"
                new_current = pool.current
        elif kind == "permanent":
            requested = amount
            new_perm = max(0, pool.permanent + amount)
            new_current = pool.current + amount
            if amount > 0 and new_current > pool.max:
                applied = False
                reason = f"would exceed max ({pool.max})"
                new_current = pool.current
                new_perm = pool.permanent
            else:
                new_current = max(new_perm, new_current)
            if applied:
                pool.permanent = new_perm
        elif kind == "rest":
            requested = -scfg.rest_recovery_per_night * max(0, amount)
            new_current = max(pool.permanent, pool.current + requested)
        else:
            raise ValueError(f"unknown system_strain kind {kind!r}")

        if applied:
            pool.current = new_current
        delta = pool.current - before

        cwn_system_strain_delta_span(
            actor=core.name,
            source=source,
            amount=requested,
            new_total=pool.current,
            max=pool.max,
            applied=applied,
            _tracer=_tracer,
        )
        return StrainResult(
            applied=applied,
            current=pool.current,
            max=pool.max,
            permanent=pool.permanent,
            delta=delta,
            reason=reason,
        )
