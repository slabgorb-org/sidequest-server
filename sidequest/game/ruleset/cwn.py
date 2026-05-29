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
from sidequest.game.lethality import DownedResult, LethalityResult, major_injury_entry
from sidequest.game.ruleset.resolution import CheckRollParams
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.system_strain import StrainResult
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import CwnConfig, SwnConfig
from sidequest.telemetry.spans.cwn import (
    cwn_hacking_security_check_span,
    cwn_major_injury_roll_span,
    cwn_mortal_injury_declared_span,
    cwn_shock_applied_span,
    cwn_system_strain_delta_span,
    cwn_trauma_roll_span,
)


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
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """CWN Shock ("Shock X/AC Y"): a melee weapon with shock>0 chips `shock`
        (the chip amount X) damage on a MISS when the target's Melee AC <=
        `spec.shock_ac` (the AC ceiling Y). The chip amount and the AC ceiling
        are two distinct content numbers. Returns the chip damage (0 when not
        applicable). Emits cwn.shock.applied only when damage is actually
        chipped."""
        if spec.shock <= 0 or spec.shock_ac is None or target_melee_ac > spec.shock_ac:
            return 0
        cwn_shock_applied_span(
            actor=actor,
            amount=spec.shock,
            melee_ac=target_melee_ac,
            shock_rating=spec.shock,
            shock_ac=spec.shock_ac,
            _tracer=_tracer,
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
        _tracer: trace.Tracer | None = None,
    ) -> LethalityResult:
        """CWN Trauma: if the weapon has a Trauma Die, roll it; on a result that
        meets/exceeds the Trauma Target, multiply total damage by trauma_rating.

        Trauma Target = the weapon's override (spec.trauma_target) if set, else
        the genre default (cfg.trauma.default_trauma_target, 6 for unarmored).
        Emits cwn.trauma.roll on every CWN strike that has a trauma_die (the GM
        lie-detector sees both traumatic and non-traumatic rolls)."""
        if spec.trauma_die is None:
            return LethalityResult(
                base_total=base_total,
                final_total=base_total,
                traumatic=False,
                trauma_roll=0,
                trauma_target=0,
            )
        if not isinstance(cfg, CwnConfig):
            raise ValueError(f"resolve_trauma requires a CwnConfig; got {type(cfg).__name__!r}")
        target = (
            spec.trauma_target
            if spec.trauma_target is not None
            else cfg.trauma.default_trauma_target
        )
        trauma_roll = DamageSpec(dice=spec.trauma_die).roll(
            rng
        )  # sum of the trauma dice (usually 1 die)
        traumatic = trauma_roll >= target
        final = base_total * spec.trauma_rating if traumatic else base_total
        cwn_trauma_roll_span(
            actor=actor,
            weapon_die=spec.trauma_die,
            roll=trauma_roll,
            target=target,
            traumatic=traumatic,
            rating=spec.trauma_rating,
            base=base_total,
            final=final,
            _tracer=_tracer,
        )
        return LethalityResult(
            base_total=base_total,
            final_total=final,
            traumatic=traumatic,
            trauma_roll=trauma_roll,
            trauma_target=target,
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

    def resolve_downed(
        self,
        *,
        core: CreatureCore,
        save_target: int,
        scene_traumatic: bool,
        cfg: SwnConfig | None,
        rng: random.Random,
        _tracer: trace.Tracer | None = None,
    ) -> DownedResult:
        """Resolve a CWN character dropped to 0 HP.

        Always declares a Mortal Injury (Scar status; the character dies at the
        end of cfg.trauma.mortal_injury_rounds unless stabilized via the
        stabilize_mortal_injury tool). If a Traumatic Hit landed this scene,
        additionally rolls a Physical save (1d20 vs save_target); on failure,
        rolls 1d12 on the Major Injury table and attaches that as a second Scar.
        Emits cwn.mortal_injury.declared and (when rolled) cwn.major_injury.roll."""
        if not isinstance(cfg, CwnConfig):
            raise ValueError(f"resolve_downed requires a CwnConfig; got {type(cfg).__name__!r}")
        rounds = cfg.trauma.mortal_injury_rounds
        core.statuses.append(
            Status(
                text=f"Mortal Injury — dies in {rounds} rounds unless stabilized",
                severity=StatusSeverity.Scar,
            )
        )
        cwn_mortal_injury_declared_span(actor=core.name, rounds_to_die=rounds, _tracer=_tracer)

        major = False
        major_roll = 0
        major_text = ""
        save_made = True
        if scene_traumatic:
            save_roll = rng.randint(1, 20)
            save_made = save_roll >= save_target
            if not save_made:
                major = True
                major_roll = rng.randint(1, 12)
                major_text = major_injury_entry(major_roll)
                core.statuses.append(
                    Status(text=f"Major Injury — {major_text}", severity=StatusSeverity.Scar)
                )
            cwn_major_injury_roll_span(
                actor=core.name,
                save_made=save_made,
                roll=major_roll,
                text=major_text,
                _tracer=_tracer,
            )

        return DownedResult(
            mortal=True,
            major=major,
            major_roll=major_roll,
            major_text=major_text,
            save_made=save_made,
        )

    def resolve_hacking(
        self,
        *,
        verb: str,
        tier: str,
        base_dc: int,
        alert_modifier: int,
        outcome: str,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Record a CWN cyberspace security check; return the effective DC.

        effective_dc = base_dc + alert_modifier (the CWN situational modifier:
        each network-alert escalation adds +1). Emits cwn.hacking.security_check
        — the GM lie-detector for the hacking subsystem; fires on EVERY net_run
        verb so the panel sees engaged + unengaged rolls alike. Does NOT mutate
        metrics or roll dice — the net_run dispatch seam builds the 2d6 check
        whose difficulty is this returned DC, the dice lib resolves the throw,
        and the confrontation engine applies the beat's tier deltas. Thin
        record-and-compute, consistent with resolve_shock/resolve_trauma."""
        effective_dc = int(base_dc) + int(alert_modifier)
        cwn_hacking_security_check_span(
            actor=actor,
            verb=verb,
            tier=tier,
            base_dc=int(base_dc),
            alert_modifier=int(alert_modifier),
            effective_dc=effective_dc,
            result=str(outcome),
            _tracer=_tracer,
        )
        return effective_dc
