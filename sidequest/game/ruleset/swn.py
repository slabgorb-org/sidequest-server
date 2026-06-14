"""SwnRulesetModule — faithful Stars Without Number resolution behind the seam.

Attacks: d20 + attack_bonus + combat_skill + attribute_mod vs target Armor Class.
Skill checks / saves: later task (non-beat dice path).
Universal SWN constants come from RulesConfig.swn; per-class/per-item numbers
(attack bonus progression, weapon dice, armor AC) come from pack content.
NOT a fallback — selected explicitly by `ruleset: swn`.
"""

from __future__ import annotations

import random

from sidequest.game.ruleset.resolution import (
    AttackRollParams,
    JumpAdjudication,
)
from sidequest.game.ruleset.without_number import (
    PSIONIC_EFFORT_SOURCE,
    WithoutNumberRulesetModule,
    _stat,
    swn_attribute_modifier,
)
from sidequest.genre.models.world import Route

# ``swn_attribute_modifier`` / ``_stat`` are the WN-family attribute helpers, and
# ``PSIONIC_EFFORT_SOURCE`` / ``activate_discipline`` are the psionic surface —
# all now homed on ``WithoutNumberRulesetModule`` (ADR-142). ``PSIONIC_EFFORT_SOURCE``
# is re-exported here so the established
# ``from sidequest.game.ruleset.swn import PSIONIC_EFFORT_SOURCE`` import surface
# (magic_working + chargen tests) keeps resolving unchanged.
__all__ = ["PSIONIC_EFFORT_SOURCE", "SwnRulesetModule", "_stat", "swn_attribute_modifier"]

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

    def save_params(self, *, stats, save, level, label, cfg, character_core: object | None = None):
        """SWN's honest save vocabulary is Physical / Evasion / Mental only — it
        has NO Luck save (the Luck save is a WWN/CWN/AWN trait that the WN core
        owns and those siblings inherit). SWN must therefore reject a ``luck``
        save loudly rather than silently resolving the core's inherited Luck
        branch (No Silent Fallbacks). Every other category delegates to the
        core's three-attribute resolution unchanged."""
        if save == "luck":
            raise ValueError("swn ruleset has no Luck save; SWN saves are physical/evasion/mental")
        return super().save_params(
            stats=stats,
            save=save,
            level=level,
            label=label,
            cfg=cfg,
            character_core=character_core,
        )

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
