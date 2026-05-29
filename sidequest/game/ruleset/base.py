"""The RulesetModule seam — a genre pack binds one module that owns turn resolution.

Spec 0 surface only: the five resolution operations the native turn already performs.
Character-shape / advancement / narrator-contract surfaces are added when the SWN
module plan needs them (YAGNI).
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.genre.models.rules import BeatDef, ConfrontationDef
from sidequest.protocol.models import InitiativeEntry

if TYPE_CHECKING:
    from sidequest.game.beat_kinds import ApplyResult
    from sidequest.game.lethality import DownedResult, LethalityResult
    from sidequest.genre.models.inventory import DamageSpec


class UnknownRulesetError(ValueError):
    """Raised at pack load when rules.ruleset names no registered module. Fail loud."""


class RulesetModule(ABC):
    """Authority for how a turn resolves. One bound per session; no fallback between modules."""

    #: registry key, also the value authors write in rules.yaml `ruleset:`
    slug: str

    @abstractmethod
    def find_confrontation(
        self, confrontations: list[ConfrontationDef], encounter_type: str
    ) -> ConfrontationDef | None:
        """Locate the ConfrontationDef governing this encounter type."""

    @abstractmethod
    def stat_modifier(self, stats: dict[str, int], stat_check: str) -> int:
        """The check modifier this ruleset derives from an ability/skill value."""

    @abstractmethod
    def compute_dc(self, beat: BeatDef) -> int:
        """The difficulty class / target number this ruleset assigns a beat."""

    @abstractmethod
    def apply_beat(
        self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver
    ) -> ApplyResult:
        """Apply a resolved beat's deltas to the encounter. Returns the engine ApplyResult."""

    @abstractmethod
    def resolve_damage(self, *, beat, actor_core, pack) -> DamageSpec | None:
        """Resolve the DamageSpec for a strike beat (weapon or override), or None."""

    @abstractmethod
    def attack_params(
        self,
        *,
        beat: BeatDef,
        attacker_stats: dict[str, int],
        attacker_core: object | None,
        target_core: object | None,
    ) -> AttackRollParams:
        """Modifier + target number for one attack. native: stat mod vs beat DC.
        SWN: attack_bonus + skill + attr-mod vs target AC."""

    def ship_attack_params(
        self,
        *,
        attacker_stats: dict[str, int],
        pilot_skill: int,
        attack_bonus: int,
        geometry_modifier: int,
        target_ac: int,
        cfg,
    ) -> AttackRollParams:
        """Modifier + target number for one ship-gunnery shot (dogfight SWN layer)."""
        raise NotImplementedError(f"{self.slug} ruleset has no ship-gunnery resolution")

    def check_params(self, *, stats, attribute, skill_level, difficulty_key, label, cfg):
        raise NotImplementedError(f"{self.slug} ruleset has no non-beat skill-check resolution")

    def save_params(self, *, stats, save, level, label, cfg):
        raise NotImplementedError(f"{self.slug} ruleset has no saving-throw resolution")

    def roll_initiative(
        self,
        *,
        actor_dex_scores: dict[str, int],
        rng: random.Random,
    ) -> list[InitiativeEntry] | None:
        """Resolution order (descending) for a confrontation, or None for no ordering.

        Default: None (no turn ordering). SWN overrides with 1d8 + DEX mod.
        `actor_dex_scores` maps actor name -> raw DEX score; the dispatch seam
        resolves it (PC from Character.stats, opponent from the dexterity
        reserved key) because CreatureCore carries no ability scores.
        """
        return None

    def resolve_trauma(
        self, *, spec, base_total, cfg, rng, actor="", _tracer=None
    ) -> LethalityResult:
        """Per-hit lethality multiplier. Default: identity passthrough (no Trauma).

        Only CWN overrides. Returns a LethalityResult. Imported lazily to avoid
        a base→game.lethality dependency at module import for the lean rulesets."""
        from sidequest.game.lethality import LethalityResult

        return LethalityResult(
            base_total=base_total,
            final_total=base_total,
            traumatic=False,
            trauma_roll=0,
            trauma_target=0,
        )

    def resolve_shock(self, *, spec, target_melee_ac, actor="", _tracer=None) -> int:
        """Chip damage applied on a MISS. Default: 0 (no Shock). Only CWN overrides."""
        return 0

    def resolve_downed(
        self, *, core, save_target, scene_traumatic, cfg, rng, _tracer=None
    ) -> DownedResult | None:
        """Resolve a 0-HP character. Default: no special consequence (None).

        CWN overrides to declare Mortal Injury and (if a Traumatic Hit landed
        this scene) roll the Major Injury table. Returns DownedResult | None."""
        return None

    def resolve_hacking(
        self, *, verb, tier, base_dc, alert_modifier, outcome, actor="", _tracer=None
    ) -> int:
        """CWN cyberspace security check. Default: compute the effective DC,
        emit nothing (parallels resolve_shock returning 0). Only CWN overrides
        to emit cwn.hacking.security_check."""
        return int(base_dc) + int(alert_modifier)
