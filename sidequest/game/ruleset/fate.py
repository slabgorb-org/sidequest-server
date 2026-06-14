"""FateRulesetModule — Fate Core behind the RulesetModule seam (ADR-144).

Resolution is 4dF + skill vs opposition on the ladder (see fate_resolution.py).
The d20/beat-shaped abstract methods are NOT part of Fate's paradigm; they fail
loud here (No Silent Fallbacks) until ADR-144 F5 demotes them to base
default-raise and these overrides are deleted. The Fate conflict engine
(fate_conflict.py) arrives in F1c; dispatch routing in F1d.
"""

from __future__ import annotations

import random

from opentelemetry import trace

from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.fate_resolution import FateOutcome, Opposition, resolve_action
from sidequest.telemetry.spans.fate import fate_action_resolved_span

_NO_D20_SURFACE = (
    "the 'fate' ruleset resolves via the Fate conflict engine (4dF + ladder), "
    "not the d20/beat surface — No Silent Fallbacks (ADR-144)"
)


class FateRulesetModule(RulesetModule):
    slug = "fate"

    @property
    def awards_native_turn_xp(self) -> bool:
        # Fate advances by milestones, not the ADR-021 native XP tick.
        return False

    def resolve_action(
        self,
        *,
        skill_rating: int,
        opposition: Opposition,
        rng: random.Random,
        invoke_bonus: int = 0,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> FateOutcome:
        """Resolve one Fate action and emit the lie-detector span."""
        outcome = resolve_action(
            skill_rating=skill_rating,
            opposition=opposition,
            rng=rng,
            invoke_bonus=invoke_bonus,
        )
        fate_action_resolved_span(
            actor=actor,
            skill_rating=skill_rating,
            dice=outcome.dice,
            ladder_total=outcome.ladder_total,
            opposition=outcome.opposition,
            opposition_kind=opposition.kind,
            shifts=outcome.shifts,
            tier=outcome.tier.value,
            _tracer=_tracer,
        )
        return outcome

    # --- d20/beat surface: not Fate's paradigm (fail loud until F5 re-cut) ---

    def find_confrontation(self, confrontations, encounter_type):
        raise NotImplementedError(_NO_D20_SURFACE)

    def stat_modifier(self, stats, stat_check):
        raise NotImplementedError(_NO_D20_SURFACE)

    def compute_dc(self, beat):
        raise NotImplementedError(_NO_D20_SURFACE)

    def apply_beat(self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver):
        raise NotImplementedError(_NO_D20_SURFACE)

    def resolve_damage(self, *, beat, actor_core, pack, world_slug=None):
        raise NotImplementedError(_NO_D20_SURFACE)

    def attack_params(self, *, beat, attacker_stats, attacker_core, target_core):
        raise NotImplementedError(_NO_D20_SURFACE)
