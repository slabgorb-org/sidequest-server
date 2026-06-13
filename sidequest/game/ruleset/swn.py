"""SwnRulesetModule — faithful Stars Without Number resolution behind the seam.

Attacks: d20 + attack_bonus + combat_skill + attribute_mod vs target Armor Class.
Skill checks / saves: later task (non-beat dice path).
Universal SWN constants come from RulesConfig.swn; per-class/per-item numbers
(attack bonus progression, weapon dice, armor AC) come from pack content.
NOT a fallback — selected explicitly by `ruleset: swn`.
"""

from __future__ import annotations

import random

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.resolution import (
    AttackRollParams,
    JumpAdjudication,
)
from sidequest.game.ruleset.without_number import (
    WithoutNumberRulesetModule,
    _stat,
    swn_attribute_modifier,
)
from sidequest.game.wwn_magic import DisciplineActivationResult
from sidequest.genre.models.psionics import PsionicDiscipline
from sidequest.genre.models.rules import SwnConfig
from sidequest.genre.models.world import Route
from sidequest.telemetry.spans.psionics import discipline_activated_span

# ``swn_attribute_modifier`` / ``_stat`` are the WN-family attribute helpers,
# now homed on ``WithoutNumberRulesetModule`` (ADR-142). Re-exported here so the
# established ``from sidequest.game.ruleset.swn import swn_attribute_modifier``
# import surface (builder.py + chargen tests) keeps resolving unchanged.
__all__ = ["PSIONIC_EFFORT_SOURCE", "SwnRulesetModule", "_stat", "swn_attribute_modifier"]

# Source key for the SWN psionic Effort pool. SWN psionics draw every discipline
# from ONE Effort pool (SRD §6), so the pool keys ``core.effort`` under this slug.
PSIONIC_EFFORT_SOURCE = "psionic"

# SWN spike-drive jump model (SRD Revised, Sine Nomine 2017, "Spike Drives" p.211):
# a spike drill crosses up to ``rating`` hexes and takes roughly six days of
# subjective transit regardless of distance, burning one fuel load per jump.
SPIKE_TRANSIT_DAYS = 6  # subjective days per spike drill (one jump)
SPIKE_FUEL_PER_JUMP = 1  # one fuel load consumed per jump
# A drive under a route's authored minimum makes the jump under strain: it still
# crosses (a bare adjacency is navigable), but burns an extra fuel load. Below-min
# is a mechanical cost, not a block — No Silent Fallbacks, and the field gets teeth.
UNDERRATED_DRIVE_FUEL_PENALTY = 1


class SwnRulesetModule(WithoutNumberRulesetModule):
    slug = "swn"

    def ship_attack_params(
        self, *, attacker_stats, pilot_skill, attack_bonus, geometry_modifier, target_ac, cfg
    ) -> AttackRollParams:
        """SWN strike-craft gunnery: d20 + attack_bonus + pilot_skill +
        better-of(INT,DEX) mod + geometry_modifier vs target fighter AC.
        Pilot stands in for Shoot on a fighter-class ship (SRD ship combat)."""
        amap = cfg.attribute_map
        flavor_attrs = []
        for swn_attr in ("DEXTERITY", "INTELLIGENCE"):
            flavor = amap.get(swn_attr)
            if flavor is None:
                raise KeyError(
                    f"attribute_map missing {swn_attr!r} for ship gunnery "
                    "(RulesConfig validator should have caught this)"
                )
            flavor_attrs.append(flavor)
        best_mod = max(self.stat_modifier(attacker_stats, f) for f in flavor_attrs)
        return AttackRollParams(
            modifier=int(attack_bonus) + int(pilot_skill) + best_mod + int(geometry_modifier),
            target_number=int(target_ac),
        )

    def adjudicate_jump(
        self,
        *,
        route: Route | None,
        drive_rating: int,
        rng: random.Random,
    ) -> JumpAdjudication:
        """SWN spike-drive inter-system jump (Story 98-5, ADR-141 campaign scale).

        With an authored ``routes`` entry, the cost reflects its fields
        (``jump_fuel`` / ``transit_days`` / ``hazard``), falling back per-field to
        the spike-drive default when a field is unauthored. A route's
        ``drive_rating_min`` gates the jump: a ship below it makes the crossing
        under strain (one extra fuel load), never a block — a bare adjacency is
        always navigable.

        With no route (``None``), the spike-drive model computes an EXPLICIT
        default — one fuel load, ~six days, no narrative hazard — labelled
        ``ruleset_default`` so the caller emits the default-cost span (No Silent
        Fallbacks: a named computation, never a swallowed zero)."""
        hazard_roll = rng.randint(1, 6)  # d6 hazard check, recorded on the span every jump
        if route is None:
            return JumpAdjudication(
                fuel_spent=SPIKE_FUEL_PER_JUMP,
                transit_days=SPIKE_TRANSIT_DAYS,
                hazard=None,
                hazard_roll=hazard_roll,
                source="ruleset_default",
            )
        fuel_spent = route.jump_fuel if route.jump_fuel is not None else SPIKE_FUEL_PER_JUMP
        transit_days = route.transit_days if route.transit_days is not None else SPIKE_TRANSIT_DAYS
        if route.drive_rating_min is not None and drive_rating < route.drive_rating_min:
            # Underrated drive: strained jump costs an extra fuel load (not a block).
            fuel_spent += UNDERRATED_DRIVE_FUEL_PENALTY
        return JumpAdjudication(
            fuel_spent=fuel_spent,
            transit_days=transit_days,
            hazard=route.hazard,
            hazard_roll=hazard_roll,
            source="route",
        )

    # ------------------------------------------------------------------
    # Psionic discipline activation (SWN SRD §6) — the cast-spine mirror.
    # ------------------------------------------------------------------

    def activate_discipline(
        self,
        *,
        core: CreatureCore,
        discipline: PsionicDiscipline,
        source: str = PSIONIC_EFFORT_SOURCE,
        cfg: SwnConfig | None = None,
        _tracer: trace.Tracer | None = None,
    ) -> DisciplineActivationResult:
        """Activate a psionic discipline: commit its ``effort_cost`` from the
        psychic's Effort pool and, on a push (``strain_cost`` > 0), route the
        System Strain through the SAME ``core.system_strain`` counter the
        lethality seam uses (AC3 — no forked strain field).

        Zero free Effort → REFUSED loudly (``applied=False``, pool unchanged) —
        never a silent success. Emits ``{slug}.discipline.activated`` on EVERY
        call (``refused`` reflects the outcome), plus ``{slug}.effort.commit``
        and (on a push) ``{slug}.system_strain.delta`` when applied. A missing
        Effort pool raises ValueError (No Silent Fallbacks).

        A ``strain_cost`` discipline requires a seeded ``core.system_strain``
        pool (only the strain-bearing rulesets — WWN/CWN/AWN — carry one; SWN
        psionics is Effort-only). This precondition is checked BEFORE any Effort
        is committed: a strain push on a strainless core is REFUSED loudly
        (``applied=False``, pool unchanged), never a partial Effort spend and
        never an opaque ``AttributeError`` from the missing strain engine."""
        pool = core.effort.get(source)
        if pool is None:
            raise ValueError(
                f"{core.name!r} has no {source!r} Effort pool; a psychic seeds one at chargen"
            )
        cost = int(discipline.effort_cost)
        strain_cost = int(discipline.strain_cost or 0)

        # Precondition FIRST, before any mutation: a push needs a strain pool.
        # Checked here so a content/config mismatch (a strain discipline on a
        # strainless ruleset) is a clean loud refusal, not a half-committed
        # Effort spend that then AttributeErrors on the absent strain engine.
        if strain_cost > 0 and core.system_strain is None:
            reason = (
                f"{discipline.id!r} costs {strain_cost} System Strain but "
                f"{core.name!r} has no System Strain pool — this ruleset has no "
                "Strain engine (SWN psionics is Effort-only); author the strain "
                "discipline on a WWN/CWN/AWN pack"
            )
            discipline_activated_span(
                ruleset=self.slug,
                actor=core.name,
                discipline_id=discipline.id,
                refused=True,
                _tracer=_tracer,
            )
            return DisciplineActivationResult(
                applied=False,
                discipline_id=discipline.id,
                available=pool.available,
                strained=0,
                reason=reason,
            )

        applied = cost <= pool.available
        strained = 0
        if applied:
            self.commit_effort(
                core=core,
                source=source,
                points=cost,
                duration=discipline.duration,
                label=discipline.name,
                _tracer=_tracer,
            )
            if strain_cost > 0:
                # Precondition above guarantees core.system_strain is seeded here,
                # which only the strain-bearing rulesets (WWN/CWN/AWN) do — and
                # those define apply_system_strain. Safe to route the push.
                self.apply_system_strain(
                    core=core,
                    kind="temporary",
                    amount=strain_cost,
                    source=f"psionic:{discipline.id}",
                    cfg=cfg,
                    _tracer=_tracer,
                )
                strained = strain_cost
        discipline_activated_span(
            ruleset=self.slug,
            actor=core.name,
            discipline_id=discipline.id,
            refused=not applied,
            _tracer=_tracer,
        )
        return DisciplineActivationResult(
            applied=applied,
            discipline_id=discipline.id,
            available=pool.available,
            strained=strained,
            reason="" if applied else f"only {pool.available} of {cost} Effort available",
        )
