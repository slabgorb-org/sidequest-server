"""CwnRulesetModule — Cities Without Number resolution behind the seam.

CWN (Sine Nomine, CC0) shares SWN's resolution engine, so this subclasses
SwnRulesetModule and inherits attack/skill/save/initiative/damage verbatim.
The only core divergence is the CWN Luck saving throw: target = save_base -
(level - 1), unmodified by any attribute. NOT a fallback — selected explicitly
by `ruleset: cwn`.
"""

from __future__ import annotations

from typing import cast

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.resolution import CheckRollParams
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.game.system_strain import StrainResult
from sidequest.genre.models.rules import CwnConfig, SwnConfig
from sidequest.telemetry.spans.cwn import cwn_system_strain_delta_span


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
        scfg = cast(CwnConfig, cfg).system_strain

        before = pool.current
        applied = True
        reason = ""
        requested: int

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
